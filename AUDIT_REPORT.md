# Comprehensive Codebase Audit Report

**Date:** 2026-02-27
**Scope:** All 22 `.py` files in `poc/`
**Method:** File-by-file code audit against CLAUDE.md spec, cross-file data flow verification, thread safety analysis
**Rule:** DO NOT FIX — report only

---

## 1. CRITICAL

### C1. BTC Depth Failure Freezes ALL Symbol Pipelines

**File:** `poc/full_metrics_viewer.py`, lines ~1418-1421
**Description:** The entire multi-symbol minute rollover (`_check_minute_rollover`) is gated on `self.state.perp_close > 0`. `perp_close` is only set by BTC orderbook depth processing. If BTC depth stream disconnects or fails to produce a valid mid-price, `perp_close` stays at 0 and the minute rollover **never fires for any symbol** — ETH and SOL are frozen too.
**Impact:** Total pipeline halt for all 3 symbols. No snapshots written, no calibrator updates, no zone lifecycle, no API data refresh. Silent — no error logged.
**Why it matters:** Single point of failure. ETH/SOL have their own mark prices via `symbol_prices` dict but the rollover gate ignores them. A Binance depth WebSocket hiccup (common with geo-blocking) takes down the entire backend.

---

## 2. HIGH

### H1. `write_json_atomic()` Silently Swallows All Exceptions

**File:** `poc/full_metrics_viewer.py`, lines ~188-206
**Description:** The `write_json_atomic()` helper wraps the entire tmp-write + fsync + rename sequence in a bare `try/except Exception: pass`. If the atomic write fails (disk full, permissions, OS error), the exception is silently discarded with no logging.
**Impact:** Snapshot files stop updating with zero indication. The API serves stale data indefinitely. Calibrator weight saves also fail silently (same helper).
**Why it matters:** Violates CLAUDE.md Rule #10 ("NEVER swallow exceptions silently in data pipelines"). This is the primary persistence function for all V1/V2 snapshots and calibrator weights.

### H2. `ob_heatmap.py` — `_emit_frame()` Uses Incremental Grid (Rule #16 Violation)

**File:** `poc/ob_heatmap.py`, lines ~703-707
**Description:** Builds price grid with `p += self.step` in a while loop. Violates CLAUDE.md Rule #16: "NEVER build price grids with incremental `p += step` loops."
**Impact:** Currently latent — BTC uses `step=20.0` (integer-safe). Will produce the exact same all-zeros bug documented in "Known Bugs" (SOL FP grid) if ETH/SOL orderbook engines are added (Phase F TODO).
**Why it matters:** The fix was applied to 8 files but `ob_heatmap.py` was missed. The correct pattern (`build_unified_grid()` at line ~1119) already exists in the same file.

### H3. `ob_heatmap.py` — `OrderbookFrame.get_prices()` Uses Incremental Grid (Rule #16 Violation)

**File:** `poc/ob_heatmap.py`, lines ~533-540
**Description:** Same incremental `p += self.step` pattern as H2. Called by `resample_frame_to_grid()` in the history serialization path.
**Impact:** Same latent risk as H2. Frame resampling would produce zero-filled grids for non-integer steps.

---

## 3. MEDIUM

### Engine & Heatmap Logic

#### M1. `get_combined_heatmap()` Default Weights Don't Match CLAUDE.md

**File:** `poc/entry_inference.py`, `get_combined_heatmap()` signature
**Description:** Default parameter values are `tape_weight=0.4, projection_weight=0.6`. CLAUDE.md documents `tape_weight=0.35, projection_weight=0.65`. Runtime callers always pass explicit values from `HeatmapConfig` (which uses 0.35/0.65), so the defaults never activate — but they create a latent mismatch if any new caller uses defaults.
**Why it matters:** Documentation/code divergence. Any new call site using defaults gets wrong weights.

#### M2. OI Closing Branch Uses Stale `last_oi` Reference

