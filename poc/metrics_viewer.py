#!/usr/bin/env python3
"""
BTC Perpetual Metrics Viewer - Proof of Concept
Real-time display of all BTC perpetual metrics from multiple exchanges.
"""

import asyncio
import json
import time
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import threading
import signal

# Add parent directories to path for imports
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
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Installing rich library...")
    os.system(f"{sys.executable} -m pip install rich --quiet")
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box

from ws_connectors import MultiExchangeConnector


@dataclass
class MetricsState:
    """Holds all computed metrics for display."""
    # Price info
    btc_price: float = 0.0
    price_change_1m: float = 0.0
    last_price_update: float = 0.0

    # Orderbook metrics (aggregated across exchanges)
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    bid_depth_10: float = 0.0  # Total bid depth within 0.1%
    ask_depth_10: float = 0.0  # Total ask depth within 0.1%
    imbalance: float = 0.0     # (bid - ask) / (bid + ask)

    # Per-exchange book snapshots
    exchange_books: Dict[str, Dict] = field(default_factory=dict)

    # Trade metrics (per minute window)
    buy_volume_1m: float = 0.0
    sell_volume_1m: float = 0.0
    total_volume_1m: float = 0.0
    num_buys_1m: int = 0
    num_sells_1m: int = 0
    vwap_1m: float = 0.0

    # Trade history for volume profile
    trade_history: deque = field(default_factory=lambda: deque(maxlen=1000))
    volume_profile: Dict[str, float] = field(default_factory=dict)

    # Liquidation metrics
    long_liquidations_1m: float = 0.0
    short_liquidations_1m: float = 0.0
    liquidation_count: int = 0
    last_liquidation: Optional[Dict] = None
    liquidation_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # Funding & OI
    funding_rates: Dict[str, float] = field(default_factory=dict)
    weighted_funding: float = 0.0
    open_interest: Dict[str, float] = field(default_factory=dict)
    total_oi: float = 0.0
    oi_change_1m: float = 0.0

    # Connection stats
    msg_counts: Dict[str, int] = field(default_factory=dict)
    last_msg_times: Dict[str, float] = field(default_factory=dict)
    connected_exchanges: List[str] = field(default_factory=list)

    # Timing
    window_start: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)


