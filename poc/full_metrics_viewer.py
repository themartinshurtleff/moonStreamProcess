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
from typing import Dict, Any, List
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


def _write_debug_log(entry: dict):
    """Append a debug entry to liq_debug.jsonl."""
    entry["ts"] = datetime.now().isoformat()
    try:
        with open(LIQ_DEBUG_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Don't crash on log failures


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
        # DEBUG #1: Log raw forceOrder data to diagnose why total_events=0
        order = data.get("o", {})
        symbol = order.get("s", "") if isinstance(order, dict) else ""
        _write_debug_log({
            "event": "raw_liquidation",
            "exchange": exchange,
            "symbol": symbol,
            "is_btc": "BTC" in symbol,
            "price": order.get("p") if isinstance(order, dict) else None,
            "qty": order.get("q") if isinstance(order, dict) else None
        })
        if not order or "BTC" not in order.get("s", ""):
            return

        # DEBUG: Wrap in try/except to catch silent failures
        try:
            side = order.get("S", "").lower()
            price = float(order.get("p", 0))
            qty = float(order.get("q", 0))

            _write_debug_log({
                "event": "btc_liq_passed_filter",
                "side": side,
                "price": price,
                "qty": qty
            })

            level = str(int(price / self.level_size) * self.level_size)

            if side == "buy":
                self.state.perp_liquidations_longsTotal += qty
                self.state.perp_liquidations_longs[level] = self.state.perp_liquidations_longs.get(level, 0) + qty
            else:
                self.state.perp_liquidations_shortsTotal += qty
                self.state.perp_liquidations_shorts[level] = self.state.perp_liquidations_shorts.get(level, 0) + qty

            # Send to calibrator for weight learning
            # Binance forceOrder: "S": "BUY" means shorts got liquidated (they had to buy back)
            # "S": "SELL" means longs got liquidated (they had to sell)
            calib_side = "short" if side == "buy" else "long"

            # Phase 1: Compute event-time src prices for better attribution
            # Priority: markPrice > mid > last > fallback
            mark_price = self.state.perp_markPrice if hasattr(self.state, 'perp_markPrice') else 0.0
            # Compute mid from orderbook if available
            mid_price = 0.0
            if self.state.perp_best_bid > 0 and self.state.perp_best_ask > 0:
                mid_price = (self.state.perp_best_bid + self.state.perp_best_ask) / 2
            last_price = self.state.btc_price  # Current BTC price from trades

            # DEBUG #2: Confirm routing to calibrator with key values
            _write_debug_log({
                "event": "to_calibrator",
                "side": calib_side,
                "price": price,
                "qty": qty,
                "mark_price": mark_price,
                "mid_price": mid_price,
                "last_price": last_price
            })
            self.calibrator.on_liquidation({
                'timestamp': time.time(),
                'symbol': 'BTC',
                'side': calib_side,
                'price': price,
                'qty': qty,
                # Phase 1: Event-time src prices
                'mark_price': mark_price,
                'mid_price': mid_price,
                'last_price': last_price
            })
        except Exception as e:
            _write_debug_log({
                "event": "liq_processing_error",
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

                # Get top predicted liquidation zones
                self.state.pred_liq_longs_top = self.liq_engine.top_levels("BTC", "long", 5)
                self.state.pred_liq_shorts_top = self.liq_engine.top_levels("BTC", "short", 5)
                self.state.liq_engine_stats = self.liq_engine.get_stats("BTC")

                # Send snapshot to calibrator for learning
                # ohlc4 = (open + high + low + close) / 4
                src = (self.state.perp_open + self.state.perp_high +
                       self.state.perp_low + self.state.perp_close) / 4
                config = self.liq_engine.get_leverage_config("BTC")
                self.calibrator.on_minute_snapshot(
                    symbol="BTC",
                    timestamp=time.time(),
                    src=src,
                    pred_longs=self.liq_engine.get_all_levels("BTC", "long"),
                    pred_shorts=self.liq_engine.get_all_levels("BTC", "short"),
                    ladder=config.ladder if config else [],
                    weights=config.weights if config else [],
                    buffer=self.liq_engine.buffer,
                    steps=self.liq_engine.steps
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
