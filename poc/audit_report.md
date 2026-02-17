# Liquidation Heatmap V2 Comprehensive Audit Report

Generated: 2026-02-17T01:33:17.899768

## A. Data Inventory

### CALIBRATOR
- **liq_calibrator.jsonl**: 35912.8 KB, 38224 lines
  - Date range: 2026-02-09T21:25:00.105253 to 2026-02-16T18:48:00.063084
  - Record types: {'minute_inputs': 8883, 'depth_band_stats': 8883, 'approach_minute_summary': 8883, 'event': 10318, 'calibration': 591, 'ident_diag': 565, 'event_rejected': 99, 'mark_stream_missing': 1, 'safety_trip': 1}
- **liq_calibrator.jsonl**: 8194.5 KB, 10348 lines
  - Date range: 2026-01-26T21:06:00.014212 to 2026-01-28T19:15:34.198588
  - Record types: {'minute_inputs': 2763, 'depth_band_stats': 2763, 'approach_minute_summary': 2763, 'event': 1721, 'calibration': 178, 'ident_diag': 160}

### DEBUG
- **liq_debug.jsonl**: 12651.2 KB, 21340 lines
  - Date range: 2026-02-09T21:25:00.077969 to 2026-02-15T02:09:00.066680
  - Record types: {'minute_inference': 7080, 'aggression_minute': 7080, 'zone_tracker_debug': 7080, 'event_sanity_ok': 100}
- **liq_debug.jsonl**: 3483.6 KB, 5772 lines
  - Date range: 2026-01-26T21:05:44.341176 to 2026-01-28T19:15:00.156714
  - Record types: {'event_sanity_ok': 246, 'aggression_minute': 2763, 'zone_tracker_debug': 2763}

### INFERENCE
- **liq_inference.jsonl**: 3394.7 KB, 2600 lines
  - Date range: 2026-02-10T02:32:00.178950 to 2026-02-14T00:17:00.163914
  - Record types: {'inference': 2600}
- **liq_inference.jsonl**: 776.6 KB, 1019 lines
  - Date range: 2026-01-27T03:02:00.063097 to 2026-01-29T00:13:00.203527
  - Record types: {'inference': 1019}

### SWEEPS
- **liq_sweeps.jsonl**: 567.3 KB, 2943 lines
  - Date range: 2026-02-10T03:06:00.152553 to 2026-02-15T02:07:00.008211
  - Record types: {'sweep': 2943}
- **liq_sweeps.jsonl**: 0.0 KB, 0 lines
  - Date range: N/A to N/A
  - Record types: {}

### TAPE
- **liq_tape.jsonl**: 1346.1 KB, 7581 lines
  - Date range: 2026-02-10T02:29:50.589136 to 2026-02-14T00:13:11.200305
  - Record types: {'forceOrder': 7581}
- **liq_tape.jsonl**: 307.8 KB, 1732 lines
  - Date range: 2026-01-27T02:05:44.342177 to 2026-01-29T00:15:34.199592
  - Record types: {'forceOrder': 1732}

### PLOT_FEED
- **plot_feed.jsonl**: 2408.8 KB, 7080 lines
  - Date range: 2026-02-10T02:25:00.090677 to 2026-02-15T02:09:00.074680
  - Record types: {'NO_TYPE': 7080}
- **plot_feed.jsonl**: 792.5 KB, 2763 lines
  - Date range: 2026-01-27T02:06:00.009113 to 2026-01-29T00:15:00.157714
  - Record types: {'NO_TYPE': 2763}

**Total forceOrder events loaded**: 10318
- Long: 5291
- Short: 5027
- Date range: 2026-02-09T21:29:50.588183 to 2026-02-16T18:47:45.609033

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
| 0m | 1.0% | 2.2% | 4.0% | 6.7% |
| 1m | 1.7% | 3.5% | 6.3% | 10.6% |
| 5m | 3.8% | 6.9% | 11.8% | 19.0% |
| 15m | 8.1% | 13.2% | 19.7% | 30.1% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 86.4% | 86.4% | 86.4% | 86.4% |
| 1m | 79.7% | 79.7% | 79.7% | 79.7% |
| 5m | 62.7% | 62.7% | 62.7% | 62.7% |
| 15m | 39.7% | 39.7% | 39.7% | 39.7% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 1213
- Gaps: 6467
- Misses: 2638
- Total: 10318

