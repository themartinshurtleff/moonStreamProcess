"""
Liquidation Stress Zone Predictor Engine

Predicts likely liquidation zones based on volume patterns and leverage assumptions.
Inspired by aggr.trade liquidation heatmap methodology.

Uses per-symbol leverage ladders with weights for realistic liquidation zone estimation.
"""

import math
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from leverage_config import get_leverage_config, get_weighted_ladder, LeverageConfig

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

    # Cached leverage config for this symbol
    leverage_config: Optional[LeverageConfig] = None


class LiquidationStressEngine:
    """
    Predicts liquidation stress zones based on volume patterns.

    Uses per-symbol leverage ladders with weights to estimate where positions
    opened at current price would get liquidated, weighted by volume ratios
    and leverage popularity.
    """

    # Configuration constants
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
        debug_symbol: str = "BTC",
        debug_enabled: bool = False
    ):
        self.steps = steps
        self.vol_length = vol_length
        self.buffer = buffer
        self.fade = fade
        self.log_scale = log_scale
        self.debug_symbol = debug_symbol
        self.debug_enabled = debug_enabled

        # Per-symbol state
        self.symbols: Dict[str, SymbolState] = {}

        # Log initialization
        if self.debug_enabled:
            logger.setLevel(logging.DEBUG)
            logger.debug(f"LiquidationStressEngine initialized with debug_symbol={debug_symbol}")

    def _get_or_create_state(self, symbol: str) -> SymbolState:
        """Get or create state for a symbol."""
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState()
            # Initialize with proper maxlen
            self.symbols[symbol].buy_vol_window = deque(maxlen=self.vol_length)
            self.symbols[symbol].sell_vol_window = deque(maxlen=self.vol_length)
            # Cache leverage config for this symbol
            self.symbols[symbol].leverage_config = get_leverage_config(symbol)

            # Debug: print ladder config on first use
            if self.debug_enabled and symbol == self.debug_symbol:
                config = self.symbols[symbol].leverage_config
                logger.debug(f"[{symbol}] Leverage ladder initialized:")
                logger.debug(f"  Ladder: {config.ladder}")
                logger.debug(f"  Weights (normalized): {[f'{w:.4f}' for w in config.weights]}")
                for lev, weight in config.get_weighted_ladder():
                    logger.debug(f"    {lev:3d}x -> weight {weight:.4f}")

        return self.symbols[symbol]

    def _compute_sma(self, window: deque) -> float:
        """Compute simple moving average of a window."""
        if not window:
            return 0.0
        return sum(window) / len(window)

    def _bucket_price(self, price: float, direction: str) -> float:
        """
        Bucket a price to the nearest step.
        direction: 'up' for floor (short zones), 'down' for ceil (long zones)
        """
        if direction == 'up':
            return math.floor(price / self.steps) * self.steps
        else:  # down
            return math.ceil(price / self.steps) * self.steps

    def update(self, symbol: str, minute_metrics: dict) -> None:
        """
        Update liquidation predictions for a symbol based on new minute metrics.

        Args:
            symbol: Symbol name (e.g., "BTC", "ETH", "SOL")
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

        # Step 5: Calculate new liquidation zones for each leverage with weights
        # Get the weighted ladder for this symbol
        weighted_ladder = state.leverage_config.get_weighted_ladder()

        # Track projected buckets for debug output
        debug_projected_longs = []
        debug_projected_shorts = []

        for lev, weight in weighted_ladder:
            # Offset calculation: (1/L) + buffer + (0.01/L)
            offset = (1.0 / lev) + self.buffer + (0.01 / lev)

            # Short liquidation zone (above price) - where shorts get stopped
            lp_up = src * (1 + offset)
            bucket_up = self._bucket_price(lp_up, 'up')

            # Long liquidation zone (below price) - where longs get stopped
            lp_dn = src * (1 - offset)
            bucket_dn = self._bucket_price(lp_dn, 'down')

            # Calculate weighted strength contribution
            # strength = ratio * leverage_weight
            short_strength_add = sell_ratio * weight
            long_strength_add = buy_ratio * weight

            # Add to short zones (weighted by sell ratio * leverage weight)
            if bucket_up not in state.short_zones:
                state.short_zones[bucket_up] = LiqBucket(
                    price=bucket_up,
                    strength=0.0,
                    last_updated_minute=state.current_minute
                )
            state.short_zones[bucket_up].strength += short_strength_add
            state.short_zones[bucket_up].last_updated_minute = state.current_minute

            # Add to long zones (weighted by buy ratio * leverage weight)
            if bucket_dn not in state.long_zones:
                state.long_zones[bucket_dn] = LiqBucket(
                    price=bucket_dn,
                    strength=0.0,
                    last_updated_minute=state.current_minute
                )
            state.long_zones[bucket_dn].strength += long_strength_add
            state.long_zones[bucket_dn].last_updated_minute = state.current_minute

            # Track for debug
            if self.debug_enabled and symbol == self.debug_symbol:
                debug_projected_shorts.append((lev, bucket_up, short_strength_add, weight))
                debug_projected_longs.append((lev, bucket_dn, long_strength_add, weight))

        # Debug logging for specified symbol
        if self.debug_enabled and symbol == self.debug_symbol:
            self._log_debug_update(
                symbol, state, src, buy_ratio, sell_ratio,
                debug_projected_longs, debug_projected_shorts
            )

    def _log_debug_update(
        self,
        symbol: str,
        state: SymbolState,
        src: float,
        buy_ratio: float,
        sell_ratio: float,
        projected_longs: List[Tuple],
        projected_shorts: List[Tuple]
    ):
        """Log detailed debug output for an update."""
        logger.debug("=" * 80)
        logger.debug(f"[{symbol}] MINUTE {state.current_minute} UPDATE")
        logger.debug("=" * 80)
        logger.debug(f"  src (ohlc4) = ${src:,.2f}")
        logger.debug(f"  buyRatio = {buy_ratio:.4f}, sellRatio = {sell_ratio:.4f}")
        logger.debug("")

        # Show projections by leverage
        logger.debug("  Projections by leverage:")
        logger.debug("  " + "-" * 70)
        logger.debug(f"  {'Lev':>5} | {'Weight':>8} | {'Long Zone':>12} | {'Short Zone':>12} | {'L Str':>8} | {'S Str':>8}")
        logger.debug("  " + "-" * 70)
        for i, (lev, bucket_dn, long_str, weight) in enumerate(projected_longs):
            _, bucket_up, short_str, _ = projected_shorts[i]
            logger.debug(
                f"  {lev:>5}x | {weight:>8.4f} | ${bucket_dn:>10,.0f} | ${bucket_up:>10,.0f} | {long_str:>8.4f} | {short_str:>8.4f}"
            )
        logger.debug("")

        # Show top 5 strongest zones
        top_longs = self.top_levels(symbol, 'long', 5)
        top_shorts = self.top_levels(symbol, 'short', 5)

        logger.debug("  Top 5 LONG liquidation zones (support):")
        for price, strength in top_longs:
            dist = ((price - src) / src) * 100
            logger.debug(f"    ${price:>10,.0f} ({dist:>+6.2f}%) -> strength {strength:.4f}")

        logger.debug("")
        logger.debug("  Top 5 SHORT liquidation zones (resistance):")
        for price, strength in top_shorts:
            dist = ((price - src) / src) * 100
            logger.debug(f"    ${price:>10,.0f} ({dist:>+6.2f}%) -> strength {strength:.4f}")

        logger.debug("=" * 80)

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
        config = state.leverage_config

        return {
            'current_minute': state.current_minute,
            'last_price': state.last_price,
            'buy_vol_window_len': len(state.buy_vol_window),
            'sell_vol_window_len': len(state.sell_vol_window),
            'long_zones_count': len(state.long_zones),
            'short_zones_count': len(state.short_zones),
            'avg_buy_vol': self._compute_sma(state.buy_vol_window),
            'avg_sell_vol': self._compute_sma(state.sell_vol_window),
            'leverage_ladder': config.ladder if config else [],
            'leverage_weights': [f'{w:.4f}' for w in config.weights] if config else [],
        }

    def get_leverage_config(self, symbol: str) -> Optional[LeverageConfig]:
        """Get the leverage config for a symbol."""
        if symbol in self.symbols:
            return self.symbols[symbol].leverage_config
        return get_leverage_config(symbol)