class MetricsProcessor:
    """Processes incoming websocket messages and computes metrics."""

    def __init__(self):
        self.state = MetricsState()
        self.lock = threading.Lock()
        self.level_size = 50.0  # Price bucket size for heatmaps

        # Per-exchange orderbooks
        self.orderbooks: Dict[str, Dict] = defaultdict(lambda: {"bids": {}, "asks": {}})

        # Rolling window data
        self.trades_window: deque = deque(maxlen=10000)
        self.liquidations_window: deque = deque(maxlen=1000)
        self.oi_history: deque = deque(maxlen=100)

        # Track minute boundaries
        self.current_minute = datetime.now().minute

    def process_message(self, msg_str: str):
        """Process incoming websocket message and update metrics."""
        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            return

        exchange = msg.get("exchange", "unknown")
        obj_type = msg.get("obj", "")
        data = msg.get("data", {})
        timestamp = msg.get("timestamp", time.time())

        with self.lock:
            # Track message counts
            key = f"{exchange}_{obj_type}"
            self.state.msg_counts[key] = self.state.msg_counts.get(key, 0) + 1
            self.state.last_msg_times[key] = timestamp

            if exchange not in self.state.connected_exchanges:
                self.state.connected_exchanges.append(exchange)

            # Route to appropriate handler
            if obj_type == "depth":
                self._process_depth(exchange, data, timestamp)
            elif obj_type == "trades":
                self._process_trades(exchange, data, timestamp)
            elif obj_type == "liquidations":
                self._process_liquidations(exchange, data, timestamp)
            elif obj_type in ["markprice", "funding", "oifunding"]:
                self._process_funding_oi(exchange, data, timestamp)

            self.state.last_update = time.time()
            self._check_minute_rollover()

    def _process_depth(self, exchange: str, data: dict, timestamp: float):
        """Process orderbook depth update."""
        book = self.orderbooks[exchange]

        # Parse Binance format
        bids = data.get("b") or data.get("bids") or []
        asks = data.get("a") or data.get("asks") or []

        # Update local book
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

        # Compute aggregate metrics
        self._update_book_metrics()

    def _update_book_metrics(self):
        """Recompute aggregate orderbook metrics."""
        all_bids = []
        all_asks = []

        for ex, book in self.orderbooks.items():
            for price, qty in book["bids"].items():
                all_bids.append((price, qty, ex))
            for price, qty in book["asks"].items():
                all_asks.append((price, qty, ex))

        if not all_bids or not all_asks:
            return

        # Best bid/ask
        best_bid = max(b[0] for b in all_bids)
        best_ask = min(a[0] for a in all_asks)

        self.state.best_bid = best_bid
        self.state.best_ask = best_ask
        self.state.spread = best_ask - best_bid

        # Update BTC price
        mid_price = (best_bid + best_ask) / 2
        if self.state.btc_price > 0:
            self.state.price_change_1m = mid_price - self.state.btc_price
        self.state.btc_price = mid_price
        self.state.last_price_update = time.time()

        # Depth within 0.1% of mid
        threshold = mid_price * 0.001
        bid_depth = sum(qty for p, qty, _ in all_bids if p >= mid_price - threshold)
        ask_depth = sum(qty for p, qty, _ in all_asks if p <= mid_price + threshold)

        self.state.bid_depth_10 = bid_depth
        self.state.ask_depth_10 = ask_depth

        if bid_depth + ask_depth > 0:
            self.state.imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)

        # Store per-exchange snapshots
        for ex, book in self.orderbooks.items():
            if book["bids"] and book["asks"]:
                ex_best_bid = max(book["bids"].keys())
                ex_best_ask = min(book["asks"].keys())
                self.state.exchange_books[ex] = {
                    "best_bid": ex_best_bid,
                    "best_ask": ex_best_ask,
                    "spread": ex_best_ask - ex_best_bid,
                    "bid_levels": len(book["bids"]),
                    "ask_levels": len(book["asks"]),
                }

    def _process_trades(self, exchange: str, data: dict, timestamp: float):
        """Process trade update."""
        # Parse Binance aggTrade format
        if "p" in data:
            price = float(data["p"])
            qty = float(data["q"])
            is_buyer_maker = data.get("m", False)
            side = "sell" if is_buyer_maker else "buy"
            trade_time = data.get("T", timestamp * 1000) / 1000
        else:
            return

        trade = {
            "exchange": exchange,
            "price": price,
            "qty": qty,
            "side": side,
            "time": trade_time
        }

        self.trades_window.append(trade)
        self.state.trade_history.append(trade)

        # Update running totals
        if side == "buy":
            self.state.buy_volume_1m += qty
            self.state.num_buys_1m += 1
        else:
            self.state.sell_volume_1m += qty
            self.state.num_sells_1m += 1

        self.state.total_volume_1m = self.state.buy_volume_1m + self.state.sell_volume_1m

        # Update volume profile
        level = int(price / self.level_size) * self.level_size
        level_key = str(level)
        self.state.volume_profile[level_key] = self.state.volume_profile.get(level_key, 0) + qty

        # VWAP
        if self.trades_window:
            total_value = sum(t["price"] * t["qty"] for t in self.trades_window)
            total_qty = sum(t["qty"] for t in self.trades_window)
            if total_qty > 0:
                self.state.vwap_1m = total_value / total_qty

    def _process_liquidations(self, exchange: str, data: dict, timestamp: float):
        """Process liquidation update."""
        # Parse Binance forceOrder format
        order = data.get("o", {})
        if not order:
            return

        symbol = order.get("s", "")
        if "BTC" not in symbol:
            return

        side = order.get("S", "").lower()
        price = float(order.get("p", 0))
        qty = float(order.get("q", 0))
        trade_time = order.get("T", timestamp * 1000) / 1000

        liq = {
            "exchange": exchange,
            "side": side,
            "price": price,
            "qty": qty,
            "value": price * qty,
            "time": trade_time
        }

        self.liquidations_window.append(liq)
        self.state.liquidation_history.append(liq)
        self.state.last_liquidation = liq
        self.state.liquidation_count += 1

        if side == "buy":  # Long liquidation
            self.state.long_liquidations_1m += qty
        else:  # Short liquidation
            self.state.short_liquidations_1m += qty

    def _process_funding_oi(self, exchange: str, data: dict, timestamp: float):
        """Process funding rate and open interest updates."""
        # Parse Binance markPrice format
        if "r" in data:  # Funding rate
            funding = float(data["r"])
            self.state.funding_rates[exchange] = funding

        if "fundingRate" in data:
            funding = float(data["fundingRate"])
            self.state.funding_rates[exchange] = funding

        # OI from ticker data (Bybit format)
        if "openInterestValue" in data:
            oi = float(data["openInterestValue"])
            self.state.open_interest[exchange] = oi

        # Recompute weighted funding
        if self.state.funding_rates and self.state.open_interest:
            total_oi = sum(self.state.open_interest.values())
            if total_oi > 0:
                weighted = sum(
                    self.state.funding_rates.get(ex, 0) * oi
                    for ex, oi in self.state.open_interest.items()
                )
                self.state.weighted_funding = weighted / total_oi
        elif self.state.funding_rates:
            # Simple average if no OI data
            self.state.weighted_funding = sum(self.state.funding_rates.values()) / len(self.state.funding_rates)

        self.state.total_oi = sum(self.state.open_interest.values())

    def _check_minute_rollover(self):
        """Check if we've crossed a minute boundary and reset counters."""
        current_minute = datetime.now().minute
        if current_minute != self.current_minute:
            # Reset minute counters
            self.state.buy_volume_1m = 0
            self.state.sell_volume_1m = 0
            self.state.total_volume_1m = 0
            self.state.num_buys_1m = 0
            self.state.num_sells_1m = 0
            self.state.long_liquidations_1m = 0
            self.state.short_liquidations_1m = 0
            self.state.volume_profile.clear()
            self.state.window_start = time.time()

            # Clear old trades from window
            cutoff = time.time() - 60
            while self.trades_window and self.trades_window[0]["time"] < cutoff:
                self.trades_window.popleft()
            while self.liquidations_window and self.liquidations_window[0]["time"] < cutoff:
                self.liquidations_window.popleft()

            self.current_minute = current_minute

    def get_state(self) -> MetricsState:
        """Get current metrics state (thread-safe copy)."""
        with self.lock:
            return self.state


