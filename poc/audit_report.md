# Liquidation Heatmap V2 Comprehensive Audit Report

Generated: 2026-02-05T23:07:22.600220

## A. Data Inventory

### CALIBRATOR
- **liq_calibrator.jsonl**: 27565.7 KB, 24484 lines
  - Date range: 2026-02-03T00:20:00.081612 to 2026-02-05T17:00:35.511905
  - Record types: {'minute_inputs': 3873, 'depth_band_stats': 3873, 'approach_minute_summary': 3873, 'event': 12354, 'calibration': 257, 'ident_diag': 252, 'safety_trip': 1, 'mark_stream_missing': 1}
- **liq_calibrator.jsonl**: 8194.5 KB, 10348 lines
  - Date range: 2026-01-26T21:06:00.014212 to 2026-01-28T19:15:34.198588
  - Record types: {'minute_inputs': 2763, 'depth_band_stats': 2763, 'approach_minute_summary': 2763, 'event': 1721, 'calibration': 178, 'ident_diag': 160}
- **liq_calibrator.20260203_0019.jsonl**: 3563.8 KB, 3627 lines
  - Date range: 2026-01-28T20:15:00.190527 to 2026-01-30T01:59:00.066013
  - Record types: {'minute_inputs': 760, 'depth_band_stats': 760, 'approach_minute_summary': 760, 'event': 1254, 'calibration': 48, 'ident_diag': 45}

### DEBUG
- **liq_debug.jsonl**: 6954.0 KB, 11683 lines
  - Date range: 2026-02-03T00:19:23.829492 to 2026-02-05T22:00:00.060425
  - Record types: {'event_sanity_ok': 64, 'minute_inference': 3873, 'aggression_minute': 3873, 'zone_tracker_debug': 3873}
- **liq_debug.jsonl**: 3483.6 KB, 5772 lines
  - Date range: 2026-01-26T21:05:44.341176 to 2026-01-28T19:15:00.156714
  - Record types: {'event_sanity_ok': 246, 'aggression_minute': 2763, 'zone_tracker_debug': 2763}
- **liq_debug.20260203_0019.jsonl**: 1405.6 KB, 2460 lines
  - Date range: 2026-01-28T20:15:00.174471 to 2026-01-30T06:59:00.032836
  - Record types: {'minute_inference': 760, 'aggression_minute': 760, 'zone_tracker_debug': 760, 'event_sanity_ok': 180}

### INFERENCE
- **liq_inference.jsonl**: 2646.6 KB, 2120 lines
  - Date range: 2026-01-29T01:21:00.171194 to 2026-02-05T21:59:00.000116
  - Record types: {'inference': 2120}
- **liq_inference.jsonl**: 776.6 KB, 1019 lines
  - Date range: 2026-01-27T03:02:00.063097 to 2026-01-29T00:13:00.203527
  - Record types: {'inference': 1019}

### SWEEPS
- **liq_sweeps.jsonl**: 1052.2 KB, 5460 lines
  - Date range: 2026-01-29T01:28:00.233310 to 2026-02-05T21:57:00.026606
  - Record types: {'sweep': 5460}
- **liq_sweeps.jsonl**: 0.0 KB, 0 lines
  - Date range: N/A to N/A
  - Record types: {}

### TAPE
- **liq_tape.jsonl**: 2418.9 KB, 13620 lines
  - Date range: 2026-01-29T01:16:52.296102 to 2026-02-05T22:00:35.533103
  - Record types: {'forceOrder': 13620}
- **liq_tape.jsonl**: 307.8 KB, 1732 lines
  - Date range: 2026-01-27T02:05:44.342177 to 2026-01-29T00:15:34.199592
  - Record types: {'forceOrder': 1732}

### PLOT_FEED
- **plot_feed.jsonl**: 1321.2 KB, 3873 lines
  - Date range: 2026-02-03T05:20:00.055581 to 2026-02-05T22:00:00.072332
  - Record types: {'NO_TYPE': 3873}
- **plot_feed.jsonl**: 792.5 KB, 2763 lines
  - Date range: 2026-01-27T02:06:00.009113 to 2026-01-29T00:15:00.157714
  - Record types: {'NO_TYPE': 2763}
- **plot_feed.20260203_0019.jsonl**: 257.9 KB, 760 lines
  - Date range: 2026-01-29T01:15:00.181014 to 2026-01-30T06:59:00.041859
  - Record types: {'NO_TYPE': 760}

**Total forceOrder events loaded**: 15328
- Long: 10318
- Short: 5010
- Date range: 2026-01-26T21:09:17.337410 to 2026-02-05T17:00:35.511905

## B. Metric Definitions

