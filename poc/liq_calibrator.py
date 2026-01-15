"""
Liquidation Predictor Calibrator

Self-calibrating system that updates leverage weights using real Binance forceOrder
liquidation prints as feedback. Uses deterministic online calibration (no ML).

The calibrator:
1. Receives forceOrder liquidation events
2. Uses SOFT ATTRIBUTION: distributes responsibility across leverage levels using
   r(L) = exp(-d(L)/tau_usd), normalized, where d(L) = |event_price - implied_price(L)|
3. Updates soft hit stats: hits[L] += r(L) * exp(-d(L)/delta_usd)
4. Maintains rolling stats over a window
5. Updates weights using bounded multiplicative rule with stabilization clamps:
   - Per-update ratio clamp: new_w/old_w in [0.85, 1.20]
   - Max weight cap: 0.35 per leverage
   - Normalized after clamping
"""

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Callable
import logging

logger = logging.getLogger(__name__)

# Default persistence file
DEFAULT_WEIGHTS_FILE = "liq_calibrator_weights.json"

# Soft attribution config constants per symbol
# tau_usd: temperature for responsibility distribution (higher = more spread)
# delta_usd: scale for hit contribution decay (higher = more lenient hits)
SOFT_ATTRIBUTION_CONFIG = {
    "BTC": {"tau_usd": 150.0, "delta_usd": 90.0},
    "ETH": {"tau_usd": 8.0, "delta_usd": 5.0},
    "SOL": {"tau_usd": 0.6, "delta_usd": 0.35},
}

# Distance band thresholds (as fraction of src_price)
# NEAR: dist_pct <= 0.010 (1%)
# MID:  0.010 < dist_pct <= 0.025 (1-2.5%)
# FAR:  dist_pct > 0.025 (>2.5%)
DIST_BAND_NEAR = 0.010
DIST_BAND_MID = 0.025

# Approach gating: predicted zone must have been approached within this % to be judged
APPROACH_PCT = 0.0075  # 0.75%


@dataclass
class LiquidationEvent:
    """A single liquidation event from forceOrder stream."""
    timestamp: float
    symbol: str
    side: str          # "long" or "short" (which positions got liquidated)
    price: float
    qty: float
    notional: float    # price * qty
    minute_key: int    # minute timestamp for grouping


@dataclass
class MinuteSnapshot:
    """Predictor state at a given minute."""
    timestamp: float
    minute_key: int
    symbol: str
    src: float                          # ohlc4 price used for predictions
    pred_longs: Dict[float, float]      # price_bucket -> strength
    pred_shorts: Dict[float, float]     # price_bucket -> strength
    ladder: List[int]
    weights: List[float]
    buffer: float
    steps: float


@dataclass
class CalibrationStats:
    """Rolling calibration statistics."""
    total_events: int = 0
    hits: int = 0
    misses: int = 0
    total_hit_strength: float = 0.0
    total_miss_distance: float = 0.0
    # Per-leverage tracking (soft attribution - floats)
    leverage_hits: Dict[int, float] = field(default_factory=dict)      # soft hit contributions
    leverage_totals: Dict[int, float] = field(default_factory=dict)    # soft responsibility totals
    leverage_notional: Dict[int, float] = field(default_factory=dict)
    # Direction tracking for buffer tuning
    distance_directions: List[float] = field(default_factory=list)  # positive = predicted farther
    # Miss distances in buckets for tolerance tuning
    miss_distances: List[float] = field(default_factory=list)
    # Per-side offset tracking (implied_miss_usd values)
    long_offsets: List[float] = field(default_factory=list)
    short_offsets: List[float] = field(default_factory=list)
    # Distance band counts (NEAR/MID/FAR)
    band_counts: Dict[str, int] = field(default_factory=lambda: {"NEAR": 0, "MID": 0, "FAR": 0})
    # Window price range for approach gating
    window_high: float = 0.0
    window_low: float = float('inf')
    # Zone judgment stats
    zones_judged: int = 0
    zones_unreached: int = 0