**File:** `poc/entry_inference.py`, `on_oi_update()` closing branch
**Description:** The closing branch computes `close_factor = abs(oi_delta) * src_price / total_projected` using `self.last_oi` which has already been updated to the new value at the top of the method. Should use the previous OI value for the ratio calculation.
**Impact:** Overestimates `close_factor` when OI drops, causing slightly more aggressive projection removal than intended.

#### M3. No Thread Safety on Projection/Tape Dicts in `EntryInference`

**File:** `poc/entry_inference.py`
**Description:** `self._projections_long`, `self._projections_short`, `self._tape_long`, `self._tape_short` are plain dicts mutated by the main thread (via `on_minute`/`on_force_order`) and read by the API thread (via `get_combined_heatmap_display`/`get_api_response_display`). No lock protects these accesses.
**Impact:** Under CPython GIL, dict iteration can raise `RuntimeError: dictionary changed size during iteration` if a key is added/removed during iteration. Rare but possible under concurrent load.

#### M4. `_push_tape_learning()` Includes Disabled 10x Tier

**File:** `poc/liq_heatmap.py`, `_push_tape_learning()`
**Description:** The tape-to-calibrator feedback loop projects hits across leverage tiers but does not exclude `DISABLED_TIERS`. The 10x tier (disabled due to -$62K median miss) still receives tape learning signals. Additionally, only covers 7 of 11 BTC tiers (missing 150x, 200x, 250x).
**Impact:** Disabled tier accumulates learning data that cannot affect calibration (weights are zeroed), wasting computation. Missing high-leverage tiers means tape feedback is incomplete.

#### M5. `LeverageConfig.__post_init__` Normalizes Including Disabled Tiers

**File:** `poc/leverage_config.py`
**Description:** The dataclass post-init normalizes weights across all tiers including disabled ones. If a consumer constructs a `LeverageConfig` with disabled tiers having non-zero weight, the normalization distributes weight to them.
**Impact:** The calibrator's `_enforce_disabled_tiers()` re-zeros them afterward, but any consumer using `LeverageConfig` directly (not through the calibrator) would get wrong weights.

#### M6. `get_api_response()` Raw USD Combination Makes Tape Invisible in Pool Selection

**File:** `poc/liq_heatmap.py`, `get_api_response()`
**Description:** Pools are sorted by `notional_usd` which combines raw tape ($1-10K) and inference ($1-5M) values. Tape-only pools are always at the bottom and get cut by `max_pools_per_side=20`. This is the raw path (not display), so the display boost doesn't help here.
**Impact:** Pure tape zones — the highest quality signal (56.5% sweep rate) — may be excluded from the API response entirely if there are 20+ inference-dominated pools per side.

#### M7. `EntryInference.DEFAULT_LEVERAGE_WEIGHTS` Missing BTC Tiers

**File:** `poc/entry_inference.py`
**Description:** The fallback `DEFAULT_LEVERAGE_WEIGHTS` dict covers only 7 tiers (5x through 100x). BTC has 11 tiers (up to 250x). For the first ~30 minutes before calibrator weights are loaded, projections for 125x/150x/200x/250x tiers are skipped.
**Impact:** Reduced heatmap coverage during cold start. Self-heals after first calibration cycle.

### Calibrator & Zone Manager

#### M8. Logged Entropy Differs From Safety Entropy

**File:** `poc/liq_calibrator.py`, `_run_calibration()`
**Description:** The debug log writes entropy computed over ALL tiers (including disabled). The safety check correctly computes entropy over enabled tiers only. The logged value doesn't match what's actually being checked, making debug analysis misleading.
**Impact:** Confusing diagnostics. An operator seeing H=1.55 in logs might think the safety threshold (1.6) is about to trip, when the actual enabled-tier entropy is much higher.

#### M9. Broad `except Exception` Without Traceback in `on_liquidation()`

**File:** `poc/liq_calibrator.py`, `on_liquidation()` outer handler
**Description:** The top-level exception handler catches all exceptions but logs only `str(e)` without `exc_info=True` or `traceback.format_exc()`. Stack trace is lost.
**Impact:** If an unexpected error occurs during liquidation processing, root cause analysis requires reproducing the error. Violates the spirit of CLAUDE.md Rule #10.

