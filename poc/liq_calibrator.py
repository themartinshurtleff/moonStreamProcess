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

# Approach learning: zone is "approached" when price comes within this % (for stress-based learning)
APPROACH_LEARN_PCT = 0.0035  # 0.35% - tighter than gating

# Stress score component scales
IMB_K = 0.15                # imbalance decay constant for aggression = 1 - exp(-abs_imb / IMB_K)
OI_SCALE = 1000.0           # scale for abs(oi_change) in contract units
DEPTH_SCALE = 100.0         # scale for depth thinning in base units

# Depth band coverage: store depth for buckets within this % of src price
DEPTH_BAND_PCT = 0.02       # ±2% around src - wide enough for any approached zone

# Stress score component weights (must sum to 1.0)
STRESS_WEIGHT_DEPTH = 0.45
STRESS_WEIGHT_AGGRESSION = 0.35
STRESS_WEIGHT_OI = 0.20

# Rolling minute cache size
MINUTE_CACHE_SIZE = 60  # ~60 minutes of history

# Approach event hit multiplier: approach events contribute at reduced strength vs forceOrders
APPROACH_HIT_MULT = 0.25  # approach hits += r(L) * S * APPROACH_HIT_MULT

# Weight anchoring: w_new = (1-ANCHOR_LAMBDA)*w_old + ANCHOR_LAMBDA*w_update
ANCHOR_LAMBDA = 0.10  # 10% toward new weights, 90% toward old

# === ANTI-COLLAPSE CONSTANTS ===
# Approach distance decay: S_eff = S * exp(-dist_pct / D0)
APPROACH_DIST_DECAY_D0 = 0.002  # 0.2% - zones closer to price get more weight

# Top-K approach selection: only top TOP_K zones by S_eff per minute contribute to learning
APPROACH_TOP_K = 3

# Unreached zone penalty: weak negative signal for zones judged reachable but never reached
UNREACHED_PENALTY = 0.05  # hits[L] += r(L) * (-UNREACHED_PENALTY)

# Mid-leverage mass floor: sum(weights for lev <= MID_LEV_THRESHOLD) >= MIN_MASS_MID
MIN_MASS_MID = 0.25
MID_LEV_THRESHOLD = 25  # Leverages <= 25x are considered "mid/low"

# Debug logging for unreached zone penalties (top 10 per cycle)
DEBUG_UNREACHED_ZONES = True  # Set to False to disable per-zone penalty logging

# Epsilon for numerical stability
EPS = 1e-9