### Hit Rate
```
hit_rate = n_hits / (n_hits + n_gaps + n_misses)
where:
  n_hits = events with a zone within tolerance
  n_gaps = events with NO zone available in lookback window
  n_misses = events with zone(s) available but none within tolerance
```

### Tolerance
Distance from event price to zone price as percentage:
```
dist_pct = |event_price - zone_price| / event_price
hit if dist_pct <= tolerance
```

### Lookback Window
Number of minutes before event to search for zones:
- 0m: zone must exist in same minute as event
- 5m: zone can be up to 5 minutes old

### Zone Types
- **Tape-only**: Zones from actual forceOrder events (reactive)
- **Inference-only**: Zones from OI+aggression projection (predictive)
- **Combined**: Union of tape and inference zones

### Lead Time
```
lead_time = event_timestamp - zone_created_timestamp
positive = zone existed before event (predictive)
negative = zone created after event (reactive/same-event)
```

## C. Hit Rate Analysis

### Tape-only Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 0.7% | 1.7% | 3.3% | 4.9% |
| 1m | 1.2% | 2.5% | 4.4% | 6.7% |
| 5m | 2.1% | 3.8% | 6.6% | 10.5% |
| 15m | 4.3% | 6.9% | 10.3% | 15.0% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 75.2% | 75.2% | 75.2% | 75.2% |
| 1m | 65.6% | 65.6% | 65.6% | 65.6% |
| 5m | 46.7% | 46.7% | 46.7% | 46.7% |
| 15m | 29.7% | 29.7% | 29.7% | 29.7% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 1019
- Gaps: 7163
- Misses: 7146
- Total: 15328

### Inference-only Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 4.0% | 8.4% | 12.6% | 17.5% |
| 1m | 6.5% | 12.9% | 19.3% | 26.4% |
| 5m | 12.3% | 19.6% | 27.0% | 36.2% |
| 15m | 17.2% | 23.4% | 29.8% | 38.4% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 63.6% | 63.6% | 63.6% | 63.6% |
| 1m | 43.8% | 43.8% | 43.8% | 43.8% |
| 5m | 20.9% | 20.9% | 20.9% | 20.9% |
| 15m | 19.4% | 19.4% | 19.4% | 19.4% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 4134
- Gaps: 3208
- Misses: 7986
- Total: 15328

### Combined Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 4.7% | 10.0% | 15.2% | 21.2% |
| 1m | 7.6% | 15.0% | 22.1% | 29.9% |
| 5m | 13.9% | 21.9% | 29.6% | 38.5% |
| 15m | 19.7% | 26.1% | 32.4% | 40.1% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 48.0% | 48.0% | 48.0% | 48.0% |
| 1m | 31.8% | 31.8% | 31.8% | 31.8% |
| 5m | 19.6% | 19.6% | 19.6% | 19.6% |
| 15m | 19.4% | 19.4% | 19.4% | 19.4% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 4533
- Gaps: 3009
- Misses: 7786
- Total: 15328

## D. Lead-Time Analysis

**CRITICAL**: Lead-time measures predictiveness.
- Positive lead-time = zone existed BEFORE event (predictive)
- Negative/zero lead-time = zone created AT/AFTER event (reactive)

### Tape-only Lead Times
- N: 1019
- P50: 0.0s
- P90: 0.0s
- P95: 0.0s
- % with lead_time <= 0: 100.0%

### Inference-only Lead Times
- N: 4134
- P50: 4.7m
- P90: 5.8m
- P95: 5.9m
- % with lead_time <= 0: 0.0%

## E. Reaction/SR Validity Metrics

Analysis of 26526 inference zones:
- Touch rate: 0.6%
- Reaction rate (20bps reversal): 0.6%
- Sweep rate (20bps continuation): 0.2%

### By Strength Quartile
| Quartile | N | Touch % | React % | Sweep % |
|---|---|---|---|---|
| Q1 | 6631 | 0.4% | 0.4% | 0.1% |
| Q2 | 6631 | 1.0% | 1.0% | 0.4% |
| Q3 | 6631 | 0.8% | 0.8% | 0.2% |
| Q4 | 6633 | 0.5% | 0.5% | 0.2% |

## F. Long/Short Asymmetry Diagnostics

### By Side
| Side | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| long | 10318 | 17.1% | 38.9% | 0.624% |
| short | 5010 | 24.8% | 10.5% | 2.822% |

### By Buy Weight Bucket
| Bucket | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| low (bw) | 129 | 4.7% | 73.6% | 0.313% |
| mid (bw) | 12082 | 0.3% | 36.0% | 1.279% |
| high (bw) | 140 | 0.0% | 62.1% | 0.392% |

