"""
Leverage configuration for liquidation stress zone prediction.

Per-symbol leverage ladders and weights for realistic liquidation zone estimation.
Weights represent the relative likelihood of positions at each leverage level.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class LeverageConfig:
    """Configuration for a single symbol's leverage ladder."""
    ladder: List[int]       # Available leverage levels
    weights: List[float]    # Weights for each level (will be normalized)

    def __post_init__(self):
        """Validate and normalize weights."""
        if len(self.ladder) != len(self.weights):
            raise ValueError(
                f"Ladder length ({len(self.ladder)}) must match weights length ({len(self.weights)})"
            )
        # Normalize weights to sum to 1.0
        total = sum(self.weights)
        if total > 0:
            self.weights = [w / total for w in self.weights]

    def get_weighted_ladder(self) -> List[Tuple[int, float]]:
        """Return list of (leverage, normalized_weight) tuples."""
        return list(zip(self.ladder, self.weights))


# =============================================================================
# Per-Symbol Leverage Configurations
# =============================================================================
# Weights represent the estimated distribution of open positions across
# leverage levels. Higher weights = more positions expected at that leverage.
#
# These are based on typical retail leverage usage patterns:
# - Most volume at 5-20x (safer, more common)
# - Decreasing usage at higher leverage (riskier, margin requirements)
# - Minimal usage at max leverage (high risk of liquidation)
# =============================================================================

# BTC perpetual leverage config (Binance max 125x, extended to 250x for tighter attribution)
# Extended to cover dist_pct ~0.4-0.6% regime which requires 200x-250x
BTC_LEVERAGE_LADDER = [5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250]
# Initial weights: lower for ultra-high leverage (150x, 200x, 250x)
# Redistribute mass from 75x/100x/125x to cover new levels with mild decay
BTC_LEVERAGE_WEIGHTS = [1.0, 1.0, 0.9, 0.7, 0.35, 0.14, 0.08, 0.05, 0.03, 0.015, 0.008]

# ETH perpetual leverage config (Binance max 125x)
ETH_LEVERAGE_LADDER = [5, 10, 20, 25, 50, 75, 100, 125]
ETH_LEVERAGE_WEIGHTS = [1.0, 1.0, 0.9, 0.7, 0.35, 0.18, 0.10, 0.07]

# SOL perpetual leverage config (Binance max 75x)
SOL_LEVERAGE_LADDER = [5, 10, 20, 25, 50, 75]
SOL_LEVERAGE_WEIGHTS = [1.0, 1.0, 0.9, 0.7, 0.35, 0.18]

# Default config for symbols not explicitly defined
DEFAULT_LEVERAGE_LADDER = [5, 10, 20, 50, 75, 100]
DEFAULT_LEVERAGE_WEIGHTS = [1.0, 1.0, 0.9, 0.5, 0.25, 0.10]


class LeverageConfigRegistry:
    """
    Registry for per-symbol leverage configurations.

    Provides lookup with fallback to defaults for undefined symbols.
    Prepared for future per-exchange overrides.
    """

    def __init__(self):
        self._configs: Dict[str, LeverageConfig] = {}
        self._exchange_overrides: Dict[str, Dict[str, LeverageConfig]] = {}
        self._load_defaults()

    def _load_defaults(self):
        """Load default symbol configurations."""
        self._configs["BTC"] = LeverageConfig(
            ladder=BTC_LEVERAGE_LADDER.copy(),
            weights=BTC_LEVERAGE_WEIGHTS.copy()
        )
        self._configs["ETH"] = LeverageConfig(
            ladder=ETH_LEVERAGE_LADDER.copy(),
            weights=ETH_LEVERAGE_WEIGHTS.copy()
        )
        self._configs["SOL"] = LeverageConfig(
            ladder=SOL_LEVERAGE_LADDER.copy(),
            weights=SOL_LEVERAGE_WEIGHTS.copy()
        )

    def get_config(self, symbol: str, exchange: str = None) -> LeverageConfig:
        """
        Get leverage config for a symbol, optionally with exchange-specific override.

        Args:
            symbol: Symbol name (e.g., "BTC", "ETH", "SOL")
            exchange: Optional exchange name for future per-exchange configs

        Returns:
            LeverageConfig for the symbol
        """
        # Check for exchange-specific override (future use)
        if exchange and exchange in self._exchange_overrides:
            if symbol in self._exchange_overrides[exchange]:
                return self._exchange_overrides[exchange][symbol]

        # Check for symbol-specific config
        if symbol in self._configs:
            return self._configs[symbol]

        # Return default config
        return LeverageConfig(
            ladder=DEFAULT_LEVERAGE_LADDER.copy(),
            weights=DEFAULT_LEVERAGE_WEIGHTS.copy()
        )

    def register_config(self, symbol: str, ladder: List[int], weights: List[float]):
        """Register a custom config for a symbol."""
        self._configs[symbol] = LeverageConfig(ladder=ladder, weights=weights)

    def register_exchange_override(
        self,
        exchange: str,
        symbol: str,
        ladder: List[int],
        weights: List[float]
    ):
        """Register an exchange-specific override for a symbol (future use)."""
        if exchange not in self._exchange_overrides:
            self._exchange_overrides[exchange] = {}
        self._exchange_overrides[exchange][symbol] = LeverageConfig(
            ladder=ladder,
            weights=weights
        )

    def list_symbols(self) -> List[str]:
        """List all symbols with explicit configurations."""
        return list(self._configs.keys())

    def update_weights(self, symbol: str, new_weights: List[float]) -> bool:
        """
        Update weights for a symbol at runtime.

        Args:
            symbol: Symbol name
            new_weights: New weight values (will be normalized)

        Returns:
            True if successful, False if symbol not found or length mismatch
        """
        if symbol not in self._configs:
            return False

        config = self._configs[symbol]
        if len(new_weights) != len(config.ladder):
            return False

        # Normalize and update
        total = sum(new_weights)
        if total > 0:
            config.weights = [w / total for w in new_weights]
        else:
            config.weights = new_weights.copy()

        return True

    def get_ladder_summary(self, symbol: str) -> str:
        """Get a human-readable summary of the ladder config for a symbol."""
        config = self.get_config(symbol)
        lines = [f"Leverage config for {symbol}:"]
        lines.append(f"  Ladder: {config.ladder}")
        lines.append(f"  Weights (raw): {[f'{w:.2f}' for w in config.weights]}")
        lines.append(f"  Weighted ladder:")
        for lev, weight in config.get_weighted_ladder():
            lines.append(f"    {lev:3d}x -> {weight:.4f}")
        return "\n".join(lines)


# Global registry instance
LEVERAGE_REGISTRY = LeverageConfigRegistry()


def get_leverage_config(symbol: str, exchange: str = None) -> LeverageConfig:
    """Convenience function to get leverage config from global registry."""
    return LEVERAGE_REGISTRY.get_config(symbol, exchange)


def get_weighted_ladder(symbol: str, exchange: str = None) -> List[Tuple[int, float]]:
    """Convenience function to get weighted ladder from global registry."""
    return LEVERAGE_REGISTRY.get_config(symbol, exchange).get_weighted_ladder()
