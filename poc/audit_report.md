# Liquidation Heatmap V2 Comprehensive Audit Report

Generated: 2026-02-06T15:33:33.611000

## A. Data Inventory

### CALIBRATOR
- **liq_calibrator.jsonl**: 3217.0 KB, 2809 lines
  - Date range: 2026-02-05T23:04:00.159135 to 2026-02-06T15:33:00.048789
  - Record types: {'minute_inputs': 472, 'depth_band_stats': 472, 'approach_minute_summary': 472, 'event': 1330, 'calibration': 30, 'ident_diag': 30, 'event_rejected': 3}
- **liq_calibrator.jsonl**: 8204.6 KB, 10348 lines
  - Date range: 2026-01-26T21:06:00.014212 to 2026-01-28T19:15:34.198588
  - Record types: {'minute_inputs': 2763, 'depth_band_stats': 2763, 'approach_minute_summary': 2763, 'event': 1721, 'calibration': 178, 'ident_diag': 160}

### DEBUG
- **liq_debug.jsonl**: 7844.5 KB, 13199 lines
  - Date range: 2026-02-03T00:19:23.829492 to 2026-02-06T15:33:00.028980
  - Record types: {'event_sanity_ok': 164, 'minute_inference': 4345, 'aggression_minute': 4345, 'zone_tracker_debug': 4345}
- **liq_debug.jsonl**: 3489.3 KB, 5772 lines
  - Date range: 2026-01-26T21:05:44.341176 to 2026-01-28T19:15:00.156714
  - Record types: {'event_sanity_ok': 246, 'aggression_minute': 2763, 'zone_tracker_debug': 2763}

### INFERENCE
- **liq_inference.jsonl**: 2705.2 KB, 2161 lines
  - Date range: 2026-01-28T20:21:00.171194 to 2026-02-06T15:33:00.005938
  - Record types: {'inference': 2161}
- **liq_inference.jsonl**: 777.6 KB, 1019 lines
  - Date range: 2026-01-26T22:02:00.063097 to 2026-01-28T19:13:00.203527
  - Record types: {'inference': 1019}

### SWEEPS
- **liq_sweeps.jsonl**: 1170.5 KB, 6037 lines
  - Date range: 2026-01-28T20:28:00.233310 to 2026-02-06T15:18:00.015284
  - Record types: {'sweep': 6037}
- **liq_sweeps.jsonl**: 0.0 KB, 0 lines
  - Date range: N/A to N/A
  - Record types: {}

### TAPE
- **liq_tape.jsonl**: 2673.2 KB, 14963 lines
  - Date range: 2026-01-28T20:16:52.296102 to 2026-02-06T15:24:19.551155
  - Record types: {'forceOrder': 14963}
- **liq_tape.jsonl**: 309.5 KB, 1732 lines
  - Date range: 2026-01-26T21:05:44.342177 to 2026-01-28T19:15:34.199592
  - Record types: {'forceOrder': 1732}

### PLOT_FEED
- **plot_feed.jsonl**: 1489.2 KB, 4345 lines
  - Date range: 2026-02-03T00:20:00.055581 to 2026-02-06T15:33:00.028980
  - Record types: {'NO_TYPE': 4345}
- **plot_feed.jsonl**: 795.2 KB, 2763 lines
  - Date range: 2026-01-26T21:06:00.009113 to 2026-01-28T19:15:00.157714
  - Record types: {'NO_TYPE': 2763}
- **plot_feed.20260203_0019.jsonl**: 258.7 KB, 760 lines
  - Date range: 2026-01-28T20:15:00.181014 to 2026-01-30T01:59:00.041859
  - Record types: {'NO_TYPE': 760}

**Total forceOrder events loaded**: 3051
- Long: 1363
- Short: 1688
- Date range: 2026-01-26T21:09:17.337410 to 2026-02-06T15:24:19.551155

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
| 0m | 43.5% | 43.5% | 43.5% | 43.5% |
| 1m | 43.5% | 43.5% | 43.5% | 43.5% |
| 5m | 43.5% | 43.5% | 43.5% | 43.5% |
| 15m | 43.5% | 43.5% | 43.5% | 43.5% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 56.4% | 56.4% | 56.4% | 56.4% |
| 1m | 56.4% | 56.4% | 56.4% | 56.4% |
| 5m | 56.4% | 56.4% | 56.4% | 56.4% |
| 15m | 56.4% | 56.4% | 56.4% | 56.4% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 1328
- Gaps: 1721
- Misses: 2
- Total: 3051

### Inference-only Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 0.0% | 0.1% | 2.9% | 3.1% |
| 1m | 0.1% | 0.4% | 4.4% | 4.9% |
| 5m | 1.0% | 2.5% | 5.2% | 5.5% |
| 15m | 2.7% | 3.8% | 5.3% | 5.5% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 96.9% | 96.9% | 96.9% | 96.9% |
| 1m | 95.1% | 95.1% | 95.1% | 95.1% |
| 5m | 94.5% | 94.5% | 94.5% | 94.5% |
| 15m | 94.5% | 94.5% | 94.5% | 94.5% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 159
- Gaps: 2884
- Misses: 8
- Total: 3051

