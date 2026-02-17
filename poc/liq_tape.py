"""
LiquidationTape: Ground-truth accumulation from forceOrder stream.

This module directly accumulates liquidation events from Binance forceOrder stream
into price buckets. No inference, no guessing - just what actually liquidated.

Each forceOrder event:
- side: which positions got liquidated (SELL = longs liquidated, BUY = shorts liquidated)
- price: exact liquidation price
- qty: size in base asset
- notional: price * qty in quote (USD)

The tape provides:
1. Real-time liquidation heatmap (where liquidations HAPPENED)
2. Offset distribution learning (for projecting future liquidations)
3. Validation signal for the inference layer
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)


@dataclass
class LiqBucket:
    """A liquidation bucket from actual forceOrder events."""
    price: float
    notional: float  # Cumulative USD liquidated at this bucket
    count: int       # Number of liquidation events
    last_ts: float   # Last update timestamp
    side: str        # "long" or "short"


@dataclass
class LiquidationEvent:
    """A single forceOrder liquidation event."""
    timestamp: float
    symbol: str
    side: str          # "long" = longs liquidated (SELL), "short" = shorts liquidated (BUY)
    price: float
    qty: float
    notional: float
    # Optional context for offset learning
    entry_estimate: Optional[float] = None  # If we can back-calculate from context


class LiquidationTape:
    """
    Ground-truth liquidation accumulator from forceOrder stream.

    This is the foundation layer - no inference, just facts.
    """

    DEFAULT_STEPS = 20.0      # Price bucket size (BTC)
    DEFAULT_DECAY = 0.995     # Per-minute decay factor
    RETENTION_MINUTES = 720   # 12 hours of history

    def __init__(
        self,
        symbol: str = "BTC",
        steps: float = DEFAULT_STEPS,
        decay: float = DEFAULT_DECAY,
        retention_minutes: int = RETENTION_MINUTES,
        log_file: str = None,
        sweep_log_file: str = None
    ):
        self.symbol = symbol
        self.steps = steps
        self.decay = decay
        self.retention_minutes = retention_minutes

        # Accumulated liquidation buckets: price -> LiqBucket
        self.long_liqs: Dict[float, LiqBucket] = {}   # Where longs got liquidated (below price)
        self.short_liqs: Dict[float, LiqBucket] = {}  # Where shorts got liquidated (above price)

        # Rolling event window for offset learning
        self.events: deque = deque(maxlen=retention_minutes * 10)  # ~10 events/min max

        # Offset distribution: track (entry_price - liq_price) for learned projection
        # Keyed by estimated leverage bucket (10x, 25x, 50x, etc.)
        self.offset_samples: Dict[int, deque] = {}

        # Stats
        self.total_long_notional: float = 0.0
        self.total_short_notional: float = 0.0
        self.total_events: int = 0
        self.current_minute: int = 0

        # Optional JSONL logging
        self.log_file = log_file
        self._log_fh = None
        if log_file:
            try:
                self._log_fh = open(log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open tape log file: {e}")

        # Sweep log file
        self.sweep_log_file = sweep_log_file
        self._sweep_log_fh = None
        if sweep_log_file:
            try:
                self._sweep_log_fh = open(sweep_log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open sweep log file: {e}")

    def _bucket_price(self, price: float) -> float:
        """Round price to nearest bucket."""
        return round(price / self.steps) * self.steps

    def on_force_order(
        self,
        timestamp: float,
        side: str,
        price: float,
        qty: float,
        notional: float,
        entry_estimate: float = None
    ) -> None:
        """
        Process a forceOrder liquidation event.

        Args:
            timestamp: Unix timestamp
            side: "long" (longs liquidated) or "short" (shorts liquidated)
            price: Liquidation price
            qty: Size in base asset
            notional: USD value (price * qty)
            entry_estimate: Optional estimated entry price (from inference layer)
        """
        bucket = self._bucket_price(price)

        # Select appropriate bucket dict
        if side == "long":
            buckets = self.long_liqs
            self.total_long_notional += notional
        else:
            buckets = self.short_liqs
            self.total_short_notional += notional

        # Accumulate
        if bucket not in buckets:
            buckets[bucket] = LiqBucket(
                price=bucket,
                notional=0.0,
                count=0,
                last_ts=timestamp,
                side=side
            )

        buckets[bucket].notional += notional
        buckets[bucket].count += 1
        buckets[bucket].last_ts = timestamp

        # Store event
        event = LiquidationEvent(
            timestamp=timestamp,
            symbol=self.symbol,
            side=side,
            price=price,
            qty=qty,
            notional=notional,
            entry_estimate=entry_estimate
        )
        self.events.append(event)
        self.total_events += 1

        # Learn offset distribution if entry estimate provided
        if entry_estimate and entry_estimate > 0:
            self._learn_offset(side, price, entry_estimate)

        # Log to JSONL
        if self._log_fh:
            log_entry = {
                "type": "forceOrder",
                "ts": timestamp,
                "symbol": self.symbol,
                "side": side,
                "price": price,
                "qty": qty,
                "notional": notional,
                "bucket": bucket,
                "entry_est": entry_estimate
            }
            self._log_fh.write(json.dumps(log_entry) + '\n')
            self._log_fh.flush()

    def _learn_offset(self, side: str, liq_price: float, entry_price: float) -> None:
        """
        Learn the offset distribution from observed liquidations.

        offset = (entry_price - liq_price) / entry_price

        For longs: entry > liq (positive offset means how far below entry)
        For shorts: entry < liq (negative offset means how far above entry)
        """
        if entry_price <= 0:
            return

        offset_pct = (entry_price - liq_price) / entry_price

        # Estimate leverage from offset: L ≈ 1 / |offset| (with buffer adjustment)
        # offset ≈ (1/L) + buffer, so L ≈ 1 / (|offset| - buffer)
        buffer_est = 0.002  # 0.2% buffer assumption
        offset_abs = abs(offset_pct)

        if offset_abs > buffer_est:
            lev_est = 1.0 / (offset_abs - buffer_est)
            lev_bucket = self._bucket_leverage(lev_est)

            # Store sample
            if lev_bucket not in self.offset_samples:
                self.offset_samples[lev_bucket] = deque(maxlen=500)
            self.offset_samples[lev_bucket].append((offset_pct, side))

    def _bucket_leverage(self, lev: float) -> int:
        """Bucket leverage to standard tiers: 5, 10, 20, 25, 50, 75, 100, 125."""
        tiers = [5, 10, 20, 25, 50, 75, 100, 125]

        # Find closest tier
        closest = min(tiers, key=lambda t: abs(t - lev))
        return closest

    def on_minute(self, minute_key: int) -> None:
        """
        Called each minute to apply decay and cleanup.
        """
        self.current_minute = minute_key

        # Decay all buckets
        self._apply_decay(self.long_liqs)
        self._apply_decay(self.short_liqs)

    def _apply_decay(self, buckets: Dict[float, LiqBucket]) -> None:
        """Apply decay and remove weak buckets."""
        to_remove = []
        for price, bucket in buckets.items():
            bucket.notional *= self.decay
            if bucket.notional < 100:  # Less than $100 - remove
                to_remove.append(price)

        for price in to_remove:
            del buckets[price]

    def sweep(self, high: float, low: float, minute_key: int = 0) -> Tuple[int, float]:
        """
        Remove liquidation buckets that price has crossed.

        Returns (count_swept, notional_swept)
        """
        swept_count = 0
        swept_notional = 0.0
        ts_now = time.time()

        # Short liqs above price get swept when high >= bucket
        to_remove = [p for p in self.short_liqs if high >= p]
        for price in to_remove:
            bucket = self.short_liqs[price]
            swept_notional += bucket.notional
            swept_count += 1

            # Log sweep event
            if self._sweep_log_fh:
                sweep_entry = {
                    "type": "sweep",
                    "ts": ts_now,
                    "minute_key": minute_key,
                    "side": "short",
                    "bucket": price,
                    # V3: Enhanced sweep logging
                    "total_notional_usd": round(bucket.notional, 2),
                    "trade_count": bucket.count,
                    "peak_notional_usd": round(bucket.notional / max(bucket.count, 1), 2),  # avg as proxy
                    "layer": "tape",
                    "reason": f"high>={price:.0f}",
                    "trigger_high": high
                }
                self._sweep_log_fh.write(json.dumps(sweep_entry) + '\n')
                self._sweep_log_fh.flush()

            del self.short_liqs[price]

        # Long liqs below price get swept when low <= bucket
        to_remove = [p for p in self.long_liqs if low <= p]
        for price in to_remove:
            bucket = self.long_liqs[price]
            swept_notional += bucket.notional
            swept_count += 1

            # Log sweep event
            if self._sweep_log_fh:
                sweep_entry = {
                    "type": "sweep",
                    "ts": ts_now,
                    "minute_key": minute_key,
                    "side": "long",
                    "bucket": price,
                    # V3: Enhanced sweep logging
                    "total_notional_usd": round(bucket.notional, 2),
                    "trade_count": bucket.count,
                    "peak_notional_usd": round(bucket.notional / max(bucket.count, 1), 2),  # avg as proxy
                    "layer": "tape",
                    "reason": f"low<={price:.0f}",
                    "trigger_low": low
                }
                self._sweep_log_fh.write(json.dumps(sweep_entry) + '\n')
                self._sweep_log_fh.flush()

            del self.long_liqs[price]

        return swept_count, swept_notional

    def get_heatmap(self) -> Dict[str, Dict[float, float]]:
        """
        Get current liquidation heatmap for display.

        Returns:
            {"long": {price: notional}, "short": {price: notional}}
        """
        return {
            "long": {b.price: b.notional for b in self.long_liqs.values()},
            "short": {b.price: b.notional for b in self.short_liqs.values()}
        }

    def get_offset_distribution(self, leverage: int = None) -> Dict[str, float]:
        """
        Get learned offset distribution statistics.

        If leverage specified, returns for that bucket only.
        Otherwise returns aggregate statistics.
        """
        if leverage and leverage in self.offset_samples:
            samples = list(self.offset_samples[leverage])
            if not samples:
                return {"mean": 0.0, "std": 0.0, "count": 0}

            offsets = [s[0] for s in samples]
            mean = sum(offsets) / len(offsets)
            variance = sum((x - mean) ** 2 for x in offsets) / len(offsets)
            std = math.sqrt(variance)

            return {"mean": mean, "std": std, "count": len(samples)}

        # Aggregate across all leverages
        all_offsets = []
        for samples in self.offset_samples.values():
            all_offsets.extend([s[0] for s in samples])

        if not all_offsets:
            return {"mean": 0.0, "std": 0.0, "count": 0}

        mean = sum(all_offsets) / len(all_offsets)
        variance = sum((x - mean) ** 2 for x in all_offsets) / len(all_offsets)
        std = math.sqrt(variance)

        return {"mean": mean, "std": std, "count": len(all_offsets)}

    def get_stats(self) -> dict:
        """Get current tape statistics."""
        return {
            "symbol": self.symbol,
            "total_events": self.total_events,
            "total_long_notional": self.total_long_notional,
            "total_short_notional": self.total_short_notional,
            "long_buckets": len(self.long_liqs),
            "short_buckets": len(self.short_liqs),
            "events_in_window": len(self.events),
            "offset_samples_by_lev": {k: len(v) for k, v in self.offset_samples.items()}
        }

    def close(self):
        """Close log file handles."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        if self._sweep_log_fh:
            self._sweep_log_fh.close()
            self._sweep_log_fh = None
