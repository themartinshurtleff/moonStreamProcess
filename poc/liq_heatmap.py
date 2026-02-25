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
import math
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from liq_tape import LiquidationTape
from entry_inference import EntryInference, _p99_scale

# V3: Import zone manager for persistent zones
try:
    from active_zone_manager import ActiveZoneManager, get_zone_manager
    HAS_ZONE_MANAGER = True
except ImportError:
    ActiveZoneManager = None
    get_zone_manager = None
    HAS_ZONE_MANAGER = False

logger = logging.getLogger(__name__)


@dataclass
class LiquidityPool:
    """A clustered liquidity pool (merged from nearby buckets)."""
    center_price: float      # Weighted centroid price
    price_low: float         # Lower bound of cluster
    price_high: float        # Upper bound of cluster
    notional_usd: float      # Total USD notional in cluster
    bucket_count: int        # Number of buckets merged
    intensity: float = 0.0   # Normalized 0-1 intensity
    side: str = ""           # "long" or "short"


@dataclass
class HeatmapConfig:
    """Configuration for the unified heatmap engine."""
    symbol: str = "BTC"
    steps: float = 20.0          # Price bucket size
    decay: float = 0.998         # Per-minute decay (slower for persistence)
    buffer: float = 0.002        # Liquidation buffer
    tape_weight: float = 0.35    # Weight for historical tape
    projection_weight: float = 0.65  # Weight for forward projections
    # Clustering parameters
    cluster_radius_pct: float = 0.005  # Cluster within ±0.5% of price (~$500 at $100k)
    min_notional_usd: float = 10000.0  # Minimum $10k to display a pool
    max_pools_per_side: int = 20       # Max pools to return per side


class LiquidationHeatmap:
    """
    Unified liquidation heatmap engine.

    Combines historical liquidation tape with OI-based forward projections
    to produce a comprehensive liquidation heatmap for trading terminals.
    """

    def __init__(
        self,
        config: HeatmapConfig = None,
        log_dir: str = None,
        zone_persist_file: str = None,
    ):
        self.config = config or HeatmapConfig()

        # Initialize sub-engines with per-symbol logging
        sym = self.config.symbol  # e.g. "BTC", "ETH", "SOL"
        tape_log = os.path.join(log_dir, f"liq_tape_{sym}.jsonl") if log_dir else None
        inference_log = os.path.join(log_dir, f"liq_inference_{sym}.jsonl") if log_dir else None
        sweep_log = os.path.join(log_dir, f"liq_sweeps_{sym}.jsonl") if log_dir else None
        debug_log = os.path.join(log_dir, f"liq_debug_{sym}.jsonl") if log_dir else None
        zone_log = os.path.join(log_dir, f"liq_zones_{sym}.jsonl") if log_dir else None
        zone_persist = zone_persist_file or (
            os.path.join(log_dir, "liq_active_zones.json") if log_dir else None
        )

        # V3: Create shared zone manager for persistent zones
        self.zone_manager = None
        if HAS_ZONE_MANAGER and log_dir:
            self.zone_manager = ActiveZoneManager(
                symbol=self.config.symbol,
                steps=self.config.steps,
                persist_file=zone_persist,
                zone_log_file=zone_log,
                auto_persist=True
            )
            logger.info(f"  V3 zone manager enabled: {zone_persist}")

        self.tape = LiquidationTape(
            symbol=self.config.symbol,
            steps=self.config.steps,
            decay=self.config.decay,
            log_file=tape_log,
            sweep_log_file=sweep_log,
            zone_manager=self.zone_manager  # V3: Share zone manager
        )

        self.inference = EntryInference(
            symbol=self.config.symbol,
            steps=self.config.steps,
            buffer=self.config.buffer,
            decay=self.config.decay,
            log_file=inference_log,
            debug_log_file=debug_log,
            sweep_log_file=sweep_log,
            zone_manager=self.zone_manager  # V3: Share zone manager
        )

        # State tracking
        self.current_minute: int = 0
        self.current_price: float = 0.0
        self.last_oi: float = 0.0
        self._last_balance_log_minute: int = 0

        # Learning feedback: how often to push tape learning to inference
        self.tape_learning_interval = 60  # Every 60 minutes
        self.last_tape_learning_push: int = 0

        # Stats
        self.total_force_orders: int = 0
        self.total_inferences: int = 0

        # V3: Cycle summary logging (per-symbol)
        self.cycle_log_file = os.path.join(log_dir, f"liq_cycles_{sym}.jsonl") if log_dir else None
        self._cycle_log_fh = None
        if self.cycle_log_file:
            try:
                self._cycle_log_fh = open(self.cycle_log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open cycle log file: {e}")

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

        _ndigits = max(0, -Decimal(str(self.config.steps)).as_tuple().exponent)
        liq_bucket = round(round(liq_price / self.config.steps) * self.config.steps, _ndigits)

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
        taker_buy_notional_usd: float,
        taker_sell_notional_usd: float
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
            taker_buy_notional_usd: Taker buy volume in USD (from aggTrade)
            taker_sell_notional_usd: Taker sell volume in USD (from aggTrade)
        """
        self.current_minute = minute_key
        self.current_price = src_price

        # Update tape (decay + sweep)
        self.tape.on_minute(minute_key)
        self.tape.sweep(high, low, minute_key)

        # Update inference (generates projections)
        inferences = self.inference.on_minute(
            minute_key=minute_key,
            src_price=src_price,
            oi=oi,
            taker_buy_notional_usd=taker_buy_notional_usd,
            taker_sell_notional_usd=taker_sell_notional_usd,
            high=high,
            low=low
        )

        self.total_inferences += len(inferences)

        # V3: Merge nearby zones and log summary (every minute)
        zone_merges = 0
        if self.zone_manager:
            zone_merges = self.zone_manager.merge_nearby_zones()
            if zone_merges > 0:
                logger.debug(f"[{self.config.symbol}] Zone manager merged {zone_merges} nearby zones")

        # V3: Log per-minute cycle summary
        self._log_cycle_summary(minute_key, src_price, high, low, zone_merges)

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

    def _log_cycle_summary(
        self,
        minute_key: int,
        src_price: float,
        high: float,
        low: float,
        zone_merges: int
    ) -> None:
        """
        V3: Log per-minute cycle summary for diagnostics.

        Captures snapshot of zone state and activity each minute.
        """
        if not self._cycle_log_fh:
            return

        import time as _time

        # Gather zone manager stats if available
        zone_stats = {}
        if self.zone_manager:
            zone_stats = self.zone_manager.get_summary()

        # Tape stats
        tape_stats = self.tape.get_stats()

        # Inference stats
        inference_stats = self.inference.get_stats()

        cycle_entry = {
            "type": "cycle_summary",
            "ts": _time.time(),
            "minute_key": minute_key,
            "src_price": src_price,
            "high": high,
            "low": low,
            "total_force_orders": self.total_force_orders,
            "total_inferences": self.total_inferences,
            "zone_merges": zone_merges,
            # Zone manager state
            "active_zones_long": zone_stats.get("active_zones_long", 0),
            "active_zones_short": zone_stats.get("active_zones_short", 0),
            "total_weight_long": zone_stats.get("total_weight_long", 0),
            "total_weight_short": zone_stats.get("total_weight_short", 0),
            "zones_created_total": zone_stats.get("zones_created_total", 0),
            "zones_swept_total": zone_stats.get("zones_swept_total", 0),
            "zones_expired_total": zone_stats.get("zones_expired_total", 0),
            # Tape state
            "tape_long_buckets": tape_stats.get("long_buckets", 0),
            "tape_short_buckets": tape_stats.get("short_buckets", 0),
            "tape_total_events": tape_stats.get("total_events", 0),
            # Inference state
            "proj_long_buckets": inference_stats.get("projected_long_buckets", 0),
            "proj_short_buckets": inference_stats.get("projected_short_buckets", 0)
        }

        try:
            self._cycle_log_fh.write(json.dumps(cycle_entry) + '\n')
            # Heatmap balance metric every 10 minutes — proves normalization is working
            if minute_key - self._last_balance_log_minute >= 10:
                combined = self.inference.get_combined_heatmap(
                    self.tape.get_heatmap(),
                    tape_weight=self.config.tape_weight,
                    projection_weight=self.config.projection_weight
                )
                combined_vals = list(combined.get("long", {}).values()) + list(combined.get("short", {}).values())
                balance_entry = {
                    "type": "heatmap_balance",
                    "ts": _time.time(),
                    "symbol": self.config.symbol,
                    "tape_max_raw": round(self.inference._last_tape_max_raw, 2),
                    "inf_max_raw": round(self.inference._last_proj_max_raw, 2),
                    "tape_scale": round(self.inference._last_tape_scale, 2),
                    "inf_scale": round(self.inference._last_proj_scale, 2),
                    "combined_max": round(max(combined_vals) if combined_vals else 0.0, 6),
                }
                self._cycle_log_fh.write(json.dumps(balance_entry) + '\n')
                self._last_balance_log_minute = minute_key
            self._cycle_log_fh.flush()
        except Exception as e:
            logger.warning(f"Failed to write cycle summary: {e}")

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

    def _cluster_zones(
        self,
        zones: Dict[float, float],
        center: float,
        side: str,
        min_notional: float = None
    ) -> List[LiquidityPool]:
        """
        Cluster nearby price buckets into liquidity pools.

        Uses greedy clustering: sort by notional, cluster each bucket with
        nearby buckets within cluster_radius_pct.

        Args:
            zones: {price: notional_usd} dict
            center: Current price for radius calculation
            side: "long" or "short"
            min_notional: Minimum notional to include (default: config value)

        Returns:
            List of LiquidityPool objects, sorted by notional descending
        """
        if not zones:
            return []

        min_notional = min_notional if min_notional is not None else self.config.min_notional_usd
        cluster_radius = center * self.config.cluster_radius_pct

        # Filter by minimum notional and convert to list of (price, notional)
        buckets = [(p, n) for p, n in zones.items() if n >= 100]  # At least $100 per bucket
        if not buckets:
            return []

        # Sort by price for clustering
        buckets.sort(key=lambda x: x[0])

        # Greedy clustering: merge adjacent buckets within radius
        clusters = []
        i = 0
        while i < len(buckets):
            cluster_prices = [buckets[i][0]]
            cluster_notionals = [buckets[i][1]]

            # Extend cluster to include nearby buckets
            j = i + 1
            while j < len(buckets):
                if buckets[j][0] - cluster_prices[-1] <= cluster_radius:
                    cluster_prices.append(buckets[j][0])
                    cluster_notionals.append(buckets[j][1])
                    j += 1
                else:
                    break

            # Create cluster if meets minimum
            total_notional = sum(cluster_notionals)
            if total_notional >= min_notional:
                # Weighted centroid price
                weighted_price = sum(p * n for p, n in zip(cluster_prices, cluster_notionals)) / total_notional

                clusters.append(LiquidityPool(
                    center_price=round(weighted_price, 2),
                    price_low=min(cluster_prices),
                    price_high=max(cluster_prices),
                    notional_usd=round(total_notional, 2),
                    bucket_count=len(cluster_prices),
                    side=side
                ))

            i = j

        # Sort by notional descending and limit
        clusters.sort(key=lambda x: x.notional_usd, reverse=True)
        return clusters[:self.config.max_pools_per_side]

    def _normalize_pools(self, pools: List[LiquidityPool]) -> None:
        """Normalize pool intensities to 0-1 range in place."""
        if not pools:
            return

        max_notional = max(p.notional_usd for p in pools)
        if max_notional <= 0:
            return

        for pool in pools:
            # Use sqrt scaling for better visual distribution
            pool.intensity = round(math.sqrt(pool.notional_usd / max_notional), 4)

    def get_api_response(
        self,
        price_center: float = None,
        price_range_pct: float = 0.10,
        min_notional_usd: float = None
    ) -> Dict:
        """
        Get heatmap formatted for API response (Rust terminal compatible).

        Returns clustered liquidity pools with notional values for UI filtering.

        Args:
            price_center: Center price for filtering (default: current price)
            price_range_pct: Range as percentage (default: ±10%)
            min_notional_usd: Minimum USD notional to include (default: config value)

        Returns:
            API-compatible response with clustered pools
        """
        tape_heatmap = self.tape.get_heatmap()
        projections = self.inference.get_projections()

        # Combine tape and projections into notional values.
        # NOTE: This path uses raw USD values (not independently normalized)
        # because _cluster_zones() and the API consumer expect real USD notional
        # for filtering and display. Visual intensity is handled downstream
        # by _normalize_pools() which applies sqrt scaling to 0-1.
        # (The heatmap intensity path in get_heatmap() uses independent
        # normalization via get_combined_heatmap() instead.)
        combined_long = {}
        combined_short = {}

        # Merge long zones
        all_long_prices = set(tape_heatmap.get("long", {}).keys()) | set(projections.get("long", {}).keys())
        for price in all_long_prices:
            tape_val = tape_heatmap.get("long", {}).get(price, 0)
            proj_val = projections.get("long", {}).get(price, 0)
            combined = tape_val * self.config.tape_weight + proj_val * self.config.projection_weight
            if combined > 0:
                combined_long[price] = combined

        # Merge short zones
        all_short_prices = set(tape_heatmap.get("short", {}).keys()) | set(projections.get("short", {}).keys())
        for price in all_short_prices:
            tape_val = tape_heatmap.get("short", {}).get(price, 0)
            proj_val = projections.get("short", {}).get(price, 0)
            combined = tape_val * self.config.tape_weight + proj_val * self.config.projection_weight
            if combined > 0:
                combined_short[price] = combined

        center = price_center or self.current_price or 0
        if center <= 0:
            return {"error": "no_price_data"}

        low_bound = center * (1 - price_range_pct)
        high_bound = center * (1 + price_range_pct)

        # Filter to price range
        long_in_range = {p: n for p, n in combined_long.items() if low_bound <= p <= center}
        short_in_range = {p: n for p, n in combined_short.items() if center <= p <= high_bound}

        # Cluster into pools
        long_pools = self._cluster_zones(long_in_range, center, "long", min_notional_usd)
        short_pools = self._cluster_zones(short_in_range, center, "short", min_notional_usd)

        # Normalize intensities (combined across both sides for consistent scaling)
        all_pools = long_pools + short_pools
        self._normalize_pools(all_pools)

        # Format for API response
        long_levels = [
            {
                "price": pool.center_price,
                "price_low": pool.price_low,
                "price_high": pool.price_high,
                "notional_usd": pool.notional_usd,
                "intensity": pool.intensity,
                "bucket_count": pool.bucket_count
            }
            for pool in long_pools
        ]

        short_levels = [
            {
                "price": pool.center_price,
                "price_low": pool.price_low,
                "price_high": pool.price_high,
                "notional_usd": pool.notional_usd,
                "intensity": pool.intensity,
                "bucket_count": pool.bucket_count
            }
            for pool in short_pools
        ]

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
                "inferences_total": self.total_inferences,
                "cluster_radius_pct": self.config.cluster_radius_pct,
                "min_notional_usd": min_notional_usd or self.config.min_notional_usd,
                "long_pools_count": len(long_pools),
                "short_pools_count": len(short_pools),
                "raw_long_buckets": len(long_in_range),
                "raw_short_buckets": len(short_in_range)
            }
        }

    def get_stats(self) -> dict:
        """Get comprehensive statistics."""
        stats = {
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
        # V3: Add zone manager stats if available
        if self.zone_manager:
            stats["zone_manager"] = self.zone_manager.get_summary()
        return stats

    def close(self):
        """Clean up resources."""
        self.tape.close()
        self.inference.close()
        # V3: Stop zone manager (saves state to disk)
        if self.zone_manager:
            self.zone_manager.stop()
        # V3: Close cycle log file
        if self._cycle_log_fh:
            self._cycle_log_fh.close()
            self._cycle_log_fh = None


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
