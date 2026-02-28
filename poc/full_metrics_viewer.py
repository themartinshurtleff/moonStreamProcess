#!/usr/bin/env python3
"""
Full BTC Perpetual Metrics Viewer
Displays ALL metrics that the moonStreamProcess library computes.
"""

import argparse
import asyncio
import json
import math
import time
import sys
import os
from datetime import datetime
from decimal import Decimal
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
import threading
import signal
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'StreamEngineBase'))

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'rich', '--quiet'])
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box

from ws_connectors import MultiExchangeConnector
from liq_calibrator import LiquidationCalibrator
from rest_pollers import BinanceRESTPollerThread, PollerState
from liq_heatmap import LiquidationHeatmap, HeatmapConfig
from ob_heatmap import OrderbookAccumulator, OrderbookHeatmapBuffer, OrderbookReconstructor
from engine_manager import EngineManager, EngineInstance
from liq_normalizer import normalize_liquidation
from oi_poller import OIPollerThread

# Directory where this script lives - use for log file paths
POC_DIR = os.path.dirname(os.path.abspath(__file__))

# Debug log for liquidation event flow diagnostics
LIQ_DEBUG_LOG = os.path.join(POC_DIR, "liq_debug.jsonl")

# Plot feed for real-time chart (OHLC + zones per minute)
PLOT_FEED_FILE = os.path.join(POC_DIR, "plot_feed.jsonl")

# Orderbook heatmap persistence file (30s frames)
OB_HEATMAP_FILE = os.path.join(POC_DIR, "ob_heatmap_30s.bin")

# Orderbook reconstructor stats file (for API diagnostics)
OB_RECON_STATS_FILE = os.path.join(POC_DIR, "ob_recon_stats.json")

# Legacy snapshot constants removed (Task 19) — per-symbol files built at runtime.
# Legacy binary history constants removed — per-symbol buffers managed by embedded_api.py.

# Log rotation thresholds
LOG_ROTATION_MAX_MB = 200
LOG_ROTATION_MAX_AGE_HOURS = 24


def _step_ndigits(step: float) -> int:
    """Compute decimal precision digits from a step size.

    Uses Decimal to derive the number of fractional digits needed for
    exact rounding.  Required for sub-integer steps (e.g. 0.1 for SOL)
    to avoid floating-point drift in price grids and bucket lookups.
    """
    return max(0, -Decimal(str(step)).as_tuple().exponent)


def _build_price_grid(price_min: float, price_max: float, step: float) -> List[float]:
    """Build an index-based price grid with exact rounding.

    Avoids incremental ``p += step`` which causes floating-point drift
    for sub-integer steps (e.g. step=0.1).
    """
    ndigits = _step_ndigits(step)
    price_min = round(price_min, ndigits)
    price_max = round(price_max, ndigits)
    n_buckets = int(round((price_max - price_min) / step)) + 1
    return [round(price_min + i * step, ndigits) for i in range(n_buckets)]

# Per-symbol engine configuration.
# BTC gets full engines (calibrator + heatmap + orderbook).
# ETH and SOL get liquidation engines only (calibrator + heatmap) for MVP.
SYMBOL_CONFIGS = {
    "BTC": {
        "symbol_short": "BTC",
        "symbol_full": "BTCUSDT",
        "steps": 20.0,
        "ob_step": 20.0,
        "ob_range_pct": 0.10,
        "ob_gap_tolerance": 1000,
        "has_orderbook": True,
    },
    "ETH": {
        "symbol_short": "ETH",
        "symbol_full": "ETHUSDT",
        "steps": 1.0,
        "ob_step": 1.0,
        "ob_range_pct": 0.10,
        "ob_gap_tolerance": 50,
        "has_orderbook": False,
    },
    "SOL": {
        "symbol_short": "SOL",
        "symbol_full": "SOLUSDT",
        "steps": 0.10,
        "ob_step": 0.10,
        "ob_range_pct": 0.10,
        "ob_gap_tolerance": 5,
        "has_orderbook": False,
    },
}

# Map lowercase instrument names (from ws_connectors wrapper envelope) to short symbols.
# Used by _process_markprice to route mark price updates to the correct symbol_prices entry.
INSTRUMENT_TO_SHORT: Dict[str, str] = {
    cfg["symbol_full"].lower(): sym for sym, cfg in SYMBOL_CONFIGS.items()
}

ZONE_PERSIST_FILES = {
    "BTC": os.path.join(POC_DIR, "liq_active_zones_BTC.json"),
    "ETH": os.path.join(POC_DIR, "liq_active_zones_ETH.json"),
    "SOL": os.path.join(POC_DIR, "liq_active_zones_SOL.json"),
}

logger = logging.getLogger(__name__)


def _rotate_log_if_needed(log_path: str, max_mb: float = 200, max_age_hours: float = 24) -> bool:
    """Rotate log file if it exceeds size or age limits."""
    if not log_path or not os.path.exists(log_path):
        return False

    try:
        stat = os.stat(log_path)
        size_mb = stat.st_size / (1024 * 1024)
        age_hours = (time.time() - stat.st_mtime) / 3600

        if size_mb > max_mb or age_hours > max_age_hours:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            base, ext = os.path.splitext(log_path)
            backup_path = f"{base}.{timestamp}{ext}"
            os.rename(log_path, backup_path)
            print(f"Rotated log: {log_path} -> {backup_path}")
            return True
    except Exception as e:
        print(f"Log rotation failed: {e}")
    return False