#### M10. No Time-Proportional Decay on Zone Load After Restart

**File:** `poc/active_zone_manager.py`, zone persistence load
**Description:** When zones are loaded from disk after a restart, their weights are restored at the value they had when last saved. No catch-up decay is applied for the time elapsed since the last save. If the process was down for 2 hours, zones resume at pre-restart weight instead of being decayed by `0.995^120 = 0.548`.
**Impact:** Stale zones appear artificially strong after a restart. They will decay normally going forward, but there's a transient period of inflated zone weights.

#### M11. `SAFETY_MAX_WEIGHT_CAP` Is Unused / Unreachable

**File:** `poc/liq_calibrator.py`
**Description:** The constant `SAFETY_MAX_WEIGHT_CAP` is defined but the max-weight safety trip uses `self.max_weight = 0.40` (instance attribute) while the per-tier cap is 0.35. Since no tier can exceed 0.35 (clamped during calibration), the 0.40 safety trip is structurally unreachable.
**Impact:** Dead safety check. The max-weight trip can never fire.

### WebSocket & Polling

#### M12. WebSocket Connectors Have Zero Error Logging

**File:** `poc/ws_connectors.py`, all three connectors
**Description:** `BinanceConnector`, `BybitConnector`, and `OKXConnector` silently catch and discard connection errors, disconnection events, and message parse failures. No `logger.error()` or `logger.warning()` calls exist in any error path.
**Impact:** WebSocket disconnections and data loss are completely invisible. The only signal is `exchange_last_seen` going stale in the dashboard.

#### M13. WebSocket Callback Exception Causes Full Reconnection

**File:** `poc/ws_connectors.py`
**Description:** If the callback function (which processes messages in `full_metrics_viewer.py`) throws an exception, it propagates up through the connector's message loop and triggers a full WebSocket reconnection. A single malformed message can cause reconnection of all streams on that exchange.
**Impact:** Transient data processing errors escalate to connection-level disruption. All streams on the affected exchange go down during reconnect.

#### M14. No Exponential Backoff on WebSocket Reconnection

**File:** `poc/ws_connectors.py`
**Description:** All three connectors use a fixed 1-second delay before reconnection. If an exchange is down or rate-limiting, the connector hammers it at 1 req/sec indefinitely.
**Impact:** Could trigger rate limits or IP bans from exchanges during extended outages.

#### M15. OI Poller Delivers `aggregated_oi=0.0` When All Exchanges Fail

**File:** `poc/oi_poller.py`
**Description:** If all three exchange requests fail in a poll cycle, `aggregated_oi` is computed as `sum([])` = 0.0. This is indistinguishable from a legitimate 0.0 OI reading. Downstream consumers (entry inference) interpret this as a massive OI drop, triggering projection removal.
**Impact:** A transient network outage causes the entry inference to wipe all projections (interpreting 0.0 as "all positions closed"). Projections must rebuild from scratch.

#### M16. `rest_pollers.py` Backoff Logic Computed But Never Used

**File:** `poc/rest_pollers.py`
**Description:** The retry backoff duration is calculated but the actual sleep always uses the fixed poll interval. The backoff variable is computed then discarded.
**Impact:** No exponential backoff on REST API failures. Fixed-interval retries on error.

#### M17. `rest_pollers.py` HTTP Session Leak

**File:** `poc/rest_pollers.py`
**Description:** `requests.Session()` is created in the thread's run loop but never closed in a `try/finally` block. If the thread exits abnormally, the session and its underlying TCP connections are leaked.
**Impact:** Resource leak under abnormal termination. Minor — Python GC will eventually clean up.

### Viewer Pipeline

#### M18. Heavy Minute-Rollover Work Under `self.lock` Blocks All Threads