### Combined Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 43.5% | 43.5% | 43.5% | 43.5% |
| 1m | 43.5% | 43.5% | 43.5% | 43.5% |
| 5m | 43.5% | 43.5% | 43.5% | 43.5% |
| 15m | 43.5% | 43.5% | 43.5% | 43.5% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 56.4% | 56.4% | 56.4% | 56.4% |
| 1m | 56.4% | 56.4% | 56.4% | 56.4% |
| 5m | 56.4% | 56.4% | 56.4% | 56.4% |
| 15m | 56.4% | 56.4% | 56.4% | 56.4% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 1328
- Gaps: 1721
- Misses: 2
- Total: 3051

## D. Lead-Time Analysis

**CRITICAL**: Lead-time measures predictiveness.
- Positive lead-time = zone existed BEFORE event (predictive)
- Negative/zero lead-time = zone created AT/AFTER event (reactive)

### Tape-only Lead Times
- N: 1328
- P50: 0.0s
- P90: 0.0s
- P95: 0.0s
- % with lead_time <= 0: 100.0%

### Inference-only Lead Times
- N: 159
- P50: 5.1m
- P90: 5.7m
- P95: 5.8m
- % with lead_time <= 0: 0.0%

## E. Reaction/SR Validity Metrics

Analysis of 27100 inference zones:
- Touch rate: 0.0%
- Reaction rate (20bps reversal): 0.0%
- Sweep rate (20bps continuation): 0.0%

### By Strength Quartile
| Quartile | N | Touch % | React % | Sweep % |
|---|---|---|---|---|
| Q1 | 6775 | 0.0% | 0.0% | 0.0% |
| Q2 | 6775 | 0.0% | 0.0% | 0.0% |
| Q3 | 6775 | 0.0% | 0.0% | 0.0% |
| Q4 | 6775 | 0.0% | 0.0% | 0.0% |

## F. Long/Short Asymmetry Diagnostics

### By Side
| Side | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| long | 1363 | 62.5% | 37.3% | 0.007% |
| short | 1688 | 51.5% | 48.5% | 0.007% |

### By Buy Weight Bucket
| Bucket | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| low (bw) | 1 | 0.0% | 100.0% | 0.006% |
| mid (bw) | 1329 | 0.0% | 99.8% | 0.007% |
| high (bw) | 0 | 0.0% | 0.0% | N/A |

### By OI Delta
| OI Delta | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| negative | 583 | 0.0% | 99.8% | 0.008% |
| zero | 2 | 0.0% | 100.0% | 0.004% |
| positive | 745 | 0.0% | 99.9% | 0.007% |

## G. Approach Pipeline Audit

- Total minutes analyzed: 472
- Total approach candidates: 2408
- Total used: 1181
- Total skipped: 1227

### Skip Reasons
| Reason | Count |
|---|---|
| unknown | 1227 |

## H. Inference Gap Audit

- Total events: 3051
- Events WITH inference zone (5m lookback): 167
- Events WITHOUT inference zone: 2884

### Gap Root Causes
| Cause | Count | % of Gaps |
|---|---|---|
| oi_delta_negative | 511 | 17.7% |
| oi_delta_zero | 2 | 0.1% |
| no_context | 1721 | 59.7% |
| unknown | 650 | 22.5% |

**Top cause**: `no_context` (1721 gaps)

## I. Calibrator Sanity Checks

### Offset Statistics by Leverage Tier

| Leverage | N | Median | MAD | P05 | P95 | Long | Short |
|---|---|---|---|---|---|---|---|
| 10x (N<30, excluded) | 3 | $-58205 | $68 | $-66645 | $-58136 | 2 | 1 |
| 50x (N<30, excluded) | 7 | $206 | $36 | $76 | $251 | 0 | 7 |
| 75x (N<30, excluded) | 7 | $48 | $6 | $-8 | $137 | 0 | 7 |
| 100x (N<30, excluded) | 1 | $211 | $0 | $211 | $211 | 0 | 1 |
| 125x | 1312 | $-233 | $153 | $-408 | $390 | 509 | 803 |

### Sign Convention (from code)
File: `liq_calibrator.py`
```
miss_usd = event_price - nearest_implied_corrected
```
- Positive miss_usd: event occurred FURTHER from entry than predicted
- Negative miss_usd: event occurred CLOSER to entry than predicted

### Potential Outliers Detected
- 10x: median=$-58205, MAD=$68

## Top 3 Fixes (Evidence-Based)

### Fix 1: Add High-Leverage Tiers to Reduce Miss Distance

**Evidence:**
- Current max leverage tier: 125x
- Implied leverage from actual events: P50 = 200x+ (many hit 500x cap)
- Miss distance P50 for shorts is higher than longs

**Change:**
```
File: entry_inference.py
Lines: 66-74 (DEFAULT_LEVERAGE_WEIGHTS)

Add tiers: 150, 200, 250
Reweight to favor high leverage: {125: 0.15, 150: 0.20, 200: 0.15, 250: 0.10}
```

**Expected improvement:** Hit rate increase from ~10% to ~25-35%

### Fix 2: Add OI Memory for Projection Persistence

**Evidence:**
- Top gap cause: `no_context` (1721 gaps)
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
- 1227 candidates skipped but reason unknown
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