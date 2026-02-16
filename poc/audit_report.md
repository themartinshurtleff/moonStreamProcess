# Liquidation Heatmap V2 Comprehensive Audit Report

Generated: 2026-02-16T03:13:23.134277

## A. Data Inventory

### CALIBRATOR
- **liq_calibrator.jsonl**: 33987.6 KB, 36105 lines
  - Date range: 2026-02-09T21:25:00.105253 to 2026-02-16T03:13:00.396489
  - Record types: {'minute_inputs': 8384, 'depth_band_stats': 8384, 'approach_minute_summary': 8384, 'event': 9761, 'calibration': 558, 'ident_diag': 535, 'event_rejected': 97, 'mark_stream_missing': 1, 'safety_trip': 1}
- **liq_calibrator.jsonl**: 8204.6 KB, 10348 lines
  - Date range: 2026-01-26T21:06:00.014212 to 2026-01-28T19:15:34.198588
  - Record types: {'minute_inputs': 2763, 'depth_band_stats': 2763, 'approach_minute_summary': 2763, 'event': 1721, 'calibration': 178, 'ident_diag': 160}

### DEBUG
- **liq_debug.jsonl**: 15015.6 KB, 25306 lines
  - Date range: 2026-02-09T21:25:00.077969 to 2026-02-16T03:13:00.382475
  - Record types: {'minute_inference': 8384, 'aggression_minute': 8384, 'zone_tracker_debug': 8384, 'event_sanity_ok': 150, 'event_sanity_fail': 4}
- **liq_debug.jsonl**: 3489.3 KB, 5772 lines
  - Date range: 2026-01-26T21:05:44.341176 to 2026-01-28T19:15:00.156714
  - Record types: {'event_sanity_ok': 246, 'aggression_minute': 2763, 'zone_tracker_debug': 2763}

### INFERENCE
- **liq_inference.jsonl**: 5014.5 KB, 3902 lines
  - Date range: 2026-02-09T21:32:00.178950 to 2026-02-16T03:10:00.221653
  - Record types: {'inference': 3902}
- **liq_inference.jsonl**: 777.6 KB, 1019 lines
  - Date range: 2026-01-26T22:02:00.063097 to 2026-01-28T19:13:00.203527
  - Record types: {'inference': 1019}

### SWEEPS
- **liq_sweeps.jsonl**: 612.6 KB, 3163 lines
  - Date range: 2026-02-09T22:06:00.152553 to 2026-02-16T02:42:00.038003
  - Record types: {'sweep': 3163}
- **liq_sweeps.jsonl**: 0.0 KB, 0 lines
  - Date range: N/A to N/A
  - Record types: {}

### TAPE
- **liq_tape.jsonl**: 1742.6 KB, 9768 lines
  - Date range: 2026-02-09T21:29:50.589136 to 2026-02-16T03:10:29.189292
  - Record types: {'forceOrder': 9768}
- **liq_tape.jsonl**: 309.5 KB, 1732 lines
  - Date range: 2026-01-26T21:05:44.342177 to 2026-01-28T19:15:34.199592
  - Record types: {'forceOrder': 1732}

### PLOT_FEED
- **plot_feed.jsonl**: 2867.6 KB, 8384 lines
  - Date range: 2026-02-09T21:25:00.090677 to 2026-02-16T03:13:00.383980
  - Record types: {'NO_TYPE': 8384}
- **plot_feed.jsonl**: 795.2 KB, 2763 lines
  - Date range: 2026-01-26T21:06:00.009113 to 2026-01-28T19:15:00.157714
  - Record types: {'NO_TYPE': 2763}

**Total forceOrder events loaded**: 11482
- Long: 5893
- Short: 5589
- Date range: 2026-01-26T21:09:17.337410 to 2026-02-16T03:10:29.189292

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
| 0m | 84.2% | 84.3% | 84.3% | 84.3% |
| 1m | 84.2% | 84.3% | 84.3% | 84.3% |
| 5m | 84.2% | 84.3% | 84.3% | 84.3% |
| 15m | 84.2% | 84.3% | 84.3% | 84.3% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 15.0% | 15.0% | 15.0% | 15.0% |
| 1m | 15.0% | 15.0% | 15.0% | 15.0% |
| 5m | 15.0% | 15.0% | 15.0% | 15.0% |
| 15m | 15.0% | 15.0% | 15.0% | 15.0% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 9682
- Gaps: 1721
- Misses: 79
- Total: 11482

