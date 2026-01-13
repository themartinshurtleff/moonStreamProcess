"""
Liquidation Stress Zone Predictor Engine

Predicts likely liquidation zones based on volume patterns and leverage assumptions.
Inspired by aggr.trade liquidation heatmap methodology.
"""

import math
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

# Configure logging for debug output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class LiqBucket:
    """A single liquidation zone bucket."""
    price: float
    strength: float
    last_updated_minute: int


@dataclass
class SymbolState:
    """Per-symbol state for liquidation prediction."""
    # Rolling volume windows
    buy_vol_window: deque = field(default_factory=lambda: deque(maxlen=50))
    sell_vol_window: deque = field(default_factory=lambda: deque(maxlen=50))

    # Predicted liquidation zones: price -> LiqBucket
    long_zones: Dict[float, LiqBucket] = field(default_factory=dict)   # Support - where longs get liquidated
    short_zones: Dict[float, LiqBucket] = field(default_factory=dict)  # Resistance - where shorts get liquidated

    # Tracking
    current_minute: int = 0
    last_price: float = 0.0


class LiquidationStressEngine:
    """
    Predicts liquidation stress zones based on volume patterns.

    Uses leverage ladder to estimate where positions opened at current price
    would get liquidated, weighted by volume ratios.
    """

    # Configuration constants
    LEVERAGE_LADDER = [5, 10, 20, 50, 75, 100]
    DEFAULT_BUFFER = 0.002  # 0.2% buffer
    DEFAULT_FADE = 0.97     # Decay factor per minute
    DEFAULT_VOL_LENGTH = 50 # SMA window length
    DEFAULT_STEPS = 20.0    # Price bucket size for BTC
    EPS = 1e-10             # Epsilon to avoid division by zero
    LOG_SCALE = False       # Whether to use log scaling for ratios

    def __init__(
        self,
        steps: float = DEFAULT_STEPS,
        vol_length: int = DEFAULT_VOL_LENGTH,
        buffer: float = DEFAULT_BUFFER,
        fade: float = DEFAULT_FADE,
        log_scale: bool = LOG_SCALE,
        debug_symbol: str = "BTC"
    ):
        self.steps = steps
        self.vol_length = vol_length
        self.buffer = buffer
        self.fade = fade
        self.log_scale = log_scale
        self.debug_symbol = debug_symbol

        # Per-symbol state
        self.symbols: Dict[str, SymbolState] = {}

    def _get_or_create_state(self, symbol: str) -> SymbolState:
        """Get or create state for a symbol."""
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState()
            # Initialize with proper maxlen
            self.symbols[symbol].buy_vol_window = deque(maxlen=self.vol_length)
            self.symbols[symbol].sell_vol_window = deque(maxlen=self.vol_length)
        return self.symbols[symbol]

    def _compute_sma(self, window: deque) -> float:
        """Compute simple moving average of a window."""
        if not window:
            return 0.0
        return sum(window) / len(window)

    def _bucket_price(self, price: float, direction: str) -> float:
        """
        Bucket a price to the nearest step.
        direction: 'up' for ceil (short zones), 'down' for floor (long zones)
        """
        if direction == 'up':
            return math.floor(price / self.steps) * self.steps
        else:  # down
            return math.ceil(price / self.steps) * self.steps

    def update(self, symbol: str, minute_metrics: dict) -> None:
        """
        Update liquidation predictions for a symbol based on new minute metrics.

        Args:
            symbol: Symbol name (e.g., "BTC")
            minute_metrics: Dict with keys:
                - perp_open, perp_high, perp_low, perp_close
                - perp_buyVol, perp_sellVol
        """
        state = self._get_or_create_state(symbol)
        state.current_minute += 1

        # Extract metrics
        perp_open = minute_metrics.get('perp_open', 0)
        perp_high = minute_metrics.get('perp_high', 0)
        perp_low = minute_metrics.get('perp_low', 0)
        perp_close = minute_metrics.get('perp_close', 0)
        buy_vol = minute_metrics.get('perp_buyVol', 0)
        sell_vol = minute_metrics.get('perp_sellVol', 0)

        # Skip if no valid price data
        if perp_close <= 0:
            return

        # Step 1: Calculate ohlc4
        src = (perp_open + perp_high + perp_low + perp_close) / 4
        state.last_price = perp_close

        # Step 2: Update rolling windows and compute ratios
        state.buy_vol_window.append(buy_vol)
        state.sell_vol_window.append(sell_vol)

        avg_buy = self._compute_sma(state.buy_vol_window)
        avg_sell = self._compute_sma(state.sell_vol_window)

        buy_ratio = buy_vol / max(avg_buy, self.EPS)
        sell_ratio = sell_vol / max(avg_sell, self.EPS)

        # Optional log scaling
        if self.log_scale:
            buy_ratio = math.log(buy_ratio + 1)
            sell_ratio = math.log(sell_ratio + 1)

        # Step 3: Apply decay to all existing buckets
        self._apply_decay(state)

        # Step 4: Remove buckets that price has crossed
        self._remove_crossed_buckets(state, perp_high, perp_low)

        # Step 5: Calculate new liquidation zones for each leverage
        for lev in self.LEVERAGE_LADDER:
            # Offset calculation: (1/L) + buffer + (0.01/L)
            offset = (1.0 / lev) + self.buffer + (0.01 / lev)

            # Short liquidation zone (above price) - where shorts get stopped
            lp_up = src * (1 + offset)
            bucket_up = self._bucket_price(lp_up, 'up')

            # Long liquidation zone (below price) - where longs get stopped
            lp_dn = src * (1 - offset)
            bucket_dn = self._bucket_price(lp_dn, 'down')

            # Add to short zones (weighted by sell ratio - shorts being opened)
            if bucket_up not in state.short_zones:
                state.short_zones[bucket_up] = LiqBucket(
                    price=bucket_up,
                    strength=0.0,
                    last_updated_minute=state.current_minute
                )
            state.short_zones[bucket_up].strength += sell_ratio
            state.short_zones[bucket_up].last_updated_minute = state.current_minute

            # Add to long zones (weighted by buy ratio - longs being opened)
            if bucket_dn not in state.long_zones:
                state.long_zones[bucket_dn] = LiqBucket(
                    price=bucket_dn,
                    strength=0.0,
                    last_updated_minute=state.current_minute
                )
            state.long_zones[bucket_dn].strength += buy_ratio
            state.long_zones[bucket_dn].last_updated_minute = state.current_minute

        # Debug logging for specified symbol
        if symbol == self.debug_symbol:
            top_longs = self.top_levels(symbol, 'long', 3)
            top_shorts = self.top_levels(symbol, 'short', 3)
            logger.debug(
                f"[{symbol}] min={state.current_minute} src={src:.2f} "
                f"buyRatio={buy_ratio:.3f} sellRatio={sell_ratio:.3f} | "
                f"topLongs={[(p, f'{s:.2f}') for p, s in top_longs]} "
                f"topShorts={[(p, f'{s:.2f}') for p, s in top_shorts]}"
            )

    def _apply_decay(self, state: SymbolState) -> None:
        """Apply decay factor to all buckets not updated this minute."""
        # Decay long zones
        to_remove = []
        for price, bucket in state.long_zones.items():
            if bucket.last_updated_minute < state.current_minute:
                bucket.strength *= self.fade
                if bucket.strength < 0.01:  # Remove very weak buckets
                    to_remove.append(price)
        for price in to_remove:
            del state.long_zones[price]

        # Decay short zones
        to_remove = []
        for price, bucket in state.short_zones.items():
            if bucket.last_updated_minute < state.current_minute:
                bucket.strength *= self.fade
                if bucket.strength < 0.01:
                    to_remove.append(price)
        for price in to_remove:
            del state.short_zones[price]

    def _remove_crossed_buckets(
        self,
        state: SymbolState,
        high: float,
        low: float
    ) -> None:
        """Remove buckets that price has crossed (liquidations triggered)."""
        # Remove short zones where price went above (shorts got liquidated)
        to_remove = [p for p in state.short_zones if high >= p]
        for price in to_remove:
            del state.short_zones[price]

        # Remove long zones where price went below (longs got liquidated)
        to_remove = [p for p in state.long_zones if low <= p]
        for price in to_remove:
            del state.long_zones[price]

    def top_levels(
        self,
        symbol: str,
        side: str,
        n: int = 5
    ) -> List[Tuple[float, float]]:
        """
        Get top N strongest liquidation levels for a symbol.

        Args:
            symbol: Symbol name
            side: 'long' for long liquidation zones (support)
                  'short' for short liquidation zones (resistance)
            n: Number of levels to return

        Returns:
            List of (price, strength) tuples, sorted by strength descending
        """
        if symbol not in self.symbols:
            return []

        state = self.symbols[symbol]
        zones = state.long_zones if side == 'long' else state.short_zones

        # Sort by strength descending
        sorted_zones = sorted(
            [(bucket.price, bucket.strength) for bucket in zones.values()],
            key=lambda x: x[1],
            reverse=True
        )

        return sorted_zones[:n]

    def get_all_levels(
        self,
        symbol: str,
        side: str
    ) -> Dict[float, float]:
        """
        Get all liquidation levels as a dict for heatmap display.

        Returns:
            Dict mapping price -> strength
        """
        if symbol not in self.symbols:
            return {}

        state = self.symbols[symbol]
        zones = state.long_zones if side == 'long' else state.short_zones

        return {bucket.price: bucket.strength for bucket in zones.values()}

    def get_stats(self, symbol: str) -> dict:
        """Get debug stats for a symbol."""
        if symbol not in self.symbols:
            return {}

        state = self.symbols[symbol]
        return {
            'current_minute': state.current_minute,
            'last_price': state.last_price,
            'buy_vol_window_len': len(state.buy_vol_window),
            'sell_vol_window_len': len(state.sell_vol_window),
            'long_zones_count': len(state.long_zones),
            'short_zones_count': len(state.short_zones),
            'avg_buy_vol': self._compute_sma(state.buy_vol_window),
            'avg_sell_vol': self._compute_sma(state.sell_vol_window),
        }
