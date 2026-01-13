#!/usr/bin/env python3
"""
Full BTC Perpetual Metrics Viewer
Displays ALL metrics that the moonStreamProcess library computes.

This lists every metric from the btcSynth.data output including:
- Orderbook heatmaps (books)
- Trade volume profiles
- Liquidation profiles
- OI/Funding data
- Position ratios (TTA/TTP/GTA)
- Voided/reinforced orders
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
    from rich.columns import Columns
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
    from rich.columns import Columns
    from rich import box

from ws_connectors import MultiExchangeConnector


# All BTC Perp metrics as defined in synthHub.py ratrive_data docstring
ALL_METRICS = {
    "price": {
        "btc_price": "Current BTC price",
        "perp_open": "Open price (1m)",
        "perp_close": "Close price (1m)",
        "perp_high": "High price (1m)",
        "perp_low": "Low price (1m)",
        "perp_Vola": "Price volatility (1m)",
    },
    "orderbook": {
        "perp_books": "Aggregated orderbook heatmap (price -> BTC)",
        "best_bid": "Best bid price",
        "best_ask": "Best ask price",
        "spread": "Bid-ask spread",
        "bid_depth": "Total bid depth (within 1%)",
        "ask_depth": "Total ask depth (within 1%)",
        "imbalance": "Order book imbalance",
    },
    "trades": {
        "perp_buyVol": "Buy volume (1m)",
        "perp_sellVol": "Sell volume (1m)",
        "perp_VolProfile": "Volume profile heatmap (price -> BTC)",
        "perp_buyVolProfile": "Buy volume profile heatmap",
        "perp_sellVolProfile": "Sell volume profile heatmap",
        "perp_numberBuyTrades": "Number of buy trades (1m)",
        "perp_numberSellTrades": "Number of sell trades (1m)",
        "perp_orderedBuyTrades": "Chronological buy trades",
        "perp_orderedSellTrades": "Chronological sell trades",
    },
    "adjustments": {
        "perp_voids": "Voided orders heatmap (canceled limit orders)",
        "perp_reinforces": "Reinforced orders heatmap (added limit orders)",
        "perp_totalVoids": "Total voided volume",
        "perp_totalReinforces": "Total reinforced volume",
        "perp_totalVoidsVola": "Void volatility",
        "perp_voidsDuration": "Median void duration by level",
        "perp_voidsDurationVola": "Void duration volatility",
        "perp_reinforcesDuration": "Median reinforce duration by level",
        "perp_reinforcesDurationVola": "Reinforce duration volatility",
    },
    "funding_oi": {
        "perp_weighted_funding": "OI-weighted funding rate",
        "perp_total_oi": "Total open interest",
        "perp_oi_change": "OI change (1m)",
        "perp_oi_Vola": "OI volatility",
        "perp_oi_increases": "OI increases heatmap",
        "perp_oi_increases_Vola": "OI increase volatility",
        "perp_oi_decreases": "OI decreases heatmap",
        "perp_oi_decreases_Vola": "OI decrease volatility",
        "perp_oi_turnover": "OI turnover heatmap",
        "perp_oi_turnover_Vola": "OI turnover volatility",
        "perp_oi_total": "Total OI by level",
        "perp_oi_total_Vola": "OI total volatility",
        "perp_orderedOIChanges": "Chronological OI changes",
        "perp_OIs_per_instrument": "OI per instrument",
        "perp_fundings_per_instrument": "Funding per instrument",
    },
    "liquidations": {
        "perp_liquidations_longsTotal": "Total long liquidations (1m)",
        "perp_liquidations_longs": "Long liquidations heatmap",
        "perp_liquidations_shortsTotal": "Total short liquidations (1m)",
        "perp_liquidations_shorts": "Short liquidations heatmap",
        "perp_liquidations_orderedLongs": "Chronological long liqs",
        "perp_liquidations_orderedShorts": "Chronological short liqs",
    },
    "positions": {
        "perp_TTA_ratio": "Top Traders Account ratio (Binance)",
        "perp_TTP_ratio": "Top Traders Position ratio (Binance)",
        "perp_GTA_ratio": "Global Traders Account ratio",
    },
}


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

    # Adjustments (voided/reinforced orders)
    perp_voids: Dict[str, float] = field(default_factory=dict)
    perp_reinforces: Dict[str, float] = field(default_factory=dict)
    perp_totalVoids: float = 0.0
    perp_totalReinforces: float = 0.0
    perp_totalVoidsVola: float = 0.0
    perp_voidsDuration: Dict[str, float] = field(default_factory=dict)
    perp_voidsDurationVola: Dict[str, float] = field(default_factory=dict)
    perp_reinforcesDuration: Dict[str, float] = field(default_factory=dict)
    perp_reinforcesDurationVola: Dict[str, float] = field(default_factory=dict)

    # Funding & OI
    perp_weighted_funding: float = 0.0
    perp_total_oi: float = 0.0
    perp_oi_change: float = 0.0
    perp_oi_Vola: float = 0.0
    perp_oi_increases: Dict[str, float] = field(default_factory=dict)
    perp_oi_increases_Vola: Dict[str, float] = field(default_factory=dict)
    perp_oi_decreases: Dict[str, float] = field(default_factory=dict)
    perp_oi_decreases_Vola: Dict[str, float] = field(default_factory=dict)
    perp_oi_turnover: Dict[str, float] = field(default_factory=dict)
    perp_oi_turnover_Vola: Dict[str, float] = field(default_factory=dict)
    perp_oi_total: Dict[str, float] = field(default_factory=dict)
    perp_oi_total_Vola: Dict[str, float] = field(default_factory=dict)
    perp_orderedOIChanges: List = field(default_factory=list)
    perp_OIs_per_instrument: Dict[str, float] = field(default_factory=dict)
    perp_fundings_per_instrument: Dict[str, float] = field(default_factory=dict)

    # Liquidations
    perp_liquidations_longsTotal: float = 0.0
    perp_liquidations_longs: Dict[str, float] = field(default_factory=dict)
    perp_liquidations_shortsTotal: float = 0.0
    perp_liquidations_shorts: Dict[str, float] = field(default_factory=dict)
    perp_liquidations_orderedLongs: List = field(default_factory=list)
    perp_liquidations_orderedShorts: List = field(default_factory=list)

    # Position ratios
    perp_TTA_ratio: float = 0.0
    perp_TTP_ratio: float = 0.0
    perp_GTA_ratio: float = 0.0

    # Connection stats
    msg_counts: Dict[str, int] = field(default_factory=dict)
    last_msg_times: Dict[str, float] = field(default_factory=dict)
    exchanges: List[str] = field(default_factory=list)


class FullMetricsProcessor:
    """Process all metrics as defined in the moonStreamProcess library."""

    def __init__(self, level_size: float = 50.0):
        self.state = FullMetricsState()
        self.lock = threading.Lock()
        self.level_size = level_size

        # Raw data storage
        self.orderbooks: Dict[str, Dict] = defaultdict(lambda: {"bids": {}, "asks": {}})
        self.trades_buffer: deque = deque(maxlen=10000)
        self.liquidations_buffer: deque = deque(maxlen=1000)

        # Previous values for diff calculation
        self.prev_books: Dict[str, Dict] = {}
        self.prev_oi: Dict[str, float] = {}

        # Minute tracking
        self.current_minute = datetime.now().minute
        self.minute_start = time.time()
        self.prices_this_minute: List[float] = []

    def process_message(self, msg_str: str):
        """Process incoming message and update all metrics."""
        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            return

        exchange = msg.get("exchange", "unknown")
        obj_type = msg.get("obj", "")
        data = msg.get("data", {})
        ts = msg.get("timestamp", time.time())

        with self.lock:
            # Track messages
            key = f"{exchange}_{obj_type}"
            self.state.msg_counts[key] = self.state.msg_counts.get(key, 0) + 1
            self.state.last_msg_times[key] = ts

            if exchange not in self.state.exchanges:
                self.state.exchanges.append(exchange)

            # Route to handler
            if obj_type == "depth":
                self._process_depth(exchange, data)
            elif obj_type == "trades":
                self._process_trades(exchange, data)
            elif obj_type == "liquidations":
                self._process_liquidations(exchange, data)
            elif obj_type in ["markprice", "funding", "oifunding"]:
                self._process_funding_oi(exchange, data)

            self.state.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._check_minute_rollover()

    def _process_depth(self, exchange: str, data: dict):
        """Process depth/orderbook data."""
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

        # Update aggregate metrics
        self._compute_book_metrics()

    def _compute_book_metrics(self):
        """Compute all orderbook-related metrics."""
        all_bids = []
        all_asks = []

        for ex, book in self.orderbooks.items():
            for price, qty in book["bids"].items():
                all_bids.append((price, qty))
            for price, qty in book["asks"].items():
                all_asks.append((price, qty))

        if not all_bids or not all_asks:
            return

        # Best bid/ask
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

        import numpy as np
        if len(self.prices_this_minute) > 1:
            self.state.perp_Vola = float(np.std(self.prices_this_minute))

        # Depth within 1%
        threshold = mid * 0.01
        bid_depth = sum(qty for p, qty in all_bids if p >= mid - threshold)
        ask_depth = sum(qty for p, qty in all_asks if p <= mid + threshold)

        self.state.bid_depth = bid_depth
        self.state.ask_depth = ask_depth
        if bid_depth + ask_depth > 0:
            self.state.imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)

        # Build heatmap
        books_heatmap = {}
        for price, qty in all_bids + all_asks:
            level = int(price / self.level_size) * self.level_size
            key = str(level)
            books_heatmap[key] = books_heatmap.get(key, 0) + qty
        self.state.perp_books = books_heatmap

        # Compute voids/reinforces (simplified - compares to previous snapshot)
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
        """Process trade data."""
        if "p" not in data:
            return

        price = float(data["p"])
        qty = float(data["q"])
        is_buyer_maker = data.get("m", False)
        side = "sell" if is_buyer_maker else "buy"

        trade = {"price": price, "qty": qty, "side": side, "time": time.time()}
        self.trades_buffer.append(trade)

        # Update volume metrics
        level = str(int(price / self.level_size) * self.level_size)

        if side == "buy":
            self.state.perp_buyVol += qty
            self.state.perp_numberBuyTrades += 1
            self.state.perp_buyVolProfile[level] = self.state.perp_buyVolProfile.get(level, 0) + qty
            self.state.perp_orderedBuyTrades.append(qty)
        else:
            self.state.perp_sellVol += qty
            self.state.perp_numberSellTrades += 1
            self.state.perp_sellVolProfile[level] = self.state.perp_sellVolProfile.get(level, 0) + qty
            self.state.perp_orderedSellTrades.append(qty)

        self.state.perp_VolProfile[level] = self.state.perp_VolProfile.get(level, 0) + qty

    def _process_liquidations(self, exchange: str, data: dict):
        """Process liquidation data."""
        order = data.get("o", {})
        if not order:
            return

        symbol = order.get("s", "")
        if "BTC" not in symbol:
            return

        side = order.get("S", "").lower()
        price = float(order.get("p", 0))
        qty = float(order.get("q", 0))

        level = str(int(price / self.level_size) * self.level_size)

        liq = {"side": side, "price": price, "qty": qty, "level": level}
        self.liquidations_buffer.append(liq)

        if side == "buy":  # Long liquidation
            self.state.perp_liquidations_longsTotal += qty
            self.state.perp_liquidations_longs[level] = self.state.perp_liquidations_longs.get(level, 0) + qty
            self.state.perp_liquidations_orderedLongs.append(qty)
        else:  # Short liquidation
            self.state.perp_liquidations_shortsTotal += qty
            self.state.perp_liquidations_shorts[level] = self.state.perp_liquidations_shorts.get(level, 0) + qty
            self.state.perp_liquidations_orderedShorts.append(qty)

    def _process_funding_oi(self, exchange: str, data: dict):
        """Process funding rate and open interest."""
        if "r" in data:
            funding = float(data["r"])
            self.state.perp_fundings_per_instrument[exchange] = funding

            # Compute weighted funding
            fundings = list(self.state.perp_fundings_per_instrument.values())
            if fundings:
                self.state.perp_weighted_funding = sum(fundings) / len(fundings)

        # Would need additional OI streams for full OI tracking
        # This is a simplified version

    def _check_minute_rollover(self):
        """Reset minute-based metrics on minute boundary."""
        current_minute = datetime.now().minute
        if current_minute != self.current_minute:
            # Reset all minute-based metrics
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
            self.state.perp_liquidations_orderedLongs = []
            self.state.perp_liquidations_orderedShorts = []
            self.state.perp_open = self.state.btc_price
            self.prices_this_minute = [self.state.btc_price] if self.state.btc_price > 0 else []

            self.current_minute = current_minute
            self.minute_start = time.time()

    def get_state(self) -> FullMetricsState:
        with self.lock:
            return self.state


class FullMetricsDisplay:
    """Display all metrics in terminal UI."""

    def __init__(self, processor: FullMetricsProcessor):
        self.processor = processor
        self.console = Console()
        self.start_time = time.time()
        self.scroll_offset = 0

    def generate_display(self) -> Layout:
        """Generate full display layout."""
        state = self.processor.get_state()
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="metrics", ratio=1),
            Layout(name="footer", size=3)
        )

        # Header
        header = self._create_header(state)
        layout["header"].update(header)

        # Main metrics area - split into columns
        layout["metrics"].split_row(
            Layout(name="col1", ratio=1),
            Layout(name="col2", ratio=1),
            Layout(name="col3", ratio=1)
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

        layout["metrics"]["col1"]["price"].update(self._create_price_panel(state))
        layout["metrics"]["col1"]["orderbook"].update(self._create_orderbook_panel(state))
        layout["metrics"]["col2"]["trades"].update(self._create_trades_panel(state))
        layout["metrics"]["col2"]["adjustments"].update(self._create_adjustments_panel(state))
        layout["metrics"]["col3"]["funding"].update(self._create_funding_panel(state))
        layout["metrics"]["col3"]["liquidations"].update(self._create_liquidations_panel(state))
        layout["metrics"]["col3"]["positions"].update(self._create_positions_panel(state))

        # Footer
        layout["footer"].update(self._create_footer(state))

        return layout

    def _create_header(self, state: FullMetricsState) -> Panel:
        runtime = int(time.time() - self.start_time)
        text = Text()
        text.append("  BTC PERPETUAL - ALL METRICS  ", style="bold white on blue")
        text.append(f"\n  Exchanges: {', '.join(state.exchanges) or 'connecting...'}", style="dim")
        text.append(f"  |  Runtime: {runtime//60}m {runtime%60}s", style="dim")
        text.append(f"  |  {state.timestamp}", style="dim")
        return Panel(text, box=box.DOUBLE)

    def _create_price_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        price_style = "green" if state.perp_close >= state.perp_open else "red"

        table.add_row("btc_price", f"[bold {price_style}]${state.btc_price:,.2f}[/]")
        table.add_row("perp_open", f"${state.perp_open:,.2f}")
        table.add_row("perp_close", f"${state.perp_close:,.2f}")
        table.add_row("perp_high", f"[green]${state.perp_high:,.2f}[/]")
        table.add_row("perp_low", f"[red]${state.perp_low:,.2f}[/]")
        table.add_row("perp_Vola", f"{state.perp_Vola:.4f}")

        return Panel(table, title="[bold cyan]PRICE METRICS[/]", border_style="cyan")

    def _create_orderbook_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="green")
        table.add_column("Value", justify="right")

        table.add_row("best_bid", f"${state.best_bid:,.2f}")
        table.add_row("best_ask", f"${state.best_ask:,.2f}")
        table.add_row("spread", f"${state.spread:.2f}")
        table.add_row("bid_depth (1%)", f"{state.bid_depth:.4f} BTC")
        table.add_row("ask_depth (1%)", f"{state.ask_depth:.4f} BTC")

        imb_style = "green" if state.imbalance > 0 else "red"
        table.add_row("imbalance", f"[{imb_style}]{state.imbalance:+.4f}[/]")

        # Heatmap preview
        if state.perp_books:
            table.add_row("", "")
            table.add_row("[bold]perp_books[/] (top 3)", "")
            sorted_books = sorted(state.perp_books.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, qty in sorted_books:
                table.add_row(f"  ${float(level):,.0f}", f"{qty:.4f}")

        return Panel(table, title="[bold green]ORDERBOOK[/]", border_style="green")

    def _create_trades_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="yellow")
        table.add_column("Value", justify="right")

        table.add_row("perp_buyVol", f"[green]{state.perp_buyVol:.4f}[/] BTC")
        table.add_row("perp_sellVol", f"[red]{state.perp_sellVol:.4f}[/] BTC")
        table.add_row("perp_numberBuyTrades", f"[green]{state.perp_numberBuyTrades:,}[/]")
        table.add_row("perp_numberSellTrades", f"[red]{state.perp_numberSellTrades:,}[/]")

        # Volume profile preview
        if state.perp_VolProfile:
            table.add_row("", "")
            table.add_row("[bold]perp_VolProfile[/] (top 3)", "")
            sorted_vol = sorted(state.perp_VolProfile.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, vol in sorted_vol:
                table.add_row(f"  ${float(level):,.0f}", f"{vol:.4f}")

        table.add_row("", "")
        table.add_row("orderedBuyTrades", f"{len(state.perp_orderedBuyTrades)} items")
        table.add_row("orderedSellTrades", f"{len(state.perp_orderedSellTrades)} items")

        return Panel(table, title="[bold yellow]TRADES[/]", border_style="yellow")

    def _create_adjustments_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="magenta")
        table.add_column("Value", justify="right")

        table.add_row("perp_totalVoids", f"[red]{state.perp_totalVoids:.4f}[/] BTC")
        table.add_row("perp_totalReinforces", f"[green]{state.perp_totalReinforces:.4f}[/] BTC")
        table.add_row("perp_totalVoidsVola", f"{state.perp_totalVoidsVola:.6f}")

        # Voids heatmap preview
        if state.perp_voids:
            table.add_row("", "")
            table.add_row("[bold]perp_voids[/] (top 3)", "")
            sorted_voids = sorted(state.perp_voids.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, qty in sorted_voids:
                table.add_row(f"  ${float(level):,.0f}", f"[red]{qty:.4f}[/]")

        # Reinforces heatmap preview
        if state.perp_reinforces:
            table.add_row("", "")
            table.add_row("[bold]perp_reinforces[/] (top 3)", "")
            sorted_reinf = sorted(state.perp_reinforces.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, qty in sorted_reinf:
                table.add_row(f"  ${float(level):,.0f}", f"[green]{qty:.4f}[/]")

        return Panel(table, title="[bold magenta]ADJUSTMENTS[/]", border_style="magenta")

    def _create_funding_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="blue")
        table.add_column("Value", justify="right")

        fund_style = "green" if state.perp_weighted_funding >= 0 else "red"
        table.add_row("perp_weighted_funding", f"[{fund_style}]{state.perp_weighted_funding*100:.4f}%[/]")
        table.add_row("perp_total_oi", f"{state.perp_total_oi:,.0f} USD")
        table.add_row("perp_oi_change", f"{state.perp_oi_change:,.2f}")
        table.add_row("perp_oi_Vola", f"{state.perp_oi_Vola:.4f}")

        # Per-instrument funding
        if state.perp_fundings_per_instrument:
            table.add_row("", "")
            table.add_row("[bold]fundings_per_instrument[/]", "")
            for ex, rate in state.perp_fundings_per_instrument.items():
                style = "green" if rate >= 0 else "red"
                table.add_row(f"  {ex}", f"[{style}]{rate*100:.4f}%[/]")

        # Per-instrument OI
        if state.perp_OIs_per_instrument:
            table.add_row("", "")
            table.add_row("[bold]OIs_per_instrument[/]", "")
            for ex, oi in state.perp_OIs_per_instrument.items():
                table.add_row(f"  {ex}", f"${oi:,.0f}")

        return Panel(table, title="[bold blue]FUNDING & OI[/]", border_style="blue")

    def _create_liquidations_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="red")
        table.add_column("Value", justify="right")

        table.add_row("perp_liquidations_longsTotal", f"[green]{state.perp_liquidations_longsTotal:.4f}[/] BTC")
        table.add_row("perp_liquidations_shortsTotal", f"[red]{state.perp_liquidations_shortsTotal:.4f}[/] BTC")

        # Liquidations heatmap preview
        if state.perp_liquidations_longs:
            table.add_row("", "")
            table.add_row("[bold]liquidations_longs[/] (top 3)", "")
            sorted_longs = sorted(state.perp_liquidations_longs.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, qty in sorted_longs:
                table.add_row(f"  ${float(level):,.0f}", f"[green]{qty:.4f}[/]")

        if state.perp_liquidations_shorts:
            table.add_row("", "")
            table.add_row("[bold]liquidations_shorts[/] (top 3)", "")
            sorted_shorts = sorted(state.perp_liquidations_shorts.items(), key=lambda x: x[1], reverse=True)[:3]
            for level, qty in sorted_shorts:
                table.add_row(f"  ${float(level):,.0f}", f"[red]{qty:.4f}[/]")

        table.add_row("", "")
        table.add_row("orderedLongs", f"{len(state.perp_liquidations_orderedLongs)} items")
        table.add_row("orderedShorts", f"{len(state.perp_liquidations_orderedShorts)} items")

        return Panel(table, title="[bold red]LIQUIDATIONS[/]", border_style="red")

    def _create_positions_panel(self, state: FullMetricsState) -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Metric", style="white")
        table.add_column("Value", justify="right")

        table.add_row("perp_TTA_ratio", f"{state.perp_TTA_ratio:.4f}")
        table.add_row("perp_TTP_ratio", f"{state.perp_TTP_ratio:.4f}")
        table.add_row("perp_GTA_ratio", f"{state.perp_GTA_ratio:.4f}")

        return Panel(table, title="[bold]POSITIONS[/]", border_style="white")

    def _create_footer(self, state: FullMetricsState) -> Panel:
        total_msgs = sum(state.msg_counts.values())
        streams = ", ".join(f"{k}:{v}" for k, v in sorted(state.msg_counts.items())[:5])
        return Panel(
            f"Total msgs: {total_msgs:,}  |  Streams: {streams}...  |  Ctrl+C to exit",
            box=box.MINIMAL
        )


async def main():
    console = Console()
    console.print("\n[bold blue]Full BTC Perpetual Metrics Viewer[/]")
    console.print("Displaying ALL metrics from moonStreamProcess library\n")

    processor = FullMetricsProcessor()
    display = FullMetricsDisplay(processor)
    connector = MultiExchangeConnector(processor.process_message)

    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutting down...[/]")
        connector.stop()
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    async def run_connectors():
        try:
            await connector.connectors["binance"].connect_all()
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")

    connector_task = asyncio.create_task(run_connectors())

    try:
        with Live(display.generate_display(), console=console, refresh_per_second=2) as live:
            while not shutdown_event.is_set():
                state = processor.get_state()
                if state.btc_price > 0:
                    connector.update_btc_price(state.btc_price)
                live.update(display.generate_display())
                await asyncio.sleep(0.5)
    finally:
        connector.stop()
        connector_task.cancel()
        try:
            await connector_task
        except asyncio.CancelledError:
            pass

    console.print("[green]Goodbye![/]")


if __name__ == "__main__":
    asyncio.run(main())