def _write_debug_log(entry: dict):
    """Append a debug entry to liq_debug.jsonl."""
    entry["ts"] = datetime.now().isoformat()
    try:
        with open(LIQ_DEBUG_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Don't crash on log failures


def _write_plot_feed(entry: dict):
    """Append a plot feed entry for the live chart."""
    try:
        with open(PLOT_FEED_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Don't crash on write failures


def write_json_atomic(filepath: str, data: dict):
    """Atomically write JSON data to filepath using tmp+flush+fsync+rename.

    Temp file is placed in the same directory as target to avoid
    cross-filesystem rename failures.
    """
    tmp_path = f"{filepath}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception as e:
        logger.error("write_json_atomic failed for %s: %s", filepath, e, exc_info=True)
        # Clean up temp file on failure, don't crash
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# =============================================================================
# ZoneTracker: UI Stability for Liquidation Zones
# =============================================================================
# Provides hysteresis and TTL-based stability to prevent zone flicker.
# - Identity matching: zones within ±1 bucket are considered the same
# - Hysteresis: enter_margin=0.07, exit_margin=0.05
# - TTL: 10 minutes unless strength drops >70% from trailing peak

@dataclass
class DisplayedZone:
    """A zone currently being displayed with stability tracking."""
    price: float           # Bucket price
    strength: float        # Current strength
    entry_minute: int      # Minute when zone entered display
    peak_strength: float   # Trailing peak strength since entry
    side: str              # "long" or "short"

    def age_minutes(self, current_minute: int) -> int:
        """How many minutes this zone has been displayed."""
        return current_minute - self.entry_minute


class ZoneTracker:
    """
    Maintains stable displayed zones using hysteresis and TTL.

    Rules:
    - Identity: zone is same if side matches and price within ±1 bucket (steps)
    - Enter: new zone must beat weakest displayed by enter_margin (0.07)
    - Exit: zone exits if strength < (best non-displayed - exit_margin) OR < min_threshold
    - TTL: keep zone for 10 minutes unless strength drops >70% from peak
    """

    def __init__(
        self,
        steps: float = 20.0,
        max_zones: int = 5,
        enter_margin: float = 0.07,
        exit_margin: float = 0.05,
        ttl_minutes: int = 10,
        peak_drop_threshold: float = 0.70,
        min_strength_threshold: float = 0.01,
        debug_log: bool = True
    ):
        self.steps = steps
        self.max_zones = max_zones
        self.enter_margin = enter_margin
        self.exit_margin = exit_margin
        self.ttl_minutes = ttl_minutes
        self.peak_drop_threshold = peak_drop_threshold
        self.min_strength_threshold = min_strength_threshold
        self.debug_log = debug_log

        # Separate tracking for long and short zones
        self.displayed_long: List[DisplayedZone] = []
        self.displayed_short: List[DisplayedZone] = []

        # Track current minute for TTL
        self._current_minute: int = 0
        self._last_log_minute: int = -1

    def _find_matching_zone(
        self,
        price: float,
        displayed: List[DisplayedZone]
    ) -> Optional[int]:
        """
        Find index of displayed zone matching price within ±1 bucket.
        Returns nearest match if multiple, else None.
        """
        best_idx = None
        best_dist = float('inf')

        for i, zone in enumerate(displayed):
            dist = abs(zone.price - price)
            if dist <= self.steps and dist < best_dist:
                best_idx = i
                best_dist = dist

        return best_idx

    def _get_weakest_strength(self, displayed: List[DisplayedZone]) -> float:
        """Get strength of weakest displayed zone."""
        if not displayed:
            return 0.0
        return min(z.strength for z in displayed)

    def _get_best_non_displayed_strength(
        self,
        candidates: List[Tuple[float, float]],
        displayed: List[DisplayedZone]
    ) -> float:
        """Get highest strength among candidates not currently displayed."""
        best = 0.0
        for price, strength in candidates:
            # Check if this candidate is displayed
            match_idx = self._find_matching_zone(price, displayed)
            if match_idx is None:
                best = max(best, strength)
        return best

    def update(
        self,
        current_minute: int,
        long_zones: List[Tuple[float, float]],
        short_zones: List[Tuple[float, float]]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """
        Update displayed zones with new candidates.

        Args:
            current_minute: Current minute for TTL tracking
            long_zones: [(price, strength), ...] from engine
            short_zones: [(price, strength), ...] from engine

        Returns:
            (stable_long_zones, stable_short_zones) for display
        """
        self._current_minute = current_minute

        # Update each side
        self.displayed_long = self._update_side(
            "long", self.displayed_long, long_zones, current_minute
        )
        self.displayed_short = self._update_side(
            "short", self.displayed_short, short_zones, current_minute
        )

        # Log debug info once per minute
        if self.debug_log and current_minute != self._last_log_minute:
            self._log_debug(current_minute)
            self._last_log_minute = current_minute

        # Return stable zones for display
        stable_long = [(z.price, z.strength) for z in sorted(
            self.displayed_long, key=lambda x: x.strength, reverse=True
        )]
        stable_short = [(z.price, z.strength) for z in sorted(
            self.displayed_short, key=lambda x: x.strength, reverse=True
        )]

        return stable_long, stable_short

    def _update_side(
        self,
        side: str,
        displayed: List[DisplayedZone],
        candidates: List[Tuple[float, float]],
        current_minute: int
    ) -> List[DisplayedZone]:
        """Update displayed zones for one side (long or short)."""

        # Step 1: Update existing zones with new strengths
        updated_zones = []
        for zone in displayed:
            # Find matching candidate
            match_strength = None
            for price, strength in candidates:
                if abs(price - zone.price) <= self.steps:
                    match_strength = strength
                    break

            if match_strength is not None:
                # Update strength and peak
                zone.strength = match_strength
                zone.peak_strength = max(zone.peak_strength, match_strength)
                updated_zones.append(zone)
            else:
                # Candidate disappeared - keep with decayed strength if TTL allows
                zone.strength = zone.strength * 0.95  # Gentle decay
                updated_zones.append(zone)

        # Step 2: Apply exit rules
        best_non_displayed = self._get_best_non_displayed_strength(candidates, updated_zones)
        surviving_zones = []

        for zone in updated_zones:
            age = zone.age_minutes(current_minute)
            peak_drop = (zone.peak_strength - zone.strength) / zone.peak_strength if zone.peak_strength > 0 else 0

            # Exit conditions:
            # 1. Below minimum threshold
            if zone.strength < self.min_strength_threshold:
                continue

            # 2. TTL expired AND significant peak drop (>70%)
            if age >= self.ttl_minutes and peak_drop > self.peak_drop_threshold:
                continue

            # 3. Falls below best non-displayed by exit_margin (only if TTL expired)
            if age >= self.ttl_minutes:
                if zone.strength < best_non_displayed - self.exit_margin:
                    continue

            surviving_zones.append(zone)

        # Step 3: Apply entry rules for new zones
        for price, strength in candidates:
            # Skip if already displayed
            if self._find_matching_zone(price, surviving_zones) is not None:
                continue

            # Check entry condition
            weakest = self._get_weakest_strength(surviving_zones)

            # Can enter if:
            # - We have room (< max_zones)
            # - OR we beat weakest by enter_margin
            can_enter = False

            if len(surviving_zones) < self.max_zones:
                can_enter = True
            elif strength > weakest + self.enter_margin:
                can_enter = True

            if can_enter:
                # Add new zone
                new_zone = DisplayedZone(
                    price=price,
                    strength=strength,
                    entry_minute=current_minute,
                    peak_strength=strength,
                    side=side
                )
                surviving_zones.append(new_zone)

                # If over max, remove weakest
                if len(surviving_zones) > self.max_zones:
                    surviving_zones.sort(key=lambda z: z.strength)
                    surviving_zones = surviving_zones[1:]  # Remove weakest

        return surviving_zones

    def _log_debug(self, current_minute: int):
        """Log debug info about displayed zones."""
        long_info = [
            (z.price, round(z.strength, 3), z.age_minutes(current_minute))
            for z in sorted(self.displayed_long, key=lambda x: x.strength, reverse=True)
        ]
        short_info = [
            (z.price, round(z.strength, 3), z.age_minutes(current_minute))
            for z in sorted(self.displayed_short, key=lambda x: x.strength, reverse=True)
        ]

        _write_debug_log({
            "type": "zone_tracker_debug",
            "minute": current_minute,
            "displayed_long": long_info,
            "displayed_short": short_info,
            "config": {
                "steps": self.steps,
                "max_zones": self.max_zones,
                "enter_margin": self.enter_margin,
                "exit_margin": self.exit_margin,
                "ttl_minutes": self.ttl_minutes
            }
        })


# =============================================================================
# TakerAggressionAccumulator: Real-time aggTrade taker direction tracking
# =============================================================================
# Tracks per-minute taker buy/sell notional from aggTrade stream.
# Uses buyer_is_maker to determine aggressor (taker) direction.

@dataclass
class MinuteAggression:
    """Per-minute taker aggression stats."""
    minute_key: int
    taker_buy_notional: float = 0.0
    taker_sell_notional: float = 0.0
    trade_count: int = 0


class TakerAggressionAccumulator:
    """
    Accumulates taker aggression from aggTrade stream.

    Mapping:
    - buyerIsMaker == false → taker buy (buyer crossed the spread)
    - buyerIsMaker == true → taker sell (seller crossed the spread)

    Notional = price * qty (USD value)
    """

    def __init__(self, history_minutes: int = 10):
        self.history_minutes = history_minutes
        self._current: Optional[MinuteAggression] = None
        self._history: deque = deque(maxlen=history_minutes)
        self._lock = threading.Lock()

    def on_trade(self, price: float, qty: float, is_buyer_maker: bool) -> None:
        """
        Process a single aggTrade.

        Args:
            price: Trade price
            qty: Trade quantity (base asset)
            is_buyer_maker: True if buyer was maker (seller is taker)
        """
        notional = price * qty
        minute_key = int(time.time() // 60)

        with self._lock:
            # Check if we need to roll to new minute
            if self._current is None or self._current.minute_key != minute_key:
                # Archive current minute if exists
                if self._current is not None:
                    self._history.append(self._current)
                # Start new minute
                self._current = MinuteAggression(minute_key=minute_key)

            # Accumulate based on taker direction
            if is_buyer_maker:
                # Seller is taker (sell aggression)
                self._current.taker_sell_notional += notional
            else:
                # Buyer is taker (buy aggression)
                self._current.taker_buy_notional += notional

            self._current.trade_count += 1

    def get_minute_data(self, minute_key: int) -> Optional[MinuteAggression]:
        """
        Get aggression data for a specific minute.

        Returns None if no data for that minute.
        """
        with self._lock:
            # Check current minute
            if self._current is not None and self._current.minute_key == minute_key:
                return MinuteAggression(
                    minute_key=self._current.minute_key,
                    taker_buy_notional=self._current.taker_buy_notional,
                    taker_sell_notional=self._current.taker_sell_notional,
                    trade_count=self._current.trade_count
                )

            # Check history
            for entry in reversed(self._history):
                if entry.minute_key == minute_key:
                    return entry

            return None

    def get_previous_minute_data(self) -> Optional[MinuteAggression]:
        """
        Get data for the most recently completed minute (not current).

        This is what we want for minute rollover processing.
        """
        with self._lock:
            if self._history:
                return self._history[-1]
            return None

    def get_last_n_minutes(self, n: int = 5) -> List[dict]:
        """
        Get diagnostics for last N minutes.

        Returns list of dicts with minute_key, trade_count, taker_buy_notional, taker_sell_notional.
        """
        with self._lock:
            result = []
            # Add history (oldest to newest)
            for entry in list(self._history)[-n:]:
                result.append({
                    "minute_key": entry.minute_key,
                    "trade_count": entry.trade_count,
                    "taker_buy_notional": round(entry.taker_buy_notional, 2),
                    "taker_sell_notional": round(entry.taker_sell_notional, 2)
                })
            return result


@dataclass
class FullMetricsState:
    """Complete metrics state matching btcSynth.data output."""
    timestamp: str = ""

    # Price metrics
    btc_price: float = 0.0
    perp_open: float = 0.0
    perp_close: float = 0.0
    perp_high: float = 0.0
    perp_low: float = 0.0
    perp_Vola: float = 0.0

    # Orderbook
    perp_books: Dict[str, float] = field(default_factory=dict)
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    imbalance: float = 0.0

    # Trades
    perp_buyVol: float = 0.0
    perp_sellVol: float = 0.0
    perp_VolProfile: Dict[str, float] = field(default_factory=dict)
    perp_buyVolProfile: Dict[str, float] = field(default_factory=dict)
    perp_sellVolProfile: Dict[str, float] = field(default_factory=dict)
    perp_numberBuyTrades: int = 0
    perp_numberSellTrades: int = 0
    perp_orderedBuyTrades: List = field(default_factory=list)
    perp_orderedSellTrades: List = field(default_factory=list)

    # Adjustments
    perp_voids: Dict[str, float] = field(default_factory=dict)
    perp_reinforces: Dict[str, float] = field(default_factory=dict)
    perp_totalVoids: float = 0.0
    perp_totalReinforces: float = 0.0
    perp_totalVoidsVola: float = 0.0

    # Funding & OI
    perp_weighted_funding: float = 0.0
    perp_total_oi: float = 0.0
    perp_oi_change: float = 0.0
    perp_oi_Vola: float = 0.0
    perp_OIs_per_instrument: Dict[str, float] = field(default_factory=dict)
    perp_fundings_per_instrument: Dict[str, float] = field(default_factory=dict)

    # Liquidations
    perp_liquidations_longsTotal: float = 0.0
    perp_liquidations_longs: Dict[str, float] = field(default_factory=dict)
    perp_liquidations_shortsTotal: float = 0.0
    perp_liquidations_shorts: Dict[str, float] = field(default_factory=dict)

    # Position ratios
    perp_TTA_ratio: float = 0.0
    perp_TTP_ratio: float = 0.0
    perp_GTA_ratio: float = 0.0

    # Predicted liquidation stress zones (from V2 LiquidationHeatmap)
    pred_liq_longs_top: List[tuple] = field(default_factory=list)   # Top 5 long liq zones (support)
    pred_liq_shorts_top: List[tuple] = field(default_factory=list)  # Top 5 short liq zones (resistance)
    liq_engine_stats: Dict[str, Any] = field(default_factory=dict)  # Debug stats

    # Connection stats
    msg_counts: Dict[str, int] = field(default_factory=dict)
    exchanges: List[str] = field(default_factory=list)


class FullMetricsProcessor:
    """Process all metrics."""

    def __init__(self, level_size: float = 50.0):
        self.state = FullMetricsState()
        self.lock = threading.Lock()
        self.level_size = level_size
        self.orderbooks: Dict[str, Dict] = defaultdict(lambda: {"bids": {}, "asks": {}})
        self.prev_books: Dict[str, float] = {}
        self.current_minute = datetime.now().minute
        self.prices_this_minute: List[float] = []

        # Per-symbol price tracking for calibrator attribution.
        # BTC prices also update self.state.perp_* for backwards compat.
        # Updated from Binance markPrice@1s streams (all 3 symbols).
        self.symbol_prices: Dict[str, Dict[str, float]] = {
            sym: {"mark": 0.0, "last": 0.0, "mid": 0.0,
                  "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
            for sym in SYMBOL_CONFIGS
        }

        # Track if we've applied persisted weights (per-symbol)
        self._applied_persisted_weights: Dict[str, bool] = {}

        # REST poller for trader ratios only (OI moved to MultiExchangeOIPoller).
        # oi_interval set very high to effectively disable the legacy OI loop.
        self.rest_poller = BinanceRESTPollerThread(
            symbols=["BTCUSDT"],
            oi_interval=999999,    # OI handled by oi_poller now
            ratio_interval=60.0,   # Poll ratios every 60s
            on_update=self._on_rest_poller_update,
            price_getter=self._get_price_for_symbol,
            debug=True
        )

        # Multi-exchange OI poller (Binance + Bybit + OKX, per symbol)
        self.oi_poller = OIPollerThread(
            symbols=list(SYMBOL_CONFIGS.keys()),  # ["BTC", "ETH", "SOL"]
            on_oi_update=self._on_oi_update,
            poll_interval=15.0,
        )
        # Per-symbol OI state for API exposure
        self.oi_by_symbol: Dict[str, Dict] = {}

        # Previous OI per symbol for delta computation in minute rollover.
        # Initialized to None (NOT 0.0) to avoid a fake spike on first value.
        self.prev_oi_by_symbol: Dict[str, Optional[float]] = {
            sym: None for sym in SYMBOL_CONFIGS
        }

        # Per-symbol, per-exchange liquidation event counters
        self.liq_counts = defaultdict(lambda: defaultdict(int))

        # Last-seen timestamps per exchange for dashboard connection health
        self.exchange_last_seen = defaultdict(float)

        # Event sanity validator - log first 50 liquidation events for verification
        self._sanity_check_count = 0
        self._sanity_check_limit = 50

        # Timestamp when perp_close was last > 0 (for markPrice fallback in C1)
        self._last_depth_price_ts: float = 0.0
        # Rate-limit fallback warning to once per minute
        self._last_fallback_warning_ts: float = 0.0

        # ZoneTracker for UI stability (hysteresis + TTL)
        self.zone_tracker = ZoneTracker(
            steps=20.0,              # $20 price buckets for BTC
            max_zones=5,             # Display top 5 zones per side
            enter_margin=0.07,       # New zone must beat weakest by 7%
            exit_margin=0.05,        # Exit if below best non-displayed by 5%
            ttl_minutes=10,          # Keep zones for 10 minutes minimum
            peak_drop_threshold=0.70,  # Exit early if drops >70% from peak
            min_strength_threshold=0.01,  # Minimum strength to display
            debug_log=True           # Log displayed zones per minute
        )

        # Per-symbol taker aggression accumulators (real-time aggTrade tracking).
        # Each symbol gets its own accumulator — NEVER share state between
        # per-symbol engines (CLAUDE.md Rule #8).
        self.taker_aggression: Dict[str, TakerAggressionAccumulator] = {
            sym: TakerAggressionAccumulator(history_minutes=10)
            for sym in SYMBOL_CONFIGS
        }

        # Per-symbol volume tracking for minute snapshots.
        # Accumulated from aggTrade stream, reset every minute.
        self.symbol_volumes: Dict[str, Dict[str, float]] = {
            sym: {"buy_vol": 0.0, "sell_vol": 0.0, "buy_count": 0, "sell_count": 0}
            for sym in SYMBOL_CONFIGS
        }

        # Orderbook heatmap (30s DoM screenshots) — BTC only for now
        self.ob_heatmap_buffer = OrderbookHeatmapBuffer(OB_HEATMAP_FILE)
        ob_stats = self.ob_heatmap_buffer.get_stats()
        print(f"[OB_HEATMAP] Loaded {ob_stats['frames_in_memory']} frames from {OB_HEATMAP_FILE}")

        # Engine Manager — bundles all per-symbol engines for multi-symbol routing.
        # Each symbol MUST have independent engines (CLAUDE.md rule).
        self.engine_manager = EngineManager()

        # Create engines for all configured symbols
        for sym_short, cfg in SYMBOL_CONFIGS.items():
            engine = self._create_engine_for_symbol(cfg)
            self.engine_manager.register_engine(
                sym_short, cfg["symbol_full"], engine
            )
            self._applied_persisted_weights[sym_short] = False

        print(f"[ENGINE_MGR] Registered engines: {self.engine_manager.get_all_symbols()}")

    def _create_engine_for_symbol(self, cfg: dict) -> EngineInstance:
        """
        Factory: create a fully independent EngineInstance for one symbol.

        BTC gets full engines (calibrator + heatmap + orderbook).
        Other symbols get liquidation engines only (calibrator + heatmap).
        """
        sym_short = cfg["symbol_short"]
        sym_full = cfg["symbol_full"]
        steps = cfg["steps"]
        has_ob = cfg.get("has_orderbook", False)

        # Per-symbol log and weight files
        log_file = os.path.join(POC_DIR, f"liq_calibrator_{sym_short}.jsonl")
        weights_file = os.path.join(
            POC_DIR, f"liq_calibrator_weights_{sym_short}.json"
        )

        # Calibrator — per-symbol weight learning
        calibrator = LiquidationCalibrator(
            symbol=sym_short,
            steps=steps,
            window_minutes=15,
            hit_bucket_tolerance=5,
            learning_rate=0.10,
            closer_level_gamma=0.35,
            enable_buffer_tuning=True,
            enable_tolerance_tuning=True,
            log_file=log_file,
            weights_file=weights_file,
            log_events=True,
            on_weights_updated=lambda s, w, b, _sym=sym_short: (
                self._on_calibrator_weights_updated(_sym, w, b)
            ),
        )

        # V2 Heatmap — per-symbol tape + OI inference
        zone_persist_file = ZONE_PERSIST_FILES.get(sym_short, os.path.join(POC_DIR, f"liq_active_zones_{sym_short}.json"))
        heatmap_v2 = LiquidationHeatmap(
            config=HeatmapConfig(
                symbol=sym_short,
                steps=steps,
                decay=0.995,
                buffer=0.002,
                tape_weight=0.35,
                projection_weight=0.65,
            ),
            log_dir=POC_DIR,
            zone_persist_file=zone_persist_file,
        )

        # Orderbook engines — BTC only for MVP
        ob_reconstructor = None
        ob_accumulator = None
        if has_ob:
            ob_accumulator = OrderbookAccumulator(
                step=cfg["ob_step"],
                range_pct=cfg["ob_range_pct"],
                on_frame_callback=self._on_ob_frame_emitted,
            )
            ob_reconstructor = OrderbookReconstructor(
                symbol=sym_full,
                on_book_update=self._on_reconstructor_update,
                gap_tolerance=cfg["ob_gap_tolerance"],
            )
            print(
                f"[OB_RECON] {sym_short}: initialized with "
                f"gap_tolerance={cfg['ob_gap_tolerance']}"
            )

        engine = EngineInstance(
            symbol_short=sym_short,
            symbol_full=sym_full,
            calibrator=calibrator,
            heatmap_v2=heatmap_v2,
            zone_manager=getattr(heatmap_v2, "zone_manager", None),
            ob_reconstructor=ob_reconstructor,
            ob_accumulator=ob_accumulator,
        )
        print(
            f"[ENGINE_MGR] Created {sym_short} engine: "
            f"steps={steps}, ob={'yes' if has_ob else 'no'}"
        )
        return engine

    def _btc(self) -> EngineInstance:
        """Shortcut to BTC engine instance (kept for backward-compat in BTC-specific UI code)."""
        return self.engine_manager.get_engine("BTC")

    def _on_ob_frame_emitted(self, frame):
        """Callback when orderbook accumulator emits a 30s frame."""
        print(f"[OB_DEBUG] Frame emitted! ts={frame.ts} src={frame.src:.2f} n_prices={frame.n_prices}")
        self.ob_heatmap_buffer.add_frame(frame)
        print(f"[OB_DEBUG] Frame added to buffer. Buffer stats: {self.ob_heatmap_buffer.get_stats()}")

    def _on_reconstructor_update(self, reconstructor: OrderbookReconstructor):
        """
        Callback when OrderbookReconstructor has applied a diff successfully.

        Feed the full reconstructed orderbook to the 30s heatmap accumulator.
        This ensures we have complete depth data (1000 levels) for wide-range heatmaps.
        """
        if not reconstructor.is_synced:
            return

        # Get full reconstructed orderbook
        bids, asks = reconstructor.get_full_book()

        # Use mark price as reference (or mid if not available)
        mid = self.state.btc_price
        if mid <= 0:
            # Fallback to mid from reconstructed book
            if bids and asks:
                best_bid = max(b[0] for b in bids)
                best_ask = min(a[0] for a in asks)
                mid = (best_bid + best_ask) / 2
            else:
                return  # No price reference available

        # Feed to accumulator (will emit frame on 30s boundary)
        if mid > 0 and bids and asks:
            # Debug: track accumulator state (throttled to every 10s)
            now = time.time()
            if not hasattr(self, '_last_accum_debug'):
                self._last_accum_debug = 0.0
            if now - self._last_accum_debug > 10.0:
                self._last_accum_debug = now
                slot = int(now // 30)
                print(f"[OB_DEBUG] Feeding accumulator: mid={mid:.2f} bids={len(bids)} asks={len(asks)} "
                      f"current_slot={self._btc().ob_accumulator._current_slot} new_slot={slot}")

            self._btc().ob_accumulator.on_depth_update(
                bids=bids,
                asks=asks,
                src_price=mid,
                timestamp=time.time()
            )

        # Write reconstructor stats periodically (throttled to every 5s)
        self._maybe_write_recon_stats(reconstructor)

    def _maybe_write_recon_stats(self, reconstructor: OrderbookReconstructor):
        """Write reconstructor stats to file (throttled to every 5 seconds)."""
        now = time.time()
        if not hasattr(self, '_last_recon_stats_write'):
            self._last_recon_stats_write = 0.0

        if now - self._last_recon_stats_write < 5.0:
            return

        self._last_recon_stats_write = now
        stats = reconstructor.get_stats()
        stats['written_at'] = now

        try:
            with open(OB_RECON_STATS_FILE, 'w') as f:
                json.dump(stats, f)
        except Exception:
            pass  # Don't crash on stats write failure

    def _get_price_for_symbol(self, symbol: str) -> float:
        """Get current price for a symbol (used by REST poller for OI conversion)."""
        # For BTCUSDT, return current BTC price
        if "BTC" in symbol.upper():
            return self.state.btc_price
        # For other symbols, could add price tracking later
        return 0.0

    def _on_calibrator_weights_updated(self, symbol: str, new_weights: list, new_buffer: float):
        """Callback when calibrator updates weights — routes to correct symbol's heatmap."""
        try:
            engine = self.engine_manager.get_engine(symbol)
            if engine is None:
                return
            v2_ladder = sorted(engine.heatmap_v2.inference.leverage_weights.keys())
            if len(new_weights) == len(v2_ladder):
                new_weights_dict = dict(zip(v2_ladder, new_weights))
                engine.heatmap_v2.inference.update_leverage_weights(new_weights_dict)
                engine.heatmap_v2.config.buffer = new_buffer
        except Exception as e:
            logger.error(f"Weight update callback failed for {symbol}: {e}", exc_info=True)

    def _on_oi_update(self, symbol_short: str, aggregated_oi: float,
                      per_exchange: Dict[str, float]):
        """Callback from MultiExchangeOIPoller with per-symbol aggregated OI."""
        with self.lock:
            prev = self.oi_by_symbol.get(symbol_short, {}).get("aggregated_oi", 0.0)
            self.oi_by_symbol[symbol_short] = {
                "aggregated_oi": aggregated_oi,
                "per_exchange": dict(per_exchange),
                "ts": time.time(),
            }

            # BTC backwards compat — update legacy state fields
            if symbol_short == "BTC":
                if prev > 0:
                    self.state.perp_oi_change = aggregated_oi - prev
                self.state.perp_total_oi = aggregated_oi
                # Update per-instrument breakdown
                for ex_name, oi_val in per_exchange.items():
                    self.state.perp_OIs_per_instrument[f"{ex_name}_BTCUSDT"] = oi_val

    def _on_rest_poller_update(self, poller_state: PollerState):
        """Callback when REST poller has new ratio data."""
        with self.lock:
            # Update trader ratios (TTA, TTP, GTA)
            if poller_state.weighted_tta > 0:
                self.state.perp_TTA_ratio = poller_state.weighted_tta
            if poller_state.weighted_ttp > 0:
                self.state.perp_TTP_ratio = poller_state.weighted_ttp
            if poller_state.weighted_gta > 0:
                self.state.perp_GTA_ratio = poller_state.weighted_gta

    def process_message(self, msg_str: str):
        """Process incoming message."""
        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            return

        exchange = msg.get("exchange", "unknown")
        obj_type = msg.get("obj", "")
        instrument = msg.get("instrument", "")
        data = msg.get("data", {})

        self.exchange_last_seen[exchange] = time.time()

        with self.lock:
            key = f"{exchange}_{obj_type}"
            self.state.msg_counts[key] = self.state.msg_counts.get(key, 0) + 1

            if exchange not in self.state.exchanges:
                self.state.exchanges.append(exchange)

            if obj_type == "depth":
                self._process_depth(exchange, data)
            elif obj_type == "trades":
                self._process_trades(exchange, instrument, data)
            elif obj_type == "liquidations":
                self._process_liquidations(exchange, data)
            elif obj_type == "markprice":
                self._process_markprice(exchange, instrument, data)
            elif obj_type == "funding":
                self._process_funding(exchange, data)
            elif obj_type in ["oi", "oifunding"]:
                self._process_oi(exchange, data)

            self.state.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._check_minute_rollover()

    def _process_depth(self, exchange: str, data: dict):
        # For Binance, route through the OrderbookReconstructor for proper
        # snapshot+diff reconciliation (ensures full 1000-level orderbook)
        if exchange == "binance":
            # Feed raw diff to reconstructor
            # The reconstructor callback will feed the accumulator with full book
            self._btc().ob_reconstructor.on_depth_diff(data)

            # Update local orderbook from reconstructor for UI display
            if self._btc().ob_reconstructor.is_synced:
                bids, asks = self._btc().ob_reconstructor.get_full_book()
                book = self.orderbooks[exchange]
                book["bids"].clear()
                book["asks"].clear()
                for price, qty in bids:
                    book["bids"][price] = qty
                for price, qty in asks:
                    book["asks"][price] = qty
        else:
            # For other exchanges, use the old diff-only path
            book = self.orderbooks[exchange]
            bids = data.get("b") or data.get("bids") or []
            asks = data.get("a") or data.get("asks") or []

            for bid in bids:
                price, qty = float(bid[0]), float(bid[1])
                if qty == 0:
                    book["bids"].pop(price, None)
                else:
                    book["bids"][price] = qty

            for ask in asks:
                price, qty = float(ask[0]), float(ask[1])
                if qty == 0:
                    book["asks"].pop(price, None)
                else:
                    book["asks"][price] = qty

        self._compute_book_metrics()

    def _compute_book_metrics(self):
        all_bids = []
        all_asks = []

        for book in self.orderbooks.values():
            for price, qty in book["bids"].items():
                all_bids.append((price, qty))
            for price, qty in book["asks"].items():
                all_asks.append((price, qty))

        if not all_bids or not all_asks:
            return

        best_bid = max(b[0] for b in all_bids)
        best_ask = min(a[0] for a in all_asks)
        mid = (best_bid + best_ask) / 2

        self.state.best_bid = best_bid
        self.state.best_ask = best_ask
        self.state.spread = best_ask - best_bid
        self.state.btc_price = mid
        self.state.perp_close = mid
        self.prices_this_minute.append(mid)

        if len(self.prices_this_minute) == 1:
            self.state.perp_open = mid
        self.state.perp_high = max(self.prices_this_minute)
        self.state.perp_low = min(self.prices_this_minute)

        if len(self.prices_this_minute) > 1:
            import numpy as np
            self.state.perp_Vola = float(np.std(self.prices_this_minute))

        threshold = mid * 0.01
        self.state.bid_depth = sum(qty for p, qty in all_bids if p >= mid - threshold)
        self.state.ask_depth = sum(qty for p, qty in all_asks if p <= mid + threshold)

        if self.state.bid_depth + self.state.ask_depth > 0:
            self.state.imbalance = (self.state.bid_depth - self.state.ask_depth) / (self.state.bid_depth + self.state.ask_depth)

        # Heatmap
        books_heatmap = {}
        for price, qty in all_bids + all_asks:
            level = int(price / self.level_size) * self.level_size
            key = str(level)
            books_heatmap[key] = books_heatmap.get(key, 0) + qty
        self.state.perp_books = books_heatmap

        # Voids/reinforces
        if self.prev_books:
            voids = {}
            reinforces = {}
            for level, qty in books_heatmap.items():
                prev_qty = self.prev_books.get(level, 0)
                diff = qty - prev_qty
                if diff < 0:
                    voids[level] = abs(diff)
                elif diff > 0:
                    reinforces[level] = diff
            self.state.perp_voids = voids
            self.state.perp_reinforces = reinforces
            self.state.perp_totalVoids = sum(voids.values())
            self.state.perp_totalReinforces = sum(reinforces.values())

        self.prev_books = books_heatmap.copy()

        # NOTE: 30s heatmap accumulator is now fed by OrderbookReconstructor callback
        # (_on_reconstructor_update) which provides the full 1000-level reconstructed
        # orderbook for Binance. This ensures wide-range heatmaps have complete data.
        # The old direct feed here was using incomplete diff-accumulated data.

    def _build_depth_band(self, src: float, steps: float) -> Dict[float, float]:
        """
        Build depth_band dict: price_bucket -> notional USD within ±2% of src.

        Returns empty dict if no orderbook data available.
        """
        if src <= 0 or steps <= 0:
            return {}

        # Collect all bids and asks from all orderbooks
        all_bids = []
        all_asks = []
        for book in self.orderbooks.values():
            for price, qty in book.get("bids", {}).items():
                all_bids.append((price, qty))
            for price, qty in book.get("asks", {}).items():
                all_asks.append((price, qty))

        if not all_bids and not all_asks:
            # Log once per minute if no book data
            if not hasattr(self, '_depth_band_warned_minute'):
                self._depth_band_warned_minute = -1
            current_minute = int(time.time() // 60)
            if self._depth_band_warned_minute != current_minute:
                self._depth_band_warned_minute = current_minute
                _write_debug_log({
                    "type": "depth_band_no_book",
                    "minute_key": current_minute,
                    "reason": "no orderbook data in self.orderbooks"
                })
            return {}

        # Define bounds: ±2% of src
        band_pct = 0.02
        low_bound = src * (1 - band_pct)
        high_bound = src * (1 + band_pct)

        depth_band = {}

        # Process bids - notional = price * size
        db_ndigits = _step_ndigits(steps)
        for price, size in all_bids:
            if low_bound <= price <= high_bound:
                bucket = round(round(price / steps) * steps, db_ndigits)
                notional = price * size
                depth_band[bucket] = depth_band.get(bucket, 0.0) + notional

        # Process asks - notional = price * size
        for price, size in all_asks:
            if low_bound <= price <= high_bound:
                bucket = round(round(price / steps) * steps, db_ndigits)
                notional = price * size
                depth_band[bucket] = depth_band.get(bucket, 0.0) + notional

        return depth_band

    def _process_trades(self, exchange: str, instrument: str, data: dict):
        """Process aggTrade data with per-symbol routing.

        Routes trades to the correct symbol using INSTRUMENT_TO_SHORT (same
        mapping used by _process_markprice).  Updates per-symbol volume
        tracking and taker aggression accumulators.  BTC also updates legacy
        self.state.perp_* fields for backwards-compatible dashboard panels.
        """
        if "p" not in data:
            return

        # Resolve symbol from instrument (e.g. "btcusdt" → "BTC")
        sym = INSTRUMENT_TO_SHORT.get(instrument)
        if sym is None:
            return  # unknown instrument, skip

        price = float(data["p"])
        qty = float(data["q"])
        is_buyer_maker = data.get("m", False)
        side = "sell" if is_buyer_maker else "buy"

        # Per-symbol volume accumulation
        vols = self.symbol_volumes[sym]
        if side == "buy":
            vols["buy_vol"] += qty
            vols["buy_count"] += 1
        else:
            vols["sell_vol"] += qty
            vols["sell_count"] += 1

        # BTC backwards compat: update legacy self.state.perp_* fields
        # used by BTC-specific dashboard panels (TRADES section)
        if sym == "BTC":
            level = str(int(price / self.level_size) * self.level_size)

            if side == "buy":
                self.state.perp_buyVol += qty
                self.state.perp_numberBuyTrades += 1
                self.state.perp_buyVolProfile[level] = self.state.perp_buyVolProfile.get(level, 0) + qty
                if len(self.state.perp_orderedBuyTrades) < 100:
                    self.state.perp_orderedBuyTrades.append(qty)
            else:
                self.state.perp_sellVol += qty
                self.state.perp_numberSellTrades += 1
                self.state.perp_sellVolProfile[level] = self.state.perp_sellVolProfile.get(level, 0) + qty
                if len(self.state.perp_orderedSellTrades) < 100:
                    self.state.perp_orderedSellTrades.append(qty)

            self.state.perp_VolProfile[level] = self.state.perp_VolProfile.get(level, 0) + qty

        # Feed per-symbol taker aggression accumulator (real-time aggTrade tracking)
        # This tracks notional USD per taker direction for V2 inference
        self.taker_aggression[sym].on_trade(price, qty, is_buyer_maker)

    def _process_liquidations(self, exchange: str, data: dict):
        """Process liquidation events from any exchange via the normalizer layer.

        The normalizer (liq_normalizer.py) handles all exchange-specific parsing,
        side convention inversion, OKX contract-to-base conversion, and symbol
        extraction.  This method only works with CommonLiqEvent fields.
        """
        events = normalize_liquidation(exchange, data)

        for event in events:
            # Check if we have an engine for this symbol
            engine = self.engine_manager.get_engine(event.symbol_short)
            if engine is None:
                continue  # skip symbols we don't track

            try:
                # Update BTC UI liquidation counters (BTC only — other symbols
                # don't have per-level dashboard tracking yet)
                if event.symbol_short == "BTC":
                    level = str(int(event.price / self.level_size) * self.level_size)
                    if event.side == "long":
                        self.state.perp_liquidations_longsTotal += event.qty
                        self.state.perp_liquidations_longs[level] = (
                            self.state.perp_liquidations_longs.get(level, 0) + event.qty
                        )
                    else:
                        self.state.perp_liquidations_shortsTotal += event.qty
                        self.state.perp_liquidations_shorts[level] = (
                            self.state.perp_liquidations_shorts.get(level, 0) + event.qty
                        )

                # Event-time src prices for calibrator attribution.
                # All symbols get mark_price from Binance markPrice@1s stream
                # via self.symbol_prices.  BTC also has mid_price from orderbook.
                sym_prices = self.symbol_prices.get(event.symbol_short, {})
                mark_price = sym_prices.get("mark", 0.0)
                last_price = sym_prices.get("close", 0.0)
                mid_price = 0.0
                if event.symbol_short == "BTC":
                    if self.state.best_bid > 0 and self.state.best_ask > 0:
                        mid_price = (self.state.best_bid + self.state.best_ask) / 2

                # Sanity log for first N events (across all exchanges/symbols)
                if self._sanity_check_count < self._sanity_check_limit:
                    self._sanity_check_count += 1
                    _write_debug_log({
                        "type": "event_sanity_ok",
                        "event_num": self._sanity_check_count,
                        "exchange": event.exchange,
                        "symbol": event.symbol_short,
                        "side": event.side,
                        "price": event.price,
                        "qty": event.qty,
                        "notional": event.notional,
                        "mark_price": mark_price,
                        "mid_price": mid_price,
                        "last_price": last_price,
                    })

                # Route to calibrator AND heatmap V2 via engine manager.
                # engine_manager.on_force_order calls BOTH calibrator.on_liquidation()
                # and heatmap_v2.on_force_order() — both are required (CLAUDE.md rule).
                self.engine_manager.on_force_order(
                    event.symbol_short,
                    event_data={
                        'timestamp': event.timestamp,
                        'symbol': event.symbol_short,
                        'side': event.side,
                        'price': event.price,
                        'qty': event.qty,
                        'mark_price': mark_price,
                        'mid_price': mid_price,
                        'last_price': last_price,
                    },
                    timestamp=event.timestamp,
                    side=event.side,
                    price=event.price,
                    qty=event.qty,
                    notional=event.notional,
                )
                self.liq_counts[event.symbol_short][event.exchange] += 1
            except Exception as e:
                _write_debug_log({
                    "type": "liq_processing_error",
                    "exchange": event.exchange,
                    "symbol": event.symbol_short,
                    "error": str(e),
                    "error_type": type(e).__name__,
                })

    def _process_markprice(self, exchange: str, instrument: str, data: dict):
        """Process Binance markPrice@1s stream for all tracked symbols.

        Updates per-symbol mark price and OHLC tracking in self.symbol_prices.
        BTC also updates self.state.perp_markPrice for backwards compat.
        Extracts funding rate via _process_funding if present.
        """
        sym = INSTRUMENT_TO_SHORT.get(instrument)
        if sym is None:
            return

        # Extract mark price (field "p" in Binance markPrice stream)
        if "p" in data:
            mark = float(data["p"])
            if mark > 0:
                prices = self.symbol_prices[sym]
                prices["mark"] = mark
                prices["close"] = mark

                # OHLC accumulation: first tick sets open, then track high/low
                if prices["open"] == 0.0:
                    prices["open"] = mark
                prices["high"] = max(prices["high"], mark) if prices["high"] > 0 else mark
                prices["low"] = min(prices["low"], mark) if prices["low"] > 0 else mark

                # BTC backwards compat: update self.state.perp_markPrice
                if sym == "BTC":
                    self.state.perp_markPrice = mark

        # Also extract funding rate if present (markPrice stream includes it)
        self._process_funding(exchange, data)

    def _process_funding(self, exchange: str, data: dict):
        if "r" in data:
            funding = float(data["r"])
            self.state.perp_fundings_per_instrument[exchange] = funding
            fundings = list(self.state.perp_fundings_per_instrument.values())
            if fundings:
                self.state.perp_weighted_funding = sum(fundings) / len(fundings)

    def _process_oi(self, exchange: str, data: dict):
        """Process open interest data from various exchanges."""
        oi_value = None
        source = "unknown"

        # DEBUG: Log raw message (first 200 chars)
        # import json
        # print(f"DEBUG OI RAW [{exchange}]: {json.dumps(data)[:200]}")

        # Bybit format: data.data.openInterestValue (ticker stream)
        if "data" in data and isinstance(data["data"], dict):
            inner_data = data["data"]
            if "openInterestValue" in inner_data:
                oi_value = float(inner_data["openInterestValue"])
                source = "bybit_openInterestValue"
            elif "openInterest" in inner_data:
                # Some exchanges send openInterest in contracts, need price to convert
                oi_contracts = float(inner_data["openInterest"])
                if self.state.btc_price > 0:
                    oi_value = oi_contracts * self.state.btc_price
                    source = "bybit_openInterest_converted"

        # OKX format: data[0].oiCcy or data[0].oi (from open-interest channel)
        elif isinstance(data, list) and len(data) > 0:
            item = data[0]
            if "oiCcy" in item:
                # OI in base currency (BTC)
                oi_btc = float(item["oiCcy"])
                if self.state.btc_price > 0:
                    oi_value = oi_btc * self.state.btc_price
                    source = "okx_oiCcy_converted"
            elif "oi" in item:
                oi_value = float(item["oi"])
                source = "okx_oi"

        # Direct openInterestValue field
        elif "openInterestValue" in data:
            oi_value = float(data["openInterestValue"])
            source = "direct_openInterestValue"

        if oi_value is not None and oi_value > 0:
            prev_total = self.state.perp_total_oi
            self.state.perp_OIs_per_instrument[exchange] = oi_value
            # Recompute total OI
            self.state.perp_total_oi = sum(self.state.perp_OIs_per_instrument.values())
            # DEBUG: Log OI update (uncomment if needed)
            # print(f"DEBUG OI [{exchange}] {source}: {oi_value:,.0f} -> total={self.state.perp_total_oi:,.0f}")

        # Also extract funding if present in oifunding stream
        if "data" in data and isinstance(data["data"], dict):
            inner_data = data["data"]
            if "fundingRate" in inner_data:
                funding = float(inner_data["fundingRate"])
                self.state.perp_fundings_per_instrument[exchange] = funding
                fundings = list(self.state.perp_fundings_per_instrument.values())
                if fundings:
                    self.state.perp_weighted_funding = sum(fundings) / len(fundings)

    def _check_minute_rollover(self):
        current_minute = datetime.now().minute
        if current_minute != self.current_minute:
            # FIX C1: BTC depth fallback — if perp_close is 0 (depth stream
            # disconnected), fall back to BTC markPrice so ETH/SOL pipelines
            # are not blocked. Depth-derived price is preferred when available.
            if self.state.perp_close > 0:
                self._last_depth_price_ts = time.time()
            elif time.time() - self._last_depth_price_ts > 60:
                # Depth has been stale for >60s — try markPrice fallback
                mark_close = self.symbol_prices.get("BTC", {}).get("close", 0.0)
                if mark_close > 0:
                    now = time.time()
                    if now - self._last_fallback_warning_ts >= 60:
                        logger.warning(
                            "BTC depth stale, using markPrice fallback for "
                            "minute rollover (markPrice=%.2f)", mark_close
                        )
                        self._last_fallback_warning_ts = now
                    # Populate perp_close (and OHLC if empty) so downstream
                    # code works unchanged.
                    self.state.perp_close = mark_close
                    if self.state.perp_open == 0:
                        self.state.perp_open = mark_close
                    if self.state.perp_high == 0:
                        self.state.perp_high = mark_close
                    if self.state.perp_low == 0:
                        self.state.perp_low = mark_close
                else:
                    logger.error(
                        "BTC depth AND markPrice both unavailable — "
                        "skipping minute rollover"
                    )

            if self.state.perp_close > 0:
                # Update V2 heatmap engine (OI inference layer)
                # ohlc4 = (open + high + low + close) / 4
                v2_src = (self.state.perp_open + self.state.perp_high +
                          self.state.perp_low + self.state.perp_close) / 4
                v2_minute_key = int(time.time() // 60)

                # Get BTC aggTrade-derived taker aggression (previous completed minute)
                # Fallback to perp_buyVol/perp_sellVol if aggTrade data missing
                prev_aggression = self.taker_aggression["BTC"].get_previous_minute_data()
                fallback_used = False
                perp_buyVol_btc = None
                perp_sellVol_btc = None

                if prev_aggression is not None and prev_aggression.trade_count > 0:
                    # Use real-time aggTrade data (already in USD notional)
                    taker_buy_notional_usd = prev_aggression.taker_buy_notional
                    taker_sell_notional_usd = prev_aggression.taker_sell_notional
                    agg_trade_count = prev_aggression.trade_count
                    agg_minute_key = prev_aggression.minute_key
                else:
                    # Fallback: convert BTC volume to USD notional
                    perp_buyVol_btc = self.state.perp_buyVol
                    perp_sellVol_btc = self.state.perp_sellVol
                    taker_buy_notional_usd = perp_buyVol_btc * v2_src
                    taker_sell_notional_usd = perp_sellVol_btc * v2_src
                    fallback_used = True
                    agg_trade_count = 0
                    agg_minute_key = v2_minute_key - 1  # Estimate

                # Compute derived values for logging
                total_notional_usd = taker_buy_notional_usd + taker_sell_notional_usd
                buy_pct = taker_buy_notional_usd / total_notional_usd if total_notional_usd > 0 else 0.5

                # === Per-symbol heatmap_v2.on_minute() ===
                # Each symbol's heatmap needs per-minute market data for
                # tape decay/sweep and inference projections.
                for sym_on in self.engine_manager.get_all_symbols():
                    try:
                        eng_on = self.engine_manager.get_engine(sym_on)
                        if eng_on is None:
                            continue

                        # Compute per-symbol OHLC4 and price bounds
                        if sym_on == "BTC":
                            sym_on_src = v2_src
                            sym_on_high = self.state.perp_high
                            sym_on_low = self.state.perp_low
                        else:
                            sp = self.symbol_prices.get(sym_on, {})
                            o_s, h_s, l_s, c_s = (
                                sp.get("open", 0.0), sp.get("high", 0.0),
                                sp.get("low", 0.0), sp.get("close", 0.0),
                            )
                            sym_on_src = (o_s + h_s + l_s + c_s) / 4 if c_s > 0 else 0.0
                            sym_on_high = h_s
                            sym_on_low = l_s

                        if sym_on_src <= 0:
                            continue  # no price data yet for this symbol

                        # Per-symbol OI from multi-exchange poller
                        sym_on_oi_data = self.oi_by_symbol.get(sym_on, {})
                        sym_on_oi = sym_on_oi_data.get("aggregated_oi", 0.0) or 0.0

                        # Per-symbol taker aggression
                        sym_prev_agg = self.taker_aggression[sym_on].get_previous_minute_data()
                        if sym_prev_agg is not None and sym_prev_agg.trade_count > 0:
                            sym_on_buy = sym_prev_agg.taker_buy_notional
                            sym_on_sell = sym_prev_agg.taker_sell_notional
                        else:
                            # Fallback: convert base volume to USD notional
                            sv = self.symbol_volumes.get(sym_on, {})
                            sym_on_buy = sv.get("buy_vol", 0.0) * sym_on_src
                            sym_on_sell = sv.get("sell_vol", 0.0) * sym_on_src

                        eng_on.heatmap_v2.on_minute(
                            minute_key=v2_minute_key,
                            src_price=sym_on_src,
                            high=sym_on_high,
                            low=sym_on_low,
                            oi=sym_on_oi,
                            taker_buy_notional_usd=sym_on_buy,
                            taker_sell_notional_usd=sym_on_sell,
                        )
                    except Exception as e:
                        logger.error(f"on_minute failed for {sym_on}: {e}", exc_info=True)

                # Log BTC aggression diagnostics with full unit verification
                agg_history = self.taker_aggression["BTC"].get_last_n_minutes(5)
                debug_entry = {
                    "type": "aggression_minute",
                    "minute_key": agg_minute_key,
                    "v2_minute_key": v2_minute_key,
                    "src": round(v2_src, 2),
                    "trade_count": agg_trade_count,
                    "buy_notional_usd": round(taker_buy_notional_usd, 2),
                    "sell_notional_usd": round(taker_sell_notional_usd, 2),
                    "total_notional_usd": round(total_notional_usd, 2),
                    "buy_pct": round(buy_pct, 4),
                    "fallback_used": fallback_used,
                    "history_last_5": agg_history
                }
                # If fallback was used, include raw BTC volumes for unit verification
                if fallback_used:
                    debug_entry["perp_buyVol_btc"] = round(perp_buyVol_btc, 6)
                    debug_entry["perp_sellVol_btc"] = round(perp_sellVol_btc, 6)
                _write_debug_log(debug_entry)

                # === Per-symbol V2 snapshot compute + atomic write ===
                # Each symbol gets its own snapshot file.
                v2_reference_keys = None  # for cross-symbol key validation
                for sym_v2 in self.engine_manager.get_all_symbols():
                    try:
                        eng_v2 = self.engine_manager.get_engine(sym_v2)
                        if eng_v2 is None:
                            continue

                        # Per-symbol price for snapshot
                        if sym_v2 == "BTC":
                            sym_v2_src = v2_src
                        else:
                            sp2 = self.symbol_prices.get(sym_v2, {})
                            c2 = sp2.get("close", 0.0)
                            sym_v2_src = (sp2.get("open", 0.0) + sp2.get("high", 0.0) +
                                          sp2.get("low", 0.0) + c2) / 4 if c2 > 0 else 0.0
                        if sym_v2_src <= 0:
                            continue

                        sym_v2_snap = eng_v2.heatmap_v2.get_api_response_display(
                            price_center=sym_v2_src,
                            price_range_pct=0.10,
                            min_notional_usd=1000.0,
                        )
                        sym_v2_snap['stats'] = eng_v2.heatmap_v2.get_stats()

                        # Build intensity arrays for history buffer
                        sym_v2_steps = eng_v2.heatmap_v2.config.steps
                        band_pct = 0.10
                        sym_v2_ndigits = _step_ndigits(sym_v2_steps)
                        sym_price_min = round(round((sym_v2_src * (1 - band_pct)) / sym_v2_steps) * sym_v2_steps, sym_v2_ndigits)
                        sym_price_max = round(round((sym_v2_src * (1 + band_pct)) / sym_v2_steps) * sym_v2_steps, sym_v2_ndigits)
                        sym_v2_prices = _build_price_grid(sym_price_min, sym_price_max, sym_v2_steps)

                        sym_v2_hm = eng_v2.heatmap_v2.get_heatmap_display()
                        sym_v2_longs = sym_v2_hm.get("long", {})
                        sym_v2_shorts = sym_v2_hm.get("short", {})
                        sym_v2_li = [sym_v2_longs.get(pp, 0.0) for pp in sym_v2_prices]
                        sym_v2_si = [sym_v2_shorts.get(pp, 0.0) for pp in sym_v2_prices]
                        sym_v2_lb = [round(u / pp, 6) if pp > 0 else 0.0
                                     for u, pp in zip(sym_v2_li, sym_v2_prices)]
                        sym_v2_sb = [round(u / pp, 6) if pp > 0 else 0.0
                                     for u, pp in zip(sym_v2_si, sym_v2_prices)]

                        sym_v2_snap['ts'] = time.time()
                        sym_v2_snap['src'] = sym_v2_src
                        sym_v2_snap['step'] = sym_v2_steps
                        sym_v2_snap['price_min'] = sym_price_min
                        sym_v2_snap['price_max'] = sym_price_max
                        sym_v2_snap['prices'] = sym_v2_prices
                        sym_v2_snap['long_intensity'] = sym_v2_li
                        sym_v2_snap['short_intensity'] = sym_v2_si
                        sym_v2_snap['long_notional_usd'] = [round(v, 2) for v in sym_v2_li]
                        sym_v2_snap['short_notional_usd'] = [round(v, 2) for v in sym_v2_si]
                        sym_v2_snap['long_size_btc'] = sym_v2_lb
                        sym_v2_snap['short_size_btc'] = sym_v2_sb

                        # Atomic write to per-symbol file
                        sym_v2_file = os.path.join(POC_DIR, f"liq_api_snapshot_v2_{sym_v2}.json")
                        write_json_atomic(sym_v2_file, sym_v2_snap)

                        # Legacy BTC backwards-compat write removed (Task 19) —
                        # embedded_api.py reads per-symbol files only.

                        # Cross-symbol key-set validation (log once on drift)
                        snap_keys = set(sym_v2_snap.keys())
                        if v2_reference_keys is None:
                            v2_reference_keys = snap_keys
                        elif snap_keys != v2_reference_keys:
                            logger.warning(
                                f"V2 snapshot key drift: {sym_v2} keys differ from reference. "
                                f"missing={v2_reference_keys - snap_keys}, "
                                f"extra={snap_keys - v2_reference_keys}"
                            )
                    except Exception as e:
                        logger.error(f"V2 snapshot failed for {sym_v2}: {e}", exc_info=True)

                # Apply persisted weights from calibrator on first update (all symbols)
                for sym_short in self.engine_manager.get_all_symbols():
                    if self._applied_persisted_weights.get(sym_short, False):
                        continue
                    eng = self.engine_manager.get_engine(sym_short)
                    if eng is None:
                        continue
                    persisted = eng.calibrator.get_persisted_weights()
                    if persisted:
                        v2_lad = sorted(eng.heatmap_v2.inference.leverage_weights.keys())
                        if len(persisted['weights']) == len(v2_lad):
                            wd = dict(zip(v2_lad, persisted['weights']))
                            eng.heatmap_v2.inference.update_leverage_weights(wd)
                            eng.heatmap_v2.config.buffer = persisted['buffer']
                            print(f"Loaded persisted weights for {sym_short} (calibration #{persisted['calibration_count']})")
                    self._applied_persisted_weights[sym_short] = True

                # Get top predicted liquidation zones from V2 engine (clustered pools)
                # Display-only: ZoneTracker is a UI widget, safe to use display wrapper
                v2_response = self._btc().heatmap_v2.get_api_response_display(
                    price_center=v2_src,
                    price_range_pct=0.25,      # ±25% range for more visibility
                    min_notional_usd=0         # No filtering, let UI handle it
                )

                # Convert V2 format to UI format: [(price, intensity), ...]
                raw_longs = [
                    (lvl['price'], lvl['intensity'])
                    for lvl in v2_response.get('long_levels', [])
                ]
                raw_shorts = [
                    (lvl['price'], lvl['intensity'])
                    for lvl in v2_response.get('short_levels', [])
                ]
                # Sort by intensity descending
                raw_longs.sort(key=lambda x: x[1], reverse=True)
                raw_shorts.sort(key=lambda x: x[1], reverse=True)

                # Use V2 stats
                self.state.liq_engine_stats = self._btc().heatmap_v2.get_stats()

                # Pass through ZoneTracker for UI stability
                current_minute_ts = int(time.time() // 60)
                stable_longs, stable_shorts = self.zone_tracker.update(
                    current_minute=current_minute_ts,
                    long_zones=raw_longs,
                    short_zones=raw_shorts
                )

                # Store stable zones for display (limited to 5)
                self.state.pred_liq_longs_top = stable_longs[:5]
                self.state.pred_liq_shorts_top = stable_shorts[:5]

                # Write plot feed entry for live chart
                # Include zone ages from tracker for HUD display
                long_zones_with_age = []
                for z in self.zone_tracker.displayed_long:
                    age = z.age_minutes(current_minute_ts)
                    long_zones_with_age.append((z.price, z.strength, age))
                long_zones_with_age.sort(key=lambda x: x[1], reverse=True)

                short_zones_with_age = []
                for z in self.zone_tracker.displayed_short:
                    age = z.age_minutes(current_minute_ts)
                    short_zones_with_age.append((z.price, z.strength, age))
                short_zones_with_age.sort(key=lambda x: x[1], reverse=True)

                _write_plot_feed({
                    "symbol": "BTC",
                    "ts": time.time(),
                    "minute": current_minute_ts,
                    "o": round(self.state.perp_open, 2),
                    "h": round(self.state.perp_high, 2),
                    "l": round(self.state.perp_low, 2),
                    "c": round(self.state.perp_close, 2),
                    "long_zones": [(round(p, 2), round(s, 4), a) for p, s, a in long_zones_with_age[:5]],
                    "short_zones": [(round(p, 2), round(s, 4), a) for p, s, a in short_zones_with_age[:5]]
                })

                # Write ETH/SOL plot feed entries (same file, with symbol field)
                for sym_pf in self.engine_manager.get_all_symbols():
                    if sym_pf == "BTC":
                        continue  # already written above
                    try:
                        sp_pf = self.symbol_prices.get(sym_pf, {})
                        pf_c = sp_pf.get("close", 0.0)
                        if pf_c <= 0:
                            continue
                        _write_plot_feed({
                            "symbol": sym_pf,
                            "ts": time.time(),
                            "minute": current_minute_ts,
                            "o": round(sp_pf.get("open", 0.0), 4),
                            "h": round(sp_pf.get("high", 0.0), 4),
                            "l": round(sp_pf.get("low", 0.0), 4),
                            "c": round(pf_c, 4),
                            "long_zones": [],
                            "short_zones": [],
                        })
                    except Exception as e:
                        logger.error(f"Plot feed failed for {sym_pf}: {e}", exc_info=True)

                # === Per-symbol V1 snapshot compute + atomic write ===
                # BTC uses ZoneTracker-stabilized zones; ETH/SOL get empty
                # zone lists (no zone tracker) but identical key schema.
                v1_reference_keys = None
                for sym_v1 in self.engine_manager.get_all_symbols():
                    try:
                        self._write_per_symbol_v1_snapshot(
                            sym_v1, current_minute_ts,
                            long_zones_with_age if sym_v1 == "BTC" else [],
                            short_zones_with_age if sym_v1 == "BTC" else [],
                            v1_reference_keys,
                        )
                    except Exception as e:
                        logger.error(f"V1 snapshot failed for {sym_v1}: {e}", exc_info=True)

                # Send snapshot to ALL symbol calibrators for learning.
                # Each symbol's calibrator MUST receive on_minute_snapshot() every
                # 60 seconds, otherwise on_liquidation() silently drops all events
                # (CLAUDE.md: Calibrator Integration Rules).
                #
                # BTC OHLC comes from the viewer's orderbook mid-price tracking
                # (perp_open/high/low/close) AND from markPrice stream (symbol_prices).
                # ETH/SOL OHLC comes from Binance markPrice@1s via symbol_prices.
                btc_src = (self.state.perp_open + self.state.perp_high +
                           self.state.perp_low + self.state.perp_close) / 4

                for sym_short in self.engine_manager.get_all_symbols():
                    try:
                        eng = self.engine_manager.get_engine(sym_short)
                        if eng is None:
                            continue

                        v2_hm = eng.heatmap_v2.get_heatmap()
                        v2_cfg = eng.heatmap_v2.config
                        lw = eng.heatmap_v2.inference.leverage_weights
                        ladder = sorted(lw.keys())
                        weights = [lw[lev] for lev in ladder]

                        # Per-symbol volume from aggTrade stream
                        sym_vols = self.symbol_volumes.get(sym_short, {})

                        # Per-symbol OI delta (change since last rollover).
                        # prev_oi_by_symbol starts as None to avoid fake spike.
                        sym_oi_data = self.oi_by_symbol.get(sym_short, {})
                        current_oi = sym_oi_data.get("aggregated_oi", None)
                        prev_oi = self.prev_oi_by_symbol.get(sym_short)
                        if (current_oi is not None and prev_oi is not None
                                and not math.isnan(current_oi) and not math.isinf(current_oi)
                                and not math.isnan(prev_oi) and not math.isinf(prev_oi)):
                            sym_oi_change = current_oi - prev_oi
                        else:
                            sym_oi_change = 0.0
                        # Update prev_oi for next minute (only if valid)
                        if current_oi is not None and not math.isnan(current_oi) and not math.isinf(current_oi):
                            self.prev_oi_by_symbol[sym_short] = current_oi
                        # BTC backwards compat
                        if sym_short == "BTC":
                            self.state.perp_oi_change = sym_oi_change

                        if sym_short == "BTC":
                            # BTC: primary price from orderbook mid (perp_*),
                            # backed by markPrice stream in symbol_prices
                            sym_src = btc_src
                            sym_high = self.state.perp_high
                            sym_low = self.state.perp_low
                            sym_close = self.state.perp_close
                            sym_buy_vol = sym_vols.get("buy_vol", 0.0)
                            sym_sell_vol = sym_vols.get("sell_vol", 0.0)
                            sym_depth = self._build_depth_band(btc_src, v2_cfg.steps)
                        else:
                            # ETH/SOL: OHLC from Binance markPrice@1s stream,
                            # volume from per-symbol aggTrade streams
                            prices = self.symbol_prices.get(sym_short, {})
                            o = prices.get("open", 0.0)
                            h = prices.get("high", 0.0)
                            l = prices.get("low", 0.0)
                            c = prices.get("close", 0.0)
                            sym_src = (o + h + l + c) / 4 if c > 0 else 0.0
                            sym_high = h
                            sym_low = l
                            sym_close = c
                            sym_buy_vol = sym_vols.get("buy_vol", 0.0)
                            sym_sell_vol = sym_vols.get("sell_vol", 0.0)
                            sym_depth = {}

                        self.engine_manager.on_minute_snapshot(
                            sym_short,
                            symbol=sym_short,
                            timestamp=time.time(),
                            src=sym_src,
                            pred_longs=v2_hm.get("long", {}),
                            pred_shorts=v2_hm.get("short", {}),
                            ladder=ladder,
                            weights=weights,
                            buffer=v2_cfg.buffer,
                            steps=v2_cfg.steps,
                            high=sym_high,
                            low=sym_low,
                            close=sym_close,
                            perp_buy_vol=sym_buy_vol,
                            perp_sell_vol=sym_sell_vol,
                            perp_oi_change=sym_oi_change,
                            depth_band=sym_depth,
                        )
                    except Exception as e:
                        logger.error(f"Minute snapshot failed for {sym_short}: {e}", exc_info=True)

                # Per-minute OI/funding/ratio log line
                oi_str = f"OI={self.state.perp_total_oi:,.0f}"
                oi_chg_str = f"Δ={self.state.perp_oi_change:+,.0f}" if self.state.perp_oi_change != 0 else ""
                fund_str = f"F={self.state.perp_weighted_funding*100:.4f}%"
                ratio_str = f"TTA={self.state.perp_TTA_ratio:.3f} TTP={self.state.perp_TTP_ratio:.3f} GTA={self.state.perp_GTA_ratio:.3f}"
                print(f"[MIN] BTC {oi_str} {oi_chg_str} {fund_str} | {ratio_str}")

            # Reset minute metrics
            self.state.perp_buyVol = 0
            self.state.perp_sellVol = 0
            self.state.perp_numberBuyTrades = 0
            self.state.perp_numberSellTrades = 0
            self.state.perp_VolProfile = {}
            self.state.perp_buyVolProfile = {}
            self.state.perp_sellVolProfile = {}
            self.state.perp_orderedBuyTrades = []
            self.state.perp_orderedSellTrades = []
            self.state.perp_voids = {}
            self.state.perp_reinforces = {}
            self.state.perp_totalVoids = 0
            self.state.perp_totalReinforces = 0
            self.state.perp_liquidations_longsTotal = 0
            self.state.perp_liquidations_shortsTotal = 0
            self.state.perp_liquidations_longs = {}
            self.state.perp_liquidations_shorts = {}
            self.state.perp_open = self.state.btc_price
            self.prices_this_minute = [self.state.btc_price] if self.state.btc_price > 0 else []

            # Reset per-symbol OHLC for new minute.
            # Carry forward last close as new open (first tick will override).
            for sym in self.symbol_prices:
                last_close = self.symbol_prices[sym]["close"]
                self.symbol_prices[sym]["open"] = last_close if last_close > 0 else 0.0
                self.symbol_prices[sym]["high"] = last_close if last_close > 0 else 0.0
                self.symbol_prices[sym]["low"] = last_close if last_close > 0 else 0.0

            # Reset per-symbol volume accumulators for new minute.
            for sym in self.symbol_volumes:
                self.symbol_volumes[sym]["buy_vol"] = 0.0
                self.symbol_volumes[sym]["sell_vol"] = 0.0
                self.symbol_volumes[sym]["buy_count"] = 0
                self.symbol_volumes[sym]["sell_count"] = 0

            self.current_minute = current_minute

    def _write_per_symbol_v1_snapshot(
        self,
        sym_short: str,
        current_minute: int,
        long_zones_with_age: list,
        short_zones_with_age: list,
        reference_keys: Optional[set] = None,
    ):
        """Write per-symbol V1 heatmap snapshot with atomic write.

        Produces identical top-level keys for all symbols. BTC uses
        ZoneTracker-stabilized zones; ETH/SOL receive empty zone lists.
        BTC also writes to the legacy filename for backwards compat.
        """
        eng = self.engine_manager.get_engine(sym_short)
        if eng is None:
            return

        if sym_short == "BTC":
            src = self.state.perp_close
        else:
            sp = self.symbol_prices.get(sym_short, {})
            src = sp.get("close", 0.0)
        if src <= 0:
            return

        steps = eng.heatmap_v2.config.steps

        # Build price range: ±8% around src, bucketed by steps
        band_pct = 0.08
        v1_ndigits = _step_ndigits(steps)
        price_min = round(round((src * (1 - band_pct)) / steps) * steps, v1_ndigits)
        price_max = round(round((src * (1 + band_pct)) / steps) * steps, v1_ndigits)

        prices = _build_price_grid(price_min, price_max, steps)

        v1_heatmap = eng.heatmap_v2.get_heatmap_display()
        all_longs = v1_heatmap.get("long", {})
        all_shorts = v1_heatmap.get("short", {})

        long_intensity_raw = [all_longs.get(price, 0.0) for price in prices]
        short_intensity_raw = [all_shorts.get(price, 0.0) for price in prices]

        # Spatial smoothing (±2 bucket weighted average)
        def smooth_array(arr, radius=2):
            if len(arr) < 2 * radius + 1:
                return arr
            weights = [0.1, 0.2, 0.4, 0.2, 0.1]
            smoothed = []
            for i in range(len(arr)):
                total = 0.0
                weight_sum = 0.0
                for j, w in enumerate(weights):
                    idx = i - radius + j
                    if 0 <= idx < len(arr):
                        total += arr[idx] * w
                        weight_sum += w
                smoothed.append(total / weight_sum if weight_sum > 0 else 0.0)
            return smoothed

        long_smooth = smooth_array(long_intensity_raw)
        short_smooth = smooth_array(short_intensity_raw)

        all_strengths = [s for s in long_smooth + short_smooth if s > 0]
        if all_strengths:
            sorted_s = sorted(all_strengths)
            n = len(sorted_s)
            p50 = sorted_s[int(n * 0.5)]
            p95 = sorted_s[min(int(n * 0.95), n - 1)]
            if p95 <= p50:
                p95 = p50 + 0.01
        else:
            p50, p95 = 0.0, 1.0

        def normalize(val):
            if val <= 0:
                return 0.0
            norm = (val - p50) / (p95 - p50)
            norm = max(0.0, min(1.0, norm))
            return norm if norm > 0.1 else 0.0

        long_intensity = [round(normalize(s), 4) for s in long_smooth]
        short_intensity = [round(normalize(s), 4) for s in short_smooth]

        top_long_zones = [
            {"price": round(p, 2), "strength": round(s, 4), "age_min": a}
            for p, s, a in long_zones_with_age[:5]
        ]
        top_short_zones = [
            {"price": round(p, 2), "strength": round(s, 4), "age_min": a}
            for p, s, a in short_zones_with_age[:5]
        ]

        long_size_btc = [round(usd / pp, 6) if pp > 0 else 0.0
                         for usd, pp in zip(long_intensity_raw, prices)]
        short_size_btc = [round(usd / pp, 6) if pp > 0 else 0.0
                          for usd, pp in zip(short_intensity_raw, prices)]

        snapshot = {
            "schema_version": 2,
            "symbol": sym_short,
            "ts": time.time(),
            "src": round(src, 2),
            "step": steps,
            "price_min": price_min,
            "price_max": price_max,
            "prices": prices,
            "long_intensity": long_intensity,
            "short_intensity": short_intensity,
            "long_notional_usd": [round(v, 2) for v in long_intensity_raw],
            "short_notional_usd": [round(v, 2) for v in short_intensity_raw],
            "long_size_btc": long_size_btc,
            "short_size_btc": short_size_btc,
            "top_long_zones": top_long_zones,
            "top_short_zones": top_short_zones,
            "norm": {
                "method": "p50_p95",
                "p50": round(p50, 6),
                "p95": round(p95, 6)
            }
        }

        # Atomic write to per-symbol file
        sym_v1_file = os.path.join(POC_DIR, f"liq_api_snapshot_{sym_short}.json")
        write_json_atomic(sym_v1_file, snapshot)

        # Legacy BTC backwards-compat write removed (Task 19) —
        # embedded_api.py reads per-symbol files only.

        # Cross-symbol key-set validation
        snap_keys = set(snapshot.keys())
        if reference_keys is not None and snap_keys != reference_keys:
            logger.warning(
                f"V1 snapshot key drift: {sym_short} keys differ from reference. "
                f"missing={reference_keys - snap_keys}, "
                f"extra={snap_keys - reference_keys}"
            )

    def get_state(self) -> FullMetricsState:
        with self.lock:
            # Return a copy to avoid threading issues
            import copy
            return copy.deepcopy(self.state)


def create_display(processor: FullMetricsProcessor, start_time: float) -> Layout:
    """Generate the display layout."""
    state = processor.get_state()

    def fmt_price(value: float) -> str:
        return f"${value:,.2f}" if value and value > 0 else "—"

    layout = Layout()

    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="metrics", ratio=1),
        Layout(name="footer", size=3)
    )

    # Header
    runtime = int(time.time() - start_time)
    header = Text()
    header.append("  MULTI-SYMBOL PERPETUAL - ALL METRICS  ", style="bold white on blue")
    header.append(f"\n  Exchanges: {', '.join(state.exchanges) or 'connecting...'}", style="dim")
    header.append(f"  |  Runtime: {runtime//60}m {runtime%60}s", style="dim")
    header.append(f"  |  {state.timestamp}", style="dim")
    layout["header"].update(Panel(header, box=box.DOUBLE))

    # Metrics columns
    # Keep enough vertical room so each multi-symbol table can always display
    # header + all symbol rows (BTC/ETH/SOL) without clipping.
    symbol_row_count = len(SYMBOL_CONFIGS)
    multi_symbol_height = max(16, 10 + (symbol_row_count * 4))
    layout["metrics"].split_column(
        Layout(name="multi_symbol", size=multi_symbol_height),
        Layout(name="btc_metrics", ratio=1)
    )

    layout["metrics"]["multi_symbol"].split_column(
        Layout(name="overview", ratio=2),
        Layout(name="liq_breakdown", ratio=2),
        Layout(name="connections", ratio=1)
    )

    layout["metrics"]["btc_metrics"].split_row(
        Layout(name="col1", ratio=1),
        Layout(name="col2", ratio=1),
        Layout(name="col3", ratio=1),
        Layout(name="col4", ratio=1)
    )

    layout["metrics"]["btc_metrics"]["col1"].split_column(
        Layout(name="price", ratio=1),
        Layout(name="orderbook", ratio=1)
    )
    layout["metrics"]["btc_metrics"]["col2"].split_column(
        Layout(name="trades", ratio=1),
        Layout(name="adjustments", ratio=1)
    )
    layout["metrics"]["btc_metrics"]["col3"].split_column(
        Layout(name="funding", ratio=1),
        Layout(name="liquidations", ratio=1),
        Layout(name="positions", size=8)
    )
    layout["metrics"]["btc_metrics"]["col4"].split_column(
        Layout(name="stress_zones", ratio=1)
    )

    # Multi-symbol sections
    try:
        overview = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        overview.add_column("Symbol", style="cyan")
        overview.add_column("Price", justify="right")
        overview.add_column("Liq Events", justify="right")
        overview.add_column("Cal Events", justify="right")
        overview.add_column("Cal Min", justify="right")
        overview.add_column("Zones", justify="right")
        overview.add_column("Agg OI", justify="right")

        dashboard_symbols = list(SYMBOL_CONFIGS.keys())

        for sym in dashboard_symbols:
            try:
                prices = processor.symbol_prices.get(sym, {})
                price_text = fmt_price(prices.get("mark", 0.0))

                liq_events = sum(processor.liq_counts.get(sym, {}).values())
                engine = processor.engine_manager.get_engine(sym)

                cal_events = 0
                cal_min = 0
                zone_count = 0
                if engine is not None:
                    calibrator = getattr(engine, "calibrator", None)
                    if calibrator is not None:
                        cal_events = getattr(getattr(calibrator, "stats", None), "total_events", 0) or 0
                        cal_min = getattr(calibrator, "minutes_since_calibration", 0) or 0
                    zone_manager = getattr(getattr(engine, "heatmap_v2", None), "zone_manager", None)
                    if zone_manager is not None:
                        try:
                            zone_count = len(zone_manager.get_active_zones())
                        except Exception:
                            zone_count = 0

                agg_oi = processor.oi_by_symbol.get(sym, {}).get("aggregated_oi", 0.0)
                agg_oi_text = f"{agg_oi:,.0f}" if agg_oi and agg_oi > 0 else "0"

                overview.add_row(
                    sym,
                    price_text,
                    f"{liq_events:,}",
                    f"{cal_events:,}",
                    f"{cal_min:,}",
                    f"{zone_count:,}",
                    agg_oi_text,
                )
            except Exception:
                # Keep all symbols visible even if one row has bad data.
                overview.add_row(sym, "—", "0", "0", "0", "0", "0")

        layout["metrics"]["multi_symbol"]["overview"].update(
            Panel(overview, title="[bold cyan]MULTI-SYMBOL OVERVIEW[/]", border_style="cyan")
        )

        liq_breakdown = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        liq_breakdown.add_column("Symbol", style="magenta")
        liq_breakdown.add_column("Binance", justify="right")
        liq_breakdown.add_column("Bybit", justify="right")
        liq_breakdown.add_column("OKX", justify="right")

        for sym in dashboard_symbols:
            try:
                per_sym = processor.liq_counts.get(sym, {})
                liq_breakdown.add_row(
                    sym,
                    f"{per_sym.get('binance', 0):,}",
                    f"{per_sym.get('bybit', 0):,}",
                    f"{per_sym.get('okx', 0):,}",
                )
            except Exception:
                liq_breakdown.add_row(sym, "0", "0", "0")

        layout["metrics"]["multi_symbol"]["liq_breakdown"].update(
            Panel(liq_breakdown, title="[bold magenta]EXCHANGE LIQ BREAKDOWN[/]", border_style="magenta")
        )

        now_ts = time.time()
        statuses = []
        for ex in ["binance", "bybit", "okx"]:
            age = now_ts - processor.exchange_last_seen.get(ex, 0.0)
            if age < 30:
                st = "[green]LIVE[/green]"
            elif age < 120:
                st = "[yellow]STALE[/yellow]"
            else:
                st = "[red]DOWN[/red]"
            statuses.append(f"{ex}: {st}")

        layout["metrics"]["multi_symbol"]["connections"].update(
            Panel("  |  ".join(statuses), title="[bold white]EXCHANGE CONNECTIONS[/]", border_style="white")
        )
    except Exception as e:
        layout["metrics"]["multi_symbol"]["overview"].update(
            Panel(f"[red]Overview render error:[/] {e}", title="[bold cyan]MULTI-SYMBOL OVERVIEW[/]")
        )
        layout["metrics"]["multi_symbol"]["liq_breakdown"].update(
            Panel("[yellow]Awaiting liquidation data...[/]", title="[bold magenta]EXCHANGE LIQ BREAKDOWN[/]")
        )
        layout["metrics"]["multi_symbol"]["connections"].update(
            Panel("binance: [red]DOWN[/red]  |  bybit: [red]DOWN[/red]  |  okx: [red]DOWN[/red]", title="[bold white]EXCHANGE CONNECTIONS[/]")
        )

    # Price panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right")
    price_style = "green" if state.perp_close >= state.perp_open else "red"
    t.add_row("btc_price", f"[bold {price_style}]${state.btc_price:,.2f}[/]")
    t.add_row("perp_open", f"${state.perp_open:,.2f}")
    t.add_row("perp_close", f"${state.perp_close:,.2f}")
    t.add_row("perp_high", f"[green]${state.perp_high:,.2f}[/]")
    t.add_row("perp_low", f"[red]${state.perp_low:,.2f}[/]")
    t.add_row("perp_Vola", f"{state.perp_Vola:.4f}")
    layout["metrics"]["btc_metrics"]["col1"]["price"].update(Panel(t, title="[bold cyan]BTC PRICE[/]", border_style="cyan"))

    # Orderbook panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="green")
    t.add_column("Value", justify="right")
    t.add_row("best_bid", f"${state.best_bid:,.2f}")
    t.add_row("best_ask", f"${state.best_ask:,.2f}")
    t.add_row("spread", f"${state.spread:.2f}")
    t.add_row("bid_depth (1%)", f"{state.bid_depth:.4f} BTC")
    t.add_row("ask_depth (1%)", f"{state.ask_depth:.4f} BTC")
    imb_style = "green" if state.imbalance > 0 else "red"
    t.add_row("imbalance", f"[{imb_style}]{state.imbalance:+.4f}[/]")
    if state.perp_books:
        t.add_row("", "")
        t.add_row("[bold]perp_books[/] (top 3)", "")
        for level, qty in sorted(state.perp_books.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"{qty:.4f}")
    layout["metrics"]["btc_metrics"]["col1"]["orderbook"].update(Panel(t, title="[bold green]BTC ORDERBOOK[/]", border_style="green"))

    # Trades panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="yellow")
    t.add_column("Value", justify="right")
    t.add_row("perp_buyVol", f"[green]{state.perp_buyVol:.4f}[/] BTC")
    t.add_row("perp_sellVol", f"[red]{state.perp_sellVol:.4f}[/] BTC")
    t.add_row("perp_numberBuyTrades", f"[green]{state.perp_numberBuyTrades:,}[/]")
    t.add_row("perp_numberSellTrades", f"[red]{state.perp_numberSellTrades:,}[/]")
    if state.perp_VolProfile:
        t.add_row("", "")
        t.add_row("[bold]perp_VolProfile[/] (top 3)", "")
        for level, vol in sorted(state.perp_VolProfile.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"{vol:.4f}")
    t.add_row("", "")
    t.add_row("orderedBuyTrades", f"{len(state.perp_orderedBuyTrades)} items")
    t.add_row("orderedSellTrades", f"{len(state.perp_orderedSellTrades)} items")
    layout["metrics"]["btc_metrics"]["col2"]["trades"].update(Panel(t, title="[bold yellow]BTC TRADES[/]", border_style="yellow"))

    # Adjustments panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="magenta")
    t.add_column("Value", justify="right")
    t.add_row("perp_totalVoids", f"[red]{state.perp_totalVoids:.4f}[/] BTC")
    t.add_row("perp_totalReinforces", f"[green]{state.perp_totalReinforces:.4f}[/] BTC")
    if state.perp_voids:
        t.add_row("", "")
        t.add_row("[bold]perp_voids[/] (top 3)", "")
        for level, qty in sorted(state.perp_voids.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"[red]{qty:.4f}[/]")
    if state.perp_reinforces:
        t.add_row("", "")
        t.add_row("[bold]perp_reinforces[/] (top 3)", "")
        for level, qty in sorted(state.perp_reinforces.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"[green]{qty:.4f}[/]")
    layout["metrics"]["btc_metrics"]["col2"]["adjustments"].update(Panel(t, title="[bold magenta]BTC ADJUSTMENTS[/]", border_style="magenta"))

    # Funding panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="blue")
    t.add_column("Value", justify="right")
    fund_style = "green" if state.perp_weighted_funding >= 0 else "red"
    t.add_row("perp_weighted_funding", f"[{fund_style}]{state.perp_weighted_funding*100:.4f}%[/]")
    t.add_row("perp_total_oi", f"{state.perp_total_oi:,.0f} USD")
    if state.perp_OIs_per_instrument:
        t.add_row("", "")
        t.add_row("[bold]OIs_per_instrument[/]", "")
        for ex, oi in state.perp_OIs_per_instrument.items():
            t.add_row(f"  {ex}", f"{oi:,.0f}")
    if state.perp_fundings_per_instrument:
        t.add_row("", "")
        t.add_row("[bold]fundings_per_instrument[/]", "")
        for ex, rate in state.perp_fundings_per_instrument.items():
            style = "green" if rate >= 0 else "red"
            t.add_row(f"  {ex}", f"[{style}]{rate*100:.4f}%[/]")
    layout["metrics"]["btc_metrics"]["col3"]["funding"].update(Panel(t, title="[bold blue]BTC FUNDING & OI[/]", border_style="blue"))

    # Liquidations panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="red")
    t.add_column("Value", justify="right")
    t.add_row("perp_liquidations_longsTotal", f"[green]{state.perp_liquidations_longsTotal:.4f}[/] BTC")
    t.add_row("perp_liquidations_shortsTotal", f"[red]{state.perp_liquidations_shortsTotal:.4f}[/] BTC")
    if state.perp_liquidations_longs:
        t.add_row("", "")
        t.add_row("[bold]liquidations_longs[/]", "")
        for level, qty in sorted(state.perp_liquidations_longs.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"[green]{qty:.4f}[/]")
    if state.perp_liquidations_shorts:
        t.add_row("", "")
        t.add_row("[bold]liquidations_shorts[/]", "")
        for level, qty in sorted(state.perp_liquidations_shorts.items(), key=lambda x: x[1], reverse=True)[:3]:
            t.add_row(f"  ${float(level):,.0f}", f"[red]{qty:.4f}[/]")
    layout["metrics"]["btc_metrics"]["col3"]["liquidations"].update(Panel(t, title="[bold red]BTC LIQUIDATIONS[/]", border_style="red"))

    # Positions panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="white")
    t.add_column("Value", justify="right")
    t.add_row("perp_TTA_ratio", f"{state.perp_TTA_ratio:.4f}")
    t.add_row("perp_TTP_ratio", f"{state.perp_TTP_ratio:.4f}")
    t.add_row("perp_GTA_ratio", f"{state.perp_GTA_ratio:.4f}")
    layout["metrics"]["btc_metrics"]["col3"]["positions"].update(Panel(t, title="[bold]BTC POSITIONS[/]", border_style="white"))

    # Stress Zones panel (predicted liquidation zones)
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Level", style="white")
    t.add_column("Strength", justify="right")

    # Engine stats
    stats = state.liq_engine_stats
    if stats:
        t.add_row("[dim]Minutes processed[/]", f"[dim]{stats.get('current_minute', 0)}[/]")
        t.add_row("[dim]Avg buy vol[/]", f"[dim]{stats.get('avg_buy_vol', 0):.4f}[/]")
        t.add_row("[dim]Avg sell vol[/]", f"[dim]{stats.get('avg_sell_vol', 0):.4f}[/]")
        t.add_row("", "")

    # Short liquidation zones (resistance - where shorts get liquidated)
    t.add_row("[bold red]SHORT LIQ ZONES[/]", "[bold red](resistance)[/]")
    if state.pred_liq_shorts_top:
        for price, strength in state.pred_liq_shorts_top:
            # Calculate distance from current price
            if state.btc_price > 0:
                dist_pct = ((price - state.btc_price) / state.btc_price) * 100
                t.add_row(
                    f"  ${price:,.0f} [dim]({dist_pct:+.2f}%)[/]",
                    f"[red]{strength:.2f}[/]"
                )
            else:
                t.add_row(f"  ${price:,.0f}", f"[red]{strength:.2f}[/]")
    else:
        t.add_row("  [dim]No zones yet[/]", "[dim]waiting...[/]")

    t.add_row("", "")

    # Long liquidation zones (support - where longs get liquidated)
    t.add_row("[bold green]LONG LIQ ZONES[/]", "[bold green](support)[/]")
    if state.pred_liq_longs_top:
        for price, strength in state.pred_liq_longs_top:
            # Calculate distance from current price
            if state.btc_price > 0:
                dist_pct = ((price - state.btc_price) / state.btc_price) * 100
                t.add_row(
                    f"  ${price:,.0f} [dim]({dist_pct:+.2f}%)[/]",
                    f"[green]{strength:.2f}[/]"
                )
            else:
                t.add_row(f"  ${price:,.0f}", f"[green]{strength:.2f}[/]")
    else:
        t.add_row("  [dim]No zones yet[/]", "[dim]waiting...[/]")

    t.add_row("", "")
    t.add_row("[dim]Zones: L={} S={}[/]".format(
        stats.get('long_zones_count', 0) if stats else 0,
        stats.get('short_zones_count', 0) if stats else 0
    ), "")

    layout["metrics"]["btc_metrics"]["col4"]["stress_zones"].update(
        Panel(t, title="[bold yellow]BTC STRESS ZONES[/]", border_style="yellow")
    )

    # Footer
    total_msgs = sum(state.msg_counts.values())
    streams = ", ".join(f"{k}:{v}" for k, v in sorted(state.msg_counts.items())[:5])
    layout["footer"].update(Panel(
        f"Total msgs: {total_msgs:,}  |  Streams: {streams}...  |  Ctrl+C to exit",
        box=box.MINIMAL
    ))

    return layout


def run_websocket_thread(connector, processor, stop_event):
    """Run websocket connections in a separate thread with its own event loop.

    Starts all exchange connectors (Binance, Bybit, OKX) via
    MultiExchangeConnector.connect_all().  Each sub-connector runs as an
    independent asyncio task so a failure in one exchange does not block
    the others.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        await connector.connect_all()  # starts Binance + Bybit + OKX

    try:
        while not stop_event.is_set():
            try:
                loop.run_until_complete(run())
            except Exception as e:
                if not stop_event.is_set():
                    logger.error(f"WebSocket thread exception: {e}", exc_info=True)
                    time.sleep(1)
    finally:
        loop.close()


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Full BTC Perpetual Metrics Viewer")
    parser.add_argument(
        "--api", action="store_true",
        help="Enable embedded API server (shares buffers directly, no disk sync needed)"
    )
    parser.add_argument(
        "--api-host", default="127.0.0.1",
        help="API server host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--api-port", type=int, default=8899,
        help="API server port (default: 8899)"
    )
    args = parser.parse_args()

    console = Console()
    console.print("\n[bold blue]Full BTC Perpetual Metrics Viewer[/]")
    console.print("Connecting to Binance futures websocket + REST pollers...\n")

    # Log rotation on startup
    _rotate_log_if_needed(LIQ_DEBUG_LOG, LOG_ROTATION_MAX_MB, LOG_ROTATION_MAX_AGE_HOURS)
    _rotate_log_if_needed(PLOT_FEED_FILE, LOG_ROTATION_MAX_MB, LOG_ROTATION_MAX_AGE_HOURS)

    processor = FullMetricsProcessor()
    connector = MultiExchangeConnector(processor.process_message)
    start_time = time.time()
    stop_event = threading.Event()

    # Start embedded API server if requested
    api_thread = None
    if args.api:
        try:
            from embedded_api import create_embedded_app, start_api_thread, HAS_FASTAPI
            if not HAS_FASTAPI:
                console.print("[yellow]WARNING: FastAPI not installed. Install with: pip install fastapi uvicorn[/]")
            else:
                console.print(f"[cyan]Starting embedded API server on {args.api_host}:{args.api_port}...[/]")
                app = create_embedded_app(
                    ob_buffer=processor.ob_heatmap_buffer,
                    snapshot_dir=POC_DIR,
                    engine_manager=processor.engine_manager,
                    oi_poller=processor.oi_poller
                )
                api_thread = start_api_thread(app, host=args.api_host, port=args.api_port)
                console.print("[green]Embedded API server started - orderbook buffer is shared directly![/]")
        except ImportError as e:
            console.print(f"[yellow]WARNING: Could not start embedded API: {e}[/]")

    # Start multi-exchange OI poller (Binance + Bybit + OKX, per symbol)
    console.print("[cyan]Starting multi-exchange OI poller (BTC, ETH, SOL)...[/]")
    processor.oi_poller.start()

    # Start REST poller for trader ratios (OI now handled by oi_poller)
    console.print("[cyan]Starting REST poller for trader ratios...[/]")
    processor.rest_poller.start()

    # Start websocket in background thread
    ws_thread = threading.Thread(
        target=run_websocket_thread,
        args=(connector, processor, stop_event),
        daemon=True
    )
    ws_thread.start()

    def _close_engines(proc):
        """Close all per-symbol heatmap engines (zone persistence, file handles)."""
        for sym_short in proc.engine_manager.get_all_symbols():
            try:
                eng = proc.engine_manager.get_engine(sym_short)
                if eng and eng.heatmap_v2:
                    eng.heatmap_v2.close()
                    logger.info("Closed heatmap engine for %s", sym_short)
            except Exception as e:
                logger.error("Failed to close engine for %s: %s", sym_short, e, exc_info=True)

    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutting down...[/]")
        stop_event.set()
        connector.stop()
        processor.oi_poller.stop()
        processor.rest_poller.stop()
        _close_engines(processor)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run display loop in main thread
    try:
        with Live(create_display(processor, start_time), console=console, refresh_per_second=4, screen=True) as live:
            while not stop_event.is_set():
                state = processor.get_state()
                if state.btc_price > 0:
                    connector.update_btc_price(state.btc_price)
                live.update(create_display(processor, start_time))
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        connector.stop()
        processor.oi_poller.stop()
        processor.rest_poller.stop()
        _close_engines(processor)

    if api_thread:
        console.print("[dim]API server thread will terminate automatically.[/]")
    console.print("[green]Goodbye![/]")


if __name__ == "__main__":
    main()
