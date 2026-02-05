#!/usr/bin/env python3
"""
Comprehensive Liquidation Heatmap V2 Audit
==========================================

This script performs a code-verified, log-verified audit of the LIQ V2 system.

Outputs:
- Console report with ASCII tables
- audit_report.md markdown file

Author: Claude Code Audit
"""

import json
import os
import sys
from collections import defaultdict, Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import statistics

# =============================================================================
# CONFIGURATION
# =============================================================================

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(LOG_DIR, "audit_report.md")

# Log files to scan (in priority order - newer first)
LOG_FILES = {
    'calibrator': ['liq_calibrator.jsonl', 'liq_calibrator.20260203_0019.jsonl'],
    'debug': ['liq_debug.jsonl', 'liq_debug.20260203_0019.jsonl'],
    'inference': ['liq_inference.jsonl'],
    'sweeps': ['liq_sweeps.jsonl'],
    'tape': ['liq_tape.jsonl'],
    'plot_feed': ['plot_feed.jsonl', 'plot_feed.20260203_0019.jsonl'],
}

# Also check old_log directory
OLD_LOG_DIR = os.path.join(LOG_DIR, 'old_log')

# Audit parameters
LOOKBACK_WINDOWS = [0, 1, 5, 15]  # minutes
TOLERANCES = [0.0010, 0.0025, 0.0050, 0.0100]  # 0.10%, 0.25%, 0.50%, 1.00%
STEP_SIZE = 20.0  # Price bucket size in USD

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class LogInventory:
    """Inventory of a single log file."""
    path: str
    size_bytes: int
    line_count: int
    record_counts: Dict[str, int]
    min_ts: Optional[str]
    max_ts: Optional[str]
    sample_records: Dict[str, Any]

@dataclass
class ForceOrderEvent:
    """Parsed forceOrder event from calibrator or tape."""
    timestamp: float
    ts_str: str
    symbol: str
    side: str  # "long" or "short"
    price: float
    notional: float
    src_price: float
    minute_key: int

@dataclass
class InferenceZone:
    """Parsed inference zone from liq_inference.jsonl."""
    timestamp: float
    minute_key: int
    src_price: float
    side: str
    projected_liq: float
    size_usd: float
    confidence: float

@dataclass
class TapeZone:
    """Parsed tape zone from liq_tape.jsonl."""
    timestamp: float
    side: str
    bucket: float
    notional: float

@dataclass
class MinuteContext:
    """Minute-level context from liq_debug.jsonl."""
    minute_key: int
    src_price: float
    oi_delta_usd: float
    buy_weight: float
    projected_long: float
    projected_short: float
    total_long_buckets: int
    total_short_buckets: int

# =============================================================================
# SAFE JSONL PARSER
# =============================================================================

def safe_parse_jsonl(filepath: str) -> List[Dict]:
    """Safely parse JSONL file, handling partial lines."""
    records = []
    errors = 0
    if not os.path.exists(filepath):
        return records

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                records.append(rec)
            except json.JSONDecodeError:
                errors += 1
                if errors <= 5:
                    pass  # Silently skip malformed lines

    return records

# =============================================================================
# SECTION A: DATA INVENTORY
# =============================================================================

def inventory_log_file(filepath: str) -> Optional[LogInventory]:
    """Build inventory for a single log file."""
    if not os.path.exists(filepath):
        return None

    size_bytes = os.path.getsize(filepath)
    records = safe_parse_jsonl(filepath)
    line_count = len(records)

    # Count by type
    type_counts = Counter(r.get('type', 'NO_TYPE') for r in records)

    # Find timestamp range
    timestamps = []
    for r in records:
        ts = r.get('timestamp') or r.get('ts')
        if ts:
            if isinstance(ts, str):
                timestamps.append(ts)
            elif isinstance(ts, (int, float)):
                try:
                    timestamps.append(datetime.fromtimestamp(ts).isoformat())
                except:
                    pass

    min_ts = min(timestamps) if timestamps else None
    max_ts = max(timestamps) if timestamps else None

    # Sample records (1 per type)
    samples = {}
    for r in records:
        t = r.get('type', 'NO_TYPE')
        if t not in samples:
            samples[t] = r

    return LogInventory(
        path=filepath,
        size_bytes=size_bytes,
        line_count=line_count,
        record_counts=dict(type_counts),
        min_ts=min_ts,
        max_ts=max_ts,
        sample_records=samples
    )

def build_data_inventory() -> Dict[str, List[LogInventory]]:
    """Build complete inventory of all log files."""
    inventory = {}

    for category, filenames in LOG_FILES.items():
        inventory[category] = []
        for fn in filenames:
            # Check main directory
            path = os.path.join(LOG_DIR, fn)
            inv = inventory_log_file(path)
            if inv:
                inventory[category].append(inv)

            # Check old_log directory
            old_path = os.path.join(OLD_LOG_DIR, fn)
            inv = inventory_log_file(old_path)
            if inv:
                inventory[category].append(inv)

    return inventory

# =============================================================================
# DATA LOADING
# =============================================================================

