"""
LiquidationHeatmap: Unified engine combining tape and inference layers.

This is the main interface for the Rust orderflow terminal. It combines:
1. LiquidationTape: Ground truth from forceOrder stream (historical)
2. EntryInference: OI + aggression based projections (forward-looking)

The resulting heatmap shows:
- WHERE liquidations happened (tape) - high confidence, historical
- WHERE liquidations will likely happen (projection) - predictive, forward-looking

Both layers feed back into each other:
- Tape learning improves inference leverage distribution
- Inference provides entry estimates for tape offset learning
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from liq_tape import LiquidationTape
from entry_inference import EntryInference

logger = logging.getLogger(__name__)


@dataclass
class HeatmapConfig:
    """Configuration for the unified heatmap engine."""
    symbol: str = "BTC"
    steps: float = 20.0          # Price bucket size
    decay: float = 0.995         # Per-minute decay
    buffer: float = 0.002        # Liquidation buffer
    tape_weight: float = 0.35    # Weight for historical tape
    projection_weight: float = 0.65  # Weight for forward projections


class LiquidationHeatmap:
    """
    Unified liquidation heatmap engine.

    Combines historical liquidation tape with OI-based forward projections
    to produce a comprehensive liquidation heatmap for trading terminals.
    """

    def __init__(
        self,
        config: HeatmapConfig = None,
        log_dir: str = None
    ):
        self.config = config or HeatmapConfig()

        # Initialize sub-engines
        tape_log = os.path.join(log_dir, "liq_tape.jsonl") if log_dir else None
        inference_log = os.path.join(log_dir, "liq_inference.jsonl") if log_dir else None

        self.tape = LiquidationTape(
            symbol=self.config.symbol,
            steps=self.config.steps,
            decay=self.config.decay,
            log_file=tape_log
        )

        self.inference = EntryInference(
            symbol=self.config.symbol,
            steps=self.config.steps,
            buffer=self.config.buffer,
            decay=self.config.decay,
            log_file=inference_log
        )

        # State tracking
        self.current_minute: int = 0
        self.current_price: float = 0.0
        self.last_oi: float = 0.0

        # Learning feedback: how often to push tape learning to inference
        self.tape_learning_interval = 60  # Every 60 minutes
        self.last_tape_learning_push: int = 0

        # Stats
        self.total_force_orders: int = 0
        self.total_inferences: int = 0

        logger.info(f"LiquidationHeatmap initialized for {self.config.symbol}")
        logger.info(f"  steps={self.config.steps}, buffer={self.config.buffer}")
        logger.info(f"  tape_weight={self.config.tape_weight}, projection_weight={self.config.projection_weight}")

    def on_force_order(
        self,
        timestamp: float,
        side: str,
        price: float,
        qty: float,
        notional: float
    ) -> None:
        """
        Process a forceOrder liquidation event.

        Args:
            timestamp: Unix timestamp
            side: "long" or "short"
            price: Liquidation price
            qty: Size in base asset
            notional: USD value
        """
        # Get entry estimate from inference layer (if we have position tracking)
        entry_estimate = self._estimate_entry_from_projection(price, side)

        self.tape.on_force_order(
            timestamp=timestamp,
            side=side,
            price=price,
            qty=qty,
            notional=notional,
            entry_estimate=entry_estimate
        )

        self.total_force_orders += 1

    def _estimate_entry_from_projection(self, liq_price: float, side: str) -> Optional[float]:
        """
        Attempt to back-calculate entry price from our projections.

        If we have a projected zone near this liquidation, use the stored entry price.
        This provides feedback for offset learning.
        """
        # Look for matching projection within ±2 buckets
        buckets = self.inference.projected_long_liqs if side == "long" else self.inference.projected_short_liqs

        liq_bucket = round(liq_price / self.config.steps) * self.config.steps

        for delta in [0, -self.config.steps, self.config.steps, -2*self.config.steps, 2*self.config.steps]:
            check_bucket = liq_bucket + delta
            if check_bucket in buckets:
                return buckets[check_bucket].entry_price

        return None

    def on_minute(
        self,
        minute_key: int,
        src_price: float,
        high: float,
        low: float,
        oi: float,
        buy_vol: float,
        sell_vol: float
    ) -> None:
        """
        Process minute-level market data.

        Updates both tape and inference layers.

        Args:
            minute_key: Minute timestamp
            src_price: Reference price (OHLC4)
            high: Minute high
            low: Minute low
            oi: Open interest
            buy_vol: Buy volume
            sell_vol: Sell volume
        """
        self.current_minute = minute_key
        self.current_price = src_price

        # Update tape (decay + sweep)
        self.tape.on_minute(minute_key)
        self.tape.sweep(high, low)

        # Update inference (generates projections)
        inferences = self.inference.on_minute(
            minute_key=minute_key,
            src_price=src_price,
            oi=oi,
            buy_vol=buy_vol,
            sell_vol=sell_vol,
            high=high,
            low=low
        )

        self.total_inferences += len(inferences)

        # Periodic tape learning push to inference
        if minute_key - self.last_tape_learning_push >= self.tape_learning_interval:
            self._push_tape_learning()
            self.last_tape_learning_push = minute_key

    def _push_tape_learning(self) -> None:
        """
        Push offset distribution learning from tape to inference.

        Uses observed liquidations to improve leverage weight estimates.
        """
        stats = self.tape.get_offset_distribution()
        if stats["count"] < 50:  # Not enough samples
            return

        # Learn per-leverage distribution
        new_weights = {}
        total_samples = 0

        for lev in [10, 20, 25, 50, 75, 100, 125]:
            lev_stats = self.tape.get_offset_distribution(lev)
            if lev_stats["count"] > 5:
                # Weight by sample count (more samples = more confidence)
                new_weights[lev] = lev_stats["count"]
                total_samples += lev_stats["count"]

        if total_samples > 0 and len(new_weights) >= 3:
            # Normalize and update inference
            normalized = {k: v / total_samples for k, v in new_weights.items()}

            # Blend with existing weights (don't jump too fast)
            blend_factor = 0.2  # 20% new, 80% old
            blended = {}
            for lev in self.inference.leverage_weights:
                old_w = self.inference.leverage_weights.get(lev, 0.1)
                new_w = normalized.get(lev, old_w)
                blended[lev] = old_w * (1 - blend_factor) + new_w * blend_factor

            self.inference.update_leverage_weights(blended)
            logger.info(f"Pushed tape learning to inference: {total_samples} samples")

    def get_heatmap(self) -> Dict[str, any]:
        """
        Get unified heatmap for display.

        Returns:
            {
                "symbol": str,
                "timestamp": float,
                "current_price": float,
                "long": {price: intensity, ...},   # Below current price
                "short": {price: intensity, ...},  # Above current price
                "tape": {                          # Raw tape data
                    "long": {price: notional},
                    "short": {price: notional}
                },
                "projection": {                    # Raw projection data
                    "long": {price: size_usd},
                    "short": {price: size_usd}
                },
                "stats": {...}
            }
        """
        tape_heatmap = self.tape.get_heatmap()
        projections = self.inference.get_projections()

        # Combine with weights
        combined = self.inference.get_combined_heatmap(
            tape_heatmap,
            tape_weight=self.config.tape_weight,
            projection_weight=self.config.projection_weight
        )

        # Normalize combined values to 0-1 intensity
        long_zones = combined.get("long", {})
        short_zones = combined.get("short", {})

        # Find max for normalization
        all_values = list(long_zones.values()) + list(short_zones.values())
        max_val = max(all_values) if all_values else 1.0

        long_normalized = {p: v / max_val for p, v in long_zones.items()} if max_val > 0 else {}
        short_normalized = {p: v / max_val for p, v in short_zones.items()} if max_val > 0 else {}

        return {
            "symbol": self.config.symbol,
            "timestamp": time.time(),
            "current_price": self.current_price,
            "long": long_normalized,
            "short": short_normalized,
            "tape": tape_heatmap,
            "projection": projections,
            "stats": {
                "total_force_orders": self.total_force_orders,
                "total_inferences": self.total_inferences,
                "tape_long_buckets": len(tape_heatmap.get("long", {})),
                "tape_short_buckets": len(tape_heatmap.get("short", {})),
                "proj_long_buckets": len(projections.get("long", {})),
                "proj_short_buckets": len(projections.get("short", {})),
                "tape_weight": self.config.tape_weight,
                "projection_weight": self.config.projection_weight
            }
        }

    def get_api_response(self, price_center: float = None, price_range_pct: float = 0.10) -> Dict:
        """
        Get heatmap formatted for API response (Rust terminal compatible).

        Args:
            price_center: Center price for filtering (default: current price)
            price_range_pct: Range as percentage (default: ±10%)

        Returns:
            API-compatible response with arrays for efficient rendering
        """
        heatmap = self.get_heatmap()
        center = price_center or self.current_price or 0

        if center <= 0:
            return {"error": "no_price_data"}

        low_bound = center * (1 - price_range_pct)
        high_bound = center * (1 + price_range_pct)

        # Filter and format as arrays for efficient rendering
        long_levels = []
        for price, intensity in sorted(heatmap["long"].items()):
            if low_bound <= price <= center:  # Longs below current price
                long_levels.append({
                    "price": price,
                    "intensity": round(intensity, 4),
                    "source": "combined"
                })

        short_levels = []
        for price, intensity in sorted(heatmap["short"].items()):
            if center <= price <= high_bound:  # Shorts above current price
                short_levels.append({
                    "price": price,
                    "intensity": round(intensity, 4),
                    "source": "combined"
                })

        return {
            "symbol": self.config.symbol,
            "ts": time.time(),
            "price": center,
            "range_pct": price_range_pct,
            "long_levels": long_levels,
            "short_levels": short_levels,
            "meta": {
                "tape_weight": self.config.tape_weight,
                "projection_weight": self.config.projection_weight,
                "force_orders_total": self.total_force_orders,
                "inferences_total": self.total_inferences
            }
        }

    def get_stats(self) -> dict:
        """Get comprehensive statistics."""
        return {
            "symbol": self.config.symbol,
            "current_minute": self.current_minute,
            "current_price": self.current_price,
            "total_force_orders": self.total_force_orders,
            "total_inferences": self.total_inferences,
            "tape": self.tape.get_stats(),
            "inference": self.inference.get_stats(),
            "config": {
                "steps": self.config.steps,
                "decay": self.config.decay,
                "buffer": self.config.buffer,
                "tape_weight": self.config.tape_weight,
                "projection_weight": self.config.projection_weight
            }
        }

    def close(self):
        """Clean up resources."""
        self.tape.close()
        self.inference.close()


# Factory function for easy initialization
def create_heatmap_engine(
    symbol: str = "BTC",
    steps: float = 20.0,
    log_dir: str = None
) -> LiquidationHeatmap:
    """
    Factory function to create a properly configured heatmap engine.

    Args:
        symbol: Trading symbol (BTC, ETH, SOL, etc.)
        steps: Price bucket size (default 20 for BTC)
        log_dir: Directory for JSONL logs (optional)

    Returns:
        Configured LiquidationHeatmap instance
    """
    # Symbol-specific configurations
    symbol_configs = {
        "BTC": {"steps": 20.0, "buffer": 0.002},
        "ETH": {"steps": 2.0, "buffer": 0.002},
        "SOL": {"steps": 0.1, "buffer": 0.003},
        "BNB": {"steps": 1.0, "buffer": 0.002},
        "XRP": {"steps": 0.001, "buffer": 0.003},
        "DOGE": {"steps": 0.0001, "buffer": 0.003},
    }

    cfg = symbol_configs.get(symbol, {"steps": steps, "buffer": 0.002})

    config = HeatmapConfig(
        symbol=symbol,
        steps=cfg["steps"],
        buffer=cfg["buffer"]
    )

    return LiquidationHeatmap(config=config, log_dir=log_dir)
