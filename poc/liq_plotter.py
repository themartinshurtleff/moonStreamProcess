#!/usr/bin/env python3
"""
Live BTC Candlestick Chart with Liquidation Zones

Displays a rolling 1-minute candlestick chart overlaid with stabilized
liquidation zones from ZoneTracker. Tails plot_feed.jsonl written by
full_metrics_viewer.py.

Usage:
    python -m poc.liq_plotter
    python poc/liq_plotter.py --window 180 --center

Requirements:
    - matplotlib (required)
    - mplfinance (optional, provides enhanced candlestick rendering)
"""

import json
import os
import sys
import time
import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Tuple, Dict, Optional, Deque

import matplotlib
matplotlib.use('TkAgg')  # Use TkAgg for better interactive performance
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D
from matplotlib.text import Text
import matplotlib.dates as mdates

# Optional mplfinance support
try:
    import mplfinance as mpf
    HAS_MPLFINANCE = True
except ImportError:
    HAS_MPLFINANCE = False

# Directory where this script lives
POC_DIR = os.path.dirname(os.path.abspath(__file__))
PLOT_FEED_FILE = os.path.join(POC_DIR, "plot_feed.jsonl")


@dataclass
class Candle:
    """A single OHLC candle."""
    timestamp: float  # Unix timestamp
    minute: int       # Minute key
    o: float
    h: float
    l: float
    c: float


@dataclass
class Zone:
    """A liquidation zone with metadata."""
    price: float
    strength: float
    age: int  # Minutes since entry
    side: str  # "long" or "short"


@dataclass
class PlotState:
    """Current state of the plot data."""
    candles: Deque[Candle] = field(default_factory=lambda: deque(maxlen=300))
    long_zones: List[Zone] = field(default_factory=list)
    short_zones: List[Zone] = field(default_factory=list)

    # Rolling strength buffer for normalization (last 60 minutes of all zone strengths)
    strength_buffer: Deque[float] = field(default_factory=lambda: deque(maxlen=600))

    # Track last minute to detect new data
    last_minute: int = -1

    def add_candle(self, candle: Candle):
        """Add a candle, avoiding duplicates."""
        if self.candles and candle.minute == self.candles[-1].minute:
            # Update existing candle
            self.candles[-1] = candle
        else:
            self.candles.append(candle)

    def update_zones(self, long_zones: List[Zone], short_zones: List[Zone]):
        """Update zones and add strengths to rolling buffer."""
        self.long_zones = long_zones
        self.short_zones = short_zones

        # Add all zone strengths to buffer for normalization
        for z in long_zones + short_zones:
            self.strength_buffer.append(z.strength)

    def get_strength_percentiles(self) -> Tuple[float, float]:
        """Compute p50 and p95 from rolling strength buffer."""
        if len(self.strength_buffer) < 2:
            return 0.0, 1.0  # Default fallback

        sorted_s = sorted(self.strength_buffer)
        n = len(sorted_s)
        p50 = sorted_s[int(n * 0.50)]
        p95 = sorted_s[min(int(n * 0.95), n - 1)]

        # Avoid division by zero
        if p95 <= p50:
            p95 = p50 + 0.01

        return p50, p95

    def normalize_strength(self, s: float) -> float:
        """Normalize strength to [0, 1] using rolling percentiles."""
        p50, p95 = self.get_strength_percentiles()
        s_norm = (s - p50) / (p95 - p50)
        return max(0.0, min(1.0, s_norm))