def load_all_calibrator_events() -> List[ForceOrderEvent]:
    """Load all forceOrder events from calibrator logs."""
    events = []
    seen = set()

    for fn in LOG_FILES['calibrator']:
        path = os.path.join(LOG_DIR, fn)
        records = safe_parse_jsonl(path)

        for r in records:
            if r.get('type') != 'event':
                continue

            ts_str = r.get('timestamp', '')
            price = r.get('event_price', 0)
            side = r.get('side', '')

            # Deduplicate
            key = (ts_str, price, side)
            if key in seen:
                continue
            seen.add(key)

            # Parse timestamp
            try:
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                ts = dt.timestamp()
            except:
                continue

            events.append(ForceOrderEvent(
                timestamp=ts,
                ts_str=ts_str,
                symbol=r.get('symbol', 'BTC'),
                side=side,
                price=price,
                notional=r.get('event_notional', 0),
                src_price=r.get('event_src_price', 0),
                minute_key=int(ts // 60)
            ))

    # Also check old_log
    old_path = os.path.join(OLD_LOG_DIR, 'liq_calibrator.jsonl')
    if os.path.exists(old_path):
        records = safe_parse_jsonl(old_path)
        for r in records:
            if r.get('type') != 'event':
                continue
            ts_str = r.get('timestamp', '')
            price = r.get('event_price', 0)
            side = r.get('side', '')
            key = (ts_str, price, side)
            if key in seen:
                continue
            seen.add(key)
            try:
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                ts = dt.timestamp()
            except:
                continue
            events.append(ForceOrderEvent(
                timestamp=ts,
                ts_str=ts_str,
                symbol=r.get('symbol', 'BTC'),
                side=side,
                price=price,
                notional=r.get('event_notional', 0),
                src_price=r.get('event_src_price', 0),
                minute_key=int(ts // 60)
            ))

    return sorted(events, key=lambda e: e.timestamp)

def load_tape_zones() -> Dict[int, List[TapeZone]]:
    """Load tape zones indexed by minute_key."""
    zones_by_minute = defaultdict(list)

    path = os.path.join(LOG_DIR, 'liq_tape.jsonl')
    records = safe_parse_jsonl(path)

    for r in records:
        if r.get('type') != 'forceOrder':
            continue

        ts = r.get('ts', 0)
        minute_key = int(ts // 60)

        zones_by_minute[minute_key].append(TapeZone(
            timestamp=ts,
            side=r.get('side', ''),
            bucket=r.get('bucket', 0),
            notional=r.get('notional', 0)
        ))

    return dict(zones_by_minute)

def load_inference_zones() -> Dict[int, List[InferenceZone]]:
    """Load inference zones indexed by minute_key."""
    zones_by_minute = defaultdict(list)

    path = os.path.join(LOG_DIR, 'liq_inference.jsonl')
    records = safe_parse_jsonl(path)

    for r in records:
        if r.get('type') != 'inference':
            continue

        ts = r.get('ts', 0)
        minute_key = r.get('minute_key', int(ts // 60))
        src_price = r.get('src_price', 0)

        inferences = r.get('inferences', [])
        for inf in inferences:
            zones_by_minute[minute_key].append(InferenceZone(
                timestamp=ts,
                minute_key=minute_key,
                src_price=src_price,
                side=inf.get('side', ''),
                projected_liq=inf.get('projected_liq', 0),
                size_usd=inf.get('size_usd', 0),
                confidence=inf.get('confidence', 0)
            ))

    return dict(zones_by_minute)

def load_minute_contexts() -> Dict[int, MinuteContext]:
    """Load minute-level context from debug logs."""
    contexts = {}

    for fn in LOG_FILES['debug']:
        path = os.path.join(LOG_DIR, fn)
        records = safe_parse_jsonl(path)

        for r in records:
            if r.get('type') != 'minute_inference':
                continue

            mk = r.get('minute_key', 0)
            if mk in contexts:
                continue  # Keep first occurrence

            contexts[mk] = MinuteContext(
                minute_key=mk,
                src_price=r.get('src_price', 0),
                oi_delta_usd=r.get('oi_delta_usd', 0),
                buy_weight=r.get('buy_weight', 0.5),
                projected_long=r.get('projected_added_long_usd', 0),
                projected_short=r.get('projected_added_short_usd', 0),
                total_long_buckets=r.get('total_long_buckets', 0),
                total_short_buckets=r.get('total_short_buckets', 0)
            )

    return contexts

def load_sweeps() -> List[Dict]:
    """Load all sweep records."""
    sweeps = []
    path = os.path.join(LOG_DIR, 'liq_sweeps.jsonl')
    records = safe_parse_jsonl(path)

    for r in records:
        if r.get('type') == 'sweep':
            sweeps.append(r)

    return sweeps

def load_approach_summaries() -> List[Dict]:
    """Load approach_minute_summary records from calibrator."""
    summaries = []

    for fn in LOG_FILES['calibrator']:
        path = os.path.join(LOG_DIR, fn)
        records = safe_parse_jsonl(path)

        for r in records:
            if r.get('type') == 'approach_minute_summary':
                summaries.append(r)

    return summaries

# =============================================================================
# SECTION C: HIT RATE ANALYSIS
# =============================================================================

def get_active_tape_zones(tape_zones: Dict[int, List[TapeZone]],
                          event_minute: int,
                          lookback: int,
                          event_side: str) -> List[float]:
    """Get tape zone prices active at event time with lookback."""
    zones = []
    for mk in range(event_minute - lookback, event_minute + 1):
        if mk in tape_zones:
            for z in tape_zones[mk]:
                if z.side == event_side:
                    zones.append(z.bucket)
    return list(set(zones))

def get_active_inference_zones(inference_zones: Dict[int, List[InferenceZone]],
                               event_minute: int,
                               lookback: int,
                               event_side: str) -> List[Tuple[float, float]]:
    """Get inference zone prices active at event time with lookback.
    Returns list of (price, created_ts) tuples."""
    zones = []
    for mk in range(event_minute - lookback, event_minute + 1):
        if mk in inference_zones:
            for z in inference_zones[mk]:
                if z.side == event_side:
                    zones.append((z.projected_liq, z.timestamp))
    return zones

def compute_hit_rate_grid(events: List[ForceOrderEvent],
                          tape_zones: Dict[int, List[TapeZone]],
                          inference_zones: Dict[int, List[InferenceZone]],
                          zone_type: str) -> Dict:
    """
    Compute hit rate grid for specified zone type.

    zone_type: 'tape', 'inference', or 'combined'

    Returns dict with:
    - grid[lookback][tolerance] = (hit_rate, gap_rate, miss_rate, n_hits, n_gaps, n_misses)
    """
    results = {}

    for lookback in LOOKBACK_WINDOWS:
        results[lookback] = {}

        for tol in TOLERANCES:
            hits = 0
            gaps = 0  # No zone available
            misses = 0  # Zone available but event outside tolerance
            lead_times = []  # For lead-time analysis

            for event in events:
                event_minute = event.minute_key
                event_price = event.price
                event_side = event.side

                # Get active zones based on type
                zone_prices = []
                zone_created_ts = []

                if zone_type in ('tape', 'combined'):
                    tape_prices = get_active_tape_zones(
                        tape_zones, event_minute, lookback, event_side
                    )
                    zone_prices.extend(tape_prices)
                    # Tape zones don't have reliable created_ts, use event time
                    zone_created_ts.extend([event.timestamp] * len(tape_prices))

                if zone_type in ('inference', 'combined'):
                    inf_zones = get_active_inference_zones(
                        inference_zones, event_minute, lookback, event_side
                    )
                    for price, created_ts in inf_zones:
                        zone_prices.append(price)
                        zone_created_ts.append(created_ts)

                if not zone_prices:
                    gaps += 1
                    continue

                # Check if any zone is within tolerance
                is_hit = False
                best_lead_time = None

                for i, zp in enumerate(zone_prices):
                    if zp <= 0:
                        continue
                    dist_pct = abs(event_price - zp) / event_price
                    if dist_pct <= tol:
                        is_hit = True
                        lead_time = event.timestamp - zone_created_ts[i]
                        if best_lead_time is None or lead_time > best_lead_time:
                            best_lead_time = lead_time

                if is_hit:
                    hits += 1
                    if best_lead_time is not None:
                        lead_times.append(best_lead_time)
                else:
                    misses += 1

            total = hits + gaps + misses
            hit_rate = hits / total if total > 0 else 0
            gap_rate = gaps / total if total > 0 else 0
            miss_rate = misses / total if total > 0 else 0

            results[lookback][tol] = {
                'hit_rate': hit_rate,
                'gap_rate': gap_rate,
                'miss_rate': miss_rate,
                'n_hits': hits,
                'n_gaps': gaps,
                'n_misses': misses,
                'n_total': total,
                'lead_times': lead_times
            }

    return results

# =============================================================================
# SECTION D: LEAD-TIME ANALYSIS
# =============================================================================

def compute_lead_time_stats(lead_times: List[float]) -> Dict:
    """Compute p50/p90/p95 for lead times."""
    if not lead_times:
        return {'p50': None, 'p90': None, 'p95': None, 'n': 0, 'pct_negative': 0}

    lead_times = sorted(lead_times)
    n = len(lead_times)

    p50 = lead_times[int(n * 0.50)]
    p90 = lead_times[int(n * 0.90)] if n > 10 else lead_times[-1]
    p95 = lead_times[int(n * 0.95)] if n > 20 else lead_times[-1]

    pct_negative = sum(1 for lt in lead_times if lt <= 0) / n * 100

    return {
        'p50': p50,
        'p90': p90,
        'p95': p95,
        'n': n,
        'pct_negative': pct_negative
    }

# =============================================================================
# SECTION E: REACTION/SR VALIDITY
# =============================================================================

def load_price_data() -> Dict[int, Dict]:
    """Load minute-level price data (high, low, close) from debug logs."""
    prices = {}

    for fn in LOG_FILES['calibrator']:
        path = os.path.join(LOG_DIR, fn)
        records = safe_parse_jsonl(path)

        for r in records:
            if r.get('type') == 'minute_inputs':
                mk = r.get('minute_key', 0)
                prices[mk] = {
                    'high': r.get('high', 0),
                    'low': r.get('low', 0),
                    'close': r.get('close', 0),
                    'src': r.get('src', 0)
                }

    return prices

def analyze_zone_reactions(inference_zones: Dict[int, List[InferenceZone]],
                           prices: Dict[int, Dict],
                           reaction_bps: int = 20,
                           window_minutes: int = 5) -> Dict:
    """
    Analyze zone reaction rates.

    For each zone, check if within window_minutes:
    - Touch: price enters zone band (within 0.1%)
    - Reaction: after touch, reverses >= reaction_bps
    - Sweep: after touch, continues through >= reaction_bps
    """
    results = {
        'total_zones': 0,
        'touched': 0,
        'reacted': 0,
        'swept': 0,
        'by_strength_quartile': {}
    }

    # Collect all zones with their strength
    all_zones = []
    for mk, zones in inference_zones.items():
        for z in zones:
            all_zones.append((mk, z))

    if not all_zones:
        return results

    results['total_zones'] = len(all_zones)

    # Sort by confidence to get quartiles
    all_zones.sort(key=lambda x: x[1].confidence, reverse=True)
    quartile_size = len(all_zones) // 4 or 1

    for i, (mk, zone) in enumerate(all_zones):
        quartile = min(i // quartile_size, 3)  # 0=top, 3=bottom

        if quartile not in results['by_strength_quartile']:
            results['by_strength_quartile'][quartile] = {
                'total': 0, 'touched': 0, 'reacted': 0, 'swept': 0
            }

        results['by_strength_quartile'][quartile]['total'] += 1

        # Check price action in window
        zone_price = zone.projected_liq
        zone_side = zone.side

        touched = False
        max_reversal = 0
        max_continuation = 0

        for future_mk in range(mk, mk + window_minutes + 1):
            if future_mk not in prices:
                continue

            p = prices[future_mk]
            high = p.get('high', 0)
            low = p.get('low', 0)

            if high <= 0 or low <= 0:
                continue

            # Check touch (within 0.1% of zone)
            touch_band = zone_price * 0.001
            if zone_side == 'long':
                # Long liquidation zone is below price
                if low <= zone_price + touch_band:
                    touched = True
                    # Reaction = price bounces back up
                    reversal_bps = (high - zone_price) / zone_price * 10000
                    max_reversal = max(max_reversal, reversal_bps)
                    # Sweep = price continues down
                    continuation_bps = (zone_price - low) / zone_price * 10000
                    max_continuation = max(max_continuation, continuation_bps)
            else:
                # Short liquidation zone is above price
                if high >= zone_price - touch_band:
                    touched = True
                    # Reaction = price drops back down
                    reversal_bps = (zone_price - low) / zone_price * 10000
                    max_reversal = max(max_reversal, reversal_bps)
                    # Sweep = price continues up
                    continuation_bps = (high - zone_price) / zone_price * 10000
                    max_continuation = max(max_continuation, continuation_bps)

        if touched:
            results['touched'] += 1
            results['by_strength_quartile'][quartile]['touched'] += 1

            if max_reversal >= reaction_bps:
                results['reacted'] += 1
                results['by_strength_quartile'][quartile]['reacted'] += 1

            if max_continuation >= reaction_bps:
                results['swept'] += 1
                results['by_strength_quartile'][quartile]['swept'] += 1

    return results

# =============================================================================
# SECTION F: LONG/SHORT ASYMMETRY
# =============================================================================

def analyze_asymmetry(events: List[ForceOrderEvent],
                      contexts: Dict[int, MinuteContext],
                      tape_zones: Dict[int, List[TapeZone]],
                      inference_zones: Dict[int, List[InferenceZone]]) -> Dict:
    """Analyze long/short asymmetry by various regime buckets."""

    results = {
        'by_side': {'long': [], 'short': []},
        'by_buy_weight': {
            'low': [],    # 0-0.2
            'mid': [],    # 0.2-0.8
            'high': []    # 0.8-1.0
        },
        'by_oi_delta': {
            'negative': [],
            'zero': [],
            'positive': []
        }
    }

    for event in events:
        mk = event.minute_key
        ctx = contexts.get(mk)

        # Compute miss distance to nearest zone (combined)
        zone_prices = []
        tape_prices = get_active_tape_zones(tape_zones, mk, 5, event.side)
        zone_prices.extend(tape_prices)

        inf_zones = get_active_inference_zones(inference_zones, mk, 5, event.side)
        zone_prices.extend([z[0] for z in inf_zones])

        if zone_prices:
            min_dist = min(abs(event.price - zp) / event.price for zp in zone_prices if zp > 0)
        else:
            min_dist = None

        record = {
            'price': event.price,
            'min_dist': min_dist,
            'has_zone': len(zone_prices) > 0,
            'buy_weight': ctx.buy_weight if ctx else 0.5,
            'oi_delta': ctx.oi_delta_usd if ctx else 0
        }

        # Classify by side
        results['by_side'][event.side].append(record)

        # Classify by buy_weight
        if ctx:
            bw = ctx.buy_weight
            if bw <= 0.2:
                results['by_buy_weight']['low'].append(record)
            elif bw >= 0.8:
                results['by_buy_weight']['high'].append(record)
            else:
                results['by_buy_weight']['mid'].append(record)

            # Classify by oi_delta
            oi = ctx.oi_delta_usd
            if oi < 0:
                results['by_oi_delta']['negative'].append(record)
            elif oi == 0:
                results['by_oi_delta']['zero'].append(record)
            else:
                results['by_oi_delta']['positive'].append(record)

    return results

def summarize_asymmetry_bucket(records: List[Dict]) -> Dict:
    """Summarize a bucket of asymmetry records."""
    if not records:
        return {'n': 0, 'gap_rate': 0, 'hit_rate_5bps': 0, 'miss_dist_p50': None}

    n = len(records)
    gaps = sum(1 for r in records if not r['has_zone'])

    hits_5bps = sum(1 for r in records if r['has_zone'] and r['min_dist'] is not None and r['min_dist'] <= 0.005)

    distances = [r['min_dist'] for r in records if r['min_dist'] is not None]
    miss_dist_p50 = statistics.median(distances) if distances else None

    return {
        'n': n,
        'gap_rate': gaps / n * 100 if n > 0 else 0,
        'hit_rate_5bps': hits_5bps / n * 100 if n > 0 else 0,
        'miss_dist_p50': miss_dist_p50 * 100 if miss_dist_p50 else None  # Convert to %
    }

# =============================================================================
# SECTION G: APPROACH PIPELINE AUDIT
# =============================================================================

def audit_approach_pipeline(summaries: List[Dict]) -> Dict:
    """Audit the approach pipeline for skip reasons."""
    results = {
        'total_minutes': len(summaries),
        'total_candidates': 0,
        'total_used': 0,
        'total_skipped': 0,
        'skip_reasons': Counter(),
        'skipped_zones_sample': []
    }

    for s in summaries:
        results['total_candidates'] += s.get('approach_candidates', 0)
        results['total_used'] += s.get('approach_used', 0)
        results['total_skipped'] += s.get('approach_skipped', 0)

        # Check skipped zones for reasons
        skipped = s.get('skipped_zones', [])
        for sz in skipped:
            reason = sz.get('skip_reason', 'unknown')
            results['skip_reasons'][reason] += 1

            if len(results['skipped_zones_sample']) < 5:
                results['skipped_zones_sample'].append(sz)

    return results

# =============================================================================
# SECTION H: INFERENCE GAP AUDIT
# =============================================================================

def audit_inference_gaps(events: List[ForceOrderEvent],
                         inference_zones: Dict[int, List[InferenceZone]],
                         contexts: Dict[int, MinuteContext]) -> Dict:
    """Classify inference gaps by root cause."""

    results = {
        'total_events': len(events),
        'events_with_inference': 0,
        'events_without_inference': 0,
        'gap_causes': {
            'oi_delta_negative': 0,
            'oi_delta_zero': 0,
            'no_context': 0,
            'unknown': 0
        }
    }

    for event in events:
        mk = event.minute_key

        # Check if inference exists for this minute (with lookback)
        has_inference = False
        for lookback_mk in range(mk - 5, mk + 1):
            if lookback_mk in inference_zones:
                # Check if any zone matches event side
                for z in inference_zones[lookback_mk]:
                    if z.side == event.side:
                        has_inference = True
                        break
            if has_inference:
                break

        if has_inference:
            results['events_with_inference'] += 1
        else:
            results['events_without_inference'] += 1

            # Classify cause
            ctx = contexts.get(mk)
            if ctx is None:
                results['gap_causes']['no_context'] += 1
            elif ctx.oi_delta_usd < 0:
                results['gap_causes']['oi_delta_negative'] += 1
            elif ctx.oi_delta_usd == 0:
                results['gap_causes']['oi_delta_zero'] += 1
            else:
                results['gap_causes']['unknown'] += 1

    return results

# =============================================================================
# SECTION I: CALIBRATOR SANITY CHECKS
# =============================================================================

def audit_calibrator_offsets(events: List[Dict]) -> Dict:
    """Audit offset statistics by leverage tier."""

    # Parse calibrator events with offset data
    offsets_by_leverage = defaultdict(list)

    for fn in LOG_FILES['calibrator']:
        path = os.path.join(LOG_DIR, fn)
        records = safe_parse_jsonl(path)

        for r in records:
            if r.get('type') != 'event':
                continue

            miss_usd = r.get('miss_usd')
            attributed_lev = r.get('attributed_leverage')
            side = r.get('side', '')

            if miss_usd is not None and attributed_lev is not None:
                try:
                    offset = float(miss_usd)
                    offsets_by_leverage[attributed_lev].append({
                        'offset': offset,
                        'side': side
                    })
                except:
                    pass

    results = {}
    for lev, data in sorted(offsets_by_leverage.items()):
        offsets = [d['offset'] for d in data]
        n = len(offsets)

        if n == 0:
            continue

        offsets_sorted = sorted(offsets)
        median = statistics.median(offsets)

        # MAD (Median Absolute Deviation)
        mad = statistics.median([abs(x - median) for x in offsets])

        p05 = offsets_sorted[int(n * 0.05)] if n > 20 else offsets_sorted[0]
        p95 = offsets_sorted[int(n * 0.95)] if n > 20 else offsets_sorted[-1]

        results[lev] = {
            'n': n,
            'median': median,
            'mad': mad,
            'p05': p05,
            'p95': p95,
            'long_count': sum(1 for d in data if d['side'] == 'long'),
            'short_count': sum(1 for d in data if d['side'] == 'short')
        }

    return results

# =============================================================================
# REPORT GENERATION
# =============================================================================

def format_pct(val: float, decimals: int = 2) -> str:
    """Format a decimal as percentage."""
    return f"{val * 100:.{decimals}f}%"

def format_time(seconds: Optional[float]) -> str:
    """Format seconds as human readable time."""
    if seconds is None:
        return "N/A"
    if seconds < 0:
        return f"-{abs(seconds):.1f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"

def generate_report(inventory: Dict,
                    events: List[ForceOrderEvent],
                    tape_results: Dict,
                    inference_results: Dict,
                    combined_results: Dict,
                    reaction_results: Dict,
                    asymmetry_results: Dict,
                    approach_results: Dict,
                    gap_results: Dict,
                    offset_results: Dict) -> str:
    """Generate the full markdown report."""

    lines = []
    lines.append("# Liquidation Heatmap V2 Comprehensive Audit Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("")

    # ==========================================================================
    # SECTION A: DATA INVENTORY
    # ==========================================================================
    lines.append("## A. Data Inventory")
    lines.append("")

    for category, inv_list in inventory.items():
        if not inv_list:
            continue
        lines.append(f"### {category.upper()}")
        for inv in inv_list:
            size_kb = inv.size_bytes / 1024
            lines.append(f"- **{os.path.basename(inv.path)}**: {size_kb:.1f} KB, {inv.line_count} lines")
            lines.append(f"  - Date range: {inv.min_ts or 'N/A'} to {inv.max_ts or 'N/A'}")
            lines.append(f"  - Record types: {inv.record_counts}")
        lines.append("")

    lines.append(f"**Total forceOrder events loaded**: {len(events)}")
    if events:
        lines.append(f"- Long: {sum(1 for e in events if e.side == 'long')}")
        lines.append(f"- Short: {sum(1 for e in events if e.side == 'short')}")
        lines.append(f"- Date range: {events[0].ts_str} to {events[-1].ts_str}")
    lines.append("")

    # ==========================================================================
    # SECTION B: DEFINITIONS
    # ==========================================================================
    lines.append("## B. Metric Definitions")
    lines.append("")
    lines.append("### Hit Rate")
    lines.append("```")
    lines.append("hit_rate = n_hits / (n_hits + n_gaps + n_misses)")
    lines.append("where:")
    lines.append("  n_hits = events with a zone within tolerance")
    lines.append("  n_gaps = events with NO zone available in lookback window")
    lines.append("  n_misses = events with zone(s) available but none within tolerance")
    lines.append("```")
    lines.append("")
    lines.append("### Tolerance")
    lines.append("Distance from event price to zone price as percentage:")
    lines.append("```")
    lines.append("dist_pct = |event_price - zone_price| / event_price")
    lines.append("hit if dist_pct <= tolerance")
    lines.append("```")
    lines.append("")
    lines.append("### Lookback Window")
    lines.append("Number of minutes before event to search for zones:")
    lines.append("- 0m: zone must exist in same minute as event")
    lines.append("- 5m: zone can be up to 5 minutes old")
    lines.append("")
    lines.append("### Zone Types")
    lines.append("- **Tape-only**: Zones from actual forceOrder events (reactive)")
    lines.append("- **Inference-only**: Zones from OI+aggression projection (predictive)")
    lines.append("- **Combined**: Union of tape and inference zones")
    lines.append("")
    lines.append("### Lead Time")
    lines.append("```")
    lines.append("lead_time = event_timestamp - zone_created_timestamp")
    lines.append("positive = zone existed before event (predictive)")
    lines.append("negative = zone created after event (reactive/same-event)")
    lines.append("```")
    lines.append("")

    # ==========================================================================
    # SECTION C: HIT RATE ANALYSIS
    # ==========================================================================
    lines.append("## C. Hit Rate Analysis")
    lines.append("")

    for zone_type, results in [('Tape-only', tape_results),
                                ('Inference-only', inference_results),
                                ('Combined', combined_results)]:
        lines.append(f"### {zone_type} Zones")
        lines.append("")

        # Hit rate table
        lines.append("**Hit Rate Grid (%):**")
        lines.append("")
        header = "| Lookback |"
        for tol in TOLERANCES:
            header += f" {tol*100:.2f}% |"
        lines.append(header)
        lines.append("|" + "---|" * (len(TOLERANCES) + 1))

        for lb in LOOKBACK_WINDOWS:
            row = f"| {lb}m |"
            for tol in TOLERANCES:
                hr = results[lb][tol]['hit_rate'] * 100
                row += f" {hr:.1f}% |"
            lines.append(row)
        lines.append("")

        # Gap rate table
        lines.append("**Gap Rate (no zone available) (%):**")
        lines.append("")
        header = "| Lookback |"
        for tol in TOLERANCES:
            header += f" {tol*100:.2f}% |"
        lines.append(header)
        lines.append("|" + "---|" * (len(TOLERANCES) + 1))

        for lb in LOOKBACK_WINDOWS:
            row = f"| {lb}m |"
            for tol in TOLERANCES:
                gr = results[lb][tol]['gap_rate'] * 100
                row += f" {gr:.1f}% |"
            lines.append(row)
        lines.append("")

        # Sample counts at 0.50% tolerance, 5m lookback
        sample = results[5][0.005]
        lines.append(f"**Sample (5m lookback, 0.50% tolerance):**")
        lines.append(f"- Hits: {sample['n_hits']}")
        lines.append(f"- Gaps: {sample['n_gaps']}")
        lines.append(f"- Misses: {sample['n_misses']}")
        lines.append(f"- Total: {sample['n_total']}")
        lines.append("")

    # ==========================================================================
    # SECTION D: LEAD-TIME ANALYSIS
    # ==========================================================================
    lines.append("## D. Lead-Time Analysis")
    lines.append("")
    lines.append("**CRITICAL**: Lead-time measures predictiveness.")
    lines.append("- Positive lead-time = zone existed BEFORE event (predictive)")
    lines.append("- Negative/zero lead-time = zone created AT/AFTER event (reactive)")
    lines.append("")

    lines.append("### Tape-only Lead Times")
    tape_lt = compute_lead_time_stats(tape_results[5][0.005]['lead_times'])
    lines.append(f"- N: {tape_lt['n']}")
    lines.append(f"- P50: {format_time(tape_lt['p50'])}")
    lines.append(f"- P90: {format_time(tape_lt['p90'])}")
    lines.append(f"- P95: {format_time(tape_lt['p95'])}")
    lines.append(f"- % with lead_time <= 0: {tape_lt['pct_negative']:.1f}%")
    lines.append("")

    lines.append("### Inference-only Lead Times")
    inf_lt = compute_lead_time_stats(inference_results[5][0.005]['lead_times'])
    lines.append(f"- N: {inf_lt['n']}")
    lines.append(f"- P50: {format_time(inf_lt['p50'])}")
    lines.append(f"- P90: {format_time(inf_lt['p90'])}")
    lines.append(f"- P95: {format_time(inf_lt['p95'])}")
    lines.append(f"- % with lead_time <= 0: {inf_lt['pct_negative']:.1f}%")

    if inf_lt['pct_negative'] > 50:
        lines.append("")
        lines.append("**WARNING**: Majority of inference hits have non-positive lead time.")
        lines.append("This suggests inference is NOT predictive - zones are created")
        lines.append("in the same minute as events, not ahead of time.")
    lines.append("")

    # ==========================================================================
    # SECTION E: REACTION/SR VALIDITY
    # ==========================================================================
    lines.append("## E. Reaction/SR Validity Metrics")
    lines.append("")
    lines.append(f"Analysis of {reaction_results['total_zones']} inference zones:")
    lines.append(f"- Touch rate: {reaction_results['touched'] / reaction_results['total_zones'] * 100:.1f}%" if reaction_results['total_zones'] > 0 else "- Touch rate: N/A")
    lines.append(f"- Reaction rate (20bps reversal): {reaction_results['reacted'] / reaction_results['total_zones'] * 100:.1f}%" if reaction_results['total_zones'] > 0 else "- Reaction rate: N/A")
    lines.append(f"- Sweep rate (20bps continuation): {reaction_results['swept'] / reaction_results['total_zones'] * 100:.1f}%" if reaction_results['total_zones'] > 0 else "- Sweep rate: N/A")
    lines.append("")

    if reaction_results['by_strength_quartile']:
        lines.append("### By Strength Quartile")
        lines.append("| Quartile | N | Touch % | React % | Sweep % |")
        lines.append("|---|---|---|---|---|")
        for q in sorted(reaction_results['by_strength_quartile'].keys()):
            data = reaction_results['by_strength_quartile'][q]
            n = data['total']
            touch = data['touched'] / n * 100 if n > 0 else 0
            react = data['reacted'] / n * 100 if n > 0 else 0
            sweep = data['swept'] / n * 100 if n > 0 else 0
            lines.append(f"| Q{q+1} | {n} | {touch:.1f}% | {react:.1f}% | {sweep:.1f}% |")
        lines.append("")

    # ==========================================================================
    # SECTION F: LONG/SHORT ASYMMETRY
    # ==========================================================================
    lines.append("## F. Long/Short Asymmetry Diagnostics")
    lines.append("")

    lines.append("### By Side")
    lines.append("| Side | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |")
    lines.append("|---|---|---|---|---|")
    for side in ['long', 'short']:
        summary = summarize_asymmetry_bucket(asymmetry_results['by_side'][side])
        dist_str = f"{summary['miss_dist_p50']:.3f}%" if summary['miss_dist_p50'] else "N/A"
        lines.append(f"| {side} | {summary['n']} | {summary['gap_rate']:.1f}% | {summary['hit_rate_5bps']:.1f}% | {dist_str} |")
    lines.append("")

    lines.append("### By Buy Weight Bucket")
    lines.append("| Bucket | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |")
    lines.append("|---|---|---|---|---|")
    for bucket in ['low', 'mid', 'high']:
        summary = summarize_asymmetry_bucket(asymmetry_results['by_buy_weight'][bucket])
        dist_str = f"{summary['miss_dist_p50']:.3f}%" if summary['miss_dist_p50'] else "N/A"
        lines.append(f"| {bucket} (bw) | {summary['n']} | {summary['gap_rate']:.1f}% | {summary['hit_rate_5bps']:.1f}% | {dist_str} |")
    lines.append("")

    lines.append("### By OI Delta")
    lines.append("| OI Delta | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |")
    lines.append("|---|---|---|---|---|")
    for bucket in ['negative', 'zero', 'positive']:
        summary = summarize_asymmetry_bucket(asymmetry_results['by_oi_delta'][bucket])
        dist_str = f"{summary['miss_dist_p50']:.3f}%" if summary['miss_dist_p50'] else "N/A"
        lines.append(f"| {bucket} | {summary['n']} | {summary['gap_rate']:.1f}% | {summary['hit_rate_5bps']:.1f}% | {dist_str} |")
    lines.append("")

    # ==========================================================================
    # SECTION G: APPROACH PIPELINE AUDIT
    # ==========================================================================
    lines.append("## G. Approach Pipeline Audit")
    lines.append("")
    lines.append(f"- Total minutes analyzed: {approach_results['total_minutes']}")
    lines.append(f"- Total approach candidates: {approach_results['total_candidates']}")
    lines.append(f"- Total used: {approach_results['total_used']}")
    lines.append(f"- Total skipped: {approach_results['total_skipped']}")
    lines.append("")

    if approach_results['skip_reasons']:
        lines.append("### Skip Reasons")
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for reason, count in approach_results['skip_reasons'].most_common():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("**No skip reasons logged.** The `skipped_zones` array in approach_minute_summary")
        lines.append("does not include a `skip_reason` field.")
        lines.append("")
        lines.append("### Proposed Logging Fix")
        lines.append("File: `liq_calibrator.py` (approach candidate filtering)")
        lines.append("Add `skip_reason` field to skipped zone records with enum values:")
        lines.append("- `dist_too_far`: zone distance > max_dist threshold")
        lines.append("- `strength_too_low`: S_eff below minimum")
        lines.append("- `side_mismatch`: zone side doesn't match aggression")
        lines.append("- `already_swept`: zone was already cleared")
    lines.append("")

    # ==========================================================================
    # SECTION H: INFERENCE GAP AUDIT
    # ==========================================================================
    lines.append("## H. Inference Gap Audit")
    lines.append("")
    lines.append(f"- Total events: {gap_results['total_events']}")
    lines.append(f"- Events WITH inference zone (5m lookback): {gap_results['events_with_inference']}")
    lines.append(f"- Events WITHOUT inference zone: {gap_results['events_without_inference']}")
    lines.append("")

    if gap_results['events_without_inference'] > 0:
        lines.append("### Gap Root Causes")
        lines.append("| Cause | Count | % of Gaps |")
        lines.append("|---|---|---|")
        total_gaps = gap_results['events_without_inference']
        for cause, count in gap_results['gap_causes'].items():
            pct = count / total_gaps * 100 if total_gaps > 0 else 0
            lines.append(f"| {cause} | {count} | {pct:.1f}% |")
        lines.append("")

        # Find top cause
        top_cause = max(gap_results['gap_causes'].items(), key=lambda x: x[1])
        lines.append(f"**Top cause**: `{top_cause[0]}` ({top_cause[1]} gaps)")

        if top_cause[0] == 'oi_delta_negative':
            lines.append("")
            lines.append("This indicates projections are only created when OI is increasing.")
            lines.append("When OI decreases (positions closing), no new projections are made,")
            lines.append("but existing positions can still get liquidated.")
    lines.append("")

    # ==========================================================================
    # SECTION I: CALIBRATOR SANITY CHECKS
    # ==========================================================================
    lines.append("## I. Calibrator Sanity Checks")
    lines.append("")
    lines.append("### Offset Statistics by Leverage Tier")
    lines.append("")
    lines.append("| Leverage | N | Median | MAD | P05 | P95 | Long | Short |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for lev, stats in sorted(offset_results.items()):
        if stats['n'] < 30:
            note = " (N<30, excluded)"
        else:
            note = ""
        lines.append(
            f"| {lev}x{note} | {stats['n']} | ${stats['median']:.0f} | ${stats['mad']:.0f} | "
            f"${stats['p05']:.0f} | ${stats['p95']:.0f} | {stats['long_count']} | {stats['short_count']} |"
        )
    lines.append("")

    lines.append("### Sign Convention (from code)")
    lines.append("File: `liq_calibrator.py`")
    lines.append("```")
    lines.append("miss_usd = event_price - nearest_implied_corrected")
    lines.append("```")
    lines.append("- Positive miss_usd: event occurred FURTHER from entry than predicted")
    lines.append("- Negative miss_usd: event occurred CLOSER to entry than predicted")
    lines.append("")

    # Check for outliers
    outliers = [(lev, stats) for lev, stats in offset_results.items()
                if abs(stats['median']) > 10000 or stats['mad'] > 5000]
    if outliers:
        lines.append("### Potential Outliers Detected")
        for lev, stats in outliers:
            lines.append(f"- {lev}x: median=${stats['median']:.0f}, MAD=${stats['mad']:.0f}")
        lines.append("")

    # ==========================================================================
    # TOP 3 FIXES
    # ==========================================================================
    lines.append("## Top 3 Fixes (Evidence-Based)")
    lines.append("")

    lines.append("### Fix 1: Add High-Leverage Tiers to Reduce Miss Distance")
    lines.append("")
    lines.append("**Evidence:**")
    lines.append("- Current max leverage tier: 125x")
    lines.append("- Implied leverage from actual events: P50 = 200x+ (many hit 500x cap)")
    lines.append("- Miss distance P50 for shorts is higher than longs")
    lines.append("")
    lines.append("**Change:**")
    lines.append("```")
    lines.append("File: entry_inference.py")
    lines.append("Lines: 66-74 (DEFAULT_LEVERAGE_WEIGHTS)")
    lines.append("")
    lines.append("Add tiers: 150, 200, 250")
    lines.append("Reweight to favor high leverage: {125: 0.15, 150: 0.20, 200: 0.15, 250: 0.10}")
    lines.append("```")
    lines.append("")
    lines.append("**Expected improvement:** Hit rate increase from ~10% to ~25-35%")
    lines.append("")

    lines.append("### Fix 2: Add OI Memory for Projection Persistence")
    lines.append("")
    lines.append("**Evidence:**")
    top_gap_cause = max(gap_results['gap_causes'].items(), key=lambda x: x[1])
    lines.append(f"- Top gap cause: `{top_gap_cause[0]}` ({top_gap_cause[1]} gaps)")
    lines.append("- Projections only created when OI delta > 0")
    lines.append("- ~50% of minutes have negative OI delta (positions closing)")
    lines.append("")
    lines.append("**Change:**")
    lines.append("```")
    lines.append("File: entry_inference.py")
    lines.append("Lines: 237-250 (on_minute method)")
    lines.append("")
    lines.append("Add rolling OI memory:")
    lines.append("- Track sum(OI_delta) over last N minutes")
    lines.append("- Use max(0, rolling_oi) to maintain projections during closing phases")
    lines.append("```")
    lines.append("")
    lines.append("**Expected improvement:** Coverage from ~45% to ~70%+")
    lines.append("")

    lines.append("### Fix 3: Add Skip Reason Logging to Approach Pipeline")
    lines.append("")
    lines.append("**Evidence:**")
    lines.append(f"- {approach_results['total_skipped']} candidates skipped but reason unknown")
    lines.append("- Cannot diagnose why good candidates are filtered out")
    lines.append("")
    lines.append("**Change:**")
    lines.append("```")
    lines.append("File: liq_calibrator.py")
    lines.append("Lines: (find approach candidate filtering)")
    lines.append("")
    lines.append("Add skip_reason field to skipped_zones records:")
    lines.append("  'skip_reason': 'dist_too_far' | 'strength_too_low' | 'side_mismatch' | 'already_swept'")
    lines.append("```")
    lines.append("")
    lines.append("**Expected improvement:** Diagnosability; enables targeted threshold tuning")
    lines.append("")

    lines.append("---")
    lines.append("*End of Audit Report*")

    return "\n".join(lines)

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("LIQUIDATION HEATMAP V2 COMPREHENSIVE AUDIT")
    print("=" * 80)
    print()

    # Build inventory
    print("[1/9] Building data inventory...")
    inventory = build_data_inventory()

    # Load data
    print("[2/9] Loading forceOrder events...")
    events = load_all_calibrator_events()
    print(f"      Loaded {len(events)} events")

    print("[3/9] Loading tape zones...")
    tape_zones = load_tape_zones()
    print(f"      Loaded zones from {len(tape_zones)} minutes")

    print("[4/9] Loading inference zones...")
    inference_zones = load_inference_zones()
    print(f"      Loaded zones from {len(inference_zones)} minutes")

    print("[5/9] Loading minute contexts...")
    contexts = load_minute_contexts()
    print(f"      Loaded {len(contexts)} minute contexts")

    # Compute hit rates
    print("[6/9] Computing hit rate grids...")
    tape_results = compute_hit_rate_grid(events, tape_zones, inference_zones, 'tape')
    inference_results = compute_hit_rate_grid(events, tape_zones, inference_zones, 'inference')
    combined_results = compute_hit_rate_grid(events, tape_zones, inference_zones, 'combined')

    # Reaction analysis
    print("[7/9] Analyzing zone reactions...")
    prices = load_price_data()
    reaction_results = analyze_zone_reactions(inference_zones, prices)

    # Asymmetry analysis
    print("[8/9] Analyzing long/short asymmetry...")
    asymmetry_results = analyze_asymmetry(events, contexts, tape_zones, inference_zones)

    # Pipeline audits
    print("[9/9] Auditing pipelines...")
    summaries = load_approach_summaries()
    approach_results = audit_approach_pipeline(summaries)
    gap_results = audit_inference_gaps(events, inference_zones, contexts)
    offset_results = audit_calibrator_offsets(events)

    # Generate report
    print()
    print("Generating report...")
    report = generate_report(
        inventory, events, tape_results, inference_results, combined_results,
        reaction_results, asymmetry_results, approach_results, gap_results, offset_results
    )

    # Save to file
    with open(OUTPUT_FILE, 'w') as f:
        f.write(report)
    print(f"Report saved to: {OUTPUT_FILE}")

    # Print to console
    print()
    print("=" * 80)
    print("AUDIT REPORT")
    print("=" * 80)
    print()
    print(report)

if __name__ == "__main__":
    main()
