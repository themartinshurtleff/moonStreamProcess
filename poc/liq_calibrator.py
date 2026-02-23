"""
Liquidation Predictor Calibrator

Self-calibrating system that updates leverage weights using real Binance forceOrder
liquidation prints as feedback. Uses deterministic online calibration (no ML).

The calibrator:
1. Receives forceOrder liquidation events
2. Uses SOFT ATTRIBUTION in percent-space: distributes responsibility across leverage levels using
   d_pct(L) = |event_price - implied_price(L)| / event_src_price
   r(L) = exp(-d_pct(L) / tau_pct), normalized
3. Updates soft hit stats: hits[L] += r(L) * exp(-d_pct(L) / delta_pct)
4. tau_pct and delta_pct are scaled by short-term EWMA volatility:
   scale = clamp(vol_ewma / 0.0015, 0.7, 1.6)
   tau_pct = 0.0012 * scale, delta_pct = 0.0008 * scale
5. Maintains rolling stats over a window
6. Updates weights using bounded multiplicative rule with stabilization clamps:
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

from leverage_config import is_tier_disabled, get_enabled_tiers, log_disabled_tiers, DISABLED_TIERS

logger = logging.getLogger(__name__)


def _rotate_log_if_needed(log_path: str, max_mb: float = 200, max_age_hours: float = 24) -> bool:
    """
    Rotate log file if it exceeds size or age limits.

    Returns True if rotation occurred.
    """
    if not log_path or not os.path.exists(log_path):
        return False

    try:
        stat = os.stat(log_path)
        size_mb = stat.st_size / (1024 * 1024)
        age_hours = (time.time() - stat.st_mtime) / 3600

        should_rotate = False
        reason = None

        if size_mb > max_mb:
            should_rotate = True
            reason = f"size={size_mb:.1f}MB > {max_mb}MB"
        elif age_hours > max_age_hours:
            should_rotate = True
            reason = f"age={age_hours:.1f}h > {max_age_hours}h"

        if should_rotate:
            # Generate timestamped backup name
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            base, ext = os.path.splitext(log_path)
            backup_path = f"{base}.{timestamp}{ext}"

            # Rename existing file
            os.rename(log_path, backup_path)
            logger.info(f"Rotated log: {log_path} -> {backup_path} ({reason})")
            return True

    except Exception as e:
        logger.warning(f"Log rotation failed for {log_path}: {e}")

    return False


# Default persistence file
DEFAULT_WEIGHTS_FILE = "old_logs/old_log/liq_calibrator_weights.json"

# Schema version for weights file (increment when format changes)
WEIGHTS_SCHEMA_VERSION = 2

# Percent-space soft attribution config (replaces USD-based)
# tau_pct: temperature for responsibility distribution in percent space
# delta_pct: scale for hit contribution decay in percent space
# All values in decimal (0.0012 = 12 bps = 0.12%)
TAU_PCT_BASE = 0.0012      # 12 bps - base temperature for responsibility
DELTA_PCT_BASE = 0.0008    # 8 bps - base scale for hit contribution decay

# Volatility scaling parameters
VOL_BASELINE = 0.0015      # 15 bps - baseline vol for scaling (neutral point)
VOL_SCALE_MIN = 0.7        # Min scaling factor (low vol environment)
VOL_SCALE_MAX = 1.6        # Max scaling factor (high vol environment)
VOL_EWMA_ALPHA = 0.1       # EWMA smoothing for volatility (10% weight to new)

# Distance band thresholds (as fraction of src_price) - "src_band"
# NEAR: dist_pct <= 0.010 (1%)
# MID:  0.010 < dist_pct <= 0.025 (1-2.5%)
# FAR:  dist_pct > 0.025 (>2.5%)
DIST_BAND_NEAR = 0.010
DIST_BAND_MID = 0.025

# === EVENT PRICE SANITY CHECK ===
# Maximum allowed deviation between event_price and src_price
# Events with larger deviation are flagged as "price_insane" and excluded from calibration
# This prevents corrupt data (e.g., event_price near $0) from poisoning offset estimates
MAX_EVENT_PRICE_DEVIATION = 0.15  # 15% - any liquidation >15% from current price is suspicious

# === OFFSET GUARDRAILS ===
# Clamp learned offsets to prevent runaway values from bad data
MAX_OFFSET_PCT = 0.005    # 0.5% max offset as fraction of price
MAX_OFFSET_USD = 2000.0   # $2000 absolute max offset (safety net)

# Minimum samples per leverage tier before using its learned offset
# Tiers with fewer samples use fallback (interpolated from neighbors or global median)
MIN_TIER_SAMPLES = 50

# Implied distance band thresholds (distance from nearest implied level / src) - "implied_band"
# IMPLIED_NEAR: <= 0.25% (very close to predicted level)
# IMPLIED_MID:  (0.25%, 0.75%] (reasonably close)
# IMPLIED_FAR:  > 0.75% (far from any predicted level)
IMPLIED_BAND_NEAR = 0.0025
IMPLIED_BAND_MID = 0.0075

# Approach gating: predicted zone must have been approached within this % to be judged
APPROACH_PCT = 0.0075  # 0.75%

# Approach learning: zone is "approached" when price comes within this % (for stress-based learning)
APPROACH_LEARN_PCT = 0.0035  # 0.35% - tighter than gating

# Stress score component scales
IMB_K = 0.15                # imbalance decay constant for aggression = 1 - exp(-abs_imb / IMB_K)
OI_SCALE = 1000.0           # scale for abs(oi_change) in contract units
# DEPTH_SCALE removed - now using fractional thinning (0-1 range naturally)

# Depth window: number of buckets on each side for zone-local depth calculation
DEPTH_WINDOW_BUCKETS = 3    # ±3 buckets = 7 total buckets around zone

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

# === PHASE 3: PERCENT-BASED ADAPTIVE BIAS ===
# EMA update rate for bias correction (slow, reversible)
# Changed 2026-02-10: was 0.05, increased to 0.10 for faster convergence
BIAS_ETA = 0.10
# Maximum bias cap (±0.80% of price)
# Changed 2026-02-10: was 0.003 (0.30%), increased to 0.008 (0.80%)
# Reason: Actual systematic bias is ~0.6% ($420 at $70k), old cap limited correction to 0.3%
BIAS_CAP = 0.008
# Minimum samples required to update bias per cycle
MIN_BIAS_SAMPLES = 10

# === HIT BUCKET TOLERANCE OVERRIDE ===
# Force hit_bucket_tolerance to this value regardless of saved weights file
# Changed 2026-02-10: was 7 (from saved weights), reduced to 4 for tighter matching
HIT_BUCKET_TOLERANCE = 4

# Epsilon for numerical stability
EPS = 1e-9

# =============================================================================
# SAFETY GUARDRAILS: Kill-switch thresholds for unattended calibration
# =============================================================================
# Log rotation
LOG_ROTATION_MAX_MB = 200           # Rotate if log exceeds this size
LOG_ROTATION_MAX_AGE_HOURS = 24     # Rotate if log older than this

# Kill-switch thresholds
SAFETY_ENTROPY_MIN = 1.6            # Minimum weight entropy (below = collapse)
SAFETY_MAX_WEIGHT_CAP = 0.35        # Max weight cap (hitting this repeatedly = bad)
SAFETY_CONSECUTIVE_ENTROPY_TRIP = 3 # Consecutive cycles with H < SAFETY_ENTROPY_MIN
SAFETY_CONSECUTIVE_CLAMP_TRIP = 3   # Consecutive cycles with max_weight at cap
SAFETY_CONSECUTIVE_BOUNDS_TRIP = 6  # Consecutive cycles with buffer/tolerance at bounds

# Src quality sentinel
SAFETY_MARK_MISSING_WARN_MINUTES = 1440  # Warn if mark=0% for this many minutes (24h)


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
    # Event-time reference price (Phase 1): priority markPrice > mid > last > ohlc4
    event_src_price: float = 0.0  # Will be set from best available price
    src_source: str = "fallback"  # One of: "markPrice", "mid", "last", "fallback_ohlc4"


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
    # Per-side offset tracking (implied_miss_usd values) - DEPRECATED, kept for backward compat
    long_offsets: List[float] = field(default_factory=list)
    short_offsets: List[float] = field(default_factory=list)
    # Phase 3: Per-side PERCENT miss tracking for adaptive bias
    # miss_pct = (event_price - implied_corrected) / event_src_price
    long_miss_pct: List[float] = field(default_factory=list)
    short_miss_pct: List[float] = field(default_factory=list)
    # Attributed leverage histogram for cycle summary
    attributed_leverage_counts: Dict[int, int] = field(default_factory=dict)
    # Distance band counts (NEAR/MID/FAR) - based on distance from src_price
    band_counts: Dict[str, int] = field(default_factory=lambda: {"NEAR": 0, "MID": 0, "FAR": 0})
    # Implied band counts (IMPLIED_NEAR/IMPLIED_MID/IMPLIED_FAR) - based on distance from nearest implied level
    implied_band_counts: Dict[str, int] = field(default_factory=lambda: {"IMPLIED_NEAR": 0, "IMPLIED_MID": 0, "IMPLIED_FAR": 0})
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
    # Identifiability diagnostics: track how distinguishable leverage levels are
    ident_best_L: List[int] = field(default_factory=list)      # best leverage per event
    ident_gap_pct: List[float] = field(default_factory=list)   # gap between best and second best d_pct
    ident_resp_best: List[float] = field(default_factory=list) # max responsibility per event


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

        # Learned directional offsets (applied to implied levels) - DEPRECATED
        # Positive = actual liquidations happen above implied level
        self.long_offset_usd: float = 0.0
        self.short_offset_usd: float = 0.0

        # Phase 3: Percent-based adaptive bias (replaces USD offsets)
        # bias_pct is applied as: implied_corrected = implied_raw + bias_pct * event_src_price
        self.bias_pct_long: float = 0.0
        self.bias_pct_short: float = 0.0

        # EWMA volatility tracking for adaptive tau/delta scaling
        # vol_ewma tracks short-term volatility as EWMA of |return_pct|
        self.vol_ewma: float = VOL_BASELINE  # Initialize at baseline
        self.last_src_price: float = 0.0      # Last src price for return calculation

        # Tracking
        self.last_calibration_minute: int = 0
        self.calibration_count: int = 0
        self.minutes_since_calibration: int = 0

        # Rolling per-symbol minute cache (in-memory only)
        self.minute_cache: deque = deque(maxlen=MINUTE_CACHE_SIZE)

        # Approach event tracking for this calibration window
        self.approach_events_count: int = 0

        # =================================================================
        # SAFETY GUARDRAILS: Kill-switch tracking
        # =================================================================
        # Calibration enabled flag - can be tripped by safety checks
        self.calibration_enabled: bool = True
        self.safety_trip_reason: Optional[str] = None

        # Consecutive trip counters
        self._consecutive_low_entropy: int = 0
        self._consecutive_max_weight_cap: int = 0
        self._consecutive_buffer_at_bounds: int = 0
        self._consecutive_tolerance_at_bounds: int = 0

        # Src quality tracking (per-cycle counts, reset after logging)
        self._src_source_counts: Dict[str, int] = {"mark": 0, "mid": 0, "last": 0, "fallback": 0}
        self._total_minutes_no_mark: int = 0  # Running counter across sessions
        self._mark_warning_emitted: bool = False

        # Setup logging with rotation
        if log_file:
            # Rotate log if too large or too old
            _rotate_log_if_needed(
                log_file,
                max_mb=LOG_ROTATION_MAX_MB,
                max_age_hours=LOG_ROTATION_MAX_AGE_HOURS
            )
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

        # Log disabled tiers at startup
        log_disabled_tiers()

    def _load_weights(self) -> bool:
        """
        Load persisted weights from file if available.

        If the stored ladder differs from current, attempts to migrate weights
        by interpolating in offset-space (1/L domain) and renormalizing.
        """
        if not self.weights_file or not os.path.exists(self.weights_file):
            return False

        try:
            with open(self.weights_file, 'r') as f:
                data = json.load(f)

            if data.get('symbol') != self.symbol:
                logger.warning(f"Weights file symbol mismatch: {data.get('symbol')} != {self.symbol}")
                return False

            stored_ladder = data.get('ladder', [])
            stored_weights = data.get('weights', [])
            schema_version = data.get('schema_version', 1)

            # Load other params regardless of ladder match
            self.current_buffer = data.get('buffer', 0.002)
            self.hit_bucket_tolerance = data.get('tolerance', self.hit_bucket_tolerance)
            self.calibration_count = data.get('calibration_count', 0)
            self.long_offset_usd = data.get('long_offset_usd', 0.0)
            self.short_offset_usd = data.get('short_offset_usd', 0.0)
            self.bias_pct_long = data.get('bias_pct_long', 0.0)
            self.bias_pct_short = data.get('bias_pct_short', 0.0)

            # Check if we need to migrate weights to a new ladder
            if stored_ladder and stored_weights and len(stored_ladder) == len(stored_weights):
                if stored_ladder == self.current_ladder or not self.current_ladder:
                    # Exact match or no current ladder yet - use stored directly
                    self.current_ladder = stored_ladder
                    self.current_weights = stored_weights
                    logger.info(f"Loaded weights from {self.weights_file} (schema v{schema_version})")
                else:
                    # Ladder differs - attempt migration via offset-space interpolation
                    migrated = self._migrate_weights_to_new_ladder(
                        stored_ladder, stored_weights, self.current_ladder
                    )
                    if migrated:
                        self.current_weights = migrated
                        logger.info(f"Migrated weights from ladder {stored_ladder} to {self.current_ladder}")
                    else:
                        logger.warning(
                            f"Could not migrate weights from ladder {stored_ladder} to {self.current_ladder}. "
                            f"Reinitializing with defaults."
                        )
                        return False
            else:
                logger.warning("Invalid stored weights/ladder, reinitializing with defaults")
                return False

            logger.info(f"  Calibration count: {self.calibration_count}")
            logger.info(f"  Bias pct: long={self.bias_pct_long:.5f}, short={self.bias_pct_short:.5f}")
            logger.info(f"  Bias config: BIAS_ETA={BIAS_ETA}, BIAS_CAP={BIAS_CAP:.4f} ({BIAS_CAP*100:.2f}%)")

            # Override hit_bucket_tolerance with constant (2026-02-10 tuning)
            if self.hit_bucket_tolerance != HIT_BUCKET_TOLERANCE:
                logger.info(f"  Overriding hit_bucket_tolerance: {self.hit_bucket_tolerance} -> {HIT_BUCKET_TOLERANCE}")
                self.hit_bucket_tolerance = HIT_BUCKET_TOLERANCE

            return True

        except Exception as e:
            logger.error(f"Error loading weights: {e}")
            return False

    def _migrate_weights_to_new_ladder(
        self,
        old_ladder: List[int],
        old_weights: List[float],
        new_ladder: List[int]
    ) -> Optional[List[float]]:
        """
        Migrate weights from old ladder to new ladder by interpolating in offset-space.

        Offset-space uses 1/L as the x-axis, which is the theoretical liquidation
        distance from entry. This provides more meaningful interpolation than
        linear leverage interpolation.

        Returns:
            List of weights for new_ladder, or None if migration impossible.
        """
        if not old_ladder or not old_weights or not new_ladder:
            return None
        if len(old_ladder) != len(old_weights):
            return None

        try:
            # Build offset -> weight mapping from old ladder
            # offset = 1/L (ignoring buffer since it's constant)
            old_offsets = [1.0 / lev for lev in old_ladder]

            # Sort by offset for interpolation (ascending = higher leverage first)
            sorted_pairs = sorted(zip(old_offsets, old_weights), key=lambda x: x[0])
            old_offsets_sorted = [p[0] for p in sorted_pairs]
            old_weights_sorted = [p[1] for p in sorted_pairs]

            # Interpolate weights for new ladder
            new_weights = []
            for new_lev in new_ladder:
                new_offset = 1.0 / new_lev

                # Find bracketing offsets in old ladder
                if new_offset <= old_offsets_sorted[0]:
                    # Extrapolate from lowest offset (highest leverage)
                    new_weights.append(old_weights_sorted[0])
                elif new_offset >= old_offsets_sorted[-1]:
                    # Extrapolate from highest offset (lowest leverage)
                    new_weights.append(old_weights_sorted[-1])
                else:
                    # Linear interpolation in offset-space
                    for i in range(len(old_offsets_sorted) - 1):
                        if old_offsets_sorted[i] <= new_offset <= old_offsets_sorted[i + 1]:
                            # Interpolate between i and i+1
                            t = (new_offset - old_offsets_sorted[i]) / (old_offsets_sorted[i + 1] - old_offsets_sorted[i])
                            interp_weight = old_weights_sorted[i] + t * (old_weights_sorted[i + 1] - old_weights_sorted[i])
                            new_weights.append(max(0.0, interp_weight))
                            break

            # Renormalize to sum to 1
            total = sum(new_weights)
            if total <= 0:
                return None
            new_weights = [w / total for w in new_weights]

            return new_weights

        except Exception as e:
            logger.error(f"Weight migration failed: {e}")
            return None

    def _update_vol_ewma(self, src_price: float) -> None:
        """Update EWMA volatility estimate from price change."""
        if self.last_src_price > 0 and src_price > 0:
            # Compute absolute return as percent
            return_pct = abs(src_price - self.last_src_price) / self.last_src_price
            # Update EWMA: vol_new = (1 - alpha) * vol_old + alpha * return_pct
            self.vol_ewma = (1 - VOL_EWMA_ALPHA) * self.vol_ewma + VOL_EWMA_ALPHA * return_pct
        self.last_src_price = src_price

    def _get_scaled_tau_delta(self) -> tuple:
        """
        Get tau_pct and delta_pct scaled by current volatility.

        Returns:
            (tau_pct, delta_pct, vol_scale) - the scaled values and the scale factor used
        """
        # Scale factor: vol_ewma / baseline, clamped to [0.7, 1.6]
        vol_scale = self.vol_ewma / VOL_BASELINE
        vol_scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, vol_scale))

        tau_pct = TAU_PCT_BASE * vol_scale
        delta_pct = DELTA_PCT_BASE * vol_scale

        return tau_pct, delta_pct, vol_scale

    def _save_weights(self) -> bool:
        """
        Save current weights to file atomically.

        Uses write-to-tmp + flush + fsync + rename pattern to prevent
        corruption from partial writes or crashes.
        """
        if not self.weights_file:
            return False

        tmp_file = self.weights_file + '.tmp'

        try:
            data = {
                'schema_version': WEIGHTS_SCHEMA_VERSION,
                'symbol': self.symbol,
                'ladder': self.current_ladder,
                'weights': [round(w, 6) for w in self.current_weights],
                'buffer': self.current_buffer,
                'tolerance': self.hit_bucket_tolerance,
                'long_offset_usd': round(self.long_offset_usd, 2),
                'short_offset_usd': round(self.short_offset_usd, 2),
                # Phase 3: Percent-based bias
                'bias_pct_long': round(self.bias_pct_long, 6),
                'bias_pct_short': round(self.bias_pct_short, 6),
                'calibration_count': self.calibration_count,
                'saved_at': datetime.now().isoformat(),
                'weights_by_leverage': {
                    str(l): round(w, 6)
                    for l, w in zip(self.current_ladder, self.current_weights)
                }
            }

            # Write to temp file with flush and fsync
            with open(tmp_file, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Atomic rename (POSIX guarantees atomicity for same-filesystem rename)
            os.rename(tmp_file, self.weights_file)

            logger.debug(f"Saved weights to {self.weights_file} (schema v{WEIGHTS_SCHEMA_VERSION})")
            return True

        except Exception as e:
            logger.error(f"Error saving weights: {e}")
            # Clean up temp file if it exists
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
            return False

    def on_liquidation(self, event_data: dict) -> None:
        """
        Process a forceOrder liquidation event.

        Args:
            event_data: Dict with keys: timestamp, symbol, side, price, qty
                Optional event-time src keys (Phase 1):
                - mark_price: Mark price at event time (highest priority)
                - mid_price: Orderbook midpoint at event time
                - last_price: Last trade price at event time
        """
        with self.lock:
            try:
                event_price = float(event_data.get('price', 0))

                # Phase 1: Determine event-time src price with priority order
                # Priority: markPrice > mid > last > fallback to minute ohlc4
                event_src_price = 0.0
                src_source = "fallback_ohlc4"

                mark_price = float(event_data.get('mark_price', 0))
                mid_price = float(event_data.get('mid_price', 0))
                last_price = float(event_data.get('last_price', 0))

                if mark_price > 0:
                    event_src_price = mark_price
                    src_source = "markPrice"
                elif mid_price > 0:
                    event_src_price = mid_price
                    src_source = "mid"
                elif last_price > 0:
                    event_src_price = last_price
                    src_source = "last"
                # else: will use snapshot.src (ohlc4) as fallback in _process_event

                # Parse event
                event = LiquidationEvent(
                    timestamp=event_data.get('timestamp', time.time()),
                    symbol=event_data.get('symbol', self.symbol),
                    side=event_data.get('side', 'unknown'),
                    price=event_price,
                    qty=float(event_data.get('qty', 0)),
                    notional=event_price * float(event_data.get('qty', 0)),
                    minute_key=int(event_data.get('timestamp', time.time()) // 60),
                    event_src_price=event_src_price,
                    src_source=src_source
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

            # === DIAGNOSTIC: Log minute_inputs to verify fields are populated ===
            missing_fields = []
            if high is None:
                missing_fields.append("high")
            if low is None:
                missing_fields.append("low")
            if close is None:
                missing_fields.append("close")
            if perp_buy_vol is None:
                missing_fields.append("perp_buy_vol")
            if perp_sell_vol is None:
                missing_fields.append("perp_sell_vol")
            if perp_oi_change is None:
                missing_fields.append("perp_oi_change")
            if depth_band is None:
                missing_fields.append("depth_band")

            inputs_ok = len(missing_fields) == 0  # All fields should be present now

            if self.log_fh:
                minute_inputs_entry = {
                    "type": "minute_inputs",
                    "minute_key": minute_key,
                    "timestamp": datetime.fromtimestamp(timestamp).isoformat(),
                    "src": round(src, 2),
                    "high": round(high, 2) if high is not None else None,
                    "low": round(low, 2) if low is not None else None,
                    "close": round(close, 2) if close is not None else None,
                    "buy_vol": round(perp_buy_vol, 4) if perp_buy_vol is not None else None,
                    "sell_vol": round(perp_sell_vol, 4) if perp_sell_vol is not None else None,
                    "oi_change": round(perp_oi_change, 4) if perp_oi_change is not None else None,
                    "ok": inputs_ok,
                    "missing": missing_fields if missing_fields else []
                }
                self.log_fh.write(json.dumps(minute_inputs_entry) + '\n')
                self.log_fh.flush()

            # === DIAGNOSTIC: Log depth_band stats ===
            if self.log_fh:
                band_pct = 0.02
                low_bound = src * (1 - band_pct)
                high_bound = src * (1 + band_pct)

                if depth_band and len(depth_band) > 0:
                    n_buckets = len(depth_band)
                    notional_values = sorted(depth_band.values())
                    notional_total = sum(notional_values)

                    # Compute p50 and p95
                    p50_idx = int(n_buckets * 0.50)
                    p95_idx = min(int(n_buckets * 0.95), n_buckets - 1)
                    notional_p50 = notional_values[p50_idx] if n_buckets > 0 else 0.0
                    notional_p95 = notional_values[p95_idx] if n_buckets > 0 else 0.0

                    depth_band_stats_entry = {
                        "type": "depth_band_stats",
                        "minute_key": minute_key,
                        "src": round(src, 2),
                        "n_buckets": n_buckets,
                        "notional_total": round(notional_total, 2),
                        "notional_p50": round(notional_p50, 2),
                        "notional_p95": round(notional_p95, 2),
                        "bounds": [round(low_bound, 2), round(high_bound, 2)]
                    }
                    self.log_fh.write(json.dumps(depth_band_stats_entry) + '\n')
                    self.log_fh.flush()
                else:
                    # Determine reason for empty depth_band
                    if depth_band is None:
                        reason = "state_missing"
                    elif len(depth_band) == 0:
                        reason = "no_book"  # Caller returned empty dict
                    else:
                        reason = "bounds_mismatch"

                    depth_band_empty_entry = {
                        "type": "depth_band_empty",
                        "minute_key": minute_key,
                        "reason": reason,
                        "src": round(src, 2)
                    }
                    self.log_fh.write(json.dumps(depth_band_empty_entry) + '\n')
                    self.log_fh.flush()

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
        """
        Process a single liquidation event for stats using soft attribution.

        Phase 1: Uses event-time src_price (markPrice/mid/last) instead of minute ohlc4
        for implied level calculations and responsibility distribution.
        """
        # Find the snapshot for this event's minute (or closest prior)
        snapshot = self._get_snapshot_for_event(event)
        if not snapshot:
            return

        # === PHASE 1: DETERMINE EVENT-TIME SRC ===
        # Use event.event_src_price if available, else fall back to snapshot.src (ohlc4)
        if event.event_src_price > 0:
            src_used = event.event_src_price
            src_source = event.src_source
        else:
            src_used = snapshot.src
            src_source = "fallback_ohlc4"

        # === DISTANCE BAND COMPUTATION (using event-time src) ===
        dist_pct_event = abs(event.price - src_used) / src_used if src_used > 0 else 0.0

        # === EVENT PRICE SANITY CHECK ===
        # Reject events with wildly incorrect prices (data corruption)
        # These would poison offset estimates with garbage miss_usd values
        price_insane = dist_pct_event > MAX_EVENT_PRICE_DEVIATION
        price_insane_reason = None
        if price_insane:
            price_insane_reason = f"price_deviation={dist_pct_event*100:.1f}%>max={MAX_EVENT_PRICE_DEVIATION*100:.0f}%"
            # Log the rejected event for debugging
            if self.log_fh:
                reject_entry = {
                    "type": "event_rejected",
                    "timestamp": datetime.fromtimestamp(event.timestamp).isoformat(),
                    "reason": "price_insane",
                    "detail": price_insane_reason,
                    "event_price": round(event.price, 2),
                    "src_price": round(src_used, 2),
                    "dist_pct": round(dist_pct_event, 4),
                    "side": event.side,
                    "notional": round(event.notional, 2)
                }
                self.log_fh.write(json.dumps(reject_entry) + '\n')
                self.log_fh.flush()

        if dist_pct_event <= DIST_BAND_NEAR:
            band = "NEAR"
        elif dist_pct_event <= DIST_BAND_MID:
            band = "MID"
        else:
            band = "FAR"

        # Track band counts
        self.stats.band_counts[band] += 1

        # Track src_source for quality sentinel
        src_key = src_source.replace("_ohlc4", "")  # Normalize fallback_ohlc4 -> fallback
        if src_key in self._src_source_counts:
            self._src_source_counts[src_key] += 1
        else:
            self._src_source_counts["fallback"] += 1

        # Update window high/low for approach gating (skip if price is insane)
        if not price_insane:
            self.stats.window_high = max(self.stats.window_high, event.price)
            self.stats.window_low = min(self.stats.window_low, event.price)

        # Determine if this event should update calibration
        # Requires: NEAR or MID band AND price is sane (not corrupted data)
        updates_calibration = band in ("NEAR", "MID") and not price_insane

        # Determine which zones to check based on event side
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
            if updates_calibration and min_distance < float('inf'):
                self.stats.miss_distances.append(min_distance)

        # === SOFT ATTRIBUTION (using event-time src, percent-space) ===
        # Update volatility EWMA and get scaled tau/delta
        self._update_vol_ewma(src_used)
        tau_pct, delta_pct, vol_scale = self._get_scaled_tau_delta()

        # Compute implied levels using EVENT-TIME src (not minute ohlc4)
        implied_levels_raw = {}     # lev -> implied_price (raw, no bias)
        implied_levels_corrected = {}  # lev -> implied_price (with bias correction)
        distances = {}              # lev -> d(L) in USD (from raw implied)
        distances_pct = {}          # lev -> d_pct(L) = d(L) / src (percent-space)
        distances_corrected = {}    # lev -> d(L) in USD (from corrected implied)
        responsibilities = {}       # lev -> r(L) normalized

        # Get side-specific bias
        bias_pct = self.bias_pct_long if event.side == "long" else self.bias_pct_short

        for lev in snapshot.ladder:
            offset = (1.0 / lev) + snapshot.buffer + (0.01 / lev)
            if event.side == "long":
                implied_raw = src_used * (1 - offset)
            else:
                implied_raw = src_used * (1 + offset)

            # Apply percent-based bias correction
            implied_corrected = implied_raw + bias_pct * src_used

            implied_levels_raw[lev] = implied_raw
            implied_levels_corrected[lev] = implied_corrected
            distances[lev] = abs(event.price - implied_raw)
            # Percent-space distance: d_pct = |event_price - implied| / src
            distances_pct[lev] = distances[lev] / src_used if src_used > 0 else 0.0
            distances_corrected[lev] = abs(event.price - implied_corrected)

        # Compute responsibilities using percent-space distances
        # r(L) ∝ exp(-d_pct(L) / tau_pct)
        unnorm_resp = {}
        for lev in snapshot.ladder:
            unnorm_resp[lev] = math.exp(-distances_pct[lev] / tau_pct)

        total_resp = sum(unnorm_resp.values())
        if total_resp > 0:
            for lev in snapshot.ladder:
                responsibilities[lev] = unnorm_resp[lev] / total_resp
        else:
            for lev in snapshot.ladder:
                responsibilities[lev] = 1.0 / len(snapshot.ladder)

        # Find best leverage (highest responsibility)
        best_lev = max(responsibilities.keys(), key=lambda l: responsibilities[l])

        # === DISABLED TIER CHECK ===
        # If the attributed tier is disabled, skip all calibration updates
        # but still log the event with tier_disabled flag
        tier_disabled = is_tier_disabled(best_lev)
        if tier_disabled:
            updates_calibration = False  # Do not update calibration for disabled tiers

        # Track attributed leverage histogram (even for disabled tiers, for monitoring)
        if best_lev not in self.stats.attributed_leverage_counts:
            self.stats.attributed_leverage_counts[best_lev] = 0
        self.stats.attributed_leverage_counts[best_lev] += 1

        # === IDENTIFIABILITY DIAGNOSTICS ===
        # Compute gap between best and second-best leverage by d_pct
        sorted_by_d_pct = sorted(distances_pct.items(), key=lambda x: x[1])
        best_L = sorted_by_d_pct[0][0]
        best_d_pct = sorted_by_d_pct[0][1]
        if len(sorted_by_d_pct) > 1:
            second_L = sorted_by_d_pct[1][0]
            second_d_pct = sorted_by_d_pct[1][1]
            gap_pct = second_d_pct - best_d_pct
        else:
            second_L = best_L
            gap_pct = 0.0
        resp_best = responsibilities[best_lev]

        # Track for cycle-level aggregation
        self.stats.ident_best_L.append(best_L)
        self.stats.ident_gap_pct.append(gap_pct)
        self.stats.ident_resp_best.append(resp_best)

        # === PHASE 0: DIAGNOSTIC LOGGING ===
        # Compute nearest_implied_distance_pct for diagnosis
        nearest_implied_raw = implied_levels_raw[best_lev]
        nearest_implied_corrected = implied_levels_corrected[best_lev]
        nearest_implied_dist_pct = abs(event.price - nearest_implied_raw) / src_used if src_used > 0 else 0.0

        # === IMPLIED BAND CLASSIFICATION ===
        # Based on distance from nearest implied level (not src price)
        if nearest_implied_dist_pct <= IMPLIED_BAND_NEAR:
            implied_band = "IMPLIED_NEAR"
        elif nearest_implied_dist_pct <= IMPLIED_BAND_MID:
            implied_band = "IMPLIED_MID"
        else:
            implied_band = "IMPLIED_FAR"

        # Track implied band counts
        self.stats.implied_band_counts[implied_band] += 1

        # Compute miss values
        miss_usd = event.price - nearest_implied_corrected
        miss_pct = miss_usd / src_used if src_used > 0 else 0.0

        # Only update soft stats for NEAR and MID events
        if updates_calibration:
            for lev in snapshot.ladder:
                r_L = responsibilities[lev]
                d_pct_L = distances_pct[lev]
                # hit_credit ∝ exp(-d_pct / delta_pct)
                hit_contrib = r_L * math.exp(-d_pct_L / delta_pct)

                if lev not in self.stats.leverage_totals:
                    self.stats.leverage_totals[lev] = 0.0
                    self.stats.leverage_hits[lev] = 0.0
                    self.stats.leverage_notional[lev] = 0.0

                self.stats.leverage_totals[lev] += r_L
                self.stats.leverage_hits[lev] += hit_contrib
                self.stats.leverage_notional[lev] += r_L * event.notional

            # Track miss_pct for Phase 3 adaptive bias (only NEAR + MID)
            if event.side == "long":
                self.stats.long_miss_pct.append(miss_pct)
                self.stats.long_offsets.append(miss_usd)  # Keep USD for backward compat
            else:
                self.stats.short_miss_pct.append(miss_pct)
                self.stats.short_offsets.append(miss_usd)

        # Track direction for buffer tuning
        nearest_pred = None
        if pred_zones:
            nearest_pred = min(pred_zones.keys(), key=lambda p: abs(p - event.price))
            if updates_calibration:
                direction = abs(nearest_pred - src_used) - abs(event.price - src_used)
                self.stats.distance_directions.append(direction)

        # Log individual event with all Phase 0-4 fields
        if self.log_events and self.log_fh:
            # Compute guardrail logging fields
            offset_used_usd = self.long_offset_usd if event.side == "long" else self.short_offset_usd
            offset_used_pct = offset_used_usd / src_used if src_used > 0 else 0.0

            self._log_event_v2(
                event=event,
                snapshot=snapshot,
                pred_zones=pred_zones,
                is_hit=is_hit,
                hit_strength=hit_strength,
                min_distance=min_distance,
                best_lev=best_lev,
                nearest_pred=nearest_pred,
                responsibilities=responsibilities,
                distances=distances,
                dist_pct_event=dist_pct_event,
                src_band=band,
                implied_band=implied_band,
                updates_calibration=updates_calibration,
                # Phase 1 fields
                src_used=src_used,
                src_source=src_source,
                # Phase 0 diagnostic fields
                nearest_implied_raw=nearest_implied_raw,
                nearest_implied_corrected=nearest_implied_corrected,
                nearest_implied_dist_pct=nearest_implied_dist_pct,
                # Phase 3 bias fields
                miss_pct=miss_pct,
                miss_usd=miss_usd,
                bias_pct=bias_pct,
                implied_levels_raw=implied_levels_raw,
                implied_levels_corrected=implied_levels_corrected,
                # Percent-space soft attribution params
                tau_pct=tau_pct,
                delta_pct=delta_pct,
                vol_scale=vol_scale,
                # Guardrail fields
                price_insane=price_insane,
                price_insane_reason=price_insane_reason,
                offset_used_usd=offset_used_usd,
                offset_used_pct=offset_used_pct,
                # V3: Tier disable flag
                tier_disabled=tier_disabled
            )

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

        # Get percent-space tau (no delta needed for approach events)
        tau_pct, _, _ = self._get_scaled_tau_delta()

        # === PHASE 1: Collect all approach candidates ===
        candidates = []
        # V3: Track zones that were not approached (for skip reason logging)
        not_approached = []
        for zone_price, zone_strength, zone_side in all_zones:
            # Check if price approached this zone
            dist_pct = abs(close - zone_price) / close
            if dist_pct > APPROACH_LEARN_PCT:
                # V3: Track reason for skip
                not_approached.append({
                    'zone_price': zone_price,
                    'zone_side': zone_side,
                    'dist_pct': dist_pct,
                    'skip_reason': 'too_far_from_price'
                })
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

            # Compute soft attribution: r(L) for each leverage using percent-space
            responsibilities = {}
            distances = {}
            distances_pct = {}

            for lev in snapshot.ladder:
                offset = (1.0 / lev) + snapshot.buffer + (0.01 / lev)
                if zone_side == "long":
                    implied = snapshot.src * (1 - offset)
                else:
                    implied = snapshot.src * (1 + offset)
                distances[lev] = abs(zone_price - implied)
                distances_pct[lev] = distances[lev] / snapshot.src if snapshot.src > 0 else 0.0

            # Compute unnormalized responsibilities using percent-space
            # r(L) ∝ exp(-d_pct(L) / tau_pct)
            unnorm_resp = {}
            for lev in snapshot.ladder:
                unnorm_resp[lev] = math.exp(-distances_pct[lev] / tau_pct)

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
                skipped=skipped,
                not_approached=not_approached  # V3: Include zones too far from price
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
        - depth_thinning = FRACTIONAL thinning: (depth_prev - depth_now) / depth_prev, clamped [0,1]

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

        # === DEPTH THINNING (zone-local, fractional) ===
        # Use robust window-based depth lookup around zone_price
        radius = DEPTH_WINDOW_BUCKETS

        # Get depth in ±W buckets around zone from current and previous depth_band
        depth_now, n_buckets_now = self._get_depth_around_zone(
            cache_entry.depth_band, zone_price, steps, radius=radius
        )

        if prev_cache is not None:
            depth_prev, n_buckets_prev = self._get_depth_around_zone(
                prev_cache.depth_band, zone_price, steps, radius=radius
            )
        else:
            # Guard: no previous depth, set depth_thinning = 0
            depth_prev = 0.0
            n_buckets_prev = 0

        # Compute FRACTIONAL thinning (not exponential)
        # thin_frac = (depth_prev - depth_now) / depth_prev, clamped to [0, 1]
        if depth_prev > EPS:
            thin_frac = (depth_prev - depth_now) / depth_prev
            depth_thinning = max(0.0, min(1.0, thin_frac))
        else:
            # Guard: no previous depth data, don't fabricate
            thin_frac = 0.0
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
            'thin_frac': thin_frac,
            'depth_thinning': depth_thinning,
            'zone_price': zone_price,
            'depth_window': radius,
            'n_buckets_prev': n_buckets_prev,
            'n_buckets_now': n_buckets_now
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

    def _get_depth_around_zone(
        self,
        depth_band: Dict[float, float],
        zone_price: float,
        steps: float,
        radius: int = DEPTH_WINDOW_BUCKETS
    ) -> Tuple[float, int]:
        """
        Sum depth from depth_band for buckets within ±radius of zone_price.

        Uses fuzzy bucket matching to avoid float key comparison issues:
        - Computes the price window: [zone_price - radius*steps, zone_price + radius*steps]
        - Sums all depth_band entries whose keys fall within this window

        Args:
            depth_band: Dict of price_bucket -> notional_size
            zone_price: Target zone price (not necessarily a bucket key)
            steps: Bucket size
            radius: Number of buckets on each side (default DEPTH_WINDOW_BUCKETS)

        Returns:
            Tuple of (total_depth, n_buckets_found)
        """
        if not depth_band:
            return 0.0, 0

        # Define price window around zone
        window_half = radius * steps
        low_bound = zone_price - window_half
        high_bound = zone_price + window_half

        total = 0.0
        n_found = 0

        # Sum all depth_band entries within the window
        for bucket_price, notional in depth_band.items():
            if low_bound <= bucket_price <= high_bound:
                total += notional
                n_found += 1

        return total, n_found

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
            'zone_side': zone_side,
            'zone_strength': round(zone_strength, 4),
            'approach_dist_pct': round(approach_dist_pct, 6),
            # Stress breakdown (updated for fractional thinning)
            'stress_breakdown': {
                'abs_imb': round(stress_breakdown['abs_imb'], 4),
                'aggression': round(stress_breakdown['aggression'], 4),
                'oi_abs': round(stress_breakdown['oi_abs'], 4),
                'oi_fuel': round(stress_breakdown['oi_fuel'], 4),
                'depth_prev': round(stress_breakdown['depth_prev'], 2),
                'depth_now': round(stress_breakdown['depth_now'], 2),
                'thin_frac': round(stress_breakdown['thin_frac'], 4),
                'depth_thinning': round(stress_breakdown['depth_thinning'], 4),
                'depth_window': stress_breakdown.get('depth_window', 0),
                'n_buckets_prev': stress_breakdown.get('n_buckets_prev', 0),
                'n_buckets_now': stress_breakdown.get('n_buckets_now', 0),
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
        skipped: List[Dict],
        not_approached: List[Dict] = None  # V3: Zones too far from price
    ) -> None:
        """Log approach throttling summary for this minute with top-K selection details and skip reasons."""
        if not self.log_fh:
            return

        # Build selected zone details with depth debug info
        selected_details = []
        for cand in selected:
            sb = cand['stress_breakdown']
            selected_details.append({
                'zone_price': round(cand['zone_price'], 2),
                'zone_side': cand['zone_side'],
                'dist_pct': round(cand['dist_pct'], 6),
                'S': round(cand['S'], 4),
                'S_eff': round(cand['S_eff'], 4),
                'resp_top3': {str(k): round(v, 4) for k, v in cand['resp_top3'].items()},
                'stress_breakdown': {
                    'aggression': round(sb['aggression'], 4),
                    'oi_fuel': round(sb['oi_fuel'], 4),
                    'depth_thinning': round(sb['depth_thinning'], 4)
                },
                # Depth debug fields (new)
                'depth_debug': {
                    'depth_prev': round(sb.get('depth_prev', 0), 2),
                    'depth_now': round(sb.get('depth_now', 0), 2),
                    'thin_frac': round(sb.get('thin_frac', 0), 4),
                    'window': sb.get('depth_window', 0),
                    'n_buckets_prev': sb.get('n_buckets_prev', 0),
                    'n_buckets_now': sb.get('n_buckets_now', 0)
                }
            })

        # Build skipped zone summary with skip reasons (V3)
        skipped_summary = []
        for cand in skipped:
            skipped_summary.append({
                'zone_price': round(cand['zone_price'], 2),
                'zone_side': cand['zone_side'],
                'S_eff': round(cand['S_eff'], 4),
                # V3: Add explicit skip reason
                'skip_reason': 'not_in_top_k',
                'rank': candidates.index(cand) + 1  # 1-indexed rank by S_eff
            })

        # V3: Build skip reason summary
        skip_reasons = {}
        if skipped:
            skip_reasons['not_in_top_k'] = len(skipped)
        if not_approached:
            skip_reasons['too_far_from_price'] = len(not_approached)

        log_entry = {
            'type': 'approach_minute_summary',
            'symbol': self.symbol,
            'minute_key': cache_entry.minute_key,
            'timestamp': datetime.fromtimestamp(cache_entry.timestamp).isoformat(),
            'close': round(close, 2),
            # Throttling stats
            'approach_candidates': len(candidates),
            'approach_used': len(selected),
            'approach_skipped': len(skipped) + (len(not_approached) if not_approached else 0),
            # V3: Explicit skip reason breakdown
            'skip_reasons': skip_reasons,
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

        # Get percent-space tau for soft attribution
        tau_pct, _, _ = self._get_scaled_tau_delta()

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

            # Compute responsibility distribution using ORIGIN src/buffer in percent-space
            responsibilities = {}
            for lev in self.current_ladder:
                offset = (1.0 / lev) + origin_buffer + (0.01 / lev)
                if zone_side == "long":
                    implied = origin_src * (1 - offset)
                else:
                    implied = origin_src * (1 + offset)
                distance = abs(zone_price - implied)
                distance_pct = distance / origin_src if origin_src > 0 else 0.0
                # r(L) ∝ exp(-d_pct(L) / tau_pct)
                responsibilities[lev] = math.exp(-distance_pct / tau_pct)

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

        # === PHASE 3: PERCENT-BASED ADAPTIVE BIAS UPDATE ===
        # Update bias_pct_long and bias_pct_short using EMA with robust aggregate
        old_bias_pct_long = self.bias_pct_long
        old_bias_pct_short = self.bias_pct_short
        bias_update_info = {
            'long_samples': len(self.stats.long_miss_pct),
            'short_samples': len(self.stats.short_miss_pct),
            'long_agg_miss_pct': None,
            'short_agg_miss_pct': None,
            'long_updated': False,
            'short_updated': False
        }

        # Long bias update (only if enough samples)
        if len(self.stats.long_miss_pct) >= MIN_BIAS_SAMPLES:
            # Use median as robust aggregate
            sorted_long = sorted(self.stats.long_miss_pct)
            median_idx = len(sorted_long) // 2
            agg_long = sorted_long[median_idx]
            bias_update_info['long_agg_miss_pct'] = agg_long

            # EMA update with clamping
            new_bias_long = (1 - BIAS_ETA) * self.bias_pct_long + BIAS_ETA * agg_long
            self.bias_pct_long = max(-BIAS_CAP, min(BIAS_CAP, new_bias_long))
            bias_update_info['long_updated'] = True

            # Warn if bias is hitting cap (indicates need for larger BIAS_CAP)
            if abs(self.bias_pct_long) >= BIAS_CAP * 0.95:
                logger.warning(f"Long bias at cap: {self.bias_pct_long:.5f} (cap={BIAS_CAP:.4f}), unclamped={new_bias_long:.5f}")

        # Short bias update (only if enough samples)
        if len(self.stats.short_miss_pct) >= MIN_BIAS_SAMPLES:
            # Use median as robust aggregate
            sorted_short = sorted(self.stats.short_miss_pct)
            median_idx = len(sorted_short) // 2
            agg_short = sorted_short[median_idx]
            bias_update_info['short_agg_miss_pct'] = agg_short

            # EMA update with clamping
            new_bias_short = (1 - BIAS_ETA) * self.bias_pct_short + BIAS_ETA * agg_short
            self.bias_pct_short = max(-BIAS_CAP, min(BIAS_CAP, new_bias_short))
            bias_update_info['short_updated'] = True

            # Warn if bias is hitting cap (indicates need for larger BIAS_CAP)
            if abs(self.bias_pct_short) >= BIAS_CAP * 0.95:
                logger.warning(f"Short bias at cap: {self.bias_pct_short:.5f} (cap={BIAS_CAP:.4f}), unclamped={new_bias_short:.5f}")

        # Keep old USD offset tuning for backward compatibility (deprecated)
        # Now with robust update (median) and clamping
        old_long_offset = self.long_offset_usd
        old_short_offset = self.short_offset_usd
        # Changed 2026-02-10: was 0.3, increased to 0.5 for faster offset convergence
        offset_lr = 0.5
        offset_clamped_long = False
        offset_clamped_short = False

        # Get current price for percent-space clamping
        current_price = snapshot.src if snapshot.src > 0 else 80000.0  # fallback
        max_offset_from_pct = current_price * MAX_OFFSET_PCT

        if len(self.stats.long_offsets) >= 3:
            # Use median instead of mean for robustness (winsorization light)
            sorted_offsets = sorted(self.stats.long_offsets)
            median_miss = sorted_offsets[len(sorted_offsets) // 2]
            self.long_offset_usd += offset_lr * median_miss

            # Clamp offset: min of percent-based and absolute USD limits
            max_allowed = min(max_offset_from_pct, MAX_OFFSET_USD)
            if abs(self.long_offset_usd) > max_allowed:
                old_val = self.long_offset_usd
                self.long_offset_usd = max(-max_allowed, min(max_allowed, self.long_offset_usd))
                offset_clamped_long = True
                logger.info(f"Long offset clamped: {old_val:.2f} -> {self.long_offset_usd:.2f}")

        if len(self.stats.short_offsets) >= 3:
            # Use median instead of mean for robustness
            sorted_offsets = sorted(self.stats.short_offsets)
            median_miss = sorted_offsets[len(sorted_offsets) // 2]
            self.short_offset_usd += offset_lr * median_miss

            # Clamp offset: min of percent-based and absolute USD limits
            max_allowed = min(max_offset_from_pct, MAX_OFFSET_USD)
            if abs(self.short_offset_usd) > max_allowed:
                old_val = self.short_offset_usd
                self.short_offset_usd = max(-max_allowed, min(max_allowed, self.short_offset_usd))
                offset_clamped_short = True
                logger.info(f"Short offset clamped: {old_val:.2f} -> {self.short_offset_usd:.2f}")

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

        # Log calibration (with Phase 3/4 fields)
        self._log_calibration(
            minute_key, hit_rate, avg_hit_strength, avg_miss_distance,
            old_weights, new_weights, old_buffer, new_buffer,
            old_tolerance, new_tolerance,
            old_long_offset, self.long_offset_usd,
            old_short_offset, self.short_offset_usd,
            snapshot,
            bias_update_info=bias_update_info,
            old_bias_pct_long=old_bias_pct_long,
            old_bias_pct_short=old_bias_pct_short,
            offset_clamped_long=offset_clamped_long,
            offset_clamped_short=offset_clamped_short
        )

        # === IDENTIFIABILITY DIAGNOSTICS LOG ===
        if self.log_fh and len(self.stats.ident_best_L) > 0:
            n_events = len(self.stats.ident_best_L)

            # Build best_L histogram
            best_L_hist = {}
            for lev in self.stats.ident_best_L:
                best_L_hist[lev] = best_L_hist.get(lev, 0) + 1

            # Compute percentiles for gap_pct
            sorted_gap = sorted(self.stats.ident_gap_pct)
            gap_p50 = sorted_gap[int(n_events * 0.50)] if n_events > 0 else 0.0
            gap_p95 = sorted_gap[min(int(n_events * 0.95), n_events - 1)] if n_events > 0 else 0.0

            # Compute percentiles for resp_best
            sorted_resp = sorted(self.stats.ident_resp_best)
            resp_p50 = sorted_resp[int(n_events * 0.50)] if n_events > 0 else 0.0
            resp_p95 = sorted_resp[min(int(n_events * 0.95), n_events - 1)] if n_events > 0 else 0.0

            ident_diag_entry = {
                "type": "ident_diag",
                "minute_key": minute_key,
                "n": n_events,
                "best_L_hist": {str(k): v for k, v in sorted(best_L_hist.items())},
                "gap_pct_p50": round(gap_p50, 6),
                "gap_pct_p95": round(gap_p95, 6),
                "resp_best_p50": round(resp_p50, 4),
                "resp_best_p95": round(resp_p95, 4)
            }
            self.log_fh.write(json.dumps(ident_diag_entry) + '\n')
            self.log_fh.flush()

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

        # Line 2: Phase 3 - Bias pct values
        long_samples = bias_update_info['long_samples']
        short_samples = bias_update_info['short_samples']
        long_agg = bias_update_info['long_agg_miss_pct']
        short_agg = bias_update_info['short_agg_miss_pct']
        long_agg_str = f"{long_agg*100:.4f}%" if long_agg is not None else "N/A"
        short_agg_str = f"{short_agg*100:.4f}%" if short_agg is not None else "N/A"
        print(f"  BIAS long={self.bias_pct_long*100:.4f}% (n={long_samples},agg={long_agg_str}) "
              f"short={self.bias_pct_short*100:.4f}% (n={short_samples},agg={short_agg_str})")

        # Line 3: Attributed leverage histogram (Phase 4)
        lev_counts = self.stats.attributed_leverage_counts
        if lev_counts:
            sorted_levs = sorted(lev_counts.items(), key=lambda x: -x[1])[:5]
            hist_str = ','.join([f"{lev}x:{cnt}" for lev, cnt in sorted_levs])
            print(f"  LEV_HIST top5=[{hist_str}]")

        # Line 4: Unreached penalty stats
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

        # Line 5: Floor enforcement stats (only if triggered)
        if floor_info.get('floor_triggered', False):
            drained_str = ','.join([f"{d['leverage']}x" for d in floor_info.get('drained_leverages', [])])
            print(f"  FLOOR triggered mid_mass={floor_info['mid_mass_before']:.3f}->"
                  f"{floor_info['mid_mass_after']:.3f} drained=[{drained_str}]")

        # Store approach events count before reset
        approach_events_this_window = self.approach_events_count

        # =================================================================
        # SAFETY GUARDRAILS: Check for dangerous conditions
        # =================================================================
        max_weight = max(new_weights) if new_weights else 0
        min_weight = min(new_weights) if new_weights else 0
        buffer_at_min = abs(new_buffer - self.min_buffer) < 1e-6
        buffer_at_max = abs(new_buffer - self.max_buffer) < 1e-6
        tolerance_at_min = new_tolerance <= self.min_tolerance
        tolerance_at_max = new_tolerance >= self.max_tolerance

        # Track consecutive safety conditions
        if weight_entropy < SAFETY_ENTROPY_MIN:
            self._consecutive_low_entropy += 1
        else:
            self._consecutive_low_entropy = 0

        if max_weight >= self.max_weight - 0.01:  # Within 1% of cap
            self._consecutive_max_weight_cap += 1
        else:
            self._consecutive_max_weight_cap = 0

        if buffer_at_min or buffer_at_max:
            self._consecutive_buffer_at_bounds += 1
        else:
            self._consecutive_buffer_at_bounds = 0

        if tolerance_at_min or tolerance_at_max:
            self._consecutive_tolerance_at_bounds += 1
        else:
            self._consecutive_tolerance_at_bounds = 0

        # Check for kill-switch conditions
        safety_trip_triggered = False
        safety_trip_reason = None

        if self._consecutive_low_entropy >= SAFETY_CONSECUTIVE_ENTROPY_TRIP:
            safety_trip_triggered = True
            safety_trip_reason = f"entropy_collapse: H={weight_entropy:.3f} < {SAFETY_ENTROPY_MIN} for {self._consecutive_low_entropy} cycles"

        elif self._consecutive_max_weight_cap >= SAFETY_CONSECUTIVE_CLAMP_TRIP:
            safety_trip_triggered = True
            safety_trip_reason = f"max_weight_cap: max_w={max_weight:.4f} at cap for {self._consecutive_max_weight_cap} cycles"

        elif (self._consecutive_buffer_at_bounds >= SAFETY_CONSECUTIVE_BOUNDS_TRIP or
              self._consecutive_tolerance_at_bounds >= SAFETY_CONSECUTIVE_BOUNDS_TRIP):
            safety_trip_triggered = True
            bounds_info = []
            if self._consecutive_buffer_at_bounds >= SAFETY_CONSECUTIVE_BOUNDS_TRIP:
                bounds_info.append(f"buffer_at_bounds={self._consecutive_buffer_at_bounds}")
            if self._consecutive_tolerance_at_bounds >= SAFETY_CONSECUTIVE_BOUNDS_TRIP:
                bounds_info.append(f"tolerance_at_bounds={self._consecutive_tolerance_at_bounds}")
            safety_trip_reason = f"bounds_pinned: {', '.join(bounds_info)}"

        # Log safety trip if triggered
        if safety_trip_triggered and self.calibration_enabled:
            self.calibration_enabled = False
            self.safety_trip_reason = safety_trip_reason
            print(f"  SAFETY_TRIP: {safety_trip_reason}")
            print(f"  Calibration DISABLED - weights/buffer/tolerance will not be updated")

            if self.log_fh:
                safety_entry = {
                    "type": "safety_trip",
                    "timestamp": datetime.now().isoformat(),
                    "minute_key": minute_key,
                    "cycle": self.calibration_count,
                    "reason": safety_trip_reason,
                    "entropy": round(weight_entropy, 4),
                    "max_weight": round(max_weight, 4),
                    "min_weight": round(min_weight, 6),
                    "buffer": round(new_buffer, 6),
                    "tolerance": new_tolerance,
                    "consecutive_low_entropy": self._consecutive_low_entropy,
                    "consecutive_max_weight_cap": self._consecutive_max_weight_cap,
                    "consecutive_buffer_at_bounds": self._consecutive_buffer_at_bounds,
                    "consecutive_tolerance_at_bounds": self._consecutive_tolerance_at_bounds
                }
                self.log_fh.write(json.dumps(safety_entry) + '\n')
                self.log_fh.flush()

        # =================================================================
        # SRC QUALITY SENTINEL: Check for missing mark price
        # =================================================================
        total_src_events = sum(self._src_source_counts.values())
        mark_count = self._src_source_counts.get("mark", 0)

        if total_src_events > 0 and mark_count == 0:
            self._total_minutes_no_mark += self.window_minutes
        else:
            self._total_minutes_no_mark = 0

        # Emit warning if mark has been missing for a full day
        if self._total_minutes_no_mark >= SAFETY_MARK_MISSING_WARN_MINUTES and not self._mark_warning_emitted:
            self._mark_warning_emitted = True
            if self.log_fh:
                mark_warning = {
                    "type": "mark_stream_missing",
                    "timestamp": datetime.now().isoformat(),
                    "minute_key": minute_key,
                    "minutes_no_mark": self._total_minutes_no_mark,
                    "action": "continue_using_mid",
                    "src_source_counts": self._src_source_counts.copy()
                }
                self.log_fh.write(json.dumps(mark_warning) + '\n')
                self.log_fh.flush()
            print(f"  WARN: mark_price stream missing for {self._total_minutes_no_mark} minutes, using mid_price")

        # Reset stats for next window
        self.stats = CalibrationStats()
        self.approach_events_count = 0
        self._src_source_counts = {"mark": 0, "mid": 0, "last": 0, "fallback": 0}  # Reset per-cycle
        self.minutes_since_calibration = 0
        self.calibration_count += 1
        self.last_calibration_minute = minute_key

        # Update current params ONLY if calibration is still enabled
        if self.calibration_enabled:
            self.current_weights = new_weights
            self.current_buffer = new_buffer
            # Persist weights to file
            self._save_weights()
        else:
            # Keep old weights, just log that we're not updating
            new_weights = self.current_weights
            new_buffer = self.current_buffer

        # Notify callback
        result = {
            'weights': new_weights,
            'buffer': new_buffer,
            'hit_rate': hit_rate,
            'calibration_count': self.calibration_count
        }

        if self.on_weights_updated:
            try:
                self.on_weights_updated(self.symbol, new_weights, new_buffer)
            except Exception as e:
                logger.error(f"on_weights_updated callback failed for {self.symbol}: {e}", exc_info=True)

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
        snapshot: MinuteSnapshot,
        # Phase 3: Bias update info
        bias_update_info: Dict = None,
        old_bias_pct_long: float = 0.0,
        old_bias_pct_short: float = 0.0,
        # Guardrail fields
        offset_clamped_long: bool = False,
        offset_clamped_short: bool = False
    ) -> None:
        """Write calibration event to JSONL log with Phase 3/4 fields."""
        if not self.log_fh:
            return

        # Get top 5 predicted levels
        top_longs = sorted(snapshot.pred_longs.items(), key=lambda x: x[1], reverse=True)[:5]
        top_shorts = sorted(snapshot.pred_shorts.items(), key=lambda x: x[1], reverse=True)[:5]

        # Phase 4: Leverage histogram
        lev_hist = dict(self.stats.attributed_leverage_counts)

        log_entry = {
            'type': 'calibration',
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
            # Phase 4: Attributed leverage histogram
            'attributed_leverage_counts': lev_hist,
            # Distance band counts (src_band: dist from src, implied_band: dist from nearest implied)
            'src_band_counts': self.stats.band_counts.copy(),
            'implied_band_counts': self.stats.implied_band_counts.copy(),
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
            # Deprecated USD offsets (kept for backward compat)
            'old_long_offset': round(old_long_offset, 2),
            'new_long_offset': round(new_long_offset, 2),
            'old_short_offset': round(old_short_offset, 2),
            'new_short_offset': round(new_short_offset, 2),
            # Phase 3: Percent-based bias
            'old_bias_pct_long': round(old_bias_pct_long, 6),
            'new_bias_pct_long': round(self.bias_pct_long, 6),
            'old_bias_pct_short': round(old_bias_pct_short, 6),
            'new_bias_pct_short': round(self.bias_pct_short, 6),
            # Phase 3: Bias update details
            'bias_update_info': bias_update_info or {},
            # Phase 4: Full ladder (confirm extended levels)
            'ladder': self.current_ladder,
            'src_price': round(snapshot.src, 2),
            'top_long_zones': [[p, round(s, 3)] for p, s in top_longs],
            'top_short_zones': [[p, round(s, 3)] for p, s in top_shorts],
            # =================================================================
            # SAFETY GUARDRAILS: Per-cycle safety metrics
            # =================================================================
            'safety': {
                'calibration_enabled': self.calibration_enabled,
                'entropy': round(-sum(w * math.log(w) if w > EPS else 0 for w in new_weights), 4),
                'max_weight': round(max(new_weights) if new_weights else 0, 4),
                'min_weight': round(min(new_weights) if new_weights else 0, 6),
                'buffer_at_min': abs(new_buffer - self.min_buffer) < 1e-6,
                'buffer_at_max': abs(new_buffer - self.max_buffer) < 1e-6,
                'tolerance_at_min': new_tolerance <= self.min_tolerance,
                'tolerance_at_max': new_tolerance >= self.max_tolerance,
                'consecutive_low_entropy': self._consecutive_low_entropy,
                'consecutive_max_weight_cap': self._consecutive_max_weight_cap,
                'consecutive_buffer_at_bounds': self._consecutive_buffer_at_bounds,
                'consecutive_tolerance_at_bounds': self._consecutive_tolerance_at_bounds,
                'src_source_counts': self._src_source_counts.copy(),
                'minutes_no_mark': self._total_minutes_no_mark,
                # Offset guardrails
                'offset_clamped_long': offset_clamped_long,
                'offset_clamped_short': offset_clamped_short,
                'max_offset_pct': MAX_OFFSET_PCT,
                'max_offset_usd': MAX_OFFSET_USD,
                'max_event_price_deviation': MAX_EVENT_PRICE_DEVIATION
            }
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

    def _log_event_v2(
        self,
        event: LiquidationEvent,
        snapshot: MinuteSnapshot,
        pred_zones: Dict[float, float],
        is_hit: bool,
        hit_strength: float,
        min_distance: float,
        best_lev: int,
        nearest_pred: Optional[float],
        responsibilities: Dict[int, float],
        distances: Dict[int, float],
        dist_pct_event: float,
        src_band: str,
        implied_band: str,
        updates_calibration: bool,
        # Phase 1 fields
        src_used: float,
        src_source: str,
        # Phase 0 diagnostic fields
        nearest_implied_raw: float,
        nearest_implied_corrected: float,
        nearest_implied_dist_pct: float,
        # Phase 3 bias fields
        miss_pct: float,
        miss_usd: float,
        bias_pct: float,
        implied_levels_raw: Dict[int, float],
        implied_levels_corrected: Dict[int, float],
        # Percent-space soft attribution params
        tau_pct: float = 0.0,
        delta_pct: float = 0.0,
        vol_scale: float = 1.0,
        # Guardrail fields
        price_insane: bool = False,
        price_insane_reason: Optional[str] = None,
        offset_used_usd: float = 0.0,
        offset_used_pct: float = 0.0,
        # V3: Tier disable flag
        tier_disabled: bool = False
    ) -> None:
        """
        Log individual liquidation event with all Phase 0-4 fields.

        Phase 0: Diagnostic fields (dist_pct_event, nearest_implied_dist_pct, src_used, src_source)
        Phase 1: Event-time src (src_used, src_source)
        Phase 3: Bias fields (miss_pct, miss_usd, bias_pct)
        Phase 4: Full verification outputs (ladder, implied_levels_raw, implied_levels_corrected)
        Soft attribution: tau_pct, delta_pct, vol_scale
        V3: tier_disabled flag for excluded leverage tiers
        """
        if not self.log_fh:
            return

        # Get top 5 predicted zones for this side
        top_zones = sorted(pred_zones.items(), key=lambda x: x[1], reverse=True)[:5]

        # Build top 3 leverages by responsibility with d(L)
        top_3_by_responsibility = []
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
            # Phase 1: Event-time src (not minute ohlc4)
            'event_src_price': round(src_used, 2),
            'src_source': src_source,
            'snapshot_src': round(snapshot.src, 2),  # Keep for comparison
            # Phase 0: Diagnostic dist_pct computed from event_src_price
            'dist_pct_event': round(dist_pct_event, 6),
            'nearest_implied_dist_pct': round(nearest_implied_dist_pct, 6),
            # Banding: src_band (dist from src), implied_band (dist from nearest implied)
            'src_band': src_band,
            'implied_band': implied_band,
            'updates_calibration': updates_calibration,
            'event_qty': round(event.qty, 6),
            'event_notional': round(event.notional, 2),
            'is_hit': is_hit,
            'hit_strength': round(hit_strength, 4) if is_hit else None,
            'miss_distance_buckets': round(min_distance, 2) if not is_hit else None,
            # Soft attribution: top 3 leverages by responsibility
            'top_3_by_responsibility': top_3_by_responsibility,
            'attributed_leverage': best_lev,
            # Phase 0/3: Implied levels (raw and corrected)
            'nearest_implied_leverage_raw': best_lev,
            'nearest_implied_raw': round(nearest_implied_raw, 2),
            'nearest_implied_corrected': round(nearest_implied_corrected, 2),
            # Phase 3: Miss values for bias tracking
            'miss_pct': round(miss_pct, 6),
            'miss_usd': round(miss_usd, 2),
            # Phase 3: Current bias values
            'bias_pct_long': round(self.bias_pct_long, 6),
            'bias_pct_short': round(self.bias_pct_short, 6),
            'bias_pct_applied': round(bias_pct, 6),
            # Phase 4: Full ladder (to confirm new levels in use)
            'ladder': snapshot.ladder,
            'implied_levels_raw': {str(k): round(v, 2) for k, v in implied_levels_raw.items()},
            'implied_levels_corrected': {str(k): round(v, 2) for k, v in implied_levels_corrected.items()},
            'top_predicted_zones': [[round(p, 2), round(s, 3)] for p, s in top_zones],
            'current_weights': {
                str(l): round(w, 4)
                for l, w in zip(snapshot.ladder, snapshot.weights)
            },
            'current_buffer': round(snapshot.buffer, 5),
            'hit_bucket_tolerance': self.hit_bucket_tolerance,
            # Percent-space soft attribution params
            'tau_pct': round(tau_pct, 6),
            'delta_pct': round(delta_pct, 6),
            'vol_scale': round(vol_scale, 4),
            'vol_ewma': round(self.vol_ewma, 6),
            # Guardrail fields
            'price_insane': price_insane,
            'price_insane_reason': price_insane_reason,
            'offset_used_usd': round(offset_used_usd, 2),
            'offset_used_pct': round(offset_used_pct, 6),
            # V3: Tier disable flag
            'tier_disabled': tier_disabled
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