**File:** `poc/full_metrics_viewer.py`, `_check_minute_rollover()`
**Description:** The minute rollover performs V2 snapshot computation, JSON serialization, atomic file writes, calibrator snapshot calls, and zone persistence — all while holding `self.lock`. This blocks the WebSocket processing thread, the display thread, and the API thread for the duration.
**Impact:** Under heavy load, a slow disk write or complex snapshot computation could cause WebSocket message backlog, stale display, or API timeouts.

#### M19. OI Double-Counting From WS + REST Paths

**File:** `poc/full_metrics_viewer.py`
**Description:** `_process_oi()` processes OI from the Bybit WebSocket `tickers` stream and updates `perp_OIs_per_instrument`. The `MultiExchangeOIPoller` also fetches Bybit OI via REST every 15s. Both write to overlapping state, potentially double-counting Bybit's OI contribution.
**Impact:** BTC OI display may be inflated. The actual engine input (via `oi_poller` callback) is correct — only the dashboard display is affected.

#### M20. `long_size_btc` / `short_size_btc` Field Names Wrong for ETH/SOL

**File:** `poc/full_metrics_viewer.py`, V2 snapshot construction
**Description:** The V2 snapshot always uses field names `long_size_btc` and `short_size_btc` regardless of symbol. For ETH snapshots, this should be `long_size_eth`; for SOL, `long_size_sol`.
**Impact:** Frontend consumers parsing these fields by name would be confused. If they use the fields generically ("the size field"), it works. If they filter by suffix, ETH/SOL sizes appear under wrong labels.

#### M21. BTC OI Dashboard Label Says "USD" But Value Is BTC

**File:** `poc/full_metrics_viewer.py`, dashboard rendering
**Description:** The Rich terminal dashboard displays BTC OI with a "USD" label, but the value from `oi_poller` is in base asset units (BTC count). CLAUDE.md Rule #17 explicitly warns about this.
**Impact:** Operator sees "OI: 152,000 USD" when it's actually 152,000 BTC (~$14B). Misleading dashboard.

#### M22. Log Rotation Only Runs at Startup

**File:** `poc/full_metrics_viewer.py`
**Description:** The log rotation check (200MB / 24h thresholds from CLAUDE.md) only runs once at process startup. Long-running processes (the normal case for a trading backend) never rotate logs mid-session.
**Impact:** Log files grow unbounded during a session. A multi-day run could produce multi-GB log files before the next restart triggers rotation.

### Embedded API

#### M23. `/liq_stats` Mutates Cached Snapshot Dict In-Place

**File:** `poc/embedded_api.py`, lines ~875-878
**Description:** The endpoint gets the stats sub-dict from the cached V2 snapshot via `snapshot.get('stats', {})` and mutates it in-place by adding `stats['symbol']`, `stats['snapshot_ts']`, `stats['snapshot_age_s']`. This modifies the cached snapshot, injecting extra keys visible to `/liq_heatmap_v2` responses.
**Impact:** V2 snapshot responses gain unexpected `symbol`, `snapshot_ts`, `snapshot_age_s` keys in their `stats` sub-object. `snapshot_age_s` is stale (baked in from last `/liq_stats` call). Data corruption of shared cache.

#### M24. `range_pct` Parameter on `/orderbook_heatmap` Accepted But Ignored

**File:** `poc/embedded_api.py`, line ~906
**Description:** The `range_pct` query parameter is accepted and included in the cache key, but never used in the endpoint body. Different `range_pct` values create separate cache entries for identical responses.
**Impact:** Misleading API contract. Clients expect filtering; get no filtering. Cache waste.

#### M25. `LiquidationHeatmapBuffer._load_from_disk` Silently Swallows Exceptions

**File:** `poc/embedded_api.py`, lines ~226-229
**Description:** Both inner and outer exception handlers use bare `except Exception: pass` without any logging. Corrupt binary files are silently ignored.
**Impact:** History data loss is invisible on startup. Violates CLAUDE.md Rule #10.

#### M26. Default API Bind Address Is `127.0.0.1`

