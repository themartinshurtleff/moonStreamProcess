#!/usr/bin/env python3
"""
Full BTC Perpetual Metrics Viewer
Displays ALL metrics that the moonStreamProcess library computes.
"""

import asyncio
import json
import time
import sys
import os
from datetime import datetime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
import threading
import signal

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
from liq_engine import LiquidationStressEngine
from liq_calibrator import LiquidationCalibrator
from rest_pollers import BinanceRESTPollerThread, PollerState

# Directory where this script lives - use for log file paths
POC_DIR = os.path.dirname(os.path.abspath(__file__))

# Debug log for liquidation event flow diagnostics
LIQ_DEBUG_LOG = os.path.join(POC_DIR, "liq_debug.jsonl")

# Plot feed for real-time chart (OHLC + zones per minute)
PLOT_FEED_FILE = os.path.join(POC_DIR, "plot_feed.jsonl")

# Log rotation thresholds
LOG_ROTATION_MAX_MB = 200
LOG_ROTATION_MAX_AGE_HOURS = 24


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

    # Predicted liquidation stress zones (from LiquidationStressEngine)
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

        # Liquidation stress zone predictor
        # Set debug_enabled=True and debug_log_file to see detailed output
        self.liq_engine = LiquidationStressEngine(
            steps=20.0,        # $20 price buckets for BTC
            vol_length=50,     # 50-minute SMA for volume normalization
            buffer=0.002,      # 0.2% buffer
            fade=0.97,         # Decay factor
            debug_symbol="BTC",
            debug_enabled=True,
            debug_log_file=os.path.join(POC_DIR, "liq_engine_debug.log")
        )

        # Self-calibrating system for leverage weights
        # Uses real Binance forceOrder liquidations as feedback
        self.calibrator = LiquidationCalibrator(
            symbol="BTC",
            steps=20.0,
            window_minutes=15,          # Calibrate every 15 minutes
            hit_bucket_tolerance=5,     # Initial tolerance (will auto-calibrate)
            learning_rate=0.10,         # Weight adjustment rate
            closer_level_gamma=0.35,    # Prior for higher leverage
            enable_buffer_tuning=True,  # Auto-tune buffer based on misses
            enable_tolerance_tuning=True,  # Auto-tune hit tolerance
            log_file=os.path.join(POC_DIR, "liq_calibrator.jsonl"),
            weights_file=os.path.join(POC_DIR, "liq_calibrator_weights.json"),
            log_events=True,            # Log individual liquidation events
            on_weights_updated=self._on_calibrator_weights_updated
        )

        # Track if we've applied persisted weights
        self._applied_persisted_weights = False

        # REST poller for OI and trader ratios (direct HTTPS)
        self.rest_poller = BinanceRESTPollerThread(
            symbols=["BTCUSDT"],
            oi_interval=10.0,      # Poll OI every 10s
            ratio_interval=60.0,   # Poll ratios every 60s
            on_update=self._on_rest_poller_update,
            price_getter=self._get_price_for_symbol,
            debug=True
        )
        self._prev_rest_oi: float = 0.0

        # Event sanity validator - log first 50 liquidation events for verification
        self._sanity_check_count = 0
        self._sanity_check_limit = 50

        # ZoneTracker for UI stability (hysteresis + TTL)
        self.zone_tracker = ZoneTracker(
            steps=20.0,              # Match liq_engine bucket size
            max_zones=5,             # Display top 5 zones per side
            enter_margin=0.07,       # New zone must beat weakest by 7%
            exit_margin=0.05,        # Exit if below best non-displayed by 5%
            ttl_minutes=10,          # Keep zones for 10 minutes minimum
            peak_drop_threshold=0.70,  # Exit early if drops >70% from peak
            min_strength_threshold=0.01,  # Minimum strength to display
            debug_log=True           # Log displayed zones per minute
        )

    def _get_price_for_symbol(self, symbol: str) -> float:
        """Get current price for a symbol (used by REST poller for OI conversion)."""
        # For BTCUSDT, return current BTC price
        if "BTC" in symbol.upper():
            return self.state.btc_price
        # For other symbols, could add price tracking later
        return 0.0

    def _on_calibrator_weights_updated(self, symbol: str, new_weights: list, new_buffer: float):
        """Callback when calibrator updates weights."""
        # Push updated weights to the liq engine
        self.liq_engine.update_leverage_weights(symbol, new_weights, new_buffer)

    def _on_rest_poller_update(self, poller_state: PollerState):
        """Callback when REST poller has new OI/ratio data."""
        with self.lock:
            # Update OI from REST (more reliable than websocket for Binance)
            if poller_state.total_oi > 0:
                # Compute OI change
                if self._prev_rest_oi > 0:
                    self.state.perp_oi_change = poller_state.total_oi - self._prev_rest_oi
                self._prev_rest_oi = poller_state.total_oi
                self.state.perp_total_oi = poller_state.total_oi

                # Update per-instrument OI
                for symbol, oi_data in poller_state.oi_data.items():
                    self.state.perp_OIs_per_instrument[f"binance_{symbol}"] = oi_data.oi_value

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
        data = msg.get("data", {})

        with self.lock:
            key = f"{exchange}_{obj_type}"
            self.state.msg_counts[key] = self.state.msg_counts.get(key, 0) + 1

            if exchange not in self.state.exchanges:
                self.state.exchanges.append(exchange)

            if obj_type == "depth":
                self._process_depth(exchange, data)
            elif obj_type == "trades":
                self._process_trades(exchange, data)
            elif obj_type == "liquidations":
                self._process_liquidations(exchange, data)
            elif obj_type in ["markprice", "funding"]:
                self._process_funding(exchange, data)
            elif obj_type in ["oi", "oifunding"]:
                self._process_oi(exchange, data)

            self.state.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._check_minute_rollover()

    def _process_depth(self, exchange: str, data: dict):
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
        for price, size in all_bids:
            if low_bound <= price <= high_bound:
                bucket = round(price / steps) * steps
                notional = price * size
                depth_band[bucket] = depth_band.get(bucket, 0.0) + notional

        # Process asks - notional = price * size
        for price, size in all_asks:
            if low_bound <= price <= high_bound:
                bucket = round(price / steps) * steps
                notional = price * size
                depth_band[bucket] = depth_band.get(bucket, 0.0) + notional

        return depth_band

    def _process_trades(self, exchange: str, data: dict):
        if "p" not in data:
            return

        price = float(data["p"])
        qty = float(data["q"])
        is_buyer_maker = data.get("m", False)
        side = "sell" if is_buyer_maker else "buy"

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

    def _process_liquidations(self, exchange: str, data: dict):
        order = data.get("o", {})
        if not order or "BTC" not in order.get("s", ""):
            return

        try:
            # Extract raw values
            raw_symbol = order.get("s", "")
            raw_side = order.get("S", "")
            side = raw_side.lower()
            price = float(order.get("p", 0))
            qty = float(order.get("q", 0))
            notional = price * qty

            level = str(int(price / self.level_size) * self.level_size)

            if side == "buy":
                self.state.perp_liquidations_longsTotal += qty
                self.state.perp_liquidations_longs[level] = self.state.perp_liquidations_longs.get(level, 0) + qty
            else:
                self.state.perp_liquidations_shortsTotal += qty
                self.state.perp_liquidations_shorts[level] = self.state.perp_liquidations_shorts.get(level, 0) + qty

            # Binance forceOrder: "S": "BUY" means shorts got liquidated (they had to buy back)
            # "S": "SELL" means longs got liquidated (they had to sell)
            calib_side = "short" if side == "buy" else "long"

            # Phase 1: Compute event-time src prices for better attribution
            # Priority: markPrice > mid > last > fallback
            mark_price = self.state.perp_markPrice if hasattr(self.state, 'perp_markPrice') else 0.0
            mid_price = 0.0
            if self.state.best_bid > 0 and self.state.best_ask > 0:
                mid_price = (self.state.best_bid + self.state.best_ask) / 2
            last_price = self.state.btc_price

            # Determine event_src_price and src_source (same logic as calibrator)
            if mark_price > 0:
                event_src_price = mark_price
                src_source = "mark"
            elif mid_price > 0:
                event_src_price = mid_price
                src_source = "mid"
            elif last_price > 0:
                event_src_price = last_price
                src_source = "last"
            else:
                event_src_price = 0.0
                src_source = "none"

            # Event sanity validator - log first N events for verification
            if self._sanity_check_count < self._sanity_check_limit:
                self._sanity_check_count += 1
                sanity_failures = []

                # Run assertions
                if not (price > 0):
                    sanity_failures.append(f"price<=0: {price}")
                if not (qty > 0):
                    sanity_failures.append(f"qty<=0: {qty}")
                if not (event_src_price > 0):
                    sanity_failures.append(f"event_src_price<=0: {event_src_price} (src_source={src_source})")
                if calib_side not in {"long", "short"}:
                    sanity_failures.append(f"invalid calib_side: {calib_side}")

                if sanity_failures:
                    # Log failure
                    _write_debug_log({
                        "type": "event_sanity_fail",
                        "event_num": self._sanity_check_count,
                        "failures": sanity_failures,
                        "raw_symbol": raw_symbol,
                        "raw_side": raw_side,
                        "calib_side": calib_side,
                        "price": price,
                        "qty": qty,
                        "notional": notional,
                        "mark_price": mark_price,
                        "mid_price": mid_price,
                        "last_price": last_price,
                        "event_src_price": event_src_price,
                        "src_source": src_source
                    })
                else:
                    # Log successful sanity check
                    _write_debug_log({
                        "type": "event_sanity_ok",
                        "event_num": self._sanity_check_count,
                        "raw_symbol": raw_symbol,
                        "raw_side": raw_side,
                        "calib_side": calib_side,
                        "price": price,
                        "qty": qty,
                        "notional": notional,
                        "mark_price": mark_price,
                        "mid_price": mid_price,
                        "last_price": last_price,
                        "event_src_price": event_src_price,
                        "src_source": src_source
                    })

            # Send to calibrator
            self.calibrator.on_liquidation({
                'timestamp': time.time(),
                'symbol': 'BTC',
                'side': calib_side,
                'price': price,
                'qty': qty,
                'mark_price': mark_price,
                'mid_price': mid_price,
                'last_price': last_price
            })
        except Exception as e:
            _write_debug_log({
                "type": "liq_processing_error",
                "error": str(e),
                "error_type": type(e).__name__
            })

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
            # Update liquidation stress engine BEFORE resetting minute metrics
            if self.state.perp_close > 0:
                minute_metrics = {
                    'perp_open': self.state.perp_open,
                    'perp_high': self.state.perp_high,
                    'perp_low': self.state.perp_low,
                    'perp_close': self.state.perp_close,
                    'perp_buyVol': self.state.perp_buyVol,
                    'perp_sellVol': self.state.perp_sellVol,
                }
                self.liq_engine.update("BTC", minute_metrics)

                # Apply persisted weights from calibrator on first update
                if not self._applied_persisted_weights:
                    persisted = self.calibrator.get_persisted_weights()
                    if persisted:
                        self.liq_engine.update_leverage_weights(
                            "BTC",
                            persisted['weights'],
                            persisted['buffer']
                        )
                        print(f"Loaded persisted weights (calibration #{persisted['calibration_count']})")
                    self._applied_persisted_weights = True

                # Get top predicted liquidation zones from engine (raw)
                raw_longs = self.liq_engine.top_levels("BTC", "long", 10)  # Get more for tracker
                raw_shorts = self.liq_engine.top_levels("BTC", "short", 10)
                self.state.liq_engine_stats = self.liq_engine.get_stats("BTC")

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
                    "ts": time.time(),
                    "minute": current_minute_ts,
                    "o": round(self.state.perp_open, 2),
                    "h": round(self.state.perp_high, 2),
                    "l": round(self.state.perp_low, 2),
                    "c": round(self.state.perp_close, 2),
                    "long_zones": [(round(p, 2), round(s, 4), a) for p, s, a in long_zones_with_age[:5]],
                    "short_zones": [(round(p, 2), round(s, 4), a) for p, s, a in short_zones_with_age[:5]]
                })

                # Send snapshot to calibrator for learning
                # ohlc4 = (open + high + low + close) / 4
                src = (self.state.perp_open + self.state.perp_high +
                       self.state.perp_low + self.state.perp_close) / 4
                config = self.liq_engine.get_leverage_config("BTC")

                # Build depth band for stress calculation
                depth_band = self._build_depth_band(src, self.liq_engine.steps)

                self.calibrator.on_minute_snapshot(
                    symbol="BTC",
                    timestamp=time.time(),
                    src=src,
                    pred_longs=self.liq_engine.get_all_levels("BTC", "long"),
                    pred_shorts=self.liq_engine.get_all_levels("BTC", "short"),
                    ladder=config.ladder if config else [],
                    weights=config.weights if config else [],
                    buffer=self.liq_engine.buffer,
                    steps=self.liq_engine.steps,
                    # Market data for approach stress calculation
                    high=self.state.perp_high,
                    low=self.state.perp_low,
                    close=self.state.perp_close,
                    perp_buy_vol=self.state.perp_buyVol,
                    perp_sell_vol=self.state.perp_sellVol,
                    perp_oi_change=self.state.perp_oi_change,
                    depth_band=depth_band
                )

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
            self.current_minute = current_minute

    def get_state(self) -> FullMetricsState:
        with self.lock:
            # Return a copy to avoid threading issues
            import copy
            return copy.deepcopy(self.state)


