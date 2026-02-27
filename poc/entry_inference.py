"""
EntryInference: Infer position entries from OI + aggression.

This module uses open interest changes combined with trade aggression to infer
where leveraged positions are being opened. This is the forward-looking component
that predicts WHERE liquidations will occur (vs LiquidationTape which shows where
they DID occur).

Key insight:
- OI↑ + buy aggression  → longs opening (projected liq below)
- OI↑ + sell aggression → shorts opening (projected liq above)
- OI↓                   → positions closing (remove from projection)

The quality of inference depends on:
1. OI data granularity (per-minute is typical)
2. Aggression signal quality (trade tape vs aggregated volume)
3. Leverage distribution assumptions (calibrated from LiquidationTape offsets)
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Tuple, Optional

from leverage_config import is_tier_disabled, get_tier_weight, log_disabled_tiers, DISABLED_TIERS

# V3: Import zone manager for persistent zones
try:
    from active_zone_manager import ActiveZoneManager, get_zone_manager
except ImportError:
    ActiveZoneManager = None
    get_zone_manager = None

logger = logging.getLogger(__name__)


def _p99_scale(values: list) -> float:
    """Compute p99-based scaling factor for normalization.

    Uses 99th percentile instead of max to prevent a single outlier spike
    from crushing all other values. Falls back to max for small sets (<10).
    Returns at least 1e-9 to prevent division by zero.
    """
    if not values:
        return 1e-9
    positive = [v for v in values if v > 0]
    if not positive:
        return 1e-9
    positive.sort()
    if len(positive) >= 10:
        idx = int(len(positive) * 0.99)
        idx = min(idx, len(positive) - 1)
        scale = positive[idx]
    else:
        scale = positive[-1]  # max
    return max(scale, 1e-9)


# --- Tuning constants for projection persistence ---

# Sweep: partial reduction instead of hard delete when price crosses projection bucket.
# First sweep takes SWEEP_REDUCTION_FACTOR, level stays visible but dimmer.
# Second sweep takes another SWEEP_REDUCTION_FACTOR of the remainder. Level fades naturally.
SWEEP_REDUCTION_FACTOR = 0.70   # Fraction to remove on sweep (starting default, expected to tune)
SWEEP_MIN_BUCKET_USD = 100.0    # Delete bucket only if reduced value falls below this threshold

# OI-based reduction: maximum fraction of projections removed per minute when OI drops.
# Previous value was 0.50 (50%); reduced to 0.25 to prevent rapid wipeout of accumulated
# projections during brief OI declines. A 2-minute decline now removes ~44% instead of 75%.
MAX_OI_REDUCTION_PER_MINUTE = 0.25

# Combined-source boost: display-only multiplier for buckets where both tape
# and inference contribute. Combined-source zones have 56.5% sweep rate vs 0%
# for pure inference — the money signal. Applied in get_combined_heatmap_display()
# only; the raw get_combined_heatmap() is unchanged for zone/calibrator consumers.
COMBINED_ZONE_BOOST = 1.5      # Multiplier for both-source overlap buckets
COMBINED_SOURCE_EPS = 1e-4     # Minimum normalized value to count as "present"


@dataclass
class InferredEntry:
    """An inferred position entry from OI + aggression analysis."""
    timestamp: float
    minute_key: int
    price: float          # Entry price estimate
    side: str             # "long" or "short"
    size_usd: float       # Estimated notional size
    confidence: float     # 0-1 confidence in this inference
    projected_liq: float  # Projected liquidation price


@dataclass
class PositionBucket:
    """Accumulated inferred positions at a price bucket."""
    price: float
    size_usd: float       # Cumulative inferred size
    side: str             # "long" or "short"
    entry_price: float    # Entry price (for offset calculation)
    last_ts: float
    leverage_est: int     # Estimated leverage tier
    # V3: Track inference count for sweep logging
    inference_count: int = 1
    peak_size_usd: float = 0.0  # Largest single inference at this bucket


class EntryInference:
    """
    Infer position entries from OI changes and trade aggression.

    Uses the OI + aggression signal to estimate:
    1. Whether new positions are being opened (OI↑) or closed (OI↓)
    2. The direction of new positions (buy aggression → longs, sell aggression → shorts)
    3. The approximate entry price
    4. The projected liquidation price (using learned offset distribution)
    """

    # Default leverage distribution (will be updated by LiquidationTape learning)
    # V3: 10x tier disabled (set to 0) due to persistent -$62K median miss corruption
    DEFAULT_LEVERAGE_WEIGHTS = {
        10: 0.00,   # DISABLED - was 5% at 10x
        20: 0.18,   # 18% at 20x (redistributed)
        25: 0.27,   # 27% at 25x (most common, redistributed)
        50: 0.30,   # 30% at 50x
        75: 0.15,   # 15% at 75x
        100: 0.08,  # 8% at 100x
        125: 0.02   # 2% at 125x
    }

    DEFAULT_BUFFER = 0.002    # 0.2% maintenance margin buffer
    DEFAULT_STEPS = 20.0      # Price bucket size
    DEFAULT_DECAY = 0.998     # Per-minute decay for projected zones (slower for persistence)

    # Minimum OI change to trigger inference (as fraction of position size)
    # For BTC with ~$10B OI, 0.001 = $10M/min threshold (way too high)
    # Lowered to 0.00005 = $500K/min for BTC - more realistic
    MIN_OI_CHANGE_PCT = 0.00005  # 0.005% of OI

    # Weight clamping for buy/sell distribution
    MIN_WEIGHT = 0.10  # Never fully zero a side
    MAX_WEIGHT = 0.90  # Never fully one side

    def __init__(
        self,
        symbol: str = "BTC",
        steps: float = DEFAULT_STEPS,
        buffer: float = DEFAULT_BUFFER,
        decay: float = DEFAULT_DECAY,
        leverage_weights: Dict[int, float] = None,
        log_file: str = None,
        debug_log_file: str = None,
        sweep_log_file: str = None,
        zone_manager: 'ActiveZoneManager' = None  # V3: Persistent zone manager
    ):
        self.symbol = symbol
        self.steps = steps
        self._ndigits = max(0, -Decimal(str(steps)).as_tuple().exponent)
        self.buffer = buffer
        self.decay = decay
        self.leverage_weights = leverage_weights or self.DEFAULT_LEVERAGE_WEIGHTS.copy()

        # Projected liquidation zones from inferred entries
        # These are WHERE we expect liquidations to occur
        self.projected_long_liqs: Dict[float, PositionBucket] = {}   # Below current price
        self.projected_short_liqs: Dict[float, PositionBucket] = {}  # Above current price

        # Rolling inference history
        self.inferences: deque = deque(maxlen=720)  # 12h of inferences

        # OI tracking for delta calculation
        self.last_oi: float = 0.0
        self.last_oi_minute: int = 0

        # Stats
        self.current_minute: int = 0
        self.total_inferred_long_usd: float = 0.0
        self.total_inferred_short_usd: float = 0.0
        self.current_price: float = 0.0

        # Optional logging
        self.log_file = log_file
        self._log_fh = None
        if log_file:
            try:
                self._log_fh = open(log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open inference log file: {e}")

        # Normalization debug metrics (populated by get_combined_heatmap)
        self._last_tape_scale: float = 1e-9
        self._last_proj_scale: float = 1e-9
        self._last_tape_max_raw: float = 0.0
        self._last_proj_max_raw: float = 0.0

        # Combined-zone boost debug logging cadence
        self._last_boost_log_minute: int = 0

        # Debug logging (per-minute detailed diagnostics)
        self.debug_log_file = debug_log_file
        self._debug_log_fh = None
        if debug_log_file:
            try:
                self._debug_log_fh = open(debug_log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open debug log file: {e}")

        # Sweep logging
        self.sweep_log_file = sweep_log_file
        self._sweep_log_fh = None
        if sweep_log_file:
            try:
                self._sweep_log_fh = open(sweep_log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open sweep log file: {e}")

        # V3: Persistent zone manager (optional)
        self.zone_manager = zone_manager

        # V3: Log disabled tiers at startup
        logger.info(f"EntryInference initialized for {symbol}")
        if zone_manager:
            logger.info(f"  Using ActiveZoneManager for persistent zones")
        log_disabled_tiers()

    def _bucket_price(self, price: float) -> float:
        """Round price to nearest bucket with precision matching step size."""
        return round(round(price / self.steps) * self.steps, self._ndigits)

    def _calculate_liq_price(self, entry: float, leverage: int, side: str) -> float:
        """
        Calculate liquidation price from entry, leverage, and side.

        For longs: liq = entry * (1 - 1/L - buffer)
        For shorts: liq = entry * (1 + 1/L + buffer)
        """
        offset = (1.0 / leverage) + self.buffer

        if side == "long":
            return entry * (1 - offset)
        else:  # short
            return entry * (1 + offset)

    def on_minute(
        self,
        minute_key: int,
        src_price: float,
        oi: float,
        taker_buy_notional_usd: float,
        taker_sell_notional_usd: float,
        high: float = None,
        low: float = None,
        oi_stale: bool = False,
        fallback_aggression: bool = False
    ) -> List[InferredEntry]:
        """
        Process minute data to infer new position entries.

        BOTH SIDES ARE ALWAYS PREDICTED based on weighted OI distribution.
        Uses buy_weight = clamp(buy_pct, 0.10, 0.90) to distribute OI to both sides.

        Args:
            minute_key: Minute timestamp
            src_price: Reference price (OHLC4)
            oi: Current open interest (in contracts or USD)
            taker_buy_notional_usd: Taker buy volume in USD (from aggTrade)
            taker_sell_notional_usd: Taker sell volume in USD (from aggTrade)
            high: Minute high (for sweep detection)
            low: Minute low (for sweep detection)
            oi_stale: Whether OI data is stale
            fallback_aggression: Whether fallback aggression values were used

        Returns:
            List of inferred entries this minute
        """
        self.current_minute = minute_key
        self.current_price = src_price
        inferences = []

        # Calculate OI delta
        oi_delta = 0.0
        if self.last_oi > 0:
            oi_delta = oi - self.last_oi
        self.last_oi = oi
        self.last_oi_minute = minute_key

        # Apply decay to existing projections
        self._apply_decay()

        # Sweep: remove projected zones that price crossed
        swept_long = 0
        swept_short = 0
        swept_long_notional = 0.0
        swept_short_notional = 0.0
        if high and low:
            swept_long, swept_short, swept_long_notional, swept_short_notional = self._sweep(high, low, minute_key)

        # Compute aggression from USD notional
        total_notional_usd = taker_buy_notional_usd + taker_sell_notional_usd

        # Calculate buy_pct and weighted distribution
        buy_pct = taker_buy_notional_usd / total_notional_usd if total_notional_usd > 0 else 0.5

        # Clamp buy_weight to never fully zero a side
        buy_weight = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, buy_pct))
        sell_weight = 1.0 - buy_weight

        # Debug log for diagnostics
        # OI is in base-asset units (e.g. BTC count); convert threshold to USD
        oi_threshold = self.last_oi * src_price * self.MIN_OI_CHANGE_PCT if self.last_oi > 0 else 0

        # Track projected additions for debug logging
        projected_added_long_usd = 0.0
        projected_added_short_usd = 0.0

        if oi_delta > 0:
            # New positions opening - compute BOTH sides
            # OI is in base-asset units (BTC/ETH/SOL count) — convert to USD
            oi_delta_usd = abs(oi_delta) * src_price

            # Distribute OI to both sides based on aggression weight
            long_open_usd = oi_delta_usd * buy_weight
            short_open_usd = oi_delta_usd * sell_weight

            # Skip if OI delta too small
            if oi_delta_usd < oi_threshold:
                long_open_usd = 0.0
                short_open_usd = 0.0
            else:
                # Create projected liquidation zones for LONG positions
                inferences.extend(self._project_side(
                    minute_key=minute_key,
                    src_price=src_price,
                    side="long",
                    size_usd=long_open_usd,
                    confidence=buy_weight  # Use weight as confidence
                ))
                projected_added_long_usd = long_open_usd

                # Create projected liquidation zones for SHORT positions
                inferences.extend(self._project_side(
                    minute_key=minute_key,
                    src_price=src_price,
                    side="short",
                    size_usd=short_open_usd,
                    confidence=sell_weight  # Use weight as confidence
                ))
                projected_added_short_usd = short_open_usd

                logger.info(
                    f"[{self.symbol}] INFERENCE: oi_delta={oi_delta:+,.0f} "
                    f"buy_pct={buy_pct:.3f} buy_weight={buy_weight:.3f} "
                    f"long_usd={long_open_usd:,.0f} short_usd={short_open_usd:,.0f}"
                )

        elif oi_delta < 0:
            # Positions closing - proportionally decay BOTH sides
            total_projected = sum(b.size_usd for b in self.projected_long_liqs.values()) + \
                              sum(b.size_usd for b in self.projected_short_liqs.values())

            if total_projected > 0:
                # Scale reduction proportionally to OI drop magnitude
                if self.last_oi > 0:
                    # OI-relative ratio: fraction of total OI that closed this minute
                    close_factor = min(abs(oi_delta) / self.last_oi, MAX_OI_REDUCTION_PER_MINUTE)
                else:
                    # Fallback: use projected-relative ratio when OI is zero
                    close_factor = min(abs(oi_delta) * src_price / total_projected, MAX_OI_REDUCTION_PER_MINUTE)

                # Reduce both sides proportionally
                self._reduce_projections(self.projected_long_liqs, close_factor)
                self._reduce_projections(self.projected_short_liqs, close_factor)

                # V3: Also apply close_factor to zone manager weights
                # This reduces zone weights when OI drops, improving accuracy
                if self.zone_manager:
                    self._apply_oi_decay_to_zones(close_factor)

                logger.debug(
                    f"[{self.symbol}] CLOSING: oi_delta={oi_delta:,.0f} "
                    f"close_factor={close_factor:.3f} total_projected={total_projected:,.0f}"
                )

        # Write enhanced debug log
        if self._debug_log_fh:
            debug_entry = {
                "type": "minute_inference",
                "ts": time.time(),
                "minute_key": minute_key,
                "src_price": src_price,
                "oi_delta_usd": round(oi_delta * src_price, 2),
                "buy_pct": round(buy_pct, 4),
                "buy_weight": round(buy_weight, 4),
                "long_open_usd": round(projected_added_long_usd, 2),
                "short_open_usd": round(projected_added_short_usd, 2),
                "projected_added_long_usd": round(projected_added_long_usd, 2),
                "projected_added_short_usd": round(projected_added_short_usd, 2),
                "oi_stale": oi_stale,
                "fallback_aggression": fallback_aggression,
                "swept_long": swept_long,
                "swept_short": swept_short,
                "swept_long_notional": round(swept_long_notional, 2),
                "swept_short_notional": round(swept_short_notional, 2),
                "total_long_buckets": len(self.projected_long_liqs),
                "total_short_buckets": len(self.projected_short_liqs)
            }
            self._debug_log_fh.write(json.dumps(debug_entry) + '\n')
            self._debug_log_fh.flush()

        # Log inference summary (original format)
        if self._log_fh and inferences:
            log_entry = {
                "type": "inference",
                "ts": time.time(),
                "minute_key": minute_key,
                "src_price": src_price,
                "oi_delta": oi_delta,
                "buy_pct": buy_pct,
                "inferences": [
                    {
                        "side": inf.side,
                        "size_usd": round(inf.size_usd, 2),
                        "projected_liq": inf.projected_liq,
                        "confidence": round(inf.confidence, 3)
                    }
                    for inf in inferences
                ]
            }
            self._log_fh.write(json.dumps(log_entry) + '\n')
            self._log_fh.flush()

        return inferences

    def _project_side(
        self,
        minute_key: int,
        src_price: float,
        side: str,
        size_usd: float,
        confidence: float
    ) -> List[InferredEntry]:
        """
        Project liquidation zones for a single side.

        Args:
            minute_key: Minute timestamp
            src_price: Reference price
            side: "long" or "short"
            size_usd: Total USD to distribute across leverage tiers
            confidence: Confidence weight (0-1)

        Returns:
            List of InferredEntry objects created
        """
        inferences = []

        if size_usd < 100:  # Less than $100 total - skip
            return inferences

        for leverage, weight in self.leverage_weights.items():
            # V3: Skip disabled tiers
            if is_tier_disabled(leverage):
                continue
            if weight < 0.01:
                continue

            liq_price = self._calculate_liq_price(src_price, leverage, side)
            bucket = self._bucket_price(liq_price)
            size_contribution = size_usd * weight

            if size_contribution < 100:  # Less than $100 - skip
                continue

            inference = InferredEntry(
                timestamp=time.time(),
                minute_key=minute_key,
                price=src_price,
                side=side,
                size_usd=size_contribution,
                confidence=confidence,
                projected_liq=bucket
            )
            inferences.append(inference)
            self.inferences.append(inference)

            # Accumulate into projection buckets
            buckets = self.projected_long_liqs if side == "long" else self.projected_short_liqs

            if bucket not in buckets:
                buckets[bucket] = PositionBucket(
                    price=bucket,
                    size_usd=0.0,
                    side=side,
                    entry_price=src_price,
                    last_ts=time.time(),
                    leverage_est=leverage,
                    inference_count=0,
                    peak_size_usd=0.0
                )

            buckets[bucket].size_usd += size_contribution
            buckets[bucket].last_ts = time.time()
            # V3: Track inference count and peak for sweep logging
            buckets[bucket].inference_count += 1
            buckets[bucket].peak_size_usd = max(buckets[bucket].peak_size_usd, size_contribution)

            # V3: Create or reinforce in persistent zone manager
            if self.zone_manager:
                # Weight is normalized size_contribution (as fraction of typical notional)
                zone_weight = size_contribution / 100000.0  # Normalize to reasonable range
                self.zone_manager.create_or_reinforce(
                    price=bucket,
                    side=side,
                    weight=zone_weight,
                    source="inference",
                    tier=leverage,
                    confidence=confidence
                )

            # Track totals
            if side == "long":
                self.total_inferred_long_usd += size_contribution
            else:
                self.total_inferred_short_usd += size_contribution

        return inferences

    def _apply_decay(self) -> None:
        """Apply decay to all projected zones."""
        for buckets in [self.projected_long_liqs, self.projected_short_liqs]:
            to_remove = []
            for price, bucket in buckets.items():
                bucket.size_usd *= self.decay
                if bucket.size_usd < 100:  # Less than $100 - remove
                    to_remove.append(price)

            for price in to_remove:
                del buckets[price]

        # V3: Apply decay and expiration to persistent zone manager
        if self.zone_manager:
            expired = self.zone_manager.apply_decay_and_expire(self.current_price)
            if expired > 0:
                logger.debug(f"[{self.symbol}] Zone manager expired {expired} zones")

    def _reduce_projections(self, buckets: Dict[float, PositionBucket], pct: float) -> None:
        """Reduce projection sizes by percentage (for position closing)."""
        to_remove = []
        for price, bucket in buckets.items():
            bucket.size_usd *= (1 - pct)
            if bucket.size_usd < 100:
                to_remove.append(price)

        for price in to_remove:
            del buckets[price]

    def _apply_oi_decay_to_zones(self, close_factor: float) -> None:
        """
        V3: Apply OI-based decay to persistent zone weights.

        When OI drops, positions are closing. This should reduce zone weights
        proportionally to reflect the reduced liquidation potential.
        """
        if not self.zone_manager:
            return

        # Access zone manager internals to apply decay
        # This is a direct weight reduction based on position closing
        with self.zone_manager._lock:
            for key, zone in self.zone_manager._zones.items():
                zone.weight *= (1 - close_factor)

    def _sweep(self, high: float, low: float, minute_key: int = 0) -> Tuple[int, int, float, float]:
        """
        Partially reduce projected zones that price has crossed.

        Uses SWEEP_REDUCTION_FACTOR to reduce bucket value instead of hard-deleting.
        Buckets are only fully deleted if reduced value falls below SWEEP_MIN_BUCKET_USD.
        This preserves levels that built over time — a single wick dims the level
        instead of erasing it.

        Returns (swept_long_count, swept_short_count, swept_long_notional, swept_short_notional).
        """
        swept_long = 0
        swept_short = 0
        swept_long_notional = 0.0
        swept_short_notional = 0.0
        ts_now = time.time()

        # Short projections above price get swept when high >= bucket
        to_sweep = [p for p in self.projected_short_liqs if high >= p]
        for price in to_sweep:
            bucket = self.projected_short_liqs[price]
            before_usd = bucket.size_usd
            bucket.size_usd *= (1.0 - SWEEP_REDUCTION_FACTOR)
            deleted = bucket.size_usd < SWEEP_MIN_BUCKET_USD

            swept_short_notional += before_usd if deleted else (before_usd - bucket.size_usd)
            swept_short += 1

            # Debug log: sweep reduction (one entry per bucket per sweep event)
            if self._debug_log_fh:
                debug_entry = {
                    "type": "sweep_reduced",
                    "symbol": self.symbol,
                    "side": "short",
                    "bucket": price,
                    "before_usd": round(before_usd, 2),
                    "after_usd": round(bucket.size_usd, 2),
                    "deleted": deleted
                }
                self._debug_log_fh.write(json.dumps(debug_entry) + '\n')
                self._debug_log_fh.flush()

            # Sweep log (existing format, enhanced with reduction info)
            if self._sweep_log_fh:
                sweep_entry = {
                    "type": "sweep",
                    "ts": ts_now,
                    "minute_key": minute_key,
                    "side": "short",
                    "bucket": price,
                    "total_notional_usd": round(before_usd, 2),
                    "trade_count": bucket.inference_count,
                    "peak_notional_usd": round(bucket.peak_size_usd, 2),
                    "layer": "projection",
                    "reason": f"high>={price:.0f}",
                    "trigger_high": high,
                    "reduced_to_usd": round(bucket.size_usd, 2),
                    "deleted": deleted
                }
                self._sweep_log_fh.write(json.dumps(sweep_entry) + '\n')
                self._sweep_log_fh.flush()

            if deleted:
                del self.projected_short_liqs[price]

        # Long projections below price get swept when low <= bucket
        to_sweep = [p for p in self.projected_long_liqs if low <= p]
        for price in to_sweep:
            bucket = self.projected_long_liqs[price]
            before_usd = bucket.size_usd
            bucket.size_usd *= (1.0 - SWEEP_REDUCTION_FACTOR)
            deleted = bucket.size_usd < SWEEP_MIN_BUCKET_USD

            swept_long_notional += before_usd if deleted else (before_usd - bucket.size_usd)
            swept_long += 1

            # Debug log: sweep reduction (one entry per bucket per sweep event)
            if self._debug_log_fh:
                debug_entry = {
                    "type": "sweep_reduced",
                    "symbol": self.symbol,
                    "side": "long",
                    "bucket": price,
                    "before_usd": round(before_usd, 2),
                    "after_usd": round(bucket.size_usd, 2),
                    "deleted": deleted
                }
                self._debug_log_fh.write(json.dumps(debug_entry) + '\n')
                self._debug_log_fh.flush()

            # Sweep log (existing format, enhanced with reduction info)
            if self._sweep_log_fh:
                sweep_entry = {
                    "type": "sweep",
                    "ts": ts_now,
                    "minute_key": minute_key,
                    "side": "long",
                    "bucket": price,
                    "total_notional_usd": round(before_usd, 2),
                    "trade_count": bucket.inference_count,
                    "peak_notional_usd": round(bucket.peak_size_usd, 2),
                    "layer": "projection",
                    "reason": f"low<={price:.0f}",
                    "trigger_low": low,
                    "reduced_to_usd": round(bucket.size_usd, 2),
                    "deleted": deleted
                }
                self._sweep_log_fh.write(json.dumps(sweep_entry) + '\n')
                self._sweep_log_fh.flush()

            if deleted:
                del self.projected_long_liqs[price]

        # V3: Sweep zones in persistent zone manager
        if self.zone_manager:
            swept_zones = self.zone_manager.sweep(high, low, minute_key)
            if swept_zones:
                logger.debug(f"[{self.symbol}] Zone manager swept {len(swept_zones)} persistent zones")

            # V3: Create "magnetic" zones from significant sweeps
            # When a large sweep occurs, price has shown interest in that level
            # New positions may open with entries near there, creating future liq potential
            self._create_sweep_zones(swept_zones, high, low)

        return swept_long, swept_short, swept_long_notional, swept_short_notional

    def _create_sweep_zones(self, swept_zones: list, high: float, low: float) -> None:
        """
        V3: Create zones from significant sweeps.

        When price sweeps through a zone with significant weight, traders often
        re-enter positions nearby. This creates "magnetic" zones at levels
        slightly past the sweep point.
        """
        if not self.zone_manager or not swept_zones:
            return

        SWEEP_WEIGHT_THRESHOLD = 0.5  # Minimum weight to consider significant
        SWEEP_ZONE_FACTOR = 0.3  # Weight factor for sweep-created zones

        for zone in swept_zones:
            if zone.weight < SWEEP_WEIGHT_THRESHOLD:
                continue

            # Create zone slightly past the sweep level
            # For long sweeps (price went down), create zone below the sweep
            # For short sweeps (price went up), create zone above the sweep
            offset = self.steps  # One bucket past

            if zone.side == "long":
                # Long was swept going down - create zone below
                new_price = zone.price - offset
                new_side = "long"  # New longs entering below may liquidate lower
            else:
                # Short was swept going up - create zone above
                new_price = zone.price + offset
                new_side = "short"  # New shorts entering above may liquidate higher

            # Create the sweep-based zone with reduced weight
            sweep_weight = zone.weight * SWEEP_ZONE_FACTOR

            self.zone_manager.create_or_reinforce(
                price=new_price,
                side=new_side,
                weight=sweep_weight,
                source="sweep",
                confidence=0.5  # Lower confidence for sweep-based zones
            )

            logger.debug(
                f"[{self.symbol}] Created sweep zone: {new_side} @ {new_price:.0f} "
                f"weight={sweep_weight:.3f} (from sweep @ {zone.price:.0f})"
            )

    def update_leverage_weights(self, new_weights: Dict[int, float]) -> None:
        """
        Update leverage distribution from LiquidationTape learning.

        Called when tape accumulates enough offset samples to improve estimates.
        """
        # Normalize weights
        total = sum(new_weights.values())
        if total > 0:
            self.leverage_weights = {k: v / total for k, v in new_weights.items()}
        else:
            self.leverage_weights = new_weights.copy()

        logger.info(f"[{self.symbol}] Leverage weights updated from tape learning:")
        for lev, weight in sorted(self.leverage_weights.items()):
            logger.info(f"  {lev}x: {weight:.3f}")

    def get_projections(self) -> Dict[str, Dict[float, float]]:
        """
        Get current projected liquidation zones.

        Returns:
            {"long": {price: size_usd}, "short": {price: size_usd}}
        """
        return {
            "long": {b.price: b.size_usd for b in self.projected_long_liqs.values()},
            "short": {b.price: b.size_usd for b in self.projected_short_liqs.values()}
        }

    def get_combined_heatmap(
        self,
        tape_heatmap: Dict[str, Dict[float, float]],
        tape_weight: float = 0.4,
        projection_weight: float = 0.6
    ) -> Dict[str, Dict[float, float]]:
        """
        Combine tape (historical) with projections (forward-looking).

        Independent normalization: tape and inference are normalized to 0-1
        independently BEFORE applying weights. This is required because tape
        values (observed liquidation flow, ~$1-10K/min) and inference values
        (estimated position inventory, ~$1-5M/min) differ by 100-1000x in
        magnitude. Without independent normalization, inference completely
        dominates and tape becomes invisible regardless of the weight split.

        Args:
            tape_heatmap: From LiquidationTape.get_heatmap()
            tape_weight: Weight for historical liquidations
            projection_weight: Weight for projected liquidations

        Returns:
            Combined heatmap with values in [0, tape_weight + projection_weight]
        """
        result = {"long": {}, "short": {}}

        # Collect all raw values from each source (both sides combined)
        tape_long = tape_heatmap.get("long", {})
        tape_short = tape_heatmap.get("short", {})
        all_tape_vals = [max(0.0, v) for v in list(tape_long.values()) + list(tape_short.values())]

        proj_long = {b.price: max(0.0, b.size_usd) for b in self.projected_long_liqs.values()}
        proj_short = {b.price: max(0.0, b.size_usd) for b in self.projected_short_liqs.values()}
        all_proj_vals = list(proj_long.values()) + list(proj_short.values())

        # Compute p99-based scaling factors (p99 prevents single spike from
        # crushing all other values; falls back to max for small sets)
        tape_scale = _p99_scale(all_tape_vals)
        proj_scale = _p99_scale(all_proj_vals)

        # Combine long zones with independent normalization
        all_long_prices = set(tape_long.keys()) | set(self.projected_long_liqs.keys())
        for price in all_long_prices:
            tape_norm = min(max(0.0, tape_long.get(price, 0)) / tape_scale, 1.0)
            proj_norm = min(max(0.0, proj_long.get(price, 0)) / proj_scale, 1.0)
            combined = tape_norm * tape_weight + proj_norm * projection_weight
            if combined > 0:
                result["long"][price] = combined

        # Combine short zones with independent normalization
        all_short_prices = set(tape_short.keys()) | set(self.projected_short_liqs.keys())
        for price in all_short_prices:
            tape_norm = min(max(0.0, tape_short.get(price, 0)) / tape_scale, 1.0)
            proj_norm = min(max(0.0, proj_short.get(price, 0)) / proj_scale, 1.0)
            combined = tape_norm * tape_weight + proj_norm * projection_weight
            if combined > 0:
                result["short"][price] = combined

        # Store raw scales for debug metric (read by LiquidationHeatmap)
        self._last_tape_scale = tape_scale
        self._last_proj_scale = proj_scale
        self._last_tape_max_raw = max(all_tape_vals) if all_tape_vals else 0.0
        self._last_proj_max_raw = max(all_proj_vals) if all_proj_vals else 0.0

        return result

    def get_combined_heatmap_display(
        self,
        tape_heatmap: Dict[str, Dict[float, float]],
        tape_weight: float = 0.4,
        projection_weight: float = 0.6
    ) -> Dict[str, Dict[float, float]]:
        """
        Display-only wrapper around get_combined_heatmap().

        Applies COMBINED_ZONE_BOOST to buckets where both tape and inference
        contribute above COMBINED_SOURCE_EPS. This highlights the highest-quality
        signal (combined-source zones have 56.5% sweep rate vs 0% for pure
        inference). Clamped to 1.0 max after application.

        For zone logic, clustering, persistence, or calibrator input,
        always use get_combined_heatmap() — never this display wrapper.
        """
        # Get raw combined result (also sets _last_tape_scale / _last_proj_scale)
        result = self.get_combined_heatmap(tape_heatmap, tape_weight, projection_weight)

        tape_scale = self._last_tape_scale
        proj_scale = self._last_proj_scale

        tape_long = tape_heatmap.get("long", {})
        tape_short = tape_heatmap.get("short", {})
        proj_long = {b.price: max(0.0, b.size_usd) for b in self.projected_long_liqs.values()}
        proj_short = {b.price: max(0.0, b.size_usd) for b in self.projected_short_liqs.values()}

        # Track boost statistics for debug logging
        total_buckets = 0
        boosted_buckets = 0
        clamped_buckets = 0
        sum_before = 0.0
        sum_after = 0.0

        for side_key, tape_dict, proj_dict in [
            ("long", tape_long, proj_long),
            ("short", tape_short, proj_short)
        ]:
            for price in list(result[side_key].keys()):
                total_buckets += 1
                tape_norm_val = min(max(0.0, tape_dict.get(price, 0)) / tape_scale, 1.0)
                inf_norm_val = min(max(0.0, proj_dict.get(price, 0)) / proj_scale, 1.0)
                if tape_norm_val > COMBINED_SOURCE_EPS and inf_norm_val > COMBINED_SOURCE_EPS:
                    before_val = result[side_key][price]
                    after_val = min(before_val * COMBINED_ZONE_BOOST, 1.0)
                    result[side_key][price] = after_val
                    boosted_buckets += 1
                    sum_before += before_val
                    sum_after += after_val
                    if before_val * COMBINED_ZONE_BOOST > 1.0:
                        clamped_buckets += 1

        # Debug log: combined_zone_boost every 10 minutes
        if self._debug_log_fh and self.current_minute - self._last_boost_log_minute >= 10:
            self._last_boost_log_minute = self.current_minute
            debug_entry = {
                "type": "combined_zone_boost",
                "ts": time.time(),
                "symbol": self.symbol,
                "total_buckets": total_buckets,
                "boosted_buckets": boosted_buckets,
                "boosted_pct": round(100.0 * boosted_buckets / total_buckets, 2) if total_buckets > 0 else 0.0,
                "clamped_buckets": clamped_buckets,
                "clamped_pct": round(100.0 * clamped_buckets / boosted_buckets, 2) if boosted_buckets > 0 else 0.0,
                "avg_before_boost": round(sum_before / boosted_buckets, 6) if boosted_buckets > 0 else 0.0,
                "avg_after_boost": round(sum_after / boosted_buckets, 6) if boosted_buckets > 0 else 0.0,
                "boost_factor": COMBINED_ZONE_BOOST
            }
            self._debug_log_fh.write(json.dumps(debug_entry) + '\n')
            self._debug_log_fh.flush()

        return result

    def get_stats(self) -> dict:
        """Get current inference statistics."""
        return {
            "symbol": self.symbol,
            "current_minute": self.current_minute,
            "current_price": self.current_price,
            "total_inferred_long_usd": self.total_inferred_long_usd,
            "total_inferred_short_usd": self.total_inferred_short_usd,
            "projected_long_buckets": len(self.projected_long_liqs),
            "projected_short_buckets": len(self.projected_short_liqs),
            "inferences_in_window": len(self.inferences),
            "leverage_weights": self.leverage_weights
        }

    def close(self):
        """Close all log file handles."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        if self._debug_log_fh:
            self._debug_log_fh.close()
            self._debug_log_fh = None
        if self._sweep_log_fh:
            self._sweep_log_fh.close()
            self._sweep_log_fh = None