**File:** `poc/embedded_api.py` line ~1464, `poc/full_metrics_viewer.py` line ~2390
**Description:** Both `start_api_thread()` default and argparse default use `"127.0.0.1"`. CLAUDE.md Rule #5: "NEVER hardcode '127.0.0.1' — use '0.0.0.0' for production."
**Impact:** Deployment requires explicit `--api-host 0.0.0.0` override. Forgetting this means the API is unreachable from the frontend on a remote server.

#### M27. `/liq_zones_heatmap` Fabricates `notional_usd` From Magic Constant

**File:** `poc/embedded_api.py`, line ~1409
**Description:** Computes `estimated_notional = z['weight'] * 100000.0`. The `100000.0` multiplier is undocumented and arbitrary. The resulting `notional_usd` is presented identically to real USD measurements from V2 endpoints.
**Impact:** API consumers cannot distinguish real from synthetic notional values. The `min_notional` filter operates on fabricated data.

### OB Heatmap

#### M28. OB Binary Compaction Is Not Atomic

**File:** `poc/ob_heatmap.py`, lines ~1004-1017
**Description:** `_maybe_compact()` reads data, then opens the same file with `'wb'` (truncating it) and rewrites. A crash between truncate and write completion loses all history data.
**Impact:** Data loss on crash during compaction. Inconsistent with the codebase's atomic write discipline.

#### M29. CLAUDE.md OB Binary Format Documentation Is Wrong

**File:** `poc/ob_heatmap.py`, lines ~492-497 vs CLAUDE.md
**Description:** CLAUDE.md documents the OB binary header as 48 bytes with format `ts(d) + src(d) + price_min(d) + price_max(d) + step(d) + n_buckets(I)`. Actual code: 76 bytes, format `<dddddIdddd` with different field order and 4 extra doubles (`norm_p50`, `norm_p95`, `total_bid`, `total_ask`).
**Impact:** Any parser built from CLAUDE.md documentation would read garbage. The 48-byte format actually describes `LiquidationHeatmapBuffer` (different binary format).

#### M30. Callback Runs Under Lock — Lock Ordering Dependency

**File:** `poc/ob_heatmap.py`, lines ~443-448
**Description:** `OrderbookReconstructor.on_book_update` callback is invoked while holding `Reconstructor._lock`. The callback calls `OrderbookAccumulator.on_depth_update()` which acquires `Accumulator._lock`. This creates a fixed lock ordering: `Reconstructor._lock` → `Accumulator._lock`.
**Impact:** Safe as long as no code path reverses this order. Undocumented and fragile.

### Orphaned Files (Active Impact Only)

#### M31. `liq_plotter.py` Silently Swallows File Read Exceptions

**File:** `poc/liq_plotter.py`, lines ~216-217
**Description:** Outer `except Exception: pass` in `_read_new_entries()` silently swallows all file I/O errors. Chart silently freezes with no indication.
**Impact:** Violates CLAUDE.md Rule #10. Only affects the standalone plotter utility.

---

## 4. LOW

### L1. HeatmapConfig Default `decay=0.998` vs Runtime `decay=0.995`
**File:** `poc/liq_heatmap.py` (HeatmapConfig) vs `poc/full_metrics_viewer.py` (runtime)
Runtime always passes `decay=0.995`, but the dataclass default is `0.998`. A new caller using defaults gets different decay.

### L2. `create_heatmap_engine()` Factory Is Dead Code
**File:** `poc/liq_heatmap.py`
Module-level factory function with stale ETH config (step=5.0, should be 1.0). Never called.

### L3. Dead Code in `liq_calibrator.py` (~209 Lines)
`_log_event()` (105 lines), `_log_approach_event()` (73 lines), `_attribute_to_leverage()` (31 lines), `MIN_TIER_SAMPLES` constant, `SAFETY_MAX_WEIGHT_CAP` constant.

