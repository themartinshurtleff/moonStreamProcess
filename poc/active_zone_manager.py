"""
Active Zone Manager - V3 Zone Lifecycle Model

Manages persistent liquidation zones with lifecycle states:
CREATED -> REINFORCED (optional, repeatable) -> SWEPT or EXPIRED

Key features:
- Zones persist across API calls until swept or expired
- Reinforcement: new predictions at same level increase weight
- Weight decay: zones fade if not reinforced
- Cross-zone clustering: merge nearby zones on same side
- Disk persistence: survives restarts

Author: Claude Code V3
"""

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime
from collections import defaultdict

from leverage_config import get_tier_weight, is_tier_disabled, TIER_WEIGHTS

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Zone lifecycle parameters
MAX_ZONE_AGE_MINUTES = 240  # 4 hours - zones expire after this without reinforcement
ZONE_DECAY_PER_MINUTE = 0.995  # Weight multiplier per minute (half-life ~2.3 hours)
MIN_ZONE_WEIGHT = 0.05  # Remove zones below this weight
MAX_ZONE_WEIGHT = 5.0  # Soft cap to prevent runaway brightness
REINFORCEMENT_FACTOR = 0.3  # weight += new_weight * REINFORCEMENT_FACTOR

# Clustering parameters
CLUSTER_DISTANCE_PCT = 0.0005  # 0.05% - merge zones within this distance

# Expiration parameters
EXPIRATION_PRICE_DIST_PCT = 0.02  # 2% - only expire if price has moved this far away

# Persistence parameters
PERSIST_INTERVAL_SECONDS = 60  # Write to disk every minute
DEFAULT_PERSIST_FILE = "liq_active_zones.json"

# Zone lifecycle log file
DEFAULT_ZONE_LOG_FILE = "liq_zones.jsonl"


@dataclass
class ActiveZone:
    """A persistent liquidation zone with lifecycle tracking."""
    price: float
    side: str  # "long" or "short"
    weight: float
    created_at: float  # timestamp
    last_reinforced_at: float  # timestamp
    reinforcement_count: int
    source: str  # "tape", "inference", "combined", "sweep"
    # Track contribution from each leverage tier
    tier_contributions: Dict[int, float] = field(default_factory=dict)
    # Lifecycle state
    state: str = "active"  # "active", "swept", "expired"
    swept_at: Optional[float] = None
    expired_at: Optional[float] = None