@dataclass
class MinuteCacheEntry:
    """Per-minute market data for approach stress calculation."""
    minute_key: int
    timestamp: float
    src: float              # OHLC4 price
    high: float
    low: float
    close: float
    perp_buy_vol: float
    perp_sell_vol: float
    perp_oi_change: float
    # Depth band: price_bucket -> size for buckets within ±DEPTH_BAND_PCT of src
    # This allows zone-local depth calculation for any approached zone
    depth_band: Dict[float, float] = field(default_factory=dict)
    # Keep depth_near_cmp for backward compatibility (deprecated, will be removed)
    depth_near_cmp: Dict[int, float] = field(default_factory=dict)


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
class ZoneMeta:
    """Metadata for a predicted zone, storing origin context for correct attribution."""
    origin_minute_key: int
    origin_src: float
    origin_buffer: float
    zone_price: float  # Original float price for logging/debugging


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
    # Reached zones tracking: set of (zone_bucket_int, side) that were approached
    # zone_bucket_int = int(round(zone_price / steps)) to avoid float key issues
    reached_zones: set = field(default_factory=set)
    # Zone metadata: (zone_bucket_int, side) -> ZoneMeta for correct attribution
    zone_meta: Dict[Tuple[int, str], 'ZoneMeta'] = field(default_factory=dict)


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

        # Rolling per-symbol minute cache (in-memory only)
        self.minute_cache: deque = deque(maxlen=MINUTE_CACHE_SIZE)

        # Approach event tracking for this calibration window
        self.approach_events_count: int = 0

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
        steps: float,
        # Optional market data for approach stress calculation
        high: float = None,
        low: float = None,
        close: float = None,
        perp_buy_vol: float = None,
        perp_sell_vol: float = None,
        perp_oi_change: float = None,
        depth_band: Dict[float, float] = None,
        depth_near_cmp: Dict[int, float] = None  # deprecated, use depth_band
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
            high: High price for the minute (optional, for stress calc)
            low: Low price for the minute (optional, for stress calc)
            close: Close price for the minute (optional, for stress calc)
            perp_buy_vol: Perpetual buy volume (optional, for stress calc)
            perp_sell_vol: Perpetual sell volume (optional, for stress calc)
            perp_oi_change: Perpetual OI change (optional, for stress calc)
            depth_band: Dict of price_bucket -> size within ±2% of src (for zone-local depth)
            depth_near_cmp: DEPRECATED - Dict of bucket_offset -> size near CMP

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

            # === STORE ZONE METADATA FOR ALL PREDICTED ZONES ===
            # This allows unreached penalty to use correct origin src/buffer
            for zone_price in pred_longs.keys():
                zone_bucket_int = self._zone_bucket_int(zone_price, steps)
                zone_key = (zone_bucket_int, "long")
                if zone_key not in self.stats.zone_meta:
                    self.stats.zone_meta[zone_key] = ZoneMeta(
                        origin_minute_key=minute_key,
                        origin_src=src,
                        origin_buffer=buffer,
                        zone_price=zone_price
                    )
            for zone_price in pred_shorts.keys():
                zone_bucket_int = self._zone_bucket_int(zone_price, steps)
                zone_key = (zone_bucket_int, "short")
                if zone_key not in self.stats.zone_meta:
                    self.stats.zone_meta[zone_key] = ZoneMeta(
                        origin_minute_key=minute_key,
                        origin_src=src,
                        origin_buffer=buffer,
                        zone_price=zone_price
                    )

            # Update current params
            self.current_ladder = ladder.copy()
            self.current_weights = weights.copy()
            self.current_buffer = buffer
            self.steps = steps

            # === UPDATE MINUTE CACHE ===
            # Store market data if provided (use defaults if not)
            cache_entry = MinuteCacheEntry(
                minute_key=minute_key,
                timestamp=timestamp,
                src=src,
                high=high if high is not None else src,
                low=low if low is not None else src,
                close=close if close is not None else src,
                perp_buy_vol=perp_buy_vol if perp_buy_vol is not None else 0.0,
                perp_sell_vol=perp_sell_vol if perp_sell_vol is not None else 0.0,
                perp_oi_change=perp_oi_change if perp_oi_change is not None else 0.0,
                depth_band=depth_band.copy() if depth_band else {},
                depth_near_cmp=depth_near_cmp.copy() if depth_near_cmp else {}  # deprecated
            )
            self.minute_cache.append(cache_entry)

            # === PROCESS APPROACH EVENTS FOR ZONES ===
            # Check if price approached any predicted zones and update soft stats
            self._process_approach_events(snapshot, cache_entry)

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

    def _process_approach_events(self, snapshot: MinuteSnapshot, cache_entry: MinuteCacheEntry) -> None:
        """
        Check if price approached any predicted zones and update soft stats.

        Anti-collapse throttling:
        1. Compute S_eff = S * exp(-dist_pct / D0) for distance decay
        2. Collect all approach candidates for this minute
        3. Select top APPROACH_TOP_K zones by S_eff
        4. Only apply learning updates for selected zones
        5. Track reached zones for unreached penalty later
        """
        if not snapshot.ladder or not self.current_weights:
            return

        close = cache_entry.close
        if close <= 0:
            return

        # Get previous minute's depth_band for depth thinning calculation
        prev_cache = self._get_previous_cache_entry()

        # Process both long and short zones
        all_zones = []
        for zone_price, strength in snapshot.pred_longs.items():
            all_zones.append((zone_price, strength, "long"))
        for zone_price, strength in snapshot.pred_shorts.items():
            all_zones.append((zone_price, strength, "short"))

        # Get soft attribution config
        soft_config = SOFT_ATTRIBUTION_CONFIG.get(self.symbol, SOFT_ATTRIBUTION_CONFIG["BTC"])
        tau_usd = soft_config["tau_usd"]

        # === PHASE 1: Collect all approach candidates ===
        candidates = []
        for zone_price, zone_strength, zone_side in all_zones:
            # Check if price approached this zone
            dist_pct = abs(close - zone_price) / close
            if dist_pct > APPROACH_LEARN_PCT:
                continue  # Not approached

            # Zone was approached - mark as reached using integer bucket key
            zone_bucket_int = self._zone_bucket_int(zone_price, snapshot.steps)
            zone_key = (zone_bucket_int, zone_side)
            self.stats.reached_zones.add(zone_key)

            # Store zone metadata if not already stored (first occurrence wins)
            if zone_key not in self.stats.zone_meta:
                self.stats.zone_meta[zone_key] = ZoneMeta(
                    origin_minute_key=snapshot.minute_key,
                    origin_src=snapshot.src,
                    origin_buffer=snapshot.buffer,
                    zone_price=zone_price
                )

            # Compute stress score with full breakdown
            stress_breakdown = self._compute_stress_score_with_breakdown(
                cache_entry=cache_entry,
                zone_price=zone_price,
                prev_cache=prev_cache,
                steps=snapshot.steps
            )
            S = stress_breakdown['S']

            # Apply distance decay: S_eff = S * exp(-dist_pct / D0)
            S_eff = S * math.exp(-dist_pct / APPROACH_DIST_DECAY_D0)

            # Compute soft attribution: r(L) for each leverage
            responsibilities = {}
            distances = {}

            for lev in snapshot.ladder:
                offset = (1.0 / lev) + snapshot.buffer + (0.01 / lev)
                if zone_side == "long":
                    implied = snapshot.src * (1 - offset)
                else:
                    implied = snapshot.src * (1 + offset)
                distances[lev] = abs(zone_price - implied)

            # Compute unnormalized responsibilities
            unnorm_resp = {}
            for lev in snapshot.ladder:
                unnorm_resp[lev] = math.exp(-distances[lev] / tau_usd)

            # Normalize
            total_resp = sum(unnorm_resp.values())
            if total_resp > 0:
                for lev in snapshot.ladder:
                    responsibilities[lev] = unnorm_resp[lev] / total_resp
            else:
                for lev in snapshot.ladder:
                    responsibilities[lev] = 1.0 / len(snapshot.ladder)

            # Get top-3 responsible leverages for logging
            sorted_resp = sorted(responsibilities.items(), key=lambda x: -x[1])[:3]
            resp_top3 = {lev: r for lev, r in sorted_resp}

            candidates.append({
                'zone_price': zone_price,
                'zone_side': zone_side,
                'zone_strength': zone_strength,
                'dist_pct': dist_pct,
                'S': S,
                'S_eff': S_eff,
                'stress_breakdown': stress_breakdown,
                'responsibilities': responsibilities,
                'resp_top3': resp_top3,
                'distances': distances
            })

        # === PHASE 2: Select top-K by S_eff ===
        candidates.sort(key=lambda c: -c['S_eff'])
        selected = candidates[:APPROACH_TOP_K]
        skipped = candidates[APPROACH_TOP_K:]

        # === PHASE 3: Apply learning updates only for selected zones ===
        for cand in selected:
            responsibilities = cand['responsibilities']
            S_eff = cand['S_eff']

            for lev in snapshot.ladder:
                r_L = responsibilities[lev]

                if lev not in self.stats.leverage_totals:
                    self.stats.leverage_totals[lev] = 0.0
                    self.stats.leverage_hits[lev] = 0.0
                    self.stats.leverage_notional[lev] = 0.0

                # Use S_eff (distance-decayed) instead of raw S
                self.stats.leverage_totals[lev] += r_L
                self.stats.leverage_hits[lev] += r_L * S_eff * APPROACH_HIT_MULT

            self.approach_events_count += 1

        # === PHASE 4: Log approach summary for this minute ===
        if self.log_events and self.log_fh:
            self._log_approach_minute_summary(
                snapshot=snapshot,
                cache_entry=cache_entry,
                close=close,
                candidates=candidates,
                selected=selected,
                skipped=skipped
            )

    def _compute_stress_score_with_breakdown(
        self,
        cache_entry: MinuteCacheEntry,
        zone_price: float,
        prev_cache: Optional['MinuteCacheEntry'],
        steps: float
    ) -> Dict:
        """
        Compute stress score S in [0, 1] with full breakdown:
        - aggression = 1 - exp(-abs_imb / IMB_K) where abs_imb = |buyVol-sellVol|/(buyVol+sellVol)
        - oi_fuel = 1 - exp(-oi_abs / OI_SCALE) where oi_abs = abs(oi_change)
        - depth_thinning = 1 - exp(-thin_raw / DEPTH_SCALE) computed around ZONE price

        S = 0.45 * depth_thinning + 0.35 * aggression + 0.20 * oi_fuel

        Returns dict with all components for logging.
        """
        # === AGGRESSION (proper 0-1 imbalance, no sigmoid) ===
        buy_vol = cache_entry.perp_buy_vol
        sell_vol = cache_entry.perp_sell_vol
        total_vol = buy_vol + sell_vol

        if total_vol > EPS:
            abs_imb = abs(buy_vol - sell_vol) / total_vol  # in [0, 1]
            aggression = 1.0 - math.exp(-abs_imb / IMB_K)  # in [0, 1), monotone
        else:
            # Guard: no volume, force aggression = 0
            abs_imb = 0.0
            aggression = 0.0

        # === OI FUEL ===
        oi_abs = abs(cache_entry.perp_oi_change)
        oi_fuel = 1.0 - math.exp(-oi_abs / OI_SCALE)

        # === DEPTH THINNING (zone-local) ===
        # Compute depth around the ZONE price, not around CMP
        zone_bucket = self._bucket_price(zone_price, steps)

        # Get depth in ±2 buckets around zone from current depth_band
        depth_now = self._get_depth_around_bucket(cache_entry.depth_band, zone_bucket, steps, radius=2)

        # Get depth from previous minute
        if prev_cache is not None:
            depth_prev = self._get_depth_around_bucket(prev_cache.depth_band, zone_bucket, steps, radius=2)
        else:
            # Guard: no previous depth, set depth_thinning = 0
            depth_prev = 0.0

        thin_raw = max(0.0, depth_prev - depth_now)

        if depth_prev > EPS:
            depth_thinning = 1.0 - math.exp(-thin_raw / DEPTH_SCALE)
        else:
            # Guard: no previous depth data, don't fabricate
            depth_thinning = 0.0

        # === COMBINED STRESS SCORE ===
        S = (
            STRESS_WEIGHT_DEPTH * depth_thinning +
            STRESS_WEIGHT_AGGRESSION * aggression +
            STRESS_WEIGHT_OI * oi_fuel
        )

        # Clamp to [0, 1]
        S = max(0.0, min(1.0, S))

        return {
            'S': S,
            'abs_imb': abs_imb,
            'aggression': aggression,
            'oi_abs': oi_abs,
            'oi_fuel': oi_fuel,
            'depth_prev': depth_prev,
            'depth_now': depth_now,
            'thin_raw': thin_raw,
            'depth_thinning': depth_thinning,
            'zone_bucket': zone_bucket
        }

    def _bucket_price(self, price: float, steps: float = None) -> float:
        """Bucket a price to the nearest step."""
        if steps is None:
            steps = self.steps
        return round(price / steps) * steps

    def _zone_bucket_int(self, zone_price: float, steps: float = None) -> int:
        """
        Convert zone_price to integer bucket count to avoid float key issues.

        Returns int(round(zone_price / steps)) which is the bucket index.
        Use this for all zone key lookups (reached_zones, zone_meta).
        """
        if steps is None:
            steps = self.steps
        return int(round(zone_price / steps))

    def _get_depth_around_bucket(
        self,
        depth_band: Dict[float, float],
        center_bucket: float,
        steps: float,
        radius: int = 2
    ) -> float:
        """
        Sum depth from depth_band for buckets within ±radius of center_bucket.

        Args:
            depth_band: Dict of price_bucket -> size
            center_bucket: Center price bucket
            steps: Bucket size
            radius: Number of buckets on each side (default 2 = ±2 buckets)

        Returns:
            Total depth around the center bucket
        """
        if not depth_band:
            return 0.0

        total = 0.0
        for offset in range(-radius, radius + 1):
            bucket = center_bucket + (offset * steps)
            if bucket in depth_band:
                total += depth_band[bucket]

        return total

    def _get_previous_cache_entry(self) -> Optional[MinuteCacheEntry]:
        """Get the previous minute's cache entry."""
        if len(self.minute_cache) < 2:
            return None
        return self.minute_cache[-2]

    def _log_approach_event(
        self,
        snapshot: MinuteSnapshot,
        zone_price: float,
        zone_side: str,
        zone_strength: float,
        stress_breakdown: Dict,
        close: float,
        approach_dist_pct: float,
        responsibilities: Dict[int, float],
        distances: Dict[int, float],
        cache_entry: MinuteCacheEntry
    ) -> None:
        """Log approach event with full stress breakdown and sanity info."""
        if not self.log_fh:
            return

        # Get top 3 leverages by responsibility with deltas
        top_3_resp = []
        sorted_by_resp = sorted(responsibilities.items(), key=lambda x: x[1], reverse=True)[:3]
        S = stress_breakdown['S']

        for lev, r_L in sorted_by_resp:
            d_L = distances.get(lev, 0.0)
            top_3_resp.append({
                'leverage': lev,
                'responsibility': round(r_L, 4),
                'distance_usd': round(d_L, 2),
                'delta_totals': round(r_L, 4),
                'delta_hits': round(r_L * S, 4)
            })

        log_entry = {
            'type': 'approach_event',
            'symbol': self.symbol,
            'minute_key': cache_entry.minute_key,
            'timestamp': datetime.fromtimestamp(cache_entry.timestamp).isoformat(),
            # Price info
            'close': round(close, 2),
            'src': round(cache_entry.src, 2),
            # Zone info
            'zone_price': round(zone_price, 2),
            'zone_bucket': round(stress_breakdown['zone_bucket'], 2),
            'zone_side': zone_side,
            'zone_strength': round(zone_strength, 4),
            'approach_dist_pct': round(approach_dist_pct, 6),
            # Stress breakdown
            'stress_breakdown': {
                'abs_imb': round(stress_breakdown['abs_imb'], 4),
                'aggression': round(stress_breakdown['aggression'], 4),
                'oi_abs': round(stress_breakdown['oi_abs'], 4),
                'oi_fuel': round(stress_breakdown['oi_fuel'], 4),
                'depth_prev': round(stress_breakdown['depth_prev'], 4),
                'depth_now': round(stress_breakdown['depth_now'], 4),
                'thin_raw': round(stress_breakdown['thin_raw'], 4),
                'depth_thinning': round(stress_breakdown['depth_thinning'], 4),
                'S': round(S, 4)
            },
            # Top 3 responsibilities with updates applied
            'top_3_responsibilities': top_3_resp,
            # Raw market data
            'market_data': {
                'perp_buy_vol': round(cache_entry.perp_buy_vol, 4),
                'perp_sell_vol': round(cache_entry.perp_sell_vol, 4),
                'perp_oi_change': round(cache_entry.perp_oi_change, 4),
                'high': round(cache_entry.high, 2),
                'low': round(cache_entry.low, 2)
            }
        }

        self.log_fh.write(json.dumps(log_entry) + '\n')
        self.log_fh.flush()

    def _log_approach_minute_summary(
        self,
        snapshot: MinuteSnapshot,
        cache_entry: MinuteCacheEntry,
        close: float,
        candidates: List[Dict],
        selected: List[Dict],
        skipped: List[Dict]
    ) -> None:
        """Log approach throttling summary for this minute with top-K selection details."""
        if not self.log_fh:
            return

        # Build selected zone details
        selected_details = []
        for cand in selected:
            selected_details.append({
                'zone_price': round(cand['zone_price'], 2),
                'zone_side': cand['zone_side'],
                'dist_pct': round(cand['dist_pct'], 6),
                'S': round(cand['S'], 4),
                'S_eff': round(cand['S_eff'], 4),
                'resp_top3': {str(k): round(v, 4) for k, v in cand['resp_top3'].items()},
                'stress_breakdown': {
                    'aggression': round(cand['stress_breakdown']['aggression'], 4),
                    'oi_fuel': round(cand['stress_breakdown']['oi_fuel'], 4),
                    'depth_thinning': round(cand['stress_breakdown']['depth_thinning'], 4)
                }
            })

        # Build skipped zone summary (just basic info)
        skipped_summary = []
        for cand in skipped:
            skipped_summary.append({
                'zone_price': round(cand['zone_price'], 2),
                'zone_side': cand['zone_side'],
                'S_eff': round(cand['S_eff'], 4)
            })

        log_entry = {
            'type': 'approach_minute_summary',
            'symbol': self.symbol,
            'minute_key': cache_entry.minute_key,
            'timestamp': datetime.fromtimestamp(cache_entry.timestamp).isoformat(),
            'close': round(close, 2),
            # Throttling stats
            'approach_candidates': len(candidates),
            'approach_used': len(selected),
            'approach_skipped': len(skipped),
            # Selected zones with full details
            'selected_zones': selected_details,
            # Skipped zones (just summary)
            'skipped_zones': skipped_summary
        }

        self.log_fh.write(json.dumps(log_entry) + '\n')
        self.log_fh.flush()

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

    def _enforce_mid_mass_floor(self, weights: List[float]) -> Tuple[List[float], Dict]:
        """
        Enforce minimum mass on low/mid leverage (<=MID_LEV_THRESHOLD) to prevent collapse.

        If sum(weights for lev <= 25x) < MIN_MASS_MID:
        - Compute deficit
        - Proportionally remove mass from high leverage weights
        - Add that mass proportionally to low/mid leverage weights

        Returns (new_weights, floor_info) for logging.
        """
        floor_info = {
            'floor_triggered': False,
            'mid_mass_before': 0.0,
            'mid_mass_after': 0.0,
            'deficit': 0.0,
            'drained_leverages': []
        }

        if not self.current_ladder or len(weights) != len(self.current_ladder):
            return weights, floor_info

        # Identify mid/low and high leverage indices
        mid_indices = []
        high_indices = []
        for i, lev in enumerate(self.current_ladder):
            if lev <= MID_LEV_THRESHOLD:
                mid_indices.append(i)
            else:
                high_indices.append(i)

        if not mid_indices or not high_indices:
            return weights, floor_info

        # Compute current mid mass
        mid_mass = sum(weights[i] for i in mid_indices)
        floor_info['mid_mass_before'] = mid_mass

        if mid_mass >= MIN_MASS_MID:
            floor_info['mid_mass_after'] = mid_mass
            return weights, floor_info

        # Need to enforce floor
        floor_info['floor_triggered'] = True
        deficit = MIN_MASS_MID - mid_mass
        floor_info['deficit'] = deficit

        new_weights = weights.copy()

        # Compute how much we can drain from high leverage proportionally
        high_mass = sum(new_weights[i] for i in high_indices)

        if high_mass <= EPS:
            # No mass to drain
            floor_info['mid_mass_after'] = mid_mass
            return new_weights, floor_info

        # Drain proportionally from high leverage
        drain_ratio = min(deficit / high_mass, 0.9)  # Don't drain more than 90%
        actual_drain = 0.0
        drained = []

        for i in high_indices:
            drain_amount = new_weights[i] * drain_ratio
            new_weights[i] -= drain_amount
            actual_drain += drain_amount
            if drain_amount > EPS:
                drained.append({
                    'leverage': self.current_ladder[i],
                    'drained': round(drain_amount, 6)
                })

        floor_info['drained_leverages'] = drained

        # Add drained mass to mid/low leverage proportionally
        if actual_drain > EPS:
            # Proportional to current mid weights, or uniform if all near zero
            mid_weight_sum = sum(new_weights[i] for i in mid_indices)
            if mid_weight_sum > EPS:
                for i in mid_indices:
                    new_weights[i] += actual_drain * (new_weights[i] / mid_weight_sum)
            else:
                # Uniform distribution
                per_mid = actual_drain / len(mid_indices)
                for i in mid_indices:
                    new_weights[i] += per_mid

        # Re-normalize to ensure sum = 1
        total = sum(new_weights)
        if total > 0:
            new_weights = [w / total for w in new_weights]

        floor_info['mid_mass_after'] = sum(new_weights[i] for i in mid_indices)

        return new_weights, floor_info

    def _apply_unreached_penalty(self, snapshot: MinuteSnapshot) -> Dict:
        """
        Apply weak negative signal for zones that were judged reachable but never reached.

        Uses integer bucket keys and origin src/buffer from zone_meta for correct attribution.

        For each unreached zone:
        - totals[L] += r(L)
        - hits[L] += r(L) * (-UNREACHED_PENALTY)

        Returns logging info about penalties applied.
        """
        result = {
            'unreached_zones_count': 0,
            'unreached_penalty_applied_count': 0,
            'avg_unreached_resp': 0.0,
            'top_penalized_leverages': [],
            'penalty_mass_by_leverage': {},
            'debug_penalized_zones': []  # For DEBUG_UNREACHED_ZONES logging
        }

        if self.stats.window_high <= 0 or self.stats.window_low >= float('inf'):
            return result

        # Define the judged reachable range
        approach_low = self.stats.window_low * (1 - APPROACH_PCT)
        approach_high = self.stats.window_high * (1 + APPROACH_PCT)

        # Collect all predicted zones with their integer bucket keys
        all_zones = []
        for zone_price, strength in snapshot.pred_longs.items():
            zone_bucket_int = self._zone_bucket_int(zone_price, snapshot.steps)
            all_zones.append((zone_price, zone_bucket_int, strength, "long"))
        for zone_price, strength in snapshot.pred_shorts.items():
            zone_bucket_int = self._zone_bucket_int(zone_price, snapshot.steps)
            all_zones.append((zone_price, zone_bucket_int, strength, "short"))

        # Get soft attribution config
        soft_config = SOFT_ATTRIBUTION_CONFIG.get(self.symbol, SOFT_ATTRIBUTION_CONFIG["BTC"])
        tau_usd = soft_config["tau_usd"]

        penalty_mass_by_lev = {lev: 0.0 for lev in self.current_ladder}
        total_resp_sum = 0.0

        # Collect penalized zone details for debug logging
        penalized_zone_details = []

        for zone_price, zone_bucket_int, zone_strength, zone_side in all_zones:
            zone_key = (zone_bucket_int, zone_side)

            # Check if zone was judged reachable
            if not (approach_low <= zone_price <= approach_high):
                continue  # Not in judged range

            result['unreached_zones_count'] += 1

            # Check if zone was reached (approached during the window) using integer key
            if zone_key in self.stats.reached_zones:
                continue  # Was reached, no penalty

            # Zone was judged reachable but never reached - apply penalty
            result['unreached_penalty_applied_count'] += 1

            # Get origin src/buffer from zone_meta (use current snapshot as fallback)
            meta = self.stats.zone_meta.get(zone_key)
            if meta:
                origin_src = meta.origin_src
                origin_buffer = meta.origin_buffer
                origin_minute_key = meta.origin_minute_key
            else:
                # Fallback to current snapshot (shouldn't happen if zone_meta is properly populated)
                origin_src = snapshot.src
                origin_buffer = snapshot.buffer
                origin_minute_key = snapshot.minute_key

            # Compute responsibility distribution using ORIGIN src/buffer
            responsibilities = {}
            for lev in self.current_ladder:
                offset = (1.0 / lev) + origin_buffer + (0.01 / lev)
                if zone_side == "long":
                    implied = origin_src * (1 - offset)
                else:
                    implied = origin_src * (1 + offset)
                distance = abs(zone_price - implied)
                responsibilities[lev] = math.exp(-distance / tau_usd)

            # Normalize
            total_resp = sum(responsibilities.values())
            if total_resp > 0:
                for lev in self.current_ladder:
                    responsibilities[lev] /= total_resp
            else:
                for lev in self.current_ladder:
                    responsibilities[lev] = 1.0 / len(self.current_ladder)

            # Apply penalty
            for lev in self.current_ladder:
                r_L = responsibilities[lev]

                if lev not in self.stats.leverage_totals:
                    self.stats.leverage_totals[lev] = 0.0
                    self.stats.leverage_hits[lev] = 0.0

                self.stats.leverage_totals[lev] += r_L
                self.stats.leverage_hits[lev] += r_L * (-UNREACHED_PENALTY)

                penalty_mass_by_lev[lev] += r_L
                total_resp_sum += r_L

            # Store debug info for this penalized zone
            if DEBUG_UNREACHED_ZONES:
                sorted_resp = sorted(responsibilities.items(), key=lambda x: -x[1])[:3]
                penalized_zone_details.append({
                    'zone_key': zone_key,
                    'zone_bucket_int': zone_bucket_int,
                    'zone_side': zone_side,
                    'zone_price': round(zone_price, 2),
                    'origin_src': round(origin_src, 2),
                    'origin_minute_key': origin_minute_key,
                    'top3_resp': [(lev, round(r, 4)) for lev, r in sorted_resp],
                    'total_penalty_contrib': round(sum(responsibilities.values()), 4)
                })

        # Compute summary stats
        if result['unreached_penalty_applied_count'] > 0:
            result['avg_unreached_resp'] = total_resp_sum / result['unreached_penalty_applied_count']

        result['penalty_mass_by_leverage'] = penalty_mass_by_lev

        # Top 3 penalized leverages by penalty mass
        sorted_penalty = sorted(penalty_mass_by_lev.items(), key=lambda x: -x[1])[:3]
        result['top_penalized_leverages'] = [
            {'leverage': lev, 'penalty_mass': round(mass, 4)}
            for lev, mass in sorted_penalty if mass > 0
        ]

        # Store top 10 penalized zones for debug logging
        if DEBUG_UNREACHED_ZONES and penalized_zone_details:
            # Sort by total_penalty_contrib descending, take top 10
            penalized_zone_details.sort(key=lambda x: -x['total_penalty_contrib'])
            result['debug_penalized_zones'] = penalized_zone_details[:10]

        return result

    def _run_calibration(self, minute_key: int, snapshot: MinuteSnapshot) -> Dict:
        """Run the calibration update and return new params."""
        old_weights = self.current_weights.copy()
        old_buffer = self.current_buffer

        # Calculate hit rate (binary, for monitoring)
        hit_rate = self.stats.hits / max(self.stats.total_events, 1)
        avg_hit_strength = self.stats.total_hit_strength / max(self.stats.hits, 1)
        avg_miss_distance = self.stats.total_miss_distance / max(self.stats.misses, 1)

        # === UNREACHED ZONE PENALTY ===
        # Apply weak negative signal for zones judged reachable but never reached
        # This prevents collapse toward CMP by penalizing over-prediction
        unreached_penalty_info = self._apply_unreached_penalty(snapshot)

        # Calculate leverage scores using SOFT attribution stats
        leverage_scores = {}
        for lev in self.current_ladder:
            hits = self.stats.leverage_hits.get(lev, 0.0)
            total = self.stats.leverage_totals.get(lev, 0.0)
            score = (hits + self.alpha) / (total + self.beta)
            leverage_scores[lev] = score

        # Calculate new weights if we have data
        new_weights = old_weights.copy()
        floor_info = {'floor_triggered': False, 'mid_mass_before': 0.0, 'mid_mass_after': 0.0}
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

            # === WEIGHT ANCHORING ===
            # w_new = (1-ANCHOR_LAMBDA)*w_old + ANCHOR_LAMBDA*w_update
            # Smooths weight changes by anchoring toward previous weights
            new_weights = [(1 - ANCHOR_LAMBDA) * old_weights[i] + ANCHOR_LAMBDA * new_weights[i]
                           for i in range(len(new_weights))]

            # Normalize weights after clamping and anchoring
            total = sum(new_weights)
            if total > 0:
                new_weights = [w / total for w in new_weights]

            # === MID-MASS FLOOR ===
            # Enforce minimum mass on low/mid leverage (<=25x) to prevent collapse toward CMP
            new_weights, floor_info = self._enforce_mid_mass_floor(new_weights)

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

        # === CYCLE SUMMARY ===
        # Compute weight entropy: H = -sum(w * log(w)) for w > 0
        # Higher entropy = more uniform, lower = more concentrated (collapse warning)
        weight_entropy = 0.0
        for w in new_weights:
            if w > EPS:
                weight_entropy -= w * math.log(w)

        # Compute approach-to-force ratio
        force_events = self.stats.total_events
        approach_events = self.approach_events_count
        if force_events > 0:
            approach_to_force = approach_events / force_events
        else:
            approach_to_force = float('inf') if approach_events > 0 else 0.0

        # Print cycle summary line with anti-collapse stats
        # Line 1: Basic stats
        print(f"CYCLE {self.symbol} force={force_events} approach={approach_events} "
              f"a/f={approach_to_force:.2f} entropy={weight_entropy:.3f} "
              f"hit={hit_rate:.0%} buf={new_buffer:.4f}")

        # Line 2: Unreached penalty stats
        unreached_count = unreached_penalty_info.get('unreached_zones_count', 0)
        penalty_applied = unreached_penalty_info.get('unreached_penalty_applied_count', 0)
        avg_resp = unreached_penalty_info.get('avg_unreached_resp', 0.0)
        top_penalized = unreached_penalty_info.get('top_penalized_leverages', [])
        top_pen_str = ','.join([f"{p['leverage']}x:{p['penalty_mass']:.3f}" for p in top_penalized[:3]])
        print(f"  UNREACHED judged={unreached_count} penalized={penalty_applied} "
              f"avg_resp={avg_resp:.3f} top_pen=[{top_pen_str}]")

        # Debug: Per-zone penalty details (top 10, behind DEBUG_UNREACHED_ZONES flag)
        if DEBUG_UNREACHED_ZONES:
            debug_zones = unreached_penalty_info.get('debug_penalized_zones', [])
            if debug_zones:
                print(f"  DEBUG_UNREACHED (top {len(debug_zones)} penalized zones):")
                for z in debug_zones:
                    resp_str = ','.join([f"{lev}x:{r:.3f}" for lev, r in z['top3_resp']])
                    print(f"    zone=({z['zone_bucket_int']},{z['zone_side']}) "
                          f"price={z['zone_price']} origin_src={z['origin_src']} "
                          f"origin_min={z['origin_minute_key']} top3=[{resp_str}]")

        # Line 3: Floor enforcement stats (only if triggered)
        if floor_info.get('floor_triggered', False):
            drained_str = ','.join([f"{d['leverage']}x" for d in floor_info.get('drained_leverages', [])])
            print(f"  FLOOR triggered mid_mass={floor_info['mid_mass_before']:.3f}->"
                  f"{floor_info['mid_mass_after']:.3f} drained=[{drained_str}]")

        # Store approach events count before reset
        approach_events_this_window = self.approach_events_count

        # Reset stats for next window
        self.stats = CalibrationStats()
        self.approach_events_count = 0
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
            # Approach events (stress-based learning)
            'approach_events': self.approach_events_count,
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