### L4. `active_zone_manager.py` — `_save_to_disk` No flush/fsync Before Rename
**File:** `poc/active_zone_manager.py`
Uses `os.rename()` instead of `os.replace()` and doesn't flush/fsync before rename. Zone state could be lost on crash.

### L5. Zone Log File Never Rotated
**File:** `poc/active_zone_manager.py`
Zone lifecycle logs (`liq_zones_{SYM}.jsonl`) grow without rotation. No size or age check.

### L6. Global Singleton `get_zone_manager()` Is Single-Symbol and Unused
**File:** `poc/active_zone_manager.py`
Module-level function creates a single shared instance. Not used by any active code.

### L7. `ws_connectors.py` — `StreamConfig` Dataclass Never Used
Defined but never instantiated. Dead code.

### L8. `ws_connectors.py` — `deque` and `field` Imported But Never Used
Dead imports.

### L9. `rest_pollers.py` — Dead OI Polling Infrastructure (~100 Lines)
OI polling disabled via `interval=999999`. Entire code path is dead since `oi_poller.py` handles OI.

### L10. OKX Contract Size Fallback to 1.0 for Unknown Symbols
**File:** `poc/liq_normalizer.py`
`OKX_CONTRACT_SIZES.get(symbol_short, 1.0)` — fallback of 1.0 would be wrong for most symbols. Currently safe because only BTC/ETH/SOL are configured.

### L11. V1 Snapshot Key Validation Broken
**File:** `poc/full_metrics_viewer.py`
V1 snapshot key validation checks for keys that may not exist in the dict shape actually written. Non-functional — just a stale guard.

### L12. Cache Key Fragmentation From Raw Symbol in V3/OB Endpoints
**File:** `poc/embedded_api.py`
Cache keys use raw `symbol` parameter before `_validate_symbol()` normalization. `?symbol=BTC` and `?symbol=btc` are separate cache entries for identical data.

### L13. `DEFAULT_STEP` Import Shadowed by Local Redefinition
**File:** `poc/embedded_api.py`
Imports `DEFAULT_STEP` from `ob_heatmap` then redefines it locally. Both are 20.0. Import is wasted.

### L14. OB History Stride Clamp Is 1..60 vs CLAUDE.md's 1..30
**File:** `poc/embedded_api.py`
Liq history endpoints correctly clamp to 1..30. OB history allows up to 60. Minor inconsistency.

### L15. Fallback Price Grid Uses `DEFAULT_STEP=20.0` for All Symbols
**File:** `poc/embedded_api.py`
When no data exists, SOL fallback grid uses $20 buckets instead of $0.10. Self-heals within ~1 minute when real data arrives.

### L16. `EngineManager.get_zones()` and `get_snapshot()` Are Dead Code
**File:** `poc/engine_manager.py`
Never called by any consumer. The embedded API duplicates the logic inline.

### L17. `register_engine()` Silently Overwrites Instance Identity Fields
**File:** `poc/engine_manager.py`
Overwrites `engine_instance.symbol_short/symbol_full` with parameters. No assertion that values match.

### L18. Print Statements in Production Code
**Files:** `poc/embedded_api.py`, `poc/ob_heatmap.py`
Multiple `print()` calls instead of `logger.info()`/`logger.debug()`. Pollute Rich terminal display.

### L19. Orphaned `liq_api.py` Reads Non-Existent Legacy Filenames
Reads `liq_api_snapshot.json` (no longer written). Would return 503 on everything if accidentally run.

### L20. Orphaned `metrics_viewer.py` Has Inverted Side Conventions
Maps Binance `"buy"` to long liquidations (should be short). Historical bug pattern.

### L21. Audit Scripts Reference Non-Per-Symbol Log File Paths
**Files:** `poc/audit_v2_comprehensive.py`, `poc/audit_v2_engine.py`
Hardcoded pre-Phase-2 filenames (`liq_debug.jsonl` instead of `liq_debug_BTC.jsonl`).

### L22. `metrics_viewer.py` Auto-Installs pip Packages (Security Anti-Pattern)
Runs `os.system(pip install rich)` if import fails. Security concern in orphaned file.