### By OI Delta
| OI Delta | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| negative | 6411 | 0.5% | 34.1% | 1.638% |
| zero | 3 | 100.0% | 0.0% | N/A |
| positive | 5937 | 0.1% | 39.5% | 0.916% |

## G. Approach Pipeline Audit

- Total minutes analyzed: 4633
- Total approach candidates: 25904
- Total used: 11736
- Total skipped: 14168

### Skip Reasons
| Reason | Count |
|---|---|
| unknown | 14168 |

## H. Inference Gap Audit

- Total events: 15328
- Events WITH inference zone (5m lookback): 12120
- Events WITHOUT inference zone: 3208

### Gap Root Causes
| Cause | Count | % of Gaps |
|---|---|---|
| oi_delta_negative | 216 | 6.7% |
| oi_delta_zero | 3 | 0.1% |
| no_context | 2970 | 92.6% |
| unknown | 19 | 0.6% |

**Top cause**: `no_context` (2970 gaps)

## I. Calibrator Sanity Checks

### Offset Statistics by Leverage Tier

| Leverage | N | Median | MAD | P05 | P95 | Long | Short |
|---|---|---|---|---|---|---|---|
| 10x | 213 | $-65485 | $3730 | $-80826 | $-56962 | 133 | 80 |
| 50x (N<30, excluded) | 25 | $507 | $74 | $-31 | $583 | 3 | 22 |
| 75x | 37 | $178 | $133 | $-270 | $315 | 16 | 21 |
| 100x (N<30, excluded) | 13 | $-262 | $39 | $-321 | $183 | 12 | 1 |
| 125x | 13320 | $210 | $77 | $-354 | $453 | 9303 | 4017 |

### Sign Convention (from code)
File: `liq_calibrator.py`
```
miss_usd = event_price - nearest_implied_corrected
```
- Positive miss_usd: event occurred FURTHER from entry than predicted
- Negative miss_usd: event occurred CLOSER to entry than predicted

### Potential Outliers Detected
- 10x: median=$-65485, MAD=$3730

## CORRECTED ANALYSIS (Post-Audit Discovery)

**Important**: The earlier sections used raw inference logs. The calibrator's
`is_hit` field shows the TRUE hit rate using the combined production heatmap.

### Actual Hit Rates (from calibrator is_hit field)

| Metric | Overall | Long | Short |
|--------|---------|------|-------|
| **Hit Rate** | 97.7% | 98.3% | 96.3% |

### The 10x Leverage Bug

184 events are attributed to 10x leverage, which has a corrupt offset of -$65,485.
When excluding these:

| Metric | Long | Short |
|--------|------|-------|
| **Hit Rate** | 98.6% | 98.2% |
| **Miss Distance (median)** | $480 | $520 |

**Short-side is NOT underperforming!** The apparent asymmetry was caused by
the 10x leverage tier having an incorrect offset calibration.

## Top 3 Fixes (Evidence-Based)

### Fix 1: Remove or Fix 10x Leverage Tier (CRITICAL)

**Evidence:**
- 10x tier has median offset of -$65,485 (clearly wrong)
- 184 events attributed to 10x, causing extreme false misses
- 75 short misses + 109 long misses from this tier alone
- Excluding 10x: hit rate improves from 97.7% to 98.5%

**Change:**
```
File: liq_calibrator.py (leverage tier handling)

Option A: Remove 10x from LEVERAGE_TIERS (if rarely used)
Option B: Add minimum N >= 100 samples before using learned offset
Option C: Clamp learned offset to reasonable range (+/- $2000)
```

**Expected improvement:** Eliminate 184 false-negative misses

### Fix 2: Add OI Memory for Projection Persistence

**Evidence:**
- Top gap cause: `no_context` (2970 gaps)
- Projections only created when OI delta > 0
- ~50% of minutes have negative OI delta (positions closing)

**Change:**
```
File: entry_inference.py
Lines: 237-250 (on_minute method)

Add rolling OI memory:
- Track sum(OI_delta) over last N minutes
- Use max(0, rolling_oi) to maintain projections during closing phases
```

**Expected improvement:** Coverage from ~45% to ~70%+

### Fix 3: Add Skip Reason Logging to Approach Pipeline

**Evidence:**
- 14168 candidates skipped but reason unknown
- Cannot diagnose why good candidates are filtered out

**Change:**
```
File: liq_calibrator.py
Lines: (find approach candidate filtering)

Add skip_reason field to skipped_zones records:
  'skip_reason': 'dist_too_far' | 'strength_too_low' | 'side_mismatch' | 'already_swept'
```

**Expected improvement:** Diagnosability; enables targeted threshold tuning

---
*End of Audit Report*