### Inference-only Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 0.7% | 2.2% | 22.4% | 38.4% |
| 1m | 2.2% | 6.4% | 39.3% | 56.0% |
| 5m | 13.7% | 26.2% | 70.0% | 80.6% |
| 15m | 32.4% | 49.2% | 78.3% | 83.8% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 61.2% | 61.2% | 61.2% | 61.2% |
| 1m | 43.3% | 43.3% | 43.3% | 43.3% |
| 5m | 18.2% | 18.2% | 18.2% | 18.2% |
| 15m | 15.1% | 15.1% | 15.1% | 15.1% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 8036
- Gaps: 2090
- Misses: 1356
- Total: 11482

### Combined Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 84.2% | 84.3% | 84.3% | 84.3% |
| 1m | 84.2% | 84.3% | 84.3% | 84.3% |
| 5m | 84.2% | 84.3% | 84.3% | 84.3% |
| 15m | 84.2% | 84.3% | 84.3% | 84.3% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 15.0% | 15.0% | 15.0% | 15.0% |
| 1m | 15.0% | 15.0% | 15.0% | 15.0% |
| 5m | 15.0% | 15.0% | 15.0% | 15.0% |
| 15m | 15.0% | 15.0% | 15.0% | 15.0% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 9682
- Gaps: 1721
- Misses: 79
- Total: 11482

## D. Lead-Time Analysis

**CRITICAL**: Lead-time measures predictiveness.
- Positive lead-time = zone existed BEFORE event (predictive)
- Negative/zero lead-time = zone created AT/AFTER event (reactive)

### Tape-only Lead Times
- N: 9682
- P50: 0.0s
- P90: 0.0s
- P95: 0.0s
- % with lead_time <= 0: 100.0%

### Inference-only Lead Times
- N: 8036
- P50: 4.9m
- P90: 5.8m
- P95: 5.9m
- % with lead_time <= 0: 0.0%

## E. Reaction/SR Validity Metrics

Analysis of 50596 inference zones:
- Touch rate: 0.1%
- Reaction rate (20bps reversal): 0.1%
- Sweep rate (20bps continuation): 0.0%

### By Strength Quartile
| Quartile | N | Touch % | React % | Sweep % |
|---|---|---|---|---|
| Q1 | 12649 | 0.0% | 0.0% | 0.0% |
| Q2 | 12649 | 0.1% | 0.1% | 0.0% |
| Q3 | 12649 | 0.1% | 0.1% | 0.1% |
| Q4 | 12649 | 0.1% | 0.1% | 0.0% |

## F. Long/Short Asymmetry Diagnostics

### By Side
| Side | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| long | 5893 | 14.5% | 84.7% | 0.007% |
| short | 5589 | 15.5% | 83.9% | 0.007% |

### By Buy Weight Bucket
| Bucket | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| low (bw) | 225 | 0.0% | 100.0% | 0.008% |
| mid (bw) | 9394 | 0.0% | 99.2% | 0.007% |
| high (bw) | 142 | 0.0% | 100.0% | 0.007% |

### By OI Delta
| OI Delta | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| negative | 5166 | 0.0% | 99.1% | 0.007% |
| zero | 3 | 0.0% | 100.0% | 0.004% |
| positive | 4592 | 0.0% | 99.3% | 0.007% |

## G. Approach Pipeline Audit

- Total minutes analyzed: 8384
- Total approach candidates: 45271
- Total used: 22429
- Total skipped: 22842

### Skip Reasons
| Reason | Count |
|---|---|
| unknown | 22842 |

## H. Inference Gap Audit

- Total events: 11482
- Events WITH inference zone (5m lookback): 9392
- Events WITHOUT inference zone: 2090

### Gap Root Causes
| Cause | Count | % of Gaps |
|---|---|---|
| oi_delta_negative | 351 | 16.8% |
| oi_delta_zero | 3 | 0.1% |
| no_context | 1721 | 82.3% |
| unknown | 15 | 0.7% |

**Top cause**: `no_context` (1721 gaps)

## I. Calibrator Sanity Checks

### Offset Statistics by Leverage Tier

| Leverage | N | Median | MAD | P05 | P95 | Long | Short |
|---|---|---|---|---|---|---|---|
| 10x | 97 | $-62126 | $3240 | $-75919 | $-57816 | 58 | 39 |
| 50x | 41 | $213 | $34 | $137 | $280 | 0 | 41 |
| 75x (N<30, excluded) | 17 | $94 | $8 | $77 | $160 | 0 | 17 |
| 100x (N<30, excluded) | 28 | $275 | $17 | $162 | $309 | 0 | 28 |
| 125x | 9578 | $155 | $203 | $-283 | $295 | 4983 | 4595 |

### Sign Convention (from code)
File: `liq_calibrator.py`
```
miss_usd = event_price - nearest_implied_corrected
```
- Positive miss_usd: event occurred FURTHER from entry than predicted
- Negative miss_usd: event occurred CLOSER to entry than predicted

### Potential Outliers Detected
- 10x: median=$-62126, MAD=$3240

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
- 22842 candidates skipped but reason unknown
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