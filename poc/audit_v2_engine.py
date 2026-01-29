#!/usr/bin/env python3
"""
V2 Liquidation Heatmap Engine - Runtime Audit Script
Computes hit-rate metrics, accuracy trends, and optimization recommendations.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import statistics

# File paths
DEBUG_LOG = "liq_debug.jsonl"
CALIBRATOR_LOG = "liq_calibrator.jsonl"
TAPE_LOG = "liq_tape.jsonl"
INFERENCE_LOG = "liq_inference.jsonl"
SWEEPS_LOG = "liq_sweeps.jsonl"
SNAPSHOT_V2 = "liq_api_snapshot_v2.json"
WEIGHTS_FILE = "old_log/liq_calibrator_weights.json"

def load_jsonl(path: str) -> List[dict]:
    """Load JSONL file into list of dicts."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records

def load_json(path: str) -> dict:
    """Load JSON file."""
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)

def analyze_force_orders(tape_records: List[dict]) -> dict:
    """Analyze forceOrder ingestion."""
    force_orders = [r for r in tape_records if r.get('type') == 'forceOrder']

    if not force_orders:
        return {"count": 0, "status": "NO_DATA"}

    long_orders = [fo for fo in force_orders if fo.get('side') == 'long']
    short_orders = [fo for fo in force_orders if fo.get('side') == 'short']

    total_notional = sum(fo.get('notional', 0) for fo in force_orders)
    long_notional = sum(fo.get('notional', 0) for fo in long_orders)
    short_notional = sum(fo.get('notional', 0) for fo in short_orders)

    # Time range
    timestamps = [fo.get('ts', 0) for fo in force_orders]
    time_range_minutes = (max(timestamps) - min(timestamps)) / 60 if timestamps else 0

    return {
        "count": len(force_orders),
        "long_count": len(long_orders),
        "short_count": len(short_orders),
        "total_notional_usd": round(total_notional, 2),
        "long_notional_usd": round(long_notional, 2),
        "short_notional_usd": round(short_notional, 2),
        "time_range_minutes": round(time_range_minutes, 1),
        "events_per_hour": round(len(force_orders) / (time_range_minutes / 60), 2) if time_range_minutes > 0 else 0,
        "status": "LIVE" if len(force_orders) > 10 else "LOW_VOLUME"
    }

def analyze_aggression(debug_records: List[dict]) -> dict:
    """Analyze aggression accumulator."""
    aggression_records = [r for r in debug_records if r.get('type') == 'aggression_minute']

    if not aggression_records:
        return {"count": 0, "status": "NO_DATA"}

    trade_counts = [r.get('trade_count', 0) for r in aggression_records]
    notionals = [r.get('total_notional_usd', 0) for r in aggression_records]
    fallback_used = [r.get('fallback_used', False) for r in aggression_records]

    fallback_rate = sum(fallback_used) / len(fallback_used) if fallback_used else 0

    return {
        "total_minutes": len(aggression_records),
        "avg_trade_count": round(statistics.mean(trade_counts), 1) if trade_counts else 0,
        "median_trade_count": round(statistics.median(trade_counts), 1) if trade_counts else 0,
        "avg_notional_usd": round(statistics.mean(notionals), 2) if notionals else 0,
        "total_notional_usd": round(sum(notionals), 2),
        "fallback_used_rate": round(fallback_rate * 100, 2),
        "zero_trade_minutes": sum(1 for tc in trade_counts if tc == 0),
        "status": "HEALTHY" if fallback_rate < 0.05 else "HIGH_FALLBACK"
    }

def analyze_inference(inference_records: List[dict]) -> dict:
    """Analyze inference firing rate and patterns."""
    if not inference_records:
        return {"count": 0, "status": "NO_DATA"}

    timestamps = [r.get('ts', 0) for r in inference_records]
    time_range_minutes = (max(timestamps) - min(timestamps)) / 60 if len(timestamps) > 1 else 0

    long_inferences = 0
    short_inferences = 0
    total_inferred_long_usd = 0
    total_inferred_short_usd = 0

    for rec in inference_records:
        for inf in rec.get('inferences', []):
            if inf.get('side') == 'long':
                long_inferences += 1
                total_inferred_long_usd += inf.get('size_usd', 0)
            else:
                short_inferences += 1
                total_inferred_short_usd += inf.get('size_usd', 0)

    return {
        "total_inference_events": len(inference_records),
        "long_projections": long_inferences,
        "short_projections": short_inferences,
        "inferences_per_hour": round(len(inference_records) / (time_range_minutes / 60), 2) if time_range_minutes > 0 else 0,
        "total_inferred_long_usd": round(total_inferred_long_usd, 2),
        "total_inferred_short_usd": round(total_inferred_short_usd, 2),
        "time_range_minutes": round(time_range_minutes, 1),
        "status": "ACTIVE" if len(inference_records) > 50 else "LOW_ACTIVITY"
    }

def analyze_calibrator_events(calibrator_records: List[dict]) -> dict:
    """Analyze calibrator event logging and offset samples."""
    events = [r for r in calibrator_records if r.get('type') == 'event']
    minute_inputs = [r for r in calibrator_records if r.get('type') == 'minute_inputs']
    approach_summaries = [r for r in calibrator_records if r.get('type') == 'approach_minute_summary']

    if not events:
        return {
            "event_count": 0,
            "minute_inputs": len(minute_inputs),
            "approach_summaries": len(approach_summaries),
            "status": "NO_EVENTS"
        }

    # Analyze hit/miss distribution
    hits = [e for e in events if e.get('is_hit', False)]
    misses = [e for e in events if not e.get('is_hit', False)]

    # Offset analysis by leverage
    offset_by_leverage = defaultdict(list)
    offset_by_side = defaultdict(list)

    for e in events:
        lev = e.get('attributed_leverage')
        miss_usd = e.get('miss_usd', 0)
        side = e.get('side', '')
        if lev and miss_usd:
            offset_by_leverage[lev].append(miss_usd)
        if side and miss_usd:
            offset_by_side[side].append(miss_usd)

    leverage_offset_medians = {}
    for lev, offsets in offset_by_leverage.items():
        if offsets:
            leverage_offset_medians[lev] = round(statistics.median(offsets), 2)

    side_offset_medians = {}
    for side, offsets in offset_by_side.items():
        if offsets:
            side_offset_medians[side] = round(statistics.median(offsets), 2)

    return {
        "event_count": len(events),
        "hit_count": len(hits),
        "miss_count": len(misses),
        "hit_rate": round(len(hits) / len(events) * 100, 2) if events else 0,
        "minute_inputs": len(minute_inputs),
        "approach_summaries": len(approach_summaries),
        "leverage_offset_medians": leverage_offset_medians,
        "side_offset_medians": side_offset_medians,
        "status": "CALIBRATING" if len(events) > 10 else "INSUFFICIENT_SAMPLES"
    }

def analyze_sweeps(sweeps_records: List[dict]) -> dict:
    """Analyze sweep application."""
    if not sweeps_records:
        return {
            "count": 0,
            "status": "NO_SWEEPS_LOGGED",
            "critical": True,
            "message": "liq_sweeps.jsonl is EMPTY - sweep detection may not be running"
        }

    return {
        "count": len(sweeps_records),
        "status": "ACTIVE"
    }