class LiveCandlestickChart:
    """
    Live candlestick chart with zone overlays.

    Uses artist reuse for efficient updates - no clearing/replotting.
    """

    # Colors
    COLOR_UP = '#26a69a'      # Green for bullish candles
    COLOR_DOWN = '#ef5350'    # Red for bearish candles
    COLOR_LONG_ZONE = '#26a69a'   # Green for long zones (support)
    COLOR_SHORT_ZONE = '#ef5350'  # Red for short zones (resistance)
    COLOR_BG = '#1e1e1e'      # Dark background
    COLOR_GRID = '#333333'    # Grid lines
    COLOR_TEXT = '#cccccc'    # Text color

    def __init__(self, window_minutes: int = 180, center_mode: bool = True):
        self.window_minutes = window_minutes
        self.center_mode = center_mode
        self.state = PlotState()

        # Set up the figure with dark theme
        plt.style.use('dark_background')
        self.fig, self.ax = plt.subplots(figsize=(14, 8))
        self.fig.patch.set_facecolor(self.COLOR_BG)
        self.ax.set_facecolor(self.COLOR_BG)

        # Configure axes
        self.ax.grid(True, alpha=0.3, color=self.COLOR_GRID)
        self.ax.set_ylabel('Price (USD)', color=self.COLOR_TEXT)
        self.ax.tick_params(colors=self.COLOR_TEXT)

        # Pre-allocate artists for candles (we'll create them lazily)
        self.candle_bodies: List[Rectangle] = []
        self.candle_wicks: List[Line2D] = []

        # Pre-allocate artists for zones
        self.zone_lines: Dict[str, Line2D] = {}  # key: "long_0", "short_2", etc.

        # HUD text elements
        self.hud_price: Optional[Text] = None
        self.hud_time: Optional[Text] = None
        self.hud_zones: Optional[Text] = None

        # Initialize HUD
        self._init_hud()

        # Track file position for tailing
        self._file_pos = 0
        self._last_check = 0

        # Animation interval
        self._update_interval = 1.0  # seconds

    def _init_hud(self):
        """Initialize HUD text elements."""
        # Price and time in top-left
        self.hud_price = self.ax.text(
            0.02, 0.98, '', transform=self.ax.transAxes,
            fontsize=14, fontweight='bold', color='white',
            verticalalignment='top', fontfamily='monospace'
        )
        self.hud_time = self.ax.text(
            0.02, 0.93, '', transform=self.ax.transAxes,
            fontsize=10, color=self.COLOR_TEXT,
            verticalalignment='top', fontfamily='monospace'
        )

        # Zone list in top-right
        self.hud_zones = self.ax.text(
            0.98, 0.98, '', transform=self.ax.transAxes,
            fontsize=9, color=self.COLOR_TEXT,
            verticalalignment='top', horizontalalignment='right',
            fontfamily='monospace'
        )

    def _read_new_entries(self) -> List[dict]:
        """Read new entries from plot_feed.jsonl."""
        entries = []

        if not os.path.exists(PLOT_FEED_FILE):
            return entries

        try:
            with open(PLOT_FEED_FILE, 'r') as f:
                f.seek(self._file_pos)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            entries.append(entry)
                        except json.JSONDecodeError:
                            pass
                self._file_pos = f.tell()
        except Exception:
            pass

        return entries

    def _process_entry(self, entry: dict):
        """Process a single plot feed entry."""
        ts = entry.get('ts', time.time())
        minute = entry.get('minute', int(ts // 60))

        # Create candle
        candle = Candle(
            timestamp=ts,
            minute=minute,
            o=entry.get('o', 0),
            h=entry.get('h', 0),
            l=entry.get('l', 0),
            c=entry.get('c', 0)
        )

        if candle.o > 0:  # Valid candle
            self.state.add_candle(candle)

        # Parse zones
        long_zones = []
        for z in entry.get('long_zones', []):
            if len(z) >= 3:
                long_zones.append(Zone(price=z[0], strength=z[1], age=z[2], side='long'))

        short_zones = []
        for z in entry.get('short_zones', []):
            if len(z) >= 3:
                short_zones.append(Zone(price=z[0], strength=z[1], age=z[2], side='short'))

        self.state.update_zones(long_zones, short_zones)
        self.state.last_minute = minute

    def _draw_candle(self, idx: int, candle: Candle, x_pos: float):
        """Draw or update a single candle."""
        is_bullish = candle.c >= candle.o
        color = self.COLOR_UP if is_bullish else self.COLOR_DOWN

        # Body dimensions
        body_bottom = min(candle.o, candle.c)
        body_height = abs(candle.c - candle.o)
        if body_height < 0.5:  # Minimum visibility
            body_height = 0.5

        width = 0.6  # Candle width in "minute" units

        # Ensure we have enough artists
        while len(self.candle_bodies) <= idx:
            # Create new body rectangle
            rect = Rectangle((0, 0), width, 1, facecolor=color, edgecolor=color, linewidth=0.5)
            self.ax.add_patch(rect)
            self.candle_bodies.append(rect)

            # Create new wick line
            line, = self.ax.plot([0, 0], [0, 0], color=color, linewidth=1)
            self.candle_wicks.append(line)

        # Update body
        body = self.candle_bodies[idx]
        body.set_xy((x_pos - width/2, body_bottom))
        body.set_width(width)
        body.set_height(body_height)
        body.set_facecolor(color)
        body.set_edgecolor(color)
        body.set_visible(True)

        # Update wick
        wick = self.candle_wicks[idx]
        wick.set_xdata([x_pos, x_pos])
        wick.set_ydata([candle.l, candle.h])
        wick.set_color(color)
        wick.set_visible(True)

    def _draw_zone(self, key: str, zone: Zone, x_min: float, x_max: float):
        """Draw or update a zone line."""
        # Compute alpha from normalized strength
        s_norm = self.state.normalize_strength(zone.strength)
        alpha = 0.15 + 0.75 * s_norm

        # Base color
        color = self.COLOR_LONG_ZONE if zone.side == 'long' else self.COLOR_SHORT_ZONE

        # Linewidth scales slightly with strength
        linewidth = 1.0 + 2.0 * s_norm

        if key not in self.zone_lines:
            # Create new line
            line, = self.ax.plot(
                [x_min, x_max], [zone.price, zone.price],
                color=color, alpha=alpha, linewidth=linewidth,
                linestyle='--' if s_norm < 0.5 else '-'
            )
            self.zone_lines[key] = line
        else:
            # Update existing line
            line = self.zone_lines[key]
            line.set_xdata([x_min, x_max])
            line.set_ydata([zone.price, zone.price])
            line.set_color(color)
            line.set_alpha(alpha)
            line.set_linewidth(linewidth)
            line.set_linestyle('--' if s_norm < 0.5 else '-')
            line.set_visible(True)

    def _hide_unused_artists(self, num_candles: int, zone_keys: set):
        """Hide artists that are no longer in use."""
        # Hide unused candles
        for i in range(num_candles, len(self.candle_bodies)):
            self.candle_bodies[i].set_visible(False)
            self.candle_wicks[i].set_visible(False)

        # Hide unused zones
        for key, line in self.zone_lines.items():
            if key not in zone_keys:
                line.set_visible(False)

    def _update_hud(self):
        """Update HUD text elements."""
        if not self.state.candles:
            return

        latest = self.state.candles[-1]

        # Price with change indicator
        if len(self.state.candles) >= 2:
            prev = self.state.candles[-2]
            change = latest.c - prev.c
            change_pct = (change / prev.c * 100) if prev.c > 0 else 0
            price_color = self.COLOR_UP if change >= 0 else self.COLOR_DOWN
            self.hud_price.set_text(f'BTC ${latest.c:,.2f} ({change_pct:+.2f}%)')
            self.hud_price.set_color(price_color)
        else:
            self.hud_price.set_text(f'BTC ${latest.c:,.2f}')
            self.hud_price.set_color('white')

        # Timestamp
        dt = datetime.fromtimestamp(latest.timestamp)
        self.hud_time.set_text(dt.strftime('%Y-%m-%d %H:%M:%S'))

        # Zone list
        zone_text_lines = ['ZONES:']

        # Short zones (resistance) - above price
        zone_text_lines.append('[SHORT (resistance)]')
        for z in self.state.short_zones[:5]:
            s_norm = self.state.normalize_strength(z.strength)
            zone_text_lines.append(f'  ${z.price:,.0f}  s={z.strength:.3f}  age={z.age}m')

        zone_text_lines.append('')

        # Long zones (support) - below price
        zone_text_lines.append('[LONG (support)]')
        for z in self.state.long_zones[:5]:
            s_norm = self.state.normalize_strength(z.strength)
            zone_text_lines.append(f'  ${z.price:,.0f}  s={z.strength:.3f}  age={z.age}m')

        self.hud_zones.set_text('\n'.join(zone_text_lines))

    def update(self):
        """Update the chart with new data."""
        # Read new entries
        entries = self._read_new_entries()
        for entry in entries:
            self._process_entry(entry)

        if not self.state.candles:
            return

        # Get visible candles based on window
        candles = list(self.state.candles)
        if len(candles) > self.window_minutes:
            candles = candles[-self.window_minutes:]

        # Compute x-axis limits
        if candles:
            latest_minute = candles[-1].minute

            if self.center_mode:
                # Center on latest candle
                half_window = self.window_minutes // 2
                x_min = latest_minute - half_window
                x_max = latest_minute + half_window
            else:
                # Latest candle at right edge
                x_min = latest_minute - self.window_minutes
                x_max = latest_minute + 5  # Small buffer on right

        # Draw candles
        for i, candle in enumerate(candles):
            x_pos = candle.minute
            self._draw_candle(i, candle, x_pos)

        # Draw zones
        zone_keys = set()

        for i, zone in enumerate(self.state.long_zones[:5]):
            key = f'long_{i}'
            zone_keys.add(key)
            self._draw_zone(key, zone, x_min, x_max)

        for i, zone in enumerate(self.state.short_zones[:5]):
            key = f'short_{i}'
            zone_keys.add(key)
            self._draw_zone(key, zone, x_min, x_max)

        # Hide unused artists
        self._hide_unused_artists(len(candles), zone_keys)

        # Update axis limits
        self.ax.set_xlim(x_min, x_max)

        # Auto-scale y-axis with padding
        if candles:
            all_prices = []
            for c in candles:
                all_prices.extend([c.h, c.l])

            # Include zone prices in range
            for z in self.state.long_zones[:5] + self.state.short_zones[:5]:
                all_prices.append(z.price)

            if all_prices:
                y_min = min(all_prices)
                y_max = max(all_prices)
                y_range = y_max - y_min
                padding = y_range * 0.05  # 5% padding
                self.ax.set_ylim(y_min - padding, y_max + padding)

        # Update HUD
        self._update_hud()

        # Update title
        self.ax.set_title(
            f'BTC/USDT 1m with Liquidation Zones (last {self.window_minutes}m)',
            color=self.COLOR_TEXT, fontsize=12
        )

        # Redraw
        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def run(self):
        """Run the live chart."""
        print(f"Starting live chart...")
        print(f"Reading from: {PLOT_FEED_FILE}")
        print(f"Window: {self.window_minutes} minutes, Center mode: {self.center_mode}")
        print("Close the window or Ctrl+C to exit.")

        # Enable interactive mode
        plt.ion()
        plt.show(block=False)

        try:
            while plt.fignum_exists(self.fig.number):
                self.update()
                plt.pause(self._update_interval)
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            plt.close(self.fig)


def load_historical_data(chart: LiveCandlestickChart, max_entries: int = 300):
    """Load historical data from plot_feed.jsonl on startup."""
    if not os.path.exists(PLOT_FEED_FILE):
        print(f"No existing data file found at {PLOT_FEED_FILE}")
        return

    print(f"Loading historical data from {PLOT_FEED_FILE}...")

    entries = []
    try:
        with open(PLOT_FEED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        pass

            # Remember file position for tailing
            chart._file_pos = f.tell()
    except Exception as e:
        print(f"Error loading historical data: {e}")
        return

    # Take last N entries
    if len(entries) > max_entries:
        entries = entries[-max_entries:]

    print(f"Loaded {len(entries)} historical entries")

    # Process each entry
    for entry in entries:
        chart._process_entry(entry)


def run(window_minutes: int = 180, center_mode: bool = True):
    """Main entry point for the plotter."""
    chart = LiveCandlestickChart(window_minutes=window_minutes, center_mode=center_mode)

    # Load historical data first
    load_historical_data(chart, max_entries=window_minutes)

    # Initial draw
    chart.update()

    # Run live updates
    chart.run()


def main():
    parser = argparse.ArgumentParser(
        description='Live BTC candlestick chart with liquidation zones'
    )
    parser.add_argument(
        '--window', '-w', type=int, default=180,
        help='Number of minutes to display (default: 180)'
    )
    parser.add_argument(
        '--center', '-c', action='store_true', default=True,
        help='Center viewport on latest candle (default: True)'
    )
    parser.add_argument(
        '--right', '-r', action='store_true',
        help='Position latest candle at right edge instead of center'
    )

    args = parser.parse_args()

    center_mode = not args.right

    print("=" * 60)
    print("BTC Live Candlestick Chart with Liquidation Zones")
    print("=" * 60)
    print()
    print("This chart reads from plot_feed.jsonl written by the viewer.")
    print("Make sure full_metrics_viewer.py is running to generate data.")
    print()

    run(window_minutes=args.window, center_mode=center_mode)


if __name__ == "__main__":
    main()