def create_display(processor: FullMetricsProcessor, start_time: float) -> Layout:
    """Generate the display layout."""
    state = processor.get_state()
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="metrics", ratio=1),
        Layout(name="footer", size=3)
    )

    # Header
    runtime = int(time.time() - start_time)
    header = Text()
    header.append("  BTC PERPETUAL - ALL METRICS  ", style="bold white on blue")
    header.append(f"\n  Exchanges: {', '.join(state.exchanges) or 'connecting...'}", style="dim")
    header.append(f"  |  Runtime: {runtime//60}m {runtime%60}s", style="dim")
    header.append(f"  |  {state.timestamp}", style="dim")
    layout["header"].update(Panel(header, box=box.DOUBLE))

    # Metrics columns
    layout["metrics"].split_row(
        Layout(name="col1", ratio=1),
        Layout(name="col2", ratio=1),
        Layout(name="col3", ratio=1),
        Layout(name="col4", ratio=1)
    )

    layout["metrics"]["col1"].split_column(
        Layout(name="price", ratio=1),
        Layout(name="orderbook", ratio=1)
    )
    layout["metrics"]["col2"].split_column(
        Layout(name="trades", ratio=1),
        Layout(name="adjustments", ratio=1)
    )
    layout["metrics"]["col3"].split_column(
        Layout(name="funding", ratio=1),
        Layout(name="liquidations", ratio=1),
        Layout(name="positions", size=8)
    )
    layout["metrics"]["col4"].split_column(
        Layout(name="stress_zones", ratio=1)
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
    layout["metrics"]["col1"]["price"].update(Panel(t, title="[bold cyan]PRICE[/]", border_style="cyan"))

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
    layout["metrics"]["col1"]["orderbook"].update(Panel(t, title="[bold green]ORDERBOOK[/]", border_style="green"))

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
    layout["metrics"]["col2"]["trades"].update(Panel(t, title="[bold yellow]TRADES[/]", border_style="yellow"))

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
    layout["metrics"]["col2"]["adjustments"].update(Panel(t, title="[bold magenta]ADJUSTMENTS[/]", border_style="magenta"))

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
    layout["metrics"]["col3"]["funding"].update(Panel(t, title="[bold blue]FUNDING & OI[/]", border_style="blue"))

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
    layout["metrics"]["col3"]["liquidations"].update(Panel(t, title="[bold red]LIQUIDATIONS[/]", border_style="red"))

    # Positions panel
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric", style="white")
    t.add_column("Value", justify="right")
    t.add_row("perp_TTA_ratio", f"{state.perp_TTA_ratio:.4f}")
    t.add_row("perp_TTP_ratio", f"{state.perp_TTP_ratio:.4f}")
    t.add_row("perp_GTA_ratio", f"{state.perp_GTA_ratio:.4f}")
    layout["metrics"]["col3"]["positions"].update(Panel(t, title="[bold]POSITIONS[/]", border_style="white"))

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

    layout["metrics"]["col4"]["stress_zones"].update(
        Panel(t, title="[bold yellow]STRESS ZONES[/]", border_style="yellow")
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
    """Run websocket connections in a separate thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        connector.binance.running = True
        await connector.binance.connect_all()

    try:
        while not stop_event.is_set():
            try:
                loop.run_until_complete(run())
            except Exception as e:
                if not stop_event.is_set():
                    time.sleep(1)
    finally:
        loop.close()


def main():
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

    # Start REST poller for OI and trader ratios
    console.print("[cyan]Starting REST poller for OI and trader ratios...[/]")
    processor.rest_poller.start()

    # Start websocket in background thread
    ws_thread = threading.Thread(
        target=run_websocket_thread,
        args=(connector, processor, stop_event),
        daemon=True
    )
    ws_thread.start()

    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutting down...[/]")
        stop_event.set()
        connector.stop()
        processor.rest_poller.stop()

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
        processor.rest_poller.stop()

    console.print("[green]Goodbye![/]")


if __name__ == "__main__":
    main()