### L23. `ob_heatmap.py` — Debug Print Statements (~7 Locations)
`[OB_ACCUM_DEBUG]` and `[OB_BUFFER_DEBUG]` prefixed prints. ~2880 prints/day to stdout.

### L24. `/oi` Endpoint Has No Backward-Compat Alias
Every other endpoint has a `/vN/` alias. `/oi` does not. Pattern inconsistency.

---

## 5. CLAUDE.md UPDATES NEEDED

| Section | Issue | Action |
|---------|-------|--------|
| Binary format documentation | Documents 48-byte OB header; actual is 76 bytes with different fields and order | Rewrite to document actual `<dddddIdddd` format (76 bytes) for OB, clarify separate 48-byte format for liq history |
| `get_combined_heatmap()` default weights | Documents 0.35/0.65; code defaults are 0.4/0.6 (callers override) | Either update code defaults to match, or document that defaults differ from runtime values |
| `DEFAULT_LEVERAGE_WEIGHTS` coverage | Not documented that fallback weights only cover 7 of 11 BTC tiers | Add note about cold-start gap for 125x-250x tiers |
| OI poller failure mode | Not documented that all-exchange failure delivers 0.0 | Add note about aggregated_oi=0.0 ambiguity and downstream impact |
| Minute rollover BTC gate | Not documented that rollover requires BTC perp_close > 0 | Document the BTC dependency or mark as a bug to fix |
| `_push_tape_learning()` tier coverage | Not documented | Add note about disabled tier inclusion and missing high-leverage tiers |
| Log rotation behavior | Documents rotation thresholds but not that they only apply at startup | Clarify that rotation is startup-only, not continuous |
| Lock ordering | Not documented | Add note about Reconstructor._lock → Accumulator._lock ordering |
| `liq_zones_heatmap` notional fabrication | `notional_usd` field presented as real USD | Document the `weight * 100000` synthetic calculation |
| V2 snapshot `long_size_btc` field name | Not documented that field name is symbol-agnostic | Document or fix the field naming |

---

## 6. RAW vs DISPLAY BOUNDARY VERIFICATION

### Boundary Definition (from CLAUDE.md)

| Method | Type | Used By |
|--------|------|---------|
| `get_combined_heatmap()` | RAW | Zone logic, calibrator, diagnostics |
| `get_combined_heatmap_display()` | DISPLAY | V1/V2 intensity grids in snapshot writes |
| `get_heatmap()` | RAW | Calibrator `pred_longs`/`pred_shorts` via `on_minute_snapshot()` |
| `get_heatmap_display()` | DISPLAY | V1/V2 snapshot intensity arrays |
| `get_api_response()` | RAW | Non-display consumers |
| `get_api_response_display()` | DISPLAY | V2 snapshot `long_levels`/`short_levels`, BTC zone tracker |

### Verification Results

| Call Site | File | Line | Method Called | Correct? |
|-----------|------|------|-------------|----------|
| Calibrator `on_minute_snapshot` pred_longs/pred_shorts | `full_metrics_viewer.py` | ~1741 | `get_heatmap()` | **YES** — raw, correct |
| V2 snapshot `long_levels`/`short_levels` | `full_metrics_viewer.py` | ~1550 | `get_api_response_display()` | **YES** — display wrapper, correct |
| BTC zone tracker display | `full_metrics_viewer.py` | ~1626 | `get_api_response_display()` | **YES** — display wrapper, correct |
| V1 snapshot intensity arrays | `full_metrics_viewer.py` | V1 path | `get_heatmap_display()` | **YES** — display wrapper, correct |
| V2 snapshot intensity grids | `full_metrics_viewer.py` | V2 path | `get_heatmap_display()` | **YES** — display wrapper, correct |
| Zone manager input | `liq_heatmap.py` | zone creation | `get_combined_heatmap()` | **YES** — raw, correct |
| `_push_tape_learning()` | `liq_heatmap.py` | tape feedback | Raw tape/inference dicts | **YES** — raw path, no display |
| Diagnostic logging (10-min) | `entry_inference.py` | boost log | `get_combined_heatmap_display()` | **YES** — display method logs display metrics |