class MetricsDisplay:
    """Terminal UI for displaying metrics."""

    def __init__(self, processor: MetricsProcessor):
        self.processor = processor
        self.console = Console()
        self.start_time = time.time()

    def create_layout(self) -> Layout:
        """Create the display layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1)
        )
        layout["left"].split_column(
            Layout(name="price", size=8),
            Layout(name="orderbook", ratio=1),
            Layout(name="trades", ratio=1)
        )
        layout["right"].split_column(
            Layout(name="liquidations", ratio=1),
            Layout(name="funding", ratio=1),
            Layout(name="connections", size=10)
        )
        return layout

    def generate_display(self) -> Layout:
        """Generate the full display."""
        state = self.processor.get_state()
        layout = self.create_layout()

        # Header
        runtime = time.time() - self.start_time
        header_text = Text()
        header_text.append(" BTC PERPETUAL METRICS VIEWER ", style="bold white on blue")
        header_text.append(f"  Runtime: {int(runtime//60)}m {int(runtime%60)}s", style="dim")
        header_text.append(f"  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style="dim")
        layout["header"].update(Panel(header_text, box=box.MINIMAL))

        # Price panel
        layout["price"].update(self._create_price_panel(state))

        # Orderbook panel
        layout["orderbook"].update(self._create_orderbook_panel(state))

        # Trades panel
        layout["trades"].update(self._create_trades_panel(state))

        # Liquidations panel
        layout["liquidations"].update(self._create_liquidations_panel(state))

        # Funding panel
        layout["funding"].update(self._create_funding_panel(state))

        # Connections panel
        layout["connections"].update(self._create_connections_panel(state))

        # Footer
        total_msgs = sum(state.msg_counts.values())
        footer_text = f"Total messages: {total_msgs:,}  |  Press Ctrl+C to exit"
        layout["footer"].update(Panel(footer_text, box=box.MINIMAL))

        return layout

    def _create_price_panel(self, state: MetricsState) -> Panel:
        """Create price information panel."""
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Label", style="dim")
        table.add_column("Value", justify="right")

        price_style = "green" if state.price_change_1m >= 0 else "red"
        change_pct = (state.price_change_1m / state.btc_price * 100) if state.btc_price > 0 else 0

        table.add_row("BTC Price", f"[bold {price_style}]${state.btc_price:,.2f}[/]")
        table.add_row("1m Change", f"[{price_style}]{state.price_change_1m:+.2f} ({change_pct:+.3f}%)[/]")
        table.add_row("Best Bid", f"${state.best_bid:,.2f}")
        table.add_row("Best Ask", f"${state.best_ask:,.2f}")
        table.add_row("Spread", f"${state.spread:.2f}")

        return Panel(table, title="[bold]Price Info[/]", border_style="blue")

    def _create_orderbook_panel(self, state: MetricsState) -> Panel:
        """Create orderbook metrics panel."""
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        imb_style = "green" if state.imbalance > 0 else "red"

        table.add_row("Bid Depth (0.1%)", f"[green]{state.bid_depth_10:.4f} BTC[/]")
        table.add_row("Ask Depth (0.1%)", f"[red]{state.ask_depth_10:.4f} BTC[/]")
        table.add_row("Imbalance", f"[{imb_style}]{state.imbalance:+.3f}[/]")

        # Per-exchange books
        table.add_row("", "")
        table.add_row("[bold]Exchange Books[/]", "")
        for ex, book in state.exchange_books.items():
            spread = book.get("spread", 0)
            table.add_row(f"  {ex}", f"spread: ${spread:.2f}")

        return Panel(table, title="[bold]Orderbook[/]", border_style="green")

    def _create_trades_panel(self, state: MetricsState) -> Panel:
        """Create trades metrics panel."""
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        buy_pct = (state.buy_volume_1m / state.total_volume_1m * 100) if state.total_volume_1m > 0 else 50

        table.add_row("Buy Volume (1m)", f"[green]{state.buy_volume_1m:.4f} BTC[/]")
        table.add_row("Sell Volume (1m)", f"[red]{state.sell_volume_1m:.4f} BTC[/]")
        table.add_row("Total Volume (1m)", f"{state.total_volume_1m:.4f} BTC")
        table.add_row("Buy/Sell Ratio", f"[{'green' if buy_pct > 50 else 'red'}]{buy_pct:.1f}% / {100-buy_pct:.1f}%[/]")
        table.add_row("# Buy Trades", f"[green]{state.num_buys_1m:,}[/]")
        table.add_row("# Sell Trades", f"[red]{state.num_sells_1m:,}[/]")
        table.add_row("VWAP (1m)", f"${state.vwap_1m:,.2f}")

        # Volume profile (top 5 levels)
        if state.volume_profile:
            table.add_row("", "")
            table.add_row("[bold]Volume Profile[/]", "")
            sorted_levels = sorted(state.volume_profile.items(), key=lambda x: x[1], reverse=True)[:5]
            for level, vol in sorted_levels:
                table.add_row(f"  ${float(level):,.0f}", f"{vol:.4f} BTC")

        return Panel(table, title="[bold]Trades (1m window)[/]", border_style="yellow")

    def _create_liquidations_panel(self, state: MetricsState) -> Panel:
        """Create liquidations panel."""
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        table.add_row("Long Liqs (1m)", f"[green]{state.long_liquidations_1m:.4f} BTC[/]")
        table.add_row("Short Liqs (1m)", f"[red]{state.short_liquidations_1m:.4f} BTC[/]")
        table.add_row("Total Liq Count", f"{state.liquidation_count:,}")

        # Last liquidation
        if state.last_liquidation:
            liq = state.last_liquidation
            side_style = "green" if liq["side"] == "buy" else "red"
            table.add_row("", "")
            table.add_row("[bold]Last Liquidation[/]", "")
            table.add_row("  Side", f"[{side_style}]{liq['side'].upper()}[/]")
            table.add_row("  Price", f"${liq['price']:,.2f}")
            table.add_row("  Size", f"{liq['qty']:.4f} BTC")
            table.add_row("  Value", f"${liq['value']:,.2f}")

        # Recent liquidations
        if state.liquidation_history:
            table.add_row("", "")
            table.add_row("[bold]Recent Liqs[/]", f"({len(state.liquidation_history)})")
            for liq in list(state.liquidation_history)[-3:]:
                side_char = "L" if liq["side"] == "buy" else "S"
                side_style = "green" if liq["side"] == "buy" else "red"
                table.add_row(f"  [{side_style}]{side_char}[/]", f"${liq['value']:,.0f}")

        return Panel(table, title="[bold]Liquidations[/]", border_style="red")

    def _create_funding_panel(self, state: MetricsState) -> Panel:
        """Create funding and OI panel."""
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        funding_style = "green" if state.weighted_funding >= 0 else "red"

        table.add_row("Weighted Funding", f"[{funding_style}]{state.weighted_funding*100:.4f}%[/]")
        table.add_row("Total OI", f"{state.total_oi:,.2f} USD")

        # Per-exchange funding
        if state.funding_rates:
            table.add_row("", "")
            table.add_row("[bold]Funding Rates[/]", "")
            for ex, rate in state.funding_rates.items():
                style = "green" if rate >= 0 else "red"
                table.add_row(f"  {ex}", f"[{style}]{rate*100:.4f}%[/]")

        # Per-exchange OI
        if state.open_interest:
            table.add_row("", "")
            table.add_row("[bold]Open Interest[/]", "")
            for ex, oi in state.open_interest.items():
                table.add_row(f"  {ex}", f"${oi:,.0f}")

        return Panel(table, title="[bold]Funding & OI[/]", border_style="magenta")

    def _create_connections_panel(self, state: MetricsState) -> Panel:
        """Create connection status panel."""
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Stream", style="dim")
        table.add_column("Msgs", justify="right")
        table.add_column("Last", justify="right")

        now = time.time()
        for key, count in sorted(state.msg_counts.items()):
            last_time = state.last_msg_times.get(key, 0)
            age = now - last_time if last_time > 0 else 999
            age_style = "green" if age < 5 else "yellow" if age < 30 else "red"
            table.add_row(key, f"{count:,}", f"[{age_style}]{age:.1f}s[/]")

        return Panel(table, title="[bold]Connections[/]", border_style="cyan")


async def main():
    """Main entry point."""
    console = Console()

    console.print("\n[bold blue]BTC Perpetual Metrics Viewer[/bold blue]")
    console.print("Starting up...\n")

    # Create processor and display
    processor = MetricsProcessor()
    display = MetricsDisplay(processor)

    # Create multi-exchange connector
    connector = MultiExchangeConnector(processor.process_message)

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutting down...[/yellow]")
        connector.stop()
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start websocket connections in background
    async def run_connectors():
        try:
            # Start with just Binance for simplicity
            await connector.connectors["binance"].connect_all()
        except Exception as e:
            console.print(f"[red]Connector error: {e}[/red]")

    connector_task = asyncio.create_task(run_connectors())

    # Run display loop
    try:
        with Live(display.generate_display(), console=console, refresh_per_second=2) as live:
            while not shutdown_event.is_set():
                # Update BTC price for connector
                state = processor.get_state()
                if state.btc_price > 0:
                    connector.update_btc_price(state.btc_price)

                live.update(display.generate_display())
                await asyncio.sleep(0.5)
    except Exception as e:
        console.print(f"[red]Display error: {e}[/red]")
    finally:
        connector.stop()
        connector_task.cancel()
        try:
            await connector_task
        except asyncio.CancelledError:
            pass

    console.print("[green]Goodbye![/green]")


if __name__ == "__main__":
    asyncio.run(main())