class LiquidationCalibrator:
    """
    Self-calibrating system for liquidation predictions.

    Uses real forceOrder liquidation events to adjust leverage weights
    and optionally buffer parameters.
    """

    def __init__(
        self,
        symbol: str = "BTC",
        steps: float = 20.0,
        window_minutes: int = 30,
        hit_bucket_tolerance: int = 2,
        learning_rate: float = 0.10,
        alpha: float = 1.0,
        beta: float = 5.0,
        min_weight: float = 0.02,
        max_weight: float = 0.40,
        closer_level_gamma: float = 0.35,
        buffer_lr: float = 0.02,
        min_buffer: float = 0.0005,
        max_buffer: float = 0.01,
        enable_buffer_tuning: bool = True,
        tolerance_lr: float = 0.20,
        min_tolerance: int = 2,
        max_tolerance: int = 50,
        enable_tolerance_tuning: bool = True,
        log_file: str = None,
        weights_file: str = DEFAULT_WEIGHTS_FILE,
        log_events: bool = True,
        on_weights_updated: Callable = None
    ):
        self.symbol = symbol
        self.steps = steps
        self.window_minutes = window_minutes
        self.hit_bucket_tolerance = hit_bucket_tolerance
        self.learning_rate = learning_rate
        self.alpha = alpha
        self.beta = beta
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.closer_level_gamma = closer_level_gamma
        self.buffer_lr = buffer_lr
        self.min_buffer = min_buffer
        self.max_buffer = max_buffer
        self.enable_buffer_tuning = enable_buffer_tuning
        self.tolerance_lr = tolerance_lr
        self.min_tolerance = min_tolerance
        self.max_tolerance = max_tolerance
        self.enable_tolerance_tuning = enable_tolerance_tuning
        self.log_file = log_file
        self.weights_file = weights_file
        self.log_events = log_events
        self.on_weights_updated = on_weights_updated

        # Thread safety
        self.lock = threading.Lock()

        # Rolling window of events
        self.events_window: deque = deque()
        self.snapshots: Dict[int, MinuteSnapshot] = {}  # minute_key -> snapshot

        # Current calibration stats
        self.stats = CalibrationStats()

        # Current params (will be updated by predictor, or loaded from file)
        self.current_ladder: List[int] = []
        self.current_weights: List[float] = []
        self.current_buffer: float = 0.002

        # Learned directional offsets (applied to implied levels)
        # Positive = actual liquidations happen above implied level
        self.long_offset_usd: float = 0.0
        self.short_offset_usd: float = 0.0

        # Tracking
        self.last_calibration_minute: int = 0
        self.calibration_count: int = 0
        self.minutes_since_calibration: int = 0

        # Setup logging
        if log_file:
            self.log_fh = open(log_file, 'a')
        else:
            self.log_fh = None

        # Load persisted weights if available
        self._load_weights()

        logger.info(f"LiquidationCalibrator initialized for {symbol}")
        logger.info(f"  window={window_minutes}min, hit_tolerance={hit_bucket_tolerance} buckets")
        logger.info(f"  lr={learning_rate}, gamma={closer_level_gamma}")
        if self.current_weights:
            logger.info(f"  Loaded persisted weights: {self.current_weights}")

    def _load_weights(self) -> bool:
        """Load persisted weights from file if available."""
        if not self.weights_file or not os.path.exists(self.weights_file):
            return False

        try:
            with open(self.weights_file, 'r') as f:
                data = json.load(f)

            if data.get('symbol') != self.symbol:
                logger.warning(f"Weights file symbol mismatch: {data.get('symbol')} != {self.symbol}")
                return False

            self.current_ladder = data.get('ladder', [])
            self.current_weights = data.get('weights', [])
            self.current_buffer = data.get('buffer', 0.002)
            self.hit_bucket_tolerance = data.get('tolerance', self.hit_bucket_tolerance)
            self.calibration_count = data.get('calibration_count', 0)
            self.long_offset_usd = data.get('long_offset_usd', 0.0)
            self.short_offset_usd = data.get('short_offset_usd', 0.0)

            logger.info(f"Loaded weights from {self.weights_file}")
            logger.info(f"  Calibration count: {self.calibration_count}")
            logger.info(f"  Offsets: long={self.long_offset_usd:.1f}, short={self.short_offset_usd:.1f}")
            return True

        except Exception as e:
            logger.error(f"Error loading weights: {e}")
            return False

    def _save_weights(self) -> bool:
        """Save current weights to file."""
        if not self.weights_file:
            return False

        try:
            data = {
                'symbol': self.symbol,
                'ladder': self.current_ladder,
                'weights': self.current_weights,
                'buffer': self.current_buffer,
                'tolerance': self.hit_bucket_tolerance,
                'long_offset_usd': round(self.long_offset_usd, 2),
                'short_offset_usd': round(self.short_offset_usd, 2),
                'calibration_count': self.calibration_count,
                'saved_at': datetime.now().isoformat(),
                'weights_by_leverage': {
                    str(l): round(w, 6)
                    for l, w in zip(self.current_ladder, self.current_weights)
                }
            }

            with open(self.weights_file, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"Saved weights to {self.weights_file}")
            return True

        except Exception as e:
            logger.error(f"Error saving weights: {e}")
            return False

    def on_liquidation(self, event_data: dict) -> None:
        """
        Process a forceOrder liquidation event.

        Args:
            event_data: Dict with keys: timestamp, symbol, side, price, qty
        """
        with self.lock:
            try:
                # Parse event
                event = LiquidationEvent(
                    timestamp=event_data.get('timestamp', time.time()),
                    symbol=event_data.get('symbol', self.symbol),
                    side=event_data.get('side', 'unknown'),
                    price=float(event_data.get('price', 0)),
                    qty=float(event_data.get('qty', 0)),
                    notional=float(event_data.get('price', 0)) * float(event_data.get('qty', 0)),
                    minute_key=int(event_data.get('timestamp', time.time()) // 60)
                )

                if event.price <= 0 or event.symbol != self.symbol:
                    return

                # Add to window
                self.events_window.append(event)

                # Process event for stats
                self._process_event(event)

                # Clean old events
                self._clean_old_events()

            except Exception as e:
                logger.error(f"Error processing liquidation event: {e}")

    def on_minute_snapshot(
        self,
        symbol: str,
        timestamp: float,
        src: float,
        pred_longs: Dict[float, float],
        pred_shorts: Dict[float, float],
        ladder: List[int],
        weights: List[float],
        buffer: float,
        steps: float
    ) -> Optional[Dict]:
        """
        Receive per-minute predictor snapshot and potentially trigger calibration.

        Args:
            symbol: Symbol name
            timestamp: Unix timestamp
            src: OHLC4 price used for predictions
            pred_longs: Long liquidation zones {price -> strength}
            pred_shorts: Short liquidation zones {price -> strength}
            ladder: Current leverage ladder
            weights: Current weights
            buffer: Current buffer
            steps: Price bucket size

        Returns:
            Dict with updated weights/buffer if calibration occurred, None otherwise
        """
        with self.lock:
            minute_key = int(timestamp // 60)

            # Store snapshot
            snapshot = MinuteSnapshot(
                timestamp=timestamp,
                minute_key=minute_key,
                symbol=symbol,
                src=src,
                pred_longs=pred_longs.copy(),
                pred_shorts=pred_shorts.copy(),
                ladder=ladder.copy(),
                weights=weights.copy(),
                buffer=buffer,
                steps=steps
            )
            self.snapshots[minute_key] = snapshot

            # Update current params
            self.current_ladder = ladder.copy()
            self.current_weights = weights.copy()
            self.current_buffer = buffer
            self.steps = steps

            # Clean old snapshots
            self._clean_old_snapshots(minute_key)

            # Check if we should calibrate
            self.minutes_since_calibration += 1

            if self.minutes_since_calibration >= self.window_minutes:
                return self._run_calibration(minute_key, snapshot)

            return None

    def _process_event(self, event: LiquidationEvent) -> None:
        """Process a single liquidation event for stats using soft attribution."""
        # Find the snapshot for this event's minute (or closest prior)
        snapshot = self._get_snapshot_for_event(event)
        if not snapshot:
            return

        # === DISTANCE BAND COMPUTATION ===
        dist_pct = abs(event.price - snapshot.src) / snapshot.src if snapshot.src > 0 else 0.0
        if dist_pct <= DIST_BAND_NEAR:
            band = "NEAR"
        elif dist_pct <= DIST_BAND_MID:
            band = "MID"
        else:
            band = "FAR"

        # Track band counts
        self.stats.band_counts[band] += 1

        # Update window high/low for approach gating
        self.stats.window_high = max(self.stats.window_high, event.price)
        self.stats.window_low = min(self.stats.window_low, event.price)

        # Determine if this event should update calibration (NEAR + MID only)
        updates_calibration = band in ("NEAR", "MID")

        # Determine which zones to check based on event side
        # Long liquidation = longs got stopped = check pred_longs
        # Short liquidation = shorts got stopped = check pred_shorts
        if event.side == "long":
            pred_zones = snapshot.pred_longs
        else:
            pred_zones = snapshot.pred_shorts

        # Bucket the event price
        event_bucket = self._bucket_price(event.price)

        # Check for hit within tolerance (binary, for logging)
        is_hit = False
        hit_strength = 0.0
        min_distance = float('inf')

        for bucket_price, strength in pred_zones.items():
            distance_buckets = abs(bucket_price - event_bucket) / self.steps
            if distance_buckets <= self.hit_bucket_tolerance:
                is_hit = True
                if strength > hit_strength:
                    hit_strength = strength
            if distance_buckets < min_distance:
                min_distance = distance_buckets

        # Update binary stats (for logging/monitoring) - always count
        self.stats.total_events += 1
        if is_hit:
            self.stats.hits += 1
            self.stats.total_hit_strength += hit_strength
        else:
            self.stats.misses += 1
            self.stats.total_miss_distance += min_distance
            # Track miss distance for tolerance tuning (only if updates calibration)
            if updates_calibration and min_distance < float('inf'):
                self.stats.miss_distances.append(min_distance)

        # === SOFT ATTRIBUTION ===
        # Get soft attribution config for this symbol
        soft_config = SOFT_ATTRIBUTION_CONFIG.get(self.symbol, SOFT_ATTRIBUTION_CONFIG["BTC"])
        tau_usd = soft_config["tau_usd"]
        delta_usd = soft_config["delta_usd"]

        # Compute implied levels and distances for each leverage
        implied_levels = {}  # lev -> implied_price
        distances = {}       # lev -> d(L) in USD
        responsibilities = {}  # lev -> r(L) normalized

        for lev in snapshot.ladder:
            offset = (1.0 / lev) + snapshot.buffer + (0.01 / lev)
            if event.side == "long":
                implied = snapshot.src * (1 - offset)
            else:
                implied = snapshot.src * (1 + offset)
            implied_levels[lev] = implied
            distances[lev] = abs(event.price - implied)

        # Compute unnormalized responsibilities: r(L) = exp(-d(L)/tau_usd)
        unnorm_resp = {}
        for lev in snapshot.ladder:
            unnorm_resp[lev] = math.exp(-distances[lev] / tau_usd)

        # Normalize responsibilities
        total_resp = sum(unnorm_resp.values())
        if total_resp > 0:
            for lev in snapshot.ladder:
                responsibilities[lev] = unnorm_resp[lev] / total_resp
        else:
            # Fallback: uniform
            for lev in snapshot.ladder:
                responsibilities[lev] = 1.0 / len(snapshot.ladder)

        # Only update soft stats for NEAR and MID events (not FAR)
        if updates_calibration:
            # Update soft stats: totals[L] += r(L), hits[L] += r(L) * exp(-d(L)/delta_usd)
            for lev in snapshot.ladder:
                r_L = responsibilities[lev]
                d_L = distances[lev]
                hit_contrib = r_L * math.exp(-d_L / delta_usd)

                if lev not in self.stats.leverage_totals:
                    self.stats.leverage_totals[lev] = 0.0
                    self.stats.leverage_hits[lev] = 0.0
                    self.stats.leverage_notional[lev] = 0.0

                self.stats.leverage_totals[lev] += r_L
                self.stats.leverage_hits[lev] += hit_contrib
                self.stats.leverage_notional[lev] += r_L * event.notional

        # Find the leverage with highest responsibility for offset tracking
        best_lev = max(responsibilities.keys(), key=lambda l: responsibilities[l])
        offset = (1.0 / best_lev) + snapshot.buffer + (0.01 / best_lev)
        if event.side == "long":
            nearest_implied = snapshot.src * (1 - offset) + self.long_offset_usd
        else:
            nearest_implied = snapshot.src * (1 + offset) + self.short_offset_usd

        implied_miss = event.price - nearest_implied

        # Track per-side offsets for calibration (only if updates calibration)
        if updates_calibration:
            if event.side == "long":
                self.stats.long_offsets.append(implied_miss)
            else:
                self.stats.short_offsets.append(implied_miss)

        # Track direction for buffer tuning (only if updates calibration)
        nearest_pred = None
        if pred_zones:
            # Find nearest predicted level
            nearest_pred = min(pred_zones.keys(), key=lambda p: abs(p - event.price))
            if updates_calibration:
                # Positive = predicted level is farther from current price than actual event
                direction = abs(nearest_pred - snapshot.src) - abs(event.price - snapshot.src)
                self.stats.distance_directions.append(direction)

        # Log individual event for analysis (with soft attribution info)
        if self.log_events and self.log_fh:
            self._log_event(event, snapshot, pred_zones, is_hit, hit_strength,
                           min_distance, best_lev, nearest_pred,
                           responsibilities, distances, dist_pct, band, updates_calibration)

    def _attribute_to_leverage(self, event: LiquidationEvent, snapshot: MinuteSnapshot) -> Optional[int]:
        """
        Attribute a liquidation event to the leverage level that best explains it.

        Returns the leverage L whose implied liquidation level is closest to event price.
        """
        if not snapshot.ladder:
            return None

        src = snapshot.src
        buffer = snapshot.buffer
        best_leverage = None
        min_distance = float('inf')

        for lev in snapshot.ladder:
            # Same offset formula as predictor
            offset = (1.0 / lev) + buffer + (0.01 / lev)

            if event.side == "long":
                # Long liquidation = price dropped, longs got stopped
                implied_level = src * (1 - offset)
            else:
                # Short liquidation = price rose, shorts got stopped
                implied_level = src * (1 + offset)

            distance = abs(implied_level - event.price)
            if distance < min_distance:
                min_distance = distance
                best_leverage = lev

        return best_leverage

    def _run_calibration(self, minute_key: int, snapshot: MinuteSnapshot) -> Dict:
        """Run the calibration update and return new params."""
        old_weights = self.current_weights.copy()
        old_buffer = self.current_buffer

        # Calculate hit rate (binary, for monitoring)
        hit_rate = self.stats.hits / max(self.stats.total_events, 1)
        avg_hit_strength = self.stats.total_hit_strength / max(self.stats.hits, 1)
        avg_miss_distance = self.stats.total_miss_distance / max(self.stats.misses, 1)

        # Calculate leverage scores using SOFT attribution stats
        leverage_scores = {}
        for lev in self.current_ladder:
            hits = self.stats.leverage_hits.get(lev, 0.0)
            total = self.stats.leverage_totals.get(lev, 0.0)
            score = (hits + self.alpha) / (total + self.beta)
            leverage_scores[lev] = score

        # Calculate new weights if we have data
        new_weights = old_weights.copy()
        if leverage_scores and self.stats.total_events > 0:
            mean_score = sum(leverage_scores.values()) / len(leverage_scores)

            for i, lev in enumerate(self.current_ladder):
                if lev in leverage_scores:
                    score = leverage_scores[lev]
                    # Multiplicative update
                    adjustment = 1 + self.learning_rate * (score - mean_score)
                    new_weights[i] = old_weights[i] * adjustment

            # === STABILIZATION CLAMPS ===
            # Per-update ratio clamp: new_w / old_w must be within [0.85, 1.20]
            for i in range(len(new_weights)):
                if old_weights[i] > 0:
                    ratio = new_weights[i] / old_weights[i]
                    clamped_ratio = max(0.85, min(1.20, ratio))
                    new_weights[i] = old_weights[i] * clamped_ratio

            # Max weight cap: new_w <= 0.35 per leverage
            new_weights = [min(0.35, w) for w in new_weights]

            # Normalize weights after clamping
            total = sum(new_weights)
            if total > 0:
                new_weights = [w / total for w in new_weights]

            # NOTE: closer-level prior has been REMOVED to fix calibration collapse

        # Buffer tuning (optional)
        new_buffer = old_buffer
        if self.enable_buffer_tuning and len(self.stats.distance_directions) >= 10:
            avg_direction = sum(self.stats.distance_directions) / len(self.stats.distance_directions)
            if avg_direction > self.steps * 0.5:
                # Predicted levels are farther than actual - reduce buffer
                new_buffer *= (1 - self.buffer_lr)
            elif avg_direction < -self.steps * 0.5:
                # Predicted levels are closer than actual - increase buffer
                new_buffer *= (1 + self.buffer_lr)
            new_buffer = max(self.min_buffer, min(self.max_buffer, new_buffer))

        # Tolerance tuning (optional)
        old_tolerance = self.hit_bucket_tolerance
        new_tolerance = old_tolerance
        if self.enable_tolerance_tuning and len(self.stats.miss_distances) >= 5:
            # Use p75 of miss distances as target tolerance
            sorted_misses = sorted(self.stats.miss_distances)
            p75_idx = int(len(sorted_misses) * 0.75)
            p75_miss = sorted_misses[p75_idx]

            # If p75 miss is much larger than current tolerance, increase
            # If p75 miss is much smaller, we could decrease (but be conservative)
            if p75_miss > self.hit_bucket_tolerance * 1.5:
                # Misses are far - increase tolerance
                new_tolerance = int(self.hit_bucket_tolerance * (1 + self.tolerance_lr))
            elif p75_miss < self.hit_bucket_tolerance * 0.5 and hit_rate > 0.5:
                # Misses are close and we have decent hit rate - decrease slightly
                new_tolerance = int(self.hit_bucket_tolerance * (1 - self.tolerance_lr * 0.5))

            new_tolerance = max(self.min_tolerance, min(self.max_tolerance, new_tolerance))
            self.hit_bucket_tolerance = new_tolerance

        # Offset tuning - learn directional bias per side
        old_long_offset = self.long_offset_usd
        old_short_offset = self.short_offset_usd
        offset_lr = 0.3  # How quickly to adjust offsets

        if len(self.stats.long_offsets) >= 3:
            mean_long_miss = sum(self.stats.long_offsets) / len(self.stats.long_offsets)
            # Smooth update: move toward mean miss
            self.long_offset_usd += offset_lr * mean_long_miss

        if len(self.stats.short_offsets) >= 3:
            mean_short_miss = sum(self.stats.short_offsets) / len(self.stats.short_offsets)
            # Smooth update: move toward mean miss
            self.short_offset_usd += offset_lr * mean_short_miss

        # === APPROACH GATING FOR ZONES ===
        # Determine which predicted zones were "approachable" during this window
        # A zone Z is approachable if price came within APPROACH_PCT of Z
        # Using window high/low as approximation of price path
        zones_judged = 0
        zones_unreached = 0

        if self.stats.window_high > 0 and self.stats.window_low < float('inf'):
            # Define the approachable range
            approach_low = self.stats.window_low * (1 - APPROACH_PCT)
            approach_high = self.stats.window_high * (1 + APPROACH_PCT)

            # Check all predicted zones from current snapshot
            all_zones = list(snapshot.pred_longs.keys()) + list(snapshot.pred_shorts.keys())
            for zone_price in all_zones:
                if approach_low <= zone_price <= approach_high:
                    zones_judged += 1
                else:
                    zones_unreached += 1

        # Store in stats for logging
        self.stats.zones_judged = zones_judged
        self.stats.zones_unreached = zones_unreached

        # Log calibration
        self._log_calibration(
            minute_key, hit_rate, avg_hit_strength, avg_miss_distance,
            old_weights, new_weights, old_buffer, new_buffer,
            old_tolerance, new_tolerance,
            old_long_offset, self.long_offset_usd,
            old_short_offset, self.short_offset_usd,
            snapshot
        )

        # Print debug line
        print(f"CALIB {self.symbol} hit={hit_rate:.0%} events={self.stats.total_events} "
              f"tol={new_tolerance} buf={new_buffer:.4f} "
              f"long_off={self.long_offset_usd:+.0f} short_off={self.short_offset_usd:+.0f}")

        # Reset stats for next window
        self.stats = CalibrationStats()
        self.minutes_since_calibration = 0
        self.calibration_count += 1
        self.last_calibration_minute = minute_key

        # Update current params
        self.current_weights = new_weights
        self.current_buffer = new_buffer

        # Persist weights to file
        self._save_weights()

        # Notify callback
        result = {
            'weights': new_weights,
            'buffer': new_buffer,
            'hit_rate': hit_rate,
            'calibration_count': self.calibration_count
        }

        if self.on_weights_updated:
            self.on_weights_updated(self.symbol, new_weights, new_buffer)

        return result

    def _log_calibration(
        self,
        minute_key: int,
        hit_rate: float,
        avg_hit_strength: float,
        avg_miss_distance: float,
        old_weights: List[float],
        new_weights: List[float],
        old_buffer: float,
        new_buffer: float,
        old_tolerance: int,
        new_tolerance: int,
        old_long_offset: float,
        new_long_offset: float,
        old_short_offset: float,
        new_short_offset: float,
        snapshot: MinuteSnapshot
    ) -> None:
        """Write calibration event to JSONL log."""
        if not self.log_fh:
            return

        # Get top 5 predicted levels
        top_longs = sorted(snapshot.pred_longs.items(), key=lambda x: x[1], reverse=True)[:5]
        top_shorts = sorted(snapshot.pred_shorts.items(), key=lambda x: x[1], reverse=True)[:5]

        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'minute_key': minute_key,
            'symbol': self.symbol,
            'calibration_count': self.calibration_count,
            'total_events': self.stats.total_events,
            'hits': self.stats.hits,
            'misses': self.stats.misses,
            'hit_rate': round(hit_rate, 4),
            'avg_hit_strength': round(avg_hit_strength, 4),
            'avg_miss_distance': round(avg_miss_distance, 2),
            'leverage_soft_stats': {
                str(lev): {
                    'total_resp': round(self.stats.leverage_totals.get(lev, 0.0), 4),
                    'hits_soft': round(self.stats.leverage_hits.get(lev, 0.0), 4)
                }
                for lev in self.current_ladder
            },
            # Distance band counts
            'band_counts': self.stats.band_counts.copy(),
            # Zone judgment stats
            'zones_judged': self.stats.zones_judged,
            'zones_unreached': self.stats.zones_unreached,
            'window_high': round(self.stats.window_high, 2) if self.stats.window_high > 0 else None,
            'window_low': round(self.stats.window_low, 2) if self.stats.window_low < float('inf') else None,
            'old_weights': {str(l): round(w, 4) for l, w in zip(self.current_ladder, old_weights)},
            'new_weights': {str(l): round(w, 4) for l, w in zip(self.current_ladder, new_weights)},
            'old_buffer': round(old_buffer, 5),
            'new_buffer': round(new_buffer, 5),
            'old_tolerance': old_tolerance,
            'new_tolerance': new_tolerance,
            'old_long_offset': round(old_long_offset, 2),
            'new_long_offset': round(new_long_offset, 2),
            'old_short_offset': round(old_short_offset, 2),
            'new_short_offset': round(new_short_offset, 2),
            'src_price': round(snapshot.src, 2),
            'top_long_zones': [[p, round(s, 3)] for p, s in top_longs],
            'top_short_zones': [[p, round(s, 3)] for p, s in top_shorts]
        }

        self.log_fh.write(json.dumps(log_entry) + '\n')
        self.log_fh.flush()

    def _log_event(
        self,
        event: LiquidationEvent,
        snapshot: MinuteSnapshot,
        pred_zones: Dict[float, float],
        is_hit: bool,
        hit_strength: float,
        min_distance: float,
        attributed_leverage: Optional[int],
        nearest_pred: Optional[float],
        responsibilities: Dict[int, float] = None,
        distances: Dict[int, float] = None,
        dist_pct: float = None,
        band: str = None,
        updates_calibration: bool = None
    ) -> None:
        """Log individual liquidation event with predicted zones and soft attribution for analysis."""
        if not self.log_fh:
            return

        # Get top 5 predicted zones for this side
        top_zones = sorted(pred_zones.items(), key=lambda x: x[1], reverse=True)[:5]

        # Calculate implied levels for all leverages and find nearest
        # Apply learned offset correction per side
        side_offset = self.long_offset_usd if event.side == "long" else self.short_offset_usd

        implied_levels = {}
        implied_levels_corrected = {}
        nearest_implied = None
        nearest_implied_corrected = None
        nearest_implied_lev = None
        min_implied_distance = float('inf')

        for lev in snapshot.ladder:
            offset = (1.0 / lev) + snapshot.buffer + (0.01 / lev)
            if event.side == "long":
                implied = snapshot.src * (1 - offset)
            else:
                implied = snapshot.src * (1 + offset)

            implied_corrected = implied + side_offset
            implied_levels[str(lev)] = round(implied, 2)
            implied_levels_corrected[str(lev)] = round(implied_corrected, 2)

            # Track nearest corrected implied level to event price
            dist = abs(implied_corrected - event.price)
            if dist < min_implied_distance:
                min_implied_distance = dist
                nearest_implied = implied
                nearest_implied_corrected = implied_corrected
                nearest_implied_lev = lev

        # Calculate how far off the corrected implied is (in price terms)
        implied_miss_usd = round(event.price - nearest_implied_corrected, 2) if nearest_implied_corrected else None

        # Build top 3 leverages by responsibility with d(L)
        top_3_by_responsibility = []
        if responsibilities and distances:
            sorted_by_resp = sorted(responsibilities.items(), key=lambda x: x[1], reverse=True)[:3]
            for lev, r_L in sorted_by_resp:
                d_L = distances.get(lev, 0.0)
                top_3_by_responsibility.append({
                    'leverage': lev,
                    'responsibility': round(r_L, 4),
                    'distance_usd': round(d_L, 2)
                })

        log_entry = {
            'type': 'event',
            'timestamp': datetime.fromtimestamp(event.timestamp).isoformat(),
            'symbol': event.symbol,
            'side': event.side,
            'event_price': round(event.price, 2),
            'src_price': round(snapshot.src, 2),
            # Distance band info
            'dist_pct': round(dist_pct, 6) if dist_pct is not None else None,
            'band': band,
            'updates_calibration': updates_calibration,
            'event_qty': round(event.qty, 6),
            'event_notional': round(event.notional, 2),
            'is_hit': is_hit,
            'hit_strength': round(hit_strength, 4) if is_hit else None,
            'miss_distance_buckets': round(min_distance, 2) if not is_hit else None,
            # Soft attribution: top 3 leverages by responsibility
            'top_3_by_responsibility': top_3_by_responsibility,
            'nearest_implied_raw': round(nearest_implied, 2) if nearest_implied else None,
            'nearest_implied_corrected': round(nearest_implied_corrected, 2) if nearest_implied_corrected else None,
            'nearest_implied_leverage': nearest_implied_lev,
            'implied_miss_usd': implied_miss_usd,
            'side_offset_applied': round(side_offset, 2),
            'attributed_leverage': attributed_leverage,
            'implied_levels_raw': implied_levels,
            'implied_levels_corrected': implied_levels_corrected,
            'top_predicted_zones': [[round(p, 2), round(s, 3)] for p, s in top_zones],
            'current_weights': {
                str(l): round(w, 4)
                for l, w in zip(snapshot.ladder, snapshot.weights)
            },
            'current_buffer': round(snapshot.buffer, 5),
            'hit_bucket_tolerance': self.hit_bucket_tolerance
        }

        self.log_fh.write(json.dumps(log_entry) + '\n')
        self.log_fh.flush()

    def _bucket_price(self, price: float) -> float:
        """Bucket a price to the nearest step."""
        return round(price / self.steps) * self.steps

    def _get_snapshot_for_event(self, event: LiquidationEvent) -> Optional[MinuteSnapshot]:
        """Get the snapshot for an event's minute or the closest prior."""
        # Try exact minute
        if event.minute_key in self.snapshots:
            return self.snapshots[event.minute_key]

        # Find closest prior
        prior_keys = [k for k in self.snapshots.keys() if k <= event.minute_key]
        if prior_keys:
            return self.snapshots[max(prior_keys)]

        return None

    def _clean_old_events(self) -> None:
        """Remove events older than window."""
        cutoff = time.time() - (self.window_minutes * 60)
        while self.events_window and self.events_window[0].timestamp < cutoff:
            self.events_window.popleft()

    def _clean_old_snapshots(self, current_minute: int) -> None:
        """Remove snapshots older than window."""
        cutoff = current_minute - self.window_minutes - 5
        old_keys = [k for k in self.snapshots.keys() if k < cutoff]
        for k in old_keys:
            del self.snapshots[k]

    def get_stats(self) -> Dict:
        """Get current calibration statistics."""
        with self.lock:
            hit_rate = self.stats.hits / max(self.stats.total_events, 1)
            return {
                'total_events': self.stats.total_events,
                'hits': self.stats.hits,
                'misses': self.stats.misses,
                'hit_rate': hit_rate,
                'calibration_count': self.calibration_count,
                'minutes_since_calibration': self.minutes_since_calibration,
                'current_weights': dict(zip(self.current_ladder, self.current_weights)),
                'current_buffer': self.current_buffer
            }

    def get_persisted_weights(self) -> Optional[Dict]:
        """
        Get persisted weights if available.
        Call this on startup to initialize the liq_engine with saved weights.

        Returns:
            Dict with 'ladder', 'weights', 'buffer' if available, None otherwise
        """
        with self.lock:
            if self.current_weights and self.current_ladder:
                return {
                    'ladder': self.current_ladder,
                    'weights': self.current_weights,
                    'buffer': self.current_buffer,
                    'calibration_count': self.calibration_count
                }
            return None

    def has_persisted_weights(self) -> bool:
        """Check if calibrator has loaded persisted weights."""
        with self.lock:
            return bool(self.current_weights and self.current_ladder)

    def close(self):
        """Clean up resources."""
        if self.log_fh:
            self.log_fh.close()
