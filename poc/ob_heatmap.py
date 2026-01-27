#!/usr/bin/env python3
"""
Orderbook Heatmap Logger - 30-second DoM screenshot frames.

Aggregates Binance futures orderbook data into 30s frames for candle-overlay heatmaps.
Keeps last 12 hours (1440 frames) with persistence across restarts.
"""

import os
import struct
import time
import threading
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# Constants
FRAME_INTERVAL_SEC = 30
HISTORY_FRAMES = 1440  # 12 hours of 30s frames
FRAME_RECORD_SIZE = 4096  # Fixed 4KB per frame for fast seek
MAX_PRICE_BUCKETS = 1000  # Max buckets per frame
DEFAULT_STEP = 20.0
DEFAULT_RANGE_PCT = 0.10

# Binary record header format (76 bytes)
# ts(d) + src(d) + step(d) + price_min(d) + price_max(d) + n_prices(I) +
# norm_p50(d) + norm_p95(d) + total_bid(d) + total_ask(d)
HEADER_FORMAT = '<ddddddIddd'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 76 bytes


@dataclass
class OrderbookFrame:
    """Single 30-second orderbook heatmap frame."""
    ts: float  # Epoch seconds (30s boundary)
    src: float  # Reference price (mark price)
    step: float  # Bucket size
    price_min: float  # Lower bound
    price_max: float  # Upper bound
    bid_u8: bytes  # u8 intensity array for bids
    ask_u8: bytes  # u8 intensity array for asks
    norm_p50: float  # 50th percentile used for scaling
    norm_p95: float  # 95th percentile used for scaling
    total_bid_notional: float  # Sum of bid notional in band
    total_ask_notional: float  # Sum of ask notional in band

    @property
    def n_prices(self) -> int:
        return len(self.bid_u8)

    def get_prices(self) -> List[float]:
        """Generate price grid from bounds."""
        prices = []
        p = self.price_min
        while p <= self.price_max + 0.01:  # Small epsilon for float comparison
            prices.append(p)
            p += self.step
        return prices[:self.n_prices]  # Ensure length matches

    def to_bytes(self) -> bytes:
        """Serialize frame to fixed-size binary record."""
        header = struct.pack(
            HEADER_FORMAT,
            self.ts, self.src, self.step, self.price_min, self.price_max,
            self.n_prices, self.norm_p50, self.norm_p95,
            self.total_bid_notional, self.total_ask_notional
        )
        # Pad bid_u8 and ask_u8 to MAX_PRICE_BUCKETS
        bid_padded = self.bid_u8.ljust(MAX_PRICE_BUCKETS, b'\x00')[:MAX_PRICE_BUCKETS]
        ask_padded = self.ask_u8.ljust(MAX_PRICE_BUCKETS, b'\x00')[:MAX_PRICE_BUCKETS]
        # Total: 76 + 1000 + 1000 = 2076, pad to 4096
        record = header + bid_padded + ask_padded
        record = record.ljust(FRAME_RECORD_SIZE, b'\x00')
        return record

    @classmethod
    def from_bytes(cls, data: bytes) -> 'OrderbookFrame':
        """Deserialize frame from binary record."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Record too short: {len(data)} < {HEADER_SIZE}")

        header = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
        ts, src, step, price_min, price_max, n_prices, norm_p50, norm_p95, total_bid, total_ask = header

        # Extract intensity arrays
        bid_start = HEADER_SIZE
        ask_start = HEADER_SIZE + MAX_PRICE_BUCKETS
        bid_u8 = data[bid_start:bid_start + n_prices]
        ask_u8 = data[ask_start:ask_start + n_prices]

        return cls(
            ts=ts, src=src, step=step, price_min=price_min, price_max=price_max,
            bid_u8=bid_u8, ask_u8=ask_u8,
            norm_p50=norm_p50, norm_p95=norm_p95,
            total_bid_notional=total_bid, total_ask_notional=total_ask
        )


class OrderbookAccumulator:
    """
    Accumulates orderbook updates within 30-second windows.

    On each depth update, updates bid/ask bucket notionals.
    At 30s boundary, emits a frame with normalized intensities.
    """

    def __init__(
        self,
        step: float = DEFAULT_STEP,
        range_pct: float = DEFAULT_RANGE_PCT,
        on_frame_callback=None
    ):
        self.step = step
        self.range_pct = range_pct
        self.on_frame_callback = on_frame_callback

        self._lock = threading.Lock()

        # Current window state
        self._current_slot: int = 0  # 30s slot (ts // 30)
        self._src: float = 0.0  # Reference price
        self._bid_notional: Dict[float, float] = {}  # {bucket_price: notional_usd}
        self._ask_notional: Dict[float, float] = {}

        # Track last update for staleness detection
        self._last_update_ts: float = 0.0

    def _bucket_price(self, price: float) -> float:
        """Round price to nearest bucket."""
        return round(price / self.step) * self.step

    def _get_slot(self, ts: float) -> int:
        """Get 30s slot for timestamp."""
        return int(ts // FRAME_INTERVAL_SEC)

    def on_depth_update(
        self,
        bids: List[Tuple[float, float]],  # [(price, qty), ...]
        asks: List[Tuple[float, float]],
        src_price: float,
        timestamp: float = None
    ) -> Optional[OrderbookFrame]:
        """
        Process a depth update.

        Args:
            bids: List of (price, qty) tuples
            asks: List of (price, qty) tuples
            src_price: Reference price (mark price)
            timestamp: Event timestamp (defaults to current time)

        Returns:
            OrderbookFrame if a 30s boundary was crossed, else None
        """
        if timestamp is None:
            timestamp = time.time()

        current_slot = self._get_slot(timestamp)
        emitted_frame = None

        with self._lock:
            # Check for slot rollover
            if self._current_slot > 0 and current_slot > self._current_slot:
                # Emit frame for completed slot
                emitted_frame = self._emit_frame()
                # Reset for new slot
                self._bid_notional.clear()
                self._ask_notional.clear()

            self._current_slot = current_slot
            self._src = src_price
            self._last_update_ts = timestamp

            # Compute price bounds for this update
            price_min = src_price * (1 - self.range_pct)
            price_max = src_price * (1 + self.range_pct)

            # Update bid notionals (replace, not accumulate - snapshot style)
            self._bid_notional.clear()
            for price, qty in bids:
                if price_min <= price <= price_max:
                    bucket = self._bucket_price(price)
                    notional = price * qty
                    self._bid_notional[bucket] = self._bid_notional.get(bucket, 0) + notional

            # Update ask notionals
            self._ask_notional.clear()
            for price, qty in asks:
                if price_min <= price <= price_max:
                    bucket = self._bucket_price(price)
                    notional = price * qty
                    self._ask_notional[bucket] = self._ask_notional.get(bucket, 0) + notional

        # Callback outside lock
        if emitted_frame and self.on_frame_callback:
            self.on_frame_callback(emitted_frame)

        return emitted_frame

    def _emit_frame(self) -> OrderbookFrame:
        """Emit frame for current slot (called with lock held)."""
        ts = self._current_slot * FRAME_INTERVAL_SEC
        src = self._src

        # Compute bounds
        price_min = round((src * (1 - self.range_pct)) / self.step) * self.step
        price_max = round((src * (1 + self.range_pct)) / self.step) * self.step

        # Generate price grid
        prices = []
        p = price_min
        while p <= price_max + 0.01:
            prices.append(p)
            p += self.step

        n_prices = len(prices)
        if n_prices > MAX_PRICE_BUCKETS:
            # Truncate if too many buckets
            prices = prices[:MAX_PRICE_BUCKETS]
            n_prices = MAX_PRICE_BUCKETS
            price_max = prices[-1]

        # Build raw notional arrays
        bid_raw = np.array([self._bid_notional.get(p, 0.0) for p in prices], dtype=np.float64)
        ask_raw = np.array([self._ask_notional.get(p, 0.0) for p in prices], dtype=np.float64)

        # Compute totals
        total_bid = float(np.sum(bid_raw))
        total_ask = float(np.sum(ask_raw))

        # Compute percentiles for normalization (combined bid+ask)
        all_nonzero = np.concatenate([bid_raw[bid_raw > 0], ask_raw[ask_raw > 0]])
        if len(all_nonzero) > 0:
            p50 = float(np.percentile(all_nonzero, 50))
            p95 = float(np.percentile(all_nonzero, 95))
        else:
            p50 = 0.0
            p95 = 1.0  # Avoid division by zero

        # Normalize to u8
        def normalize_to_u8(arr: np.ndarray, p50: float, p95: float) -> bytes:
            if p95 <= p50:
                return bytes(len(arr))  # All zeros
            scaled = (arr - p50) / (p95 - p50)
            scaled = np.clip(scaled * 255, 0, 255).astype(np.uint8)
            return bytes(scaled)

        bid_u8 = normalize_to_u8(bid_raw, p50, p95)
        ask_u8 = normalize_to_u8(ask_raw, p50, p95)

        frame = OrderbookFrame(
            ts=ts,
            src=src,
            step=self.step,
            price_min=price_min,
            price_max=price_max,
            bid_u8=bid_u8,
            ask_u8=ask_u8,
            norm_p50=p50,
            norm_p95=p95,
            total_bid_notional=total_bid,
            total_ask_notional=total_ask
        )

        # Log diagnostics
        bid_nonzero = sum(1 for b in bid_u8 if b > 0)
        ask_nonzero = sum(1 for a in ask_u8 if a > 0)
        logger.info(
            f"[OB_HEATMAP] ts={ts} src={src:.2f} range=[{price_min:.0f}, {price_max:.0f}] "
            f"n_prices={n_prices} bid_notional={total_bid:.0f} ask_notional={total_ask:.0f} "
            f"p50={p50:.0f} p95={p95:.0f} bid_nonzero={bid_nonzero} ask_nonzero={ask_nonzero}"
        )

        return frame

    def force_emit(self) -> Optional[OrderbookFrame]:
        """Force emit current frame (for shutdown/testing)."""
        with self._lock:
            if self._current_slot > 0 and self._src > 0:
                return self._emit_frame()
        return None


class OrderbookHeatmapBuffer:
    """
    In-memory ring buffer with binary file persistence.

    Maintains last 12 hours of 30s frames.
    Fast reads from memory, persistent across restarts.
    """

    def __init__(self, persistence_path: str):
        self.persistence_path = persistence_path
        self._lock = threading.RLock()
        self._frames: deque = deque(maxlen=HISTORY_FRAMES)
        self._frames_written_since_compact = 0
        self._compact_threshold = 100  # Compact every 100 frames (~50 min)

        # Load existing frames on init
        self._load_from_disk()

    def _load_from_disk(self):
        """Load frames from persistence file on startup."""
        if not os.path.exists(self.persistence_path):
            logger.info(f"[OB_HEATMAP] No persistence file found at {self.persistence_path}")
            return

        file_size = os.path.getsize(self.persistence_path)
        total_frames = file_size // FRAME_RECORD_SIZE

        if total_frames == 0:
            logger.info("[OB_HEATMAP] Persistence file is empty")
            return

        # Calculate how many frames to load (last HISTORY_FRAMES)
        frames_to_load = min(total_frames, HISTORY_FRAMES)
        start_offset = (total_frames - frames_to_load) * FRAME_RECORD_SIZE

        loaded = 0
        oldest_ts = None
        newest_ts = None

        try:
            with open(self.persistence_path, 'rb') as f:
                f.seek(start_offset)
                for _ in range(frames_to_load):
                    record = f.read(FRAME_RECORD_SIZE)
                    if len(record) < FRAME_RECORD_SIZE:
                        break
                    try:
                        frame = OrderbookFrame.from_bytes(record)
                        self._frames.append(frame)
                        loaded += 1
                        if oldest_ts is None:
                            oldest_ts = frame.ts
                        newest_ts = frame.ts
                    except Exception as e:
                        logger.warning(f"[OB_HEATMAP] Failed to parse frame: {e}")
        except Exception as e:
            logger.error(f"[OB_HEATMAP] Failed to load from disk: {e}")
            return

        logger.info(
            f"[OB_HEATMAP] Loaded {loaded} frames from disk, "
            f"oldest={oldest_ts}, newest={newest_ts}"
        )

    def add_frame(self, frame: OrderbookFrame):
        """Add a new frame to buffer and persist."""
        with self._lock:
            self._frames.append(frame)
            self._persist_frame(frame)

            self._frames_written_since_compact += 1
            if self._frames_written_since_compact >= self._compact_threshold:
                self._maybe_compact()
                self._frames_written_since_compact = 0

    def _persist_frame(self, frame: OrderbookFrame):
        """Append frame to persistence file."""
        try:
            with open(self.persistence_path, 'ab') as f:
                f.write(frame.to_bytes())
        except Exception as e:
            logger.error(f"[OB_HEATMAP] Failed to persist frame: {e}")

    def _maybe_compact(self):
        """Compact file if it exceeds HISTORY_FRAMES."""
        if not os.path.exists(self.persistence_path):
            return

        file_size = os.path.getsize(self.persistence_path)
        total_frames = file_size // FRAME_RECORD_SIZE

        if total_frames <= HISTORY_FRAMES:
            return

        # Rewrite with only last HISTORY_FRAMES
        logger.info(f"[OB_HEATMAP] Compacting file from {total_frames} to {HISTORY_FRAMES} frames")

        try:
            # Read last HISTORY_FRAMES
            start_offset = (total_frames - HISTORY_FRAMES) * FRAME_RECORD_SIZE
            with open(self.persistence_path, 'rb') as f:
                f.seek(start_offset)
                data = f.read()

            # Write back
            with open(self.persistence_path, 'wb') as f:
                f.write(data)

            logger.info(f"[OB_HEATMAP] Compaction complete, file now {len(data)} bytes")
        except Exception as e:
            logger.error(f"[OB_HEATMAP] Compaction failed: {e}")

    def get_latest(self) -> Optional[OrderbookFrame]:
        """Get most recent frame."""
        with self._lock:
            if self._frames:
                return self._frames[-1]
            return None

    def get_frames(
        self,
        minutes: int = 360,
        stride: int = 1
    ) -> List[OrderbookFrame]:
        """
        Get frames for last N minutes with optional stride.

        Args:
            minutes: How many minutes of history
            stride: Return every Nth frame (1 = all)

        Returns:
            List of frames (oldest first)
        """
        # 30s frames, so minutes * 2 = number of frames
        n_frames = minutes * 2

        with self._lock:
            if not self._frames:
                return []

            # Get last n_frames
            frames = list(self._frames)[-n_frames:]

            # Apply stride
            if stride > 1:
                frames = frames[::stride]

            return frames

    def get_stats(self) -> dict:
        """Get buffer statistics."""
        with self._lock:
            frames_in_memory = len(self._frames)
            last_ts = self._frames[-1].ts if self._frames else 0
            oldest_ts = self._frames[0].ts if self._frames else 0

        file_size = 0
        frames_on_disk = 0
        if os.path.exists(self.persistence_path):
            file_size = os.path.getsize(self.persistence_path)
            frames_on_disk = file_size // FRAME_RECORD_SIZE

        return {
            "frames_in_memory": frames_in_memory,
            "frames_on_disk": frames_on_disk,
            "last_ts": last_ts,
            "oldest_ts": oldest_ts,
            "file_size_bytes": file_size
        }


def build_unified_grid(
    frames: List[OrderbookFrame],
    price_min: float = None,
    price_max: float = None,
    step: float = DEFAULT_STEP
) -> Tuple[List[float], float, float]:
    """
    Build unified price grid covering all frames.

    Args:
        frames: List of frames to cover
        price_min: Override min (else use min across frames)
        price_max: Override max (else use max across frames)
        step: Bucket size

    Returns:
        (prices, price_min, price_max)
    """
    if not frames:
        return [], 0.0, 0.0

    if price_min is None:
        price_min = min(f.price_min for f in frames)
    if price_max is None:
        price_max = max(f.price_max for f in frames)

    # Round to step
    price_min = round(price_min / step) * step
    price_max = round(price_max / step) * step

    prices = []
    p = price_min
    while p <= price_max + 0.01:
        prices.append(p)
        p += step

    return prices, price_min, price_max


def resample_frame_to_grid(
    frame: OrderbookFrame,
    target_prices: List[float],
    step: float
) -> Tuple[List[int], List[int]]:
    """
    Resample frame's u8 arrays to target price grid.

    Returns:
        (bid_u8_list, ask_u8_list) as lists of ints
    """
    frame_prices = frame.get_prices()

    # Build lookup from frame price -> index
    frame_price_to_idx = {round(p / step) * step: i for i, p in enumerate(frame_prices)}

    bid_resampled = []
    ask_resampled = []

    for tp in target_prices:
        bucket = round(tp / step) * step
        if bucket in frame_price_to_idx:
            idx = frame_price_to_idx[bucket]
            if idx < len(frame.bid_u8):
                bid_resampled.append(frame.bid_u8[idx])
            else:
                bid_resampled.append(0)
            if idx < len(frame.ask_u8):
                ask_resampled.append(frame.ask_u8[idx])
            else:
                ask_resampled.append(0)
        else:
            bid_resampled.append(0)
            ask_resampled.append(0)

    return bid_resampled, ask_resampled