class ActiveZoneManager:
    """
    Manages persistent liquidation zones with lifecycle model.

    Thread-safe for concurrent access from multiple sources.
    """

    def __init__(
        self,
        symbol: str = "BTC",
        steps: float = 20.0,
        persist_file: str = None,
        zone_log_file: str = None,
        auto_persist: bool = True
    ):
        self.symbol = symbol
        self.steps = steps
        self._ndigits = max(0, -Decimal(str(steps)).as_tuple().exponent)
        self.persist_file = persist_file or DEFAULT_PERSIST_FILE
        self.zone_log_file = zone_log_file

        # Active zones indexed by (side, price_bucket)
        self._zones: Dict[Tuple[str, float], ActiveZone] = {}
        self._lock = threading.RLock()

        # Current price for expiration check
        self._current_price = 0.0
        self._last_price_update = 0.0

        # Stats
        self.zones_created = 0
        self.zones_reinforced = 0
        self.zones_swept = 0
        self.zones_expired = 0
        self.zones_merged = 0

        # Zone log file handle
        self._zone_log_fh = None
        if zone_log_file:
            try:
                self._zone_log_fh = open(zone_log_file, 'a')
            except Exception as e:
                logger.warning(f"Could not open zone log file: {e}")

        # Auto-persistence thread
        self._persist_thread = None
        self._stop_persist = threading.Event()

        # Load existing zones from disk
        self._load_from_disk()

        # Start auto-persist thread if enabled
        if auto_persist:
            self._start_persist_thread()

        logger.info(f"ActiveZoneManager initialized for {symbol}")
        logger.info(f"  Loaded {len(self._zones)} active zones from disk")
        logger.info(f"  Persistence: {self.persist_file}")
        if zone_log_file:
            logger.info(f"  Zone log: {zone_log_file}")

    def _bucket_price(self, price: float) -> float:
        """Round price to nearest bucket with precision matching step size."""
        return round(round(price / self.steps) * self.steps, self._ndigits)

    def _zone_key(self, side: str, price: float) -> Tuple[str, float]:
        """Create zone key from side and bucketed price."""
        return (side, self._bucket_price(price))

    def _log_zone_event(self, event_type: str, zone: ActiveZone, **extra) -> None:
        """Log zone lifecycle event to JSONL file."""
        if not self._zone_log_fh:
            return

        entry = {
            "type": event_type,
            "ts": time.time(),
            "side": zone.side,
            "price": zone.price,
            "weight": round(zone.weight, 4),
            "source": zone.source,
            **extra
        }
        try:
            self._zone_log_fh.write(json.dumps(entry) + '\n')
            self._zone_log_fh.flush()
        except Exception as e:
            logger.warning(f"Failed to write zone log: {e}")

    def create_or_reinforce(
        self,
        price: float,
        side: str,
        weight: float,
        source: str = "inference",
        tier: int = None,
        confidence: float = 1.0
    ) -> Tuple[str, ActiveZone]:
        """
        Create a new zone or reinforce an existing one.

        Returns:
            ("created", zone) or ("reinforced", zone)
        """
        with self._lock:
            key = self._zone_key(side, price)
            bucket = self._bucket_price(price)

            # Apply tier weight if provided
            tier_weight = get_tier_weight(tier) if tier else 1.0
            if is_tier_disabled(tier) if tier else False:
                return ("skipped", None)

            # Adjust weight by tier importance and confidence
            adjusted_weight = weight * tier_weight * confidence

            if key in self._zones:
                # Reinforce existing zone
                zone = self._zones[key]
                old_weight = zone.weight

                # Weight increases with reinforcement
                zone.weight = min(
                    zone.weight + adjusted_weight * REINFORCEMENT_FACTOR,
                    MAX_ZONE_WEIGHT
                )
                zone.last_reinforced_at = time.time()
                zone.reinforcement_count += 1

                # Track tier contribution
                if tier:
                    zone.tier_contributions[tier] = zone.tier_contributions.get(tier, 0) + adjusted_weight

                # Update source to combined if different
                if zone.source != source and zone.source != "combined":
                    zone.source = "combined"

                self.zones_reinforced += 1
                self._log_zone_event(
                    "zone_reinforced", zone,
                    count=zone.reinforcement_count,
                    weight_before=round(old_weight, 4)
                )

                return ("reinforced", zone)
            else:
                # Create new zone
                zone = ActiveZone(
                    price=bucket,
                    side=side,
                    weight=adjusted_weight,
                    created_at=time.time(),
                    last_reinforced_at=time.time(),
                    reinforcement_count=0,
                    source=source,
                    tier_contributions={tier: adjusted_weight} if tier else {}
                )
                self._zones[key] = zone
                self.zones_created += 1

                self._log_zone_event("zone_created", zone)

                return ("created", zone)

    def sweep(self, high: float, low: float, minute_key: int = 0) -> List[ActiveZone]:
        """
        Sweep zones that price has crossed.

        Returns list of swept zones.
        """
        swept = []
        ts_now = time.time()

        with self._lock:
            keys_to_remove = []

            for key, zone in self._zones.items():
                side, bucket = key

                # Short zones above price get swept when high >= bucket
                if side == "short" and high >= bucket:
                    zone.state = "swept"
                    zone.swept_at = ts_now
                    swept.append(zone)
                    keys_to_remove.append(key)

                    lifespan_min = (ts_now - zone.created_at) / 60
                    self._log_zone_event(
                        "zone_swept", zone,
                        final_weight=round(zone.weight, 4),
                        lifespan_min=round(lifespan_min, 1),
                        reinforcement_count=zone.reinforcement_count
                    )

                # Long zones below price get swept when low <= bucket
                elif side == "long" and low <= bucket:
                    zone.state = "swept"
                    zone.swept_at = ts_now
                    swept.append(zone)
                    keys_to_remove.append(key)

                    lifespan_min = (ts_now - zone.created_at) / 60
                    self._log_zone_event(
                        "zone_swept", zone,
                        final_weight=round(zone.weight, 4),
                        lifespan_min=round(lifespan_min, 1),
                        reinforcement_count=zone.reinforcement_count
                    )

            for key in keys_to_remove:
                del self._zones[key]

            self.zones_swept += len(swept)

        return swept

    def apply_decay_and_expire(self, current_price: float = None) -> int:
        """
        Apply weight decay and expire old zones.

        Returns count of expired zones.
        """
        if current_price:
            self._current_price = current_price
            self._last_price_update = time.time()

        expired_count = 0
        ts_now = time.time()

        with self._lock:
            keys_to_remove = []

            for key, zone in self._zones.items():
                # Apply decay
                zone.weight *= ZONE_DECAY_PER_MINUTE

                # Check for expiration
                should_expire = False

                # Expire if weight too low
                if zone.weight < MIN_ZONE_WEIGHT:
                    should_expire = True

                # Expire if too old AND price has moved away
                age_minutes = (ts_now - zone.last_reinforced_at) / 60
                if age_minutes > MAX_ZONE_AGE_MINUTES and self._current_price > 0:
                    dist_pct = abs(zone.price - self._current_price) / self._current_price
                    if dist_pct > EXPIRATION_PRICE_DIST_PCT:
                        should_expire = True

                if should_expire:
                    zone.state = "expired"
                    zone.expired_at = ts_now
                    keys_to_remove.append(key)

                    lifespan_min = (ts_now - zone.created_at) / 60
                    self._log_zone_event(
                        "zone_expired", zone,
                        final_weight=round(zone.weight, 4),
                        lifespan_min=round(lifespan_min, 1),
                        age_since_reinforce_min=round(age_minutes, 1)
                    )

            for key in keys_to_remove:
                del self._zones[key]

            expired_count = len(keys_to_remove)
            self.zones_expired += expired_count

        return expired_count

    def merge_nearby_zones(self) -> int:
        """
        Merge zones on the same side that are within clustering distance.

        Returns count of merges performed.
        """
        merge_count = 0

        with self._lock:
            for side in ["long", "short"]:
                # Get all zones for this side sorted by price
                side_zones = [
                    (key, zone) for key, zone in self._zones.items()
                    if key[0] == side
                ]
                side_zones.sort(key=lambda x: x[1].price)

                i = 0
                while i < len(side_zones) - 1:
                    key1, zone1 = side_zones[i]
                    key2, zone2 = side_zones[i + 1]

                    # Check if within clustering distance
                    mid_price = (zone1.price + zone2.price) / 2
                    dist_pct = abs(zone1.price - zone2.price) / mid_price

                    if dist_pct <= CLUSTER_DISTANCE_PCT:
                        # Merge: keep higher weight zone, sum weights (capped)
                        if zone1.weight >= zone2.weight:
                            keep_zone = zone1
                            remove_key = key2
                            other_zone = zone2
                        else:
                            keep_zone = zone2
                            remove_key = key1
                            other_zone = zone1

                        # Sum weights (capped)
                        keep_zone.weight = min(
                            keep_zone.weight + other_zone.weight,
                            MAX_ZONE_WEIGHT
                        )

                        # Combine tier contributions
                        for tier, contrib in other_zone.tier_contributions.items():
                            keep_zone.tier_contributions[tier] = \
                                keep_zone.tier_contributions.get(tier, 0) + contrib

                        # Update source
                        if keep_zone.source != other_zone.source:
                            keep_zone.source = "combined"

                        # Remove merged zone
                        del self._zones[remove_key]
                        side_zones = [x for x in side_zones if x[0] != remove_key]

                        merge_count += 1
                        self.zones_merged += 1

                        # Don't increment i - check against next zone
                    else:
                        i += 1

        return merge_count

    def get_active_zones(
        self,
        side: str = None,
        min_leverage: int = None,
        max_leverage: int = None
    ) -> List[Dict]:
        """
        Get active zones for API response.

        Args:
            side: Filter by side ("long" or "short")
            min_leverage: Only include zones with tier contribution >= this
            max_leverage: Only include zones with tier contribution <= this

        Returns:
            List of zone dictionaries suitable for API response
        """
        with self._lock:
            zones = []

            for key, zone in self._zones.items():
                zone_side, bucket = key

                # Filter by side
                if side and zone_side != side:
                    continue

                # Filter by leverage tier
                if min_leverage or max_leverage:
                    relevant_contrib = 0
                    for tier, contrib in zone.tier_contributions.items():
                        if min_leverage and tier < min_leverage:
                            continue
                        if max_leverage and tier > max_leverage:
                            continue
                        relevant_contrib += contrib

                    # Skip if no relevant contribution
                    if relevant_contrib < 0.01:
                        continue

                zones.append({
                    "price": zone.price,
                    "side": zone.side,
                    "weight": round(zone.weight, 4),
                    "created_at": int(zone.created_at * 1000),  # ms timestamp
                    "last_reinforced_at": int(zone.last_reinforced_at * 1000),
                    "reinforcement_count": zone.reinforcement_count,
                    "source": zone.source,
                    "tier_contributions": {
                        str(k): round(v, 4)
                        for k, v in zone.tier_contributions.items()
                    }
                })

            # Sort by weight descending
            zones.sort(key=lambda z: -z["weight"])

            return zones

    def get_summary(self) -> Dict:
        """Get summary statistics for cycle logging."""
        with self._lock:
            long_count = sum(1 for k in self._zones if k[0] == "long")
            short_count = sum(1 for k in self._zones if k[0] == "short")
            total_long_weight = sum(
                z.weight for k, z in self._zones.items() if k[0] == "long"
            )
            total_short_weight = sum(
                z.weight for k, z in self._zones.items() if k[0] == "short"
            )

            return {
                "active_zones_long": long_count,
                "active_zones_short": short_count,
                "total_weight_long": round(total_long_weight, 2),
                "total_weight_short": round(total_short_weight, 2),
                "zones_created_total": self.zones_created,
                "zones_reinforced_total": self.zones_reinforced,
                "zones_swept_total": self.zones_swept,
                "zones_expired_total": self.zones_expired,
                "zones_merged_total": self.zones_merged
            }

    def _load_from_disk(self) -> bool:
        """Load zones from persist file."""
        if not self.persist_file or not os.path.exists(self.persist_file):
            return False

        try:
            with open(self.persist_file, 'r') as f:
                data = json.load(f)

            if data.get('symbol') != self.symbol:
                logger.warning(f"Zone file symbol mismatch: {data.get('symbol')} != {self.symbol}")
                return False

            ts_now = time.time()
            loaded = 0
            expired_at_load = 0

            for zone_data in data.get('zones', []):
                # Check if zone should be expired at load time
                age_minutes = (ts_now - zone_data.get('last_reinforced_at', 0)) / 60
                if age_minutes > MAX_ZONE_AGE_MINUTES:
                    expired_at_load += 1
                    continue

                zone = ActiveZone(
                    price=zone_data['price'],
                    side=zone_data['side'],
                    weight=zone_data['weight'],
                    created_at=zone_data.get('created_at', ts_now),
                    last_reinforced_at=zone_data.get('last_reinforced_at', ts_now),
                    reinforcement_count=zone_data.get('reinforcement_count', 0),
                    source=zone_data.get('source', 'unknown'),
                    tier_contributions={
                        int(k): v for k, v in zone_data.get('tier_contributions', {}).items()
                    }
                )

                key = self._zone_key(zone.side, zone.price)
                self._zones[key] = zone
                loaded += 1

            if expired_at_load > 0:
                logger.info(f"  Expired {expired_at_load} zones older than {MAX_ZONE_AGE_MINUTES} minutes at load")

            return True

        except Exception as e:
            logger.error(f"Error loading zones from disk: {e}")
            return False

    def _save_to_disk(self) -> bool:
        """Save zones to persist file."""
        if not self.persist_file:
            return False

        try:
            with self._lock:
                zones_data = []
                for key, zone in self._zones.items():
                    zones_data.append({
                        'price': zone.price,
                        'side': zone.side,
                        'weight': zone.weight,
                        'created_at': zone.created_at,
                        'last_reinforced_at': zone.last_reinforced_at,
                        'reinforcement_count': zone.reinforcement_count,
                        'source': zone.source,
                        'tier_contributions': zone.tier_contributions
                    })

                data = {
                    'symbol': self.symbol,
                    'saved_at': time.time(),
                    'zone_count': len(zones_data),
                    'zones': zones_data
                }

            # Write to temp file first, then rename (atomic)
            temp_file = self.persist_file + '.tmp'
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)

            os.replace(temp_file, self.persist_file)
            return True

        except Exception as e:
            logger.error(f"Error saving zones to disk: {e}")
            return False

    def _start_persist_thread(self):
        """Start background thread for periodic persistence."""
        def persist_loop():
            while not self._stop_persist.wait(timeout=PERSIST_INTERVAL_SECONDS):
                self._save_to_disk()

        self._persist_thread = threading.Thread(target=persist_loop, daemon=True)
        self._persist_thread.start()

    def stop(self):
        """Stop persistence thread and save final state."""
        self._stop_persist.set()
        if self._persist_thread:
            self._persist_thread.join(timeout=5)
        self._save_to_disk()

        if self._zone_log_fh:
            self._zone_log_fh.close()

    def __del__(self):
        """Cleanup on deletion."""
        try:
            self.stop()
        except:
            pass


# Global manager instance (created on first access)
_global_manager: Optional[ActiveZoneManager] = None
_global_manager_lock = threading.Lock()


def get_zone_manager(
    symbol: str = "BTC",
    persist_dir: str = None,
    create_if_missing: bool = True
) -> Optional[ActiveZoneManager]:
    """Get or create global zone manager instance."""
    global _global_manager

    with _global_manager_lock:
        if _global_manager is None and create_if_missing:
            persist_file = None
            zone_log_file = None

            if persist_dir:
                persist_file = os.path.join(persist_dir, DEFAULT_PERSIST_FILE)
                zone_log_file = os.path.join(persist_dir, DEFAULT_ZONE_LOG_FILE)

            _global_manager = ActiveZoneManager(
                symbol=symbol,
                persist_file=persist_file,
                zone_log_file=zone_log_file
            )

        return _global_manager