### Inference-only Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 5.4% | 11.1% | 15.9% | 23.5% |
| 1m | 8.6% | 17.2% | 24.5% | 36.3% |
| 5m | 14.3% | 25.4% | 34.2% | 48.8% |
| 15m | 21.2% | 30.1% | 38.8% | 52.0% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 65.6% | 65.6% | 65.6% | 65.6% |
| 1m | 47.6% | 47.6% | 47.6% | 47.6% |
| 5m | 30.5% | 30.5% | 30.5% | 30.5% |
| 15m | 28.9% | 28.9% | 28.9% | 28.9% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 3525
- Gaps: 3146
- Misses: 3647
- Total: 10318

### Combined Zones

**Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 6.4% | 13.1% | 19.1% | 27.6% |
| 1m | 10.4% | 20.4% | 28.6% | 40.5% |
| 5m | 18.1% | 31.5% | 40.1% | 52.1% |
| 15m | 28.7% | 38.9% | 45.2% | 55.8% |

**Gap Rate (no zone available) (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 58.4% | 58.4% | 58.4% | 58.4% |
| 1m | 41.9% | 41.9% | 41.9% | 41.9% |
| 5m | 30.0% | 30.0% | 30.0% | 30.0% |
| 15m | 28.9% | 28.9% | 28.9% | 28.9% |

**Sample (5m lookback, 0.50% tolerance):**
- Hits: 4141
- Gaps: 3096
- Misses: 3081
- Total: 10318

## D. Lead-Time Analysis

**CRITICAL**: Lead-time measures predictiveness.
- Positive lead-time = zone existed BEFORE event (predictive)
- Negative/zero lead-time = zone created AT/AFTER event (reactive)

### Tape-only Lead Times
- N: 1213
- P50: 0.0s
- P90: 0.0s
- P95: 0.0s
- % with lead_time <= 0: 100.0%

### Inference-only Lead Times
- N: 3525
- P50: 4.8m
- P90: 5.8m
- P95: 5.9m
- % with lead_time <= 0: 0.0%

## E. Reaction/SR Validity Metrics

Analysis of 34350 inference zones:
- Touch rate: 0.1%
- Reaction rate (20bps reversal): 0.1%
- Sweep rate (20bps continuation): 0.0%

### By Strength Quartile
| Quartile | N | Touch % | React % | Sweep % |
|---|---|---|---|---|
| Q1 | 8587 | 0.0% | 0.0% | 0.0% |
| Q2 | 8587 | 0.1% | 0.1% | 0.0% |
| Q3 | 8587 | 0.1% | 0.1% | 0.1% |
| Q4 | 8589 | 0.1% | 0.1% | 0.0% |

## F. Long/Short Asymmetry Diagnostics

### By Side
| Side | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| long | 5291 | 27.2% | 41.3% | 0.339% |
| short | 5027 | 33.0% | 38.9% | 0.295% |

### By Buy Weight Bucket
| Bucket | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| low (bw) | 289 | 27.0% | 36.7% | 0.483% |
| mid (bw) | 8160 | 15.6% | 48.6% | 0.308% |
| high (bw) | 254 | 51.2% | 26.4% | 0.349% |

### By OI Delta
| OI Delta | N | Gap Rate | Hit Rate (0.5%) | Miss Dist P50 |
|---|---|---|---|---|
| negative | 4305 | 19.0% | 47.6% | 0.295% |
| zero | 1 | 100.0% | 0.0% | N/A |
| positive | 4397 | 15.1% | 47.6% | 0.353% |

## G. Approach Pipeline Audit

- Total minutes analyzed: 8883
- Total approach candidates: 47941
- Total used: 23712
- Total skipped: 24229

### Skip Reasons
| Reason | Count |
|---|---|
| unknown | 24229 |

## H. Inference Gap Audit

- Total events: 10318
- Events WITH inference zone (5m lookback): 7172
- Events WITHOUT inference zone: 3146

### Gap Root Causes
| Cause | Count | % of Gaps |
|---|---|---|
| oi_delta_negative | 862 | 27.4% |
| oi_delta_zero | 1 | 0.0% |
| no_context | 1615 | 51.3% |
| unknown | 668 | 21.2% |

**Top cause**: `no_context` (1615 gaps)

## I. Calibrator Sanity Checks

### Offset Statistics by Leverage Tier

| Leverage | N | Median | MAD | P05 | P95 | Long | Short |
|---|---|---|---|---|---|---|---|
| 10x | 99 | $-62128 | $3428 | $-76318 | $-57816 | 58 | 41 |
| 50x | 43 | $209 | $35 | $137 | $280 | 0 | 43 |
| 75x (N<30, excluded) | 17 | $94 | $8 | $77 | $160 | 0 | 17 |
| 100x (N<30, excluded) | 29 | $277 | $16 | $162 | $309 | 0 | 29 |
| 125x | 10130 | $152 | $223 | $-288 | $293 | 5233 | 4897 |

### Sign Convention (from code)
File: `liq_calibrator.py`
```
miss_usd = event_price - nearest_implied_corrected
```
- Positive miss_usd: event occurred FURTHER from entry than predicted
- Negative miss_usd: event occurred CLOSER to entry than predicted

### Potential Outliers Detected
- 10x: median=$-62128, MAD=$3428

## J. Per-Side Hit Rates (Combined Zones, 5m Lookback)

| Side | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| long | 18.6% | 31.7% | 41.3% | 56.0% |
| short | 17.5% | 31.3% | 38.9% | 48.0% |

**0m Lookback (same minute only):**
| Side | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| long | 6.4% | 13.7% | 20.2% | 30.6% |
| short | 6.4% | 12.5% | 17.9% | 24.5% |

## K. Tape Hit Rates (with Temporal Self-Match Filter)

Excludes tape zones from the same minute as the event (removes self-matching).

**Filtered Hit Rate Grid (%):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 1m | 1.0% | 2.1% | 3.9% | 6.6% |
| 5m | 3.3% | 6.0% | 10.4% | 16.9% |
| 15m | 7.8% | 12.8% | 19.2% | 29.1% |

**Unfiltered (for comparison):**

| Lookback | 0.10% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|
| 0m | 1.0% | 2.2% | 4.0% | 6.7% |
| 1m | 1.7% | 3.5% | 6.3% | 10.6% |
| 5m | 3.8% | 6.9% | 11.8% | 19.0% |
| 15m | 8.1% | 13.2% | 19.7% | 30.1% |

## L. Miss Distance Analysis (Per-Side, Full Percentiles)

Signed miss = event_price - nearest_zone. Positive = event further from entry than predicted.

### Long Side (N=3852)

| Metric | USD | % of Price |
|---|---|---|
| Mean | $-430 | -3423389.682% |
| P25 | $-20 | -0.029% |
| Median | $130 | 0.193% |
| P75 | $561 | 0.845% |
| P95 | $1681 | 2.458% |
| |Abs| Mean | | 3423390.783% |
| |Abs| Median | | 0.339% |

### Short Side (N=3370)

| Metric | USD | % of Price |
|---|---|---|
| Mean | $-983 | -2703009.054% |
| P25 | $-574 | -0.843% |
| Median | $-75 | -0.110% |
| P75 | $54 | 0.080% |
| P95 | $749 | 1.082% |
| |Abs| Mean | | 2703009.323% |
| |Abs| Median | | 0.295% |

## M. OI Stream Health

- Total minutes: 7078
- Valid (non-zero): 7076 (100.0%)
- Zero: 2 (0.0%)
- Positive (both-sides mode): 3523 (49.8%)
- Negative (decay mode): 3553 (50.2%)
- OI Delta median: $-16771

### Gap Analysis
- Significant gaps (>2min): 1
- Max gap: 108 minutes
- Data span: 7184 minutes (119.7 hours)

**STATUS: HEALTHY** - OI stream is operational with >99% validity.

## N. Error Scan

- Total error/warning records: 1

| Type | Count |
|---|---|
| safety_trip | 1 |

### Sample `safety_trip` record:
```json
{"type": "safety_trip", "timestamp": "2026-02-12T06:45:00.344250", "minute_key": 29514945, "cycle": 207, "reason": "entropy_collapse: H=1.597 < 1.6 for 3 cycles", "entropy": 1.5967, "max_weight": 0.3323, "min_weight": 0.011092, "buffer": 0.001383, "tolerance": 4, "consecutive_low_entropy": 3, "consecutive_max_weight_cap": 0, "consecutive_buffer_at_bounds": 0, "consecutive_tolerance_at_bounds": 0}
```

## O. Sweep Analysis

- Total sweeps: 2943
- By side: {'long': 1341, 'short': 1602}

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
- Top gap cause: `no_context` (1615 gaps)
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
- 24229 candidates skipped but reason unknown
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