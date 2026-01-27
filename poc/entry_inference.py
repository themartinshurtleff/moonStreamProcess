"""
EntryInference: Infer position entries from OI + aggression.

This module uses open interest changes combined with trade aggression to infer
where leveraged positions are being opened. This is the forward-looking component
that predicts WHERE liquidations will occur (vs LiquidationTape which shows where
they DID occur).

Key insight:
- OI↑ + buy aggression  → longs opening (projected liq below)
- OI↑ + sell aggression → shorts opening (projected liq above)
- OI↓                   → positions closing (remove from projection)

The quality of inference depends on:
1. OI data granularity (per-minute is typical)
2. Aggression signal quality (trade tape vs aggregated volume)
3. Leverage distribution assumptions (calibrated from LiquidationTape offsets)
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class InferredEntry:
    """An inferred position entry from OI + aggression analysis."""
    timestamp: float
    minute_key: int
    price: float          # Entry price estimate
    side: str             # "long" or "short"
    size_usd: float       # Estimated notional size
    confidence: float     # 0-1 confidence in this inference
    projected_liq: float  # Projected liquidation price


@dataclass
class PositionBucket:
    """Accumulated inferred positions at a price bucket."""
    price: float
    size_usd: float       # Cumulative inferred size
    side: str             # "long" or "short"
    entry_price: float    # Entry price (for offset calculation)
    last_ts: float
    leverage_est: int     # Estimated leverage tier


class EntryInference:
    """
    Infer position entries from OI changes and trade aggression.

    Uses the OI + aggression signal to estimate:
    1. Whether new positions are being opened (OI↑) or closed (OI↓)
    2. The direction of new positions (buy aggression → longs, sell aggression → shorts)
    3. The approximate entry price
    4. The projected liquidation price (using learned offset distribution)
    """

    # Default leverage distribution (will be updated by LiquidationTape learning)
    DEFAULT_LEVERAGE_WEIGHTS = {
        10: 0.05,   # 5% at 10x
        20: 0.15,   # 15% at 20x
        25: 0.25,   # 25% at 25x (most common)
        50: 0.30,   # 30% at 50x
        75: 0.15,   # 15% at 75x
        100: 0.08,  # 8% at 100x
        125: 0.02   # 2% at 125x
    }

    DEFAULT_BUFFER = 0.002    # 0.2% maintenance margin buffer
    DEFAULT_STEPS = 20.0      # Price bucket size
    DEFAULT_DECAY = 0.998     # Per-minute decay for projected zones (slower for persistence)

    # Minimum OI change to trigger inference (as fraction of position size)
    MIN_OI_CHANGE_PCT = 0.001  # 0.1% of OI

    # Aggression imbalance threshold
    AGGRESSION_THRESHOLD = 0.55  # 55% skew needed to classify direction

    def __init__(
        self,
        symbol: str = "BTC",
        steps: float = DEFAULT_STEPS,
        buffer: float = DEFAULT_BUFFER,
        decay: float = DEFAULT_DECAY,
        leverage_weights: Dict[int, float] = None,
        log_file: str = None
    ):
        self.symbol = symbol
        self.steps = steps
        self.buffer = buffer
        self.decay = decay
        self.leverage_weights = leverage_weights or self.DEFAULT_LEVERAGE_WEIGHTS.copy()

        # Projected liquidation zones from inferred entries
        # These are WHERE we expect liquidations to occur
        self.projected_long_liqs: Dict[float, PositionBucket] = {}   # Below current price
        self.projected_short_liqs: Dict[float, PositionBucket] = {}  # Above current price

        # Rolling inference history
        self.inferences: deque = deque(maxlen=720)  # 12h of inferences

        # OI tracking for delta calculation
        self.last_oi: float = 0.0
        self.last_oi_minute: int = 0

        # Stats
        self.current_minute: int = 0
        self.total_inferred_long_usd: float = 0.0
        self.total_inferred_short_usd: float = 0.0
        self.current_price: float = 0.0

        # Optional logging
        self.log_file = log_file
        self._log_fh = None
        if log_file:
            try:
                self._log_fh = open(log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open inference log file: {e}")

    def _bucket_price(self, price: float) -> float:
        """Round price to nearest bucket."""
        return round(price / self.steps) * self.steps

    def _calculate_liq_price(self, entry: float, leverage: int, side: str) -> float:
        """
        Calculate liquidation price from entry, leverage, and side.

        For longs: liq = entry * (1 - 1/L - buffer)
        For shorts: liq = entry * (1 + 1/L + buffer)
        """
        offset = (1.0 / leverage) + self.buffer

        if side == "long":
            return entry * (1 - offset)
        else:  # short
            return entry * (1 + offset)

    def on_minute(
        self,
        minute_key: int,
        src_price: float,
        oi: float,
        taker_buy_notional_usd: float,
        taker_sell_notional_usd: float,
        high: float = None,
        low: float = None
    ) -> List[InferredEntry]:
        """
        Process minute data to infer new position entries.

        Args:
            minute_key: Minute timestamp
            src_price: Reference price (OHLC4)
            oi: Current open interest (in contracts or USD)
            taker_buy_notional_usd: Taker buy volume in USD (from aggTrade)
            taker_sell_notional_usd: Taker sell volume in USD (from aggTrade)
            high: Minute high (for sweep detection)
            low: Minute low (for sweep detection)

        Returns:
            List of inferred entries this minute
        """
        self.current_minute = minute_key
        self.current_price = src_price
        inferences = []

        # Calculate OI delta
        oi_delta = 0.0
        if self.last_oi > 0:
            oi_delta = oi - self.last_oi
        self.last_oi = oi
        self.last_oi_minute = minute_key

        # Apply decay to existing projections
        self._apply_decay()

        # Sweep: remove projected zones that price crossed
        if high and low:
            self._sweep(high, low)

        # Compute aggression from USD notional
        total_notional_usd = taker_buy_notional_usd + taker_sell_notional_usd

        # Skip inference if volume too small (< $1000 USD)
        if total_notional_usd < 1000 or abs(oi_delta) < self.last_oi * self.MIN_OI_CHANGE_PCT:
            return inferences

        # Determine aggression direction from USD notional ratio
        buy_pct = taker_buy_notional_usd / total_notional_usd if total_notional_usd > 0 else 0.5

        # Debug log: units verification
        logger.debug(
            f"[{self.symbol}] minute={minute_key} src={src_price:.2f} "
            f"buy_usd={taker_buy_notional_usd:.0f} sell_usd={taker_sell_notional_usd:.0f} "
            f"total_usd={total_notional_usd:.0f} buy_pct={buy_pct:.3f}"
        )

        # Infer direction from OI change + aggression
        # OI↑ + buy aggression → longs opening
        # OI↑ + sell aggression → shorts opening
        # OI↓ → positions closing (we reduce projections proportionally)

        if oi_delta > 0:
            # New positions opening
            if buy_pct >= self.AGGRESSION_THRESHOLD:
                # Longs opening
                side = "long"
                confidence = min(1.0, buy_pct / self.AGGRESSION_THRESHOLD)
            elif buy_pct <= (1 - self.AGGRESSION_THRESHOLD):
                # Shorts opening
                side = "short"
                confidence = min(1.0, (1 - buy_pct) / self.AGGRESSION_THRESHOLD)
            else:
                # Mixed/unclear - split proportionally
                # Don't infer when signal is ambiguous
                return inferences

            # Estimate notional size from OI delta
            # Assuming OI is in contracts and we need to convert
            estimated_size_usd = abs(oi_delta) * src_price if oi_delta < 1000 else abs(oi_delta)

            # Create projected liquidation zones for each leverage tier
            for leverage, weight in self.leverage_weights.items():
                if weight < 0.01:
                    continue

                liq_price = self._calculate_liq_price(src_price, leverage, side)
                bucket = self._bucket_price(liq_price)
                size_contribution = estimated_size_usd * weight * confidence

                if size_contribution < 100:  # Less than $100 - skip
                    continue

                inference = InferredEntry(
                    timestamp=time.time(),
                    minute_key=minute_key,
                    price=src_price,
                    side=side,
                    size_usd=size_contribution,
                    confidence=confidence,
                    projected_liq=bucket
                )
                inferences.append(inference)
                self.inferences.append(inference)

                # Accumulate into projection buckets
                buckets = self.projected_long_liqs if side == "long" else self.projected_short_liqs

                if bucket not in buckets:
                    buckets[bucket] = PositionBucket(
                        price=bucket,
                        size_usd=0.0,
                        side=side,
                        entry_price=src_price,
                        last_ts=time.time(),
                        leverage_est=leverage
                    )

                buckets[bucket].size_usd += size_contribution
                buckets[bucket].last_ts = time.time()

                # Track totals
                if side == "long":
                    self.total_inferred_long_usd += size_contribution
                else:
                    self.total_inferred_short_usd += size_contribution

        elif oi_delta < 0:
            # Positions closing - reduce projections proportionally
            close_pct = abs(oi_delta) / self.last_oi if self.last_oi > 0 else 0
            close_pct = min(0.5, close_pct)  # Cap at 50% reduction per minute

            # Determine which side is closing based on aggression
            # Sell aggression with OI↓ → longs closing
            # Buy aggression with OI↓ → shorts closing
            if buy_pct < (1 - self.AGGRESSION_THRESHOLD):
                # Longs closing (selling to close)
                self._reduce_projections(self.projected_long_liqs, close_pct)
            elif buy_pct > self.AGGRESSION_THRESHOLD:
                # Shorts closing (buying to close)
                self._reduce_projections(self.projected_short_liqs, close_pct)
            else:
                # Mixed - reduce both sides slightly
                self._reduce_projections(self.projected_long_liqs, close_pct * 0.5)
                self._reduce_projections(self.projected_short_liqs, close_pct * 0.5)

        # Log inference summary
        if self._log_fh and inferences:
            log_entry = {
                "type": "inference",
                "ts": time.time(),
                "minute_key": minute_key,
                "src_price": src_price,
                "oi_delta": oi_delta,
                "buy_pct": buy_pct,
                "inferences": [
                    {
                        "side": inf.side,
                        "size_usd": round(inf.size_usd, 2),
                        "projected_liq": inf.projected_liq,
                        "confidence": round(inf.confidence, 3)
                    }
                    for inf in inferences
                ]
            }
            self._log_fh.write(json.dumps(log_entry) + '\n')
            self._log_fh.flush()

        return inferences

    def _apply_decay(self) -> None:
        """Apply decay to all projected zones."""
        for buckets in [self.projected_long_liqs, self.projected_short_liqs]:
            to_remove = []
            for price, bucket in buckets.items():
                bucket.size_usd *= self.decay
                if bucket.size_usd < 100:  # Less than $100 - remove
                    to_remove.append(price)

            for price in to_remove:
                del buckets[price]

    def _reduce_projections(self, buckets: Dict[float, PositionBucket], pct: float) -> None:
        """Reduce projection sizes by percentage (for position closing)."""
        to_remove = []
        for price, bucket in buckets.items():
            bucket.size_usd *= (1 - pct)
            if bucket.size_usd < 100:
                to_remove.append(price)

        for price in to_remove:
            del buckets[price]

    def _sweep(self, high: float, low: float) -> int:
        """
        Remove projected zones that price has crossed.

        Returns count of swept zones.
        """
        swept = 0

        # Short projections above price get swept when high >= bucket
        to_remove = [p for p in self.projected_short_liqs if high >= p]
        for price in to_remove:
            del self.projected_short_liqs[price]
            swept += 1

        # Long projections below price get swept when low <= bucket
        to_remove = [p for p in self.projected_long_liqs if low <= p]
        for price in to_remove:
            del self.projected_long_liqs[price]
            swept += 1

        return swept

    def update_leverage_weights(self, new_weights: Dict[int, float]) -> None:
        """
        Update leverage distribution from LiquidationTape learning.

        Called when tape accumulates enough offset samples to improve estimates.
        """
        # Normalize weights
        total = sum(new_weights.values())
        if total > 0:
            self.leverage_weights = {k: v / total for k, v in new_weights.items()}
        else:
            self.leverage_weights = new_weights.copy()

        logger.info(f"[{self.symbol}] Leverage weights updated from tape learning:")
        for lev, weight in sorted(self.leverage_weights.items()):
            logger.info(f"  {lev}x: {weight:.3f}")

    def get_projections(self) -> Dict[str, Dict[float, float]]:
        """
        Get current projected liquidation zones.

        Returns:
            {"long": {price: size_usd}, "short": {price: size_usd}}
        """
        return {
            "long": {b.price: b.size_usd for b in self.projected_long_liqs.values()},
            "short": {b.price: b.size_usd for b in self.projected_short_liqs.values()}
        }

    def get_combined_heatmap(
        self,
        tape_heatmap: Dict[str, Dict[float, float]],
        tape_weight: float = 0.4,
        projection_weight: float = 0.6
    ) -> Dict[str, Dict[float, float]]:
        """
        Combine tape (historical) with projections (forward-looking).

        Args:
            tape_heatmap: From LiquidationTape.get_heatmap()
            tape_weight: Weight for historical liquidations
            projection_weight: Weight for projected liquidations

        Returns:
            Combined heatmap for display
        """
        result = {"long": {}, "short": {}}

        # Combine long zones
        all_long_prices = set(tape_heatmap.get("long", {}).keys()) | set(self.projected_long_liqs.keys())
        for price in all_long_prices:
            tape_val = tape_heatmap.get("long", {}).get(price, 0)
            proj_val = self.projected_long_liqs.get(price, PositionBucket(0, 0, "", 0, 0, 0)).size_usd
            combined = tape_val * tape_weight + proj_val * projection_weight
            if combined > 0:
                result["long"][price] = combined

        # Combine short zones
        all_short_prices = set(tape_heatmap.get("short", {}).keys()) | set(self.projected_short_liqs.keys())
        for price in all_short_prices:
            tape_val = tape_heatmap.get("short", {}).get(price, 0)
            proj_val = self.projected_short_liqs.get(price, PositionBucket(0, 0, "", 0, 0, 0)).size_usd
            combined = tape_val * tape_weight + proj_val * projection_weight
            if combined > 0:
                result["short"][price] = combined

        return result

    def get_stats(self) -> dict:
        """Get current inference statistics."""
        return {
            "symbol": self.symbol,
            "current_minute": self.current_minute,
            "current_price": self.current_price,
            "total_inferred_long_usd": self.total_inferred_long_usd,
            "total_inferred_short_usd": self.total_inferred_short_usd,
            "projected_long_buckets": len(self.projected_long_liqs),
            "projected_short_buckets": len(self.projected_short_liqs),
            "inferences_in_window": len(self.inferences),
            "leverage_weights": self.leverage_weights
        }

    def close(self):
        """Close log file handle."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