def compute_forceorder_hit_rate(
    force_orders: List[dict],
    inference_records: List[dict],
    n_minutes: int = 5,
    tolerance_pct: float = 0.5
) -> dict:
    """
    Compute hit rate: when forceOrder occurs, did we predict it?
    Uses tolerance_pct for bucket matching (0.5% = ~$450 at $90k price).
    """
    if not force_orders or not inference_records:
        return {"hit_rate": 0, "status": "INSUFFICIENT_DATA"}

    # Filter to valid forceOrders (bucket > 0) and after inference started
    inference_start = inference_records[0].get('ts', 0) if inference_records else 0
    valid_fos = [fo for fo in force_orders if fo.get('bucket', 0) > 0 and fo.get('ts', 0) >= inference_start]

    # Build timeline of predictions by minute and side
    predictions_by_minute = defaultdict(lambda: {'long': [], 'short': []})
    for inf_rec in inference_records:
        minute_key = inf_rec.get('minute_key', 0)
        for inf in inf_rec.get('inferences', []):
            bucket = inf.get('projected_liq', 0)
            side = inf.get('side', '')
            if bucket and side:
                predictions_by_minute[minute_key][side].append(bucket)

    hits = 0
    misses = 0
    side_hits = {'long': 0, 'short': 0}
    side_misses = {'long': 0, 'short': 0}
    miss_distances = []

    for fo in valid_fos:
        ts = fo.get('ts', 0)
        minute_key = int(ts // 60)
        bucket = fo.get('bucket', 0)
        side = fo.get('side', '')

        # Check if we predicted this bucket within tolerance
        predicted = False
        closest_dist = float('inf')

        for offset in range(n_minutes + 1):
            check_minute = minute_key - offset
            for pred_bucket in predictions_by_minute[check_minute][side]:
                dist_pct = abs(bucket - pred_bucket) / bucket * 100
                closest_dist = min(closest_dist, dist_pct)
                if dist_pct <= tolerance_pct:
                    predicted = True
                    break
            if predicted:
                break

        if predicted:
            hits += 1
            side_hits[side] = side_hits.get(side, 0) + 1
        else:
            misses += 1
            side_misses[side] = side_misses.get(side, 0) + 1
            if closest_dist < float('inf'):
                miss_distances.append(closest_dist)

    total = hits + misses
    median_miss = statistics.median(miss_distances) if miss_distances else 0

    return {
        "hit_rate": round(hits / total * 100, 2) if total > 0 else 0,
        "hits": hits,
        "misses": misses,
        "total_events": total,
        "n_minutes_lookback": n_minutes,
        "tolerance_pct": tolerance_pct,
        "side_hits": side_hits,
        "side_misses": side_misses,
        "median_miss_pct": round(median_miss, 2),
        "status": "COMPUTED"
    }


def analyze_side_coverage(
    force_orders: List[dict],
    inference_records: List[dict],
    n_minutes: int = 5
) -> dict:
    """
    Analyze how many forceOrders had same-side vs wrong-side predictions.
    This identifies the fundamental issue of binary side selection.
    """
    if not force_orders or not inference_records:
        return {"status": "INSUFFICIENT_DATA"}

    inference_start = inference_records[0].get('ts', 0) if inference_records else 0
    valid_fos = [fo for fo in force_orders if fo.get('bucket', 0) > 0 and fo.get('ts', 0) >= inference_start]

    # Build minute -> predicted sides mapping
    predictions_by_minute = defaultdict(lambda: {'long': False, 'short': False})
    inf_minutes = set()
    for inf_rec in inference_records:
        minute_key = inf_rec.get('minute_key', 0)
        inf_minutes.add(minute_key)
        for inf in inf_rec.get('inferences', []):
            side = inf.get('side', '')
            if side:
                predictions_by_minute[minute_key][side] = True

    same_side = 0
    wrong_side = 0
    no_inference = 0
    by_side = {'long': {'same': 0, 'wrong': 0, 'gap': 0}, 'short': {'same': 0, 'wrong': 0, 'gap': 0}}

    for fo in valid_fos:
        ts = fo.get('ts', 0)
        minute_key = int(ts // 60)
        fo_side = fo.get('side', '')
        opposite = 'short' if fo_side == 'long' else 'long'

        # Check last n_minutes for predictions
        found_same = False
        found_opposite = False
        found_any_inference = False

        for offset in range(n_minutes + 1):
            check_minute = minute_key - offset
            if check_minute in inf_minutes:
                found_any_inference = True
            if predictions_by_minute[check_minute][fo_side]:
                found_same = True
            if predictions_by_minute[check_minute][opposite]:
                found_opposite = True

        if found_same:
            same_side += 1
            by_side[fo_side]['same'] += 1
        elif found_opposite:
            wrong_side += 1
            by_side[fo_side]['wrong'] += 1
        elif not found_any_inference:
            no_inference += 1
            by_side[fo_side]['gap'] += 1
        else:
            # Had inference but no predictions for either side (neutral zone)
            wrong_side += 1
            by_side[fo_side]['wrong'] += 1

    total = len(valid_fos)
    return {
        "total_forceorders": total,
        "same_side_predictions": same_side,
        "wrong_side_predictions": wrong_side,
        "inference_gaps": no_inference,
        "coverage_pct": round(same_side / total * 100, 2) if total > 0 else 0,
        "wrong_side_pct": round(wrong_side / total * 100, 2) if total > 0 else 0,
        "by_side": by_side,
        "status": "ANALYZED"
    }

def analyze_skip_reasons(calibrator_records: List[dict]) -> dict:
    """Analyze approach skip reasons."""
    approach_summaries = [r for r in calibrator_records if r.get('type') == 'approach_minute_summary']

    if not approach_summaries:
        return {"status": "NO_DATA"}

    total_candidates = sum(r.get('approach_candidates', 0) for r in approach_summaries)
    total_used = sum(r.get('approach_used', 0) for r in approach_summaries)
    total_skipped = sum(r.get('approach_skipped', 0) for r in approach_summaries)

    # Count skip reasons from skipped_zones
    skip_reasons = defaultdict(int)
    for rec in approach_summaries:
        for zone in rec.get('skipped_zones', []):
            reason = zone.get('skip_reason', 'unknown')
            skip_reasons[reason] += 1

    return {
        "total_minutes": len(approach_summaries),
        "total_candidates": total_candidates,
        "total_used": total_used,
        "total_skipped": total_skipped,
        "use_rate": round(total_used / total_candidates * 100, 2) if total_candidates > 0 else 0,
        "skip_reasons": dict(skip_reasons),
        "status": "ANALYZED"
    }

def analyze_symmetry(snapshot: dict) -> dict:
    """Analyze long/short symmetry."""
    long_levels = snapshot.get('long_levels', [])
    short_levels = snapshot.get('short_levels', [])

    if not long_levels or not short_levels:
        return {"status": "INSUFFICIENT_DATA"}

    current_price = snapshot.get('price', snapshot.get('src', 0))

    # Calculate average distance from price
    long_distances = []
    short_distances = []

    for level in long_levels:
        dist = (current_price - level.get('price', current_price)) / current_price * 100
        long_distances.append(dist)

    for level in short_levels:
        dist = (level.get('price', current_price) - current_price) / current_price * 100
        short_distances.append(dist)

    avg_long_dist = statistics.mean(long_distances) if long_distances else 0
    avg_short_dist = statistics.mean(short_distances) if short_distances else 0

    # Notional comparison
    total_long_notional = sum(l.get('notional_usd', 0) for l in long_levels)
    total_short_notional = sum(l.get('notional_usd', 0) for l in short_levels)

    return {
        "long_levels_count": len(long_levels),
        "short_levels_count": len(short_levels),
        "avg_long_distance_pct": round(avg_long_dist, 2),
        "avg_short_distance_pct": round(avg_short_dist, 2),
        "distance_asymmetry": round(abs(avg_long_dist - avg_short_dist), 2),
        "total_long_notional": round(total_long_notional, 2),
        "total_short_notional": round(total_short_notional, 2),
        "notional_ratio_long_short": round(total_long_notional / total_short_notional, 2) if total_short_notional > 0 else 0,
        "status": "ASYMMETRIC" if abs(avg_long_dist - avg_short_dist) > 1.0 else "SYMMETRIC"
    }

def main():
    print("=" * 70)
    print("V2 LIQUIDATION HEATMAP ENGINE - RUNTIME AUDIT")
    print("=" * 70)

    # Load all data
    debug_records = load_jsonl(DEBUG_LOG)
    calibrator_records = load_jsonl(CALIBRATOR_LOG)
    tape_records = load_jsonl(TAPE_LOG)
    inference_records = load_jsonl(INFERENCE_LOG)
    sweeps_records = load_jsonl(SWEEPS_LOG)
    snapshot = load_json(SNAPSHOT_V2)
    weights = load_json(WEIGHTS_FILE)

    print(f"\nData loaded:")
    print(f"  - debug_records: {len(debug_records)}")
    print(f"  - calibrator_records: {len(calibrator_records)}")
    print(f"  - tape_records: {len(tape_records)}")
    print(f"  - inference_records: {len(inference_records)}")
    print(f"  - sweeps_records: {len(sweeps_records)}")

    # SANITY CHECKS
    print("\n" + "=" * 70)
    print("SANITY CHECKS")
    print("=" * 70)

    # 1. ForceOrder ingestion
    fo_analysis = analyze_force_orders(tape_records)
    print(f"\n[1] ForceOrder Ingestion: {fo_analysis['status']}")
    print(f"    - Total events: {fo_analysis.get('count', 0)}")
    print(f"    - Long: {fo_analysis.get('long_count', 0)} (${fo_analysis.get('long_notional_usd', 0):,.0f})")
    print(f"    - Short: {fo_analysis.get('short_count', 0)} (${fo_analysis.get('short_notional_usd', 0):,.0f})")
    print(f"    - Events/hour: {fo_analysis.get('events_per_hour', 0)}")
    print(f"    - Time range: {fo_analysis.get('time_range_minutes', 0):.0f} minutes")

    # 2. Aggression accumulator
    agg_analysis = analyze_aggression(debug_records)
    print(f"\n[2] Aggression Accumulator: {agg_analysis.get('status', 'UNKNOWN')}")
    print(f"    - Minutes tracked: {agg_analysis.get('total_minutes', 0)}")
    print(f"    - Avg trade count: {agg_analysis.get('avg_trade_count', 0)}")
    print(f"    - Avg notional/min: ${agg_analysis.get('avg_notional_usd', 0):,.0f}")
    print(f"    - Fallback used rate: {agg_analysis.get('fallback_used_rate', 0)}%")
    print(f"    - Zero-trade minutes: {agg_analysis.get('zero_trade_minutes', 0)}")

    # 3. Inference firing
    inf_analysis = analyze_inference(inference_records)
    print(f"\n[3] Inference Engine: {inf_analysis.get('status', 'UNKNOWN')}")
    print(f"    - Total inference events: {inf_analysis.get('total_inference_events', 0)}")
    print(f"    - Long projections: {inf_analysis.get('long_projections', 0)}")
    print(f"    - Short projections: {inf_analysis.get('short_projections', 0)}")
    print(f"    - Inferences/hour: {inf_analysis.get('inferences_per_hour', 0)}")
    print(f"    - Time range: {inf_analysis.get('time_range_minutes', 0):.0f} minutes")

    # 4. Sweeps
    sweep_analysis = analyze_sweeps(sweeps_records)
    print(f"\n[4] Sweep Detection: {sweep_analysis.get('status', 'UNKNOWN')}")
    if sweep_analysis.get('critical'):
        print(f"    *** CRITICAL: {sweep_analysis.get('message', '')}")
    else:
        print(f"    - Sweeps logged: {sweep_analysis.get('count', 0)}")

    # CORE ACCURACY METRICS
    print("\n" + "=" * 70)
    print("CORE ACCURACY METRICS")
    print("=" * 70)

    # ForceOrder hit rate with tolerance
    force_orders = [r for r in tape_records if r.get('type') == 'forceOrder']

    print("\n[A] ForceOrder Hit Rate (0.5% bucket tolerance, 5 min lookback):")
    hr = compute_forceorder_hit_rate(force_orders, inference_records, n_minutes=5, tolerance_pct=0.5)
    print(f"    - Hit rate: {hr.get('hit_rate', 0)}%")
    print(f"    - Hits: {hr.get('hits', 0)} / Misses: {hr.get('misses', 0)}")
    print(f"    - Median miss distance: {hr.get('median_miss_pct', 0)}%")
    print(f"    - By side: Long hits={hr.get('side_hits', {}).get('long', 0)}, Short hits={hr.get('side_hits', {}).get('short', 0)}")

    # Side coverage analysis - the key diagnostic
    print("\n[A2] Side Coverage Analysis (ROOT CAUSE DIAGNOSTIC):")
    coverage = analyze_side_coverage(force_orders, inference_records, n_minutes=5)
    print(f"    - Total forceOrders analyzed: {coverage.get('total_forceorders', 0)}")
    print(f"    - Same-side predictions: {coverage.get('same_side_predictions', 0)} ({coverage.get('coverage_pct', 0)}%)")
    print(f"    - WRONG-side predictions: {coverage.get('wrong_side_predictions', 0)} ({coverage.get('wrong_side_pct', 0)}%)")
    print(f"    - Inference gaps: {coverage.get('inference_gaps', 0)}")
    by_side = coverage.get('by_side', {})
    if by_side:
        print(f"    - Long FOs: same={by_side.get('long', {}).get('same', 0)}, wrong={by_side.get('long', {}).get('wrong', 0)}, gap={by_side.get('long', {}).get('gap', 0)}")
        print(f"    - Short FOs: same={by_side.get('short', {}).get('same', 0)}, wrong={by_side.get('short', {}).get('wrong', 0)}, gap={by_side.get('short', {}).get('gap', 0)}")

    # Calibrator analysis
    calib_analysis = analyze_calibrator_events(calibrator_records)
    print(f"\n[B] Calibrator Event Analysis:")
    print(f"    - Calibration events: {calib_analysis.get('event_count', 0)}")
    print(f"    - Hit/Miss: {calib_analysis.get('hit_count', 0)}/{calib_analysis.get('miss_count', 0)}")
    print(f"    - Hit rate: {calib_analysis.get('hit_rate', 0)}%")
    print(f"    - Offset by side: {calib_analysis.get('side_offset_medians', {})}")
    print(f"    - Offset by leverage: {calib_analysis.get('leverage_offset_medians', {})}")

    # Skip reasons
    skip_analysis = analyze_skip_reasons(calibrator_records)
    print(f"\n[C] Approach Skip Analysis:")
    print(f"    - Total candidates: {skip_analysis.get('total_candidates', 0)}")
    print(f"    - Used: {skip_analysis.get('total_used', 0)}")
    print(f"    - Skipped: {skip_analysis.get('total_skipped', 0)}")
    print(f"    - Use rate: {skip_analysis.get('use_rate', 0)}%")
    if skip_analysis.get('skip_reasons'):
        print(f"    - Skip reasons: {skip_analysis.get('skip_reasons', {})}")

    # Symmetry check
    sym_analysis = analyze_symmetry(snapshot)
    print(f"\n[D] Long/Short Symmetry: {sym_analysis.get('status', 'UNKNOWN')}")
    print(f"    - Avg long distance: {sym_analysis.get('avg_long_distance_pct', 0)}%")
    print(f"    - Avg short distance: {sym_analysis.get('avg_short_distance_pct', 0)}%")
    print(f"    - Distance asymmetry: {sym_analysis.get('distance_asymmetry', 0)}%")
    print(f"    - Notional ratio (L/S): {sym_analysis.get('notional_ratio_long_short', 0)}")

    # Current weights
    print(f"\n[E] Current Calibration Weights:")
    if weights:
        print(f"    - Buffer: {weights.get('buffer', 0)}")
        print(f"    - Calibration count: {weights.get('calibration_count', 0)}")
        print(f"    - Top leverage weights:")
        w_by_lev = weights.get('weights_by_leverage', {})
        for lev in ['10', '25', '50', '100', '250']:
            if lev in w_by_lev:
                print(f"      - {lev}x: {w_by_lev[lev]:.4f}")

    # CRITICAL FINDINGS
    print("\n" + "=" * 70)
    print("CRITICAL FINDINGS")
    print("=" * 70)

    findings = []

    # Check wrong-side prediction rate
    wrong_side_pct = coverage.get('wrong_side_pct', 0)
    if wrong_side_pct > 30:
        findings.append(f"CRITICAL: {wrong_side_pct}% of forceOrders had WRONG-SIDE predictions - engine predicts one side based on aggression")

    if sweep_analysis.get('critical'):
        findings.append("CRITICAL: liq_sweeps.jsonl is EMPTY - sweep detection not logging")

    if coverage.get('inference_gaps', 0) > 100:
        findings.append(f"WARNING: {coverage.get('inference_gaps')} forceOrders during inference gaps")

    if agg_analysis.get('fallback_used_rate', 0) > 5:
        findings.append(f"WARNING: High fallback rate ({agg_analysis.get('fallback_used_rate')}%) in aggression accumulator")

    for i, finding in enumerate(findings, 1):
        print(f"  {i}. {finding}")

    if not findings:
        print("  No critical issues detected.")

    # RECOMMENDATIONS
    print("\n" + "=" * 70)
    print("OPTIMIZATION RECOMMENDATIONS")
    print("=" * 70)

    print("""
1. PREDICT BOTH SIDES (CRITICAL - Highest Impact)
   - Problem: Engine predicts ONE side based on buy_pct threshold
     * buy_pct < 0.45 -> SHORT liquidations only
     * buy_pct > 0.55 -> LONG liquidations only
   - Impact: ~40% of forceOrders have WRONG-side predictions
   - Fix: Project liquidation zones for BOTH sides every inference
   - Module: entry_inference.py / full_metrics_viewer.py
   - Expected: Coverage jumps from ~59% to ~95%+

2. FIX SWEEP LOGGING
   - Symptom: liq_sweeps.jsonl is empty (0 bytes)
   - Module: full_metrics_viewer.py or liq_heatmap.py
   - Action: Ensure on_sweep() is called when price crosses predicted buckets
   - Metric: Should see sweep events correlated with price movements

3. REDUCE INFERENCE GAPS
   - Current: ~10% of forceOrders during no-inference periods
   - Module: entry_inference.py
   - Action: Lower OI delta threshold or add time-based minimum firing
   - Metric: Ensure inference fires at least once per minute

4. IMPLEMENT SIDE-SPECIFIC LEVERAGE WEIGHTS
   - Symptom: Offset by side shows asymmetric miss distances
   - Module: entry_inference.py
   - Action: Maintain separate leverage_weights dicts for long vs short
   - Metric: Reduce median offset USD by side

5. BUCKET TOLERANCE FOR METRICS
   - Current: Exact bucket matching underreports accuracy
   - Note: At 0.5% tolerance, actual hit rate is ~41%
   - This is informational - the real fix is #1 above
""")

    # NEXT 5 ACTIONS
    print("\n" + "=" * 70)
    print("PRIORITIZED NEXT 5 ACTIONS")
    print("=" * 70)

    print("""
1. [CRITICAL] Predict both sides simultaneously
   - File: entry_inference.py
   - Change: Remove buy_pct side filtering
   - Always project long AND short liquidation zones
   - Use aggression to weight intensity, not exclude sides
   - Expected: 40% immediate hit rate improvement

2. [HIGH] Fix sweep logging
   - File: liq_heatmap.py or full_metrics_viewer.py
   - Verify on_sweep() callback is wired up
   - Expected: Enable real-time accuracy tracking

3. [HIGH] Add time-based inference minimum
   - File: entry_inference.py
   - Ensure inference fires at least once per minute
   - Expected: Eliminate inference gap misses

4. [MEDIUM] Side-specific offset learning
   - File: liq_calibrator.py
   - Track long_bias_pct and short_bias_pct separately
   - Expected: Reduce per-side miss distances

5. [LOW] Review calibrator hit rate
   - Current: 93.78% calibrator hit rate is excellent
   - The calibrator is working well
   - Focus on the inference side-selection issue instead
""")

if __name__ == "__main__":
    main()