**Boundary verdict: ALL CALL SITES CORRECT.** No display wrapper is used in any logic/calibration path. No raw method is used where display boost should apply. The A3a-v2 changes (cluster-level boost in `get_api_response_display()`) correctly preserve this boundary.

---

## 7. DEAD CODE INVENTORY

| File | Code | Lines | Notes |
|------|------|-------|-------|
| `liq_calibrator.py` | `_log_event()` | ~105 | Never called |
| `liq_calibrator.py` | `_log_approach_event()` | ~73 | Never called |
| `liq_calibrator.py` | `_attribute_to_leverage()` | ~31 | Never called |
| `liq_calibrator.py` | `MIN_TIER_SAMPLES` | 1 | Never referenced |
| `liq_calibrator.py` | `SAFETY_MAX_WEIGHT_CAP` | 1 | Unreachable (M11) |
| `liq_heatmap.py` | `create_heatmap_engine()` | ~20 | Never called, stale config |
| `engine_manager.py` | `get_zones()` | ~33 | Never called |
| `engine_manager.py` | `get_snapshot()` | ~10 | Never called |
| `active_zone_manager.py` | `get_zone_manager()` | ~15 | Never called |
| `ws_connectors.py` | `StreamConfig` | ~5 | Never instantiated |
| `rest_pollers.py` | OI polling code | ~100 | Disabled via interval=999999 |
| `full_metrics_viewer.py` | `liq_engine_stats` state | ~5 | V1 reference, unused |
| **TOTAL** | | **~399 lines** | |

### Orphaned Files (entire files, not imported by active code)

| File | Lines | Status |
|------|-------|--------|
| `liq_engine.py` | ~597 | Superseded by V2 heatmap |
| `liq_api.py` | ~1863 | Superseded by embedded_api.py |
| `metrics_viewer.py` | ~580 | Superseded by full_metrics_viewer.py |
| **TOTAL** | **~3040 lines** | |

---

## 8. THREAD SAFETY SUMMARY

| Shared Resource | Writer Thread | Reader Thread | Protection | Status |
|----------------|--------------|---------------|------------|--------|
| `ActiveZoneManager` zones | Main (via on_minute) | API daemon | `RLock` | **SAFE** |
| `OrderbookHeatmapBuffer` frames | Main (via on_depth) | API daemon | `RLock` | **SAFE** |
| `OISnapshot` dict | OI poller thread | API daemon | CPython GIL (atomic dict set) | **GIL-dependent** |
| `_v1_caches` / `_v2_caches` | Refresh loop thread | API daemon | CPython GIL (atomic dict set) | **GIL-dependent** |
| `EngineManager.engines` | Main (registration) | API daemon | Init-before-serve ordering | **SAFE** (by design) |
| `EntryInference` projection dicts | Main (on_minute) | API daemon (get_*_display) | **NONE** | **UNSAFE** (M3) |
| `self.state.*` fields | Main (process_*) | Display thread | `self.lock` (partial) | **PARTIALLY SAFE** |
| `self.lock` scope | Main (minute rollover) | WS thread, display, API | Single lock | **CONTENTION RISK** (M18) |

---

## 9. SUMMARY STATISTICS

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 3 |
| MEDIUM | 31 |
| LOW | 24 |
| **TOTAL** | **59** |

| Category | Count |
|----------|-------|
| Silent failures / swallowed exceptions | 8 |
| CLAUDE.md rule violations | 7 |
| Thread safety concerns | 4 |
| Dead code | 12 items (~399 lines + 3040 lines orphaned) |
| Documentation mismatches | 6 |
| Data correctness | 5 |
| Resource leaks | 2 |
| API contract issues | 3 |

---

*Report generated by exhaustive file-by-file audit of 22 files in `poc/`. All findings are code-verified against CLAUDE.md specifications.*
