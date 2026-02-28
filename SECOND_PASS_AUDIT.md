# Second-Pass Codebase Audit — moonStreamProcess

**Date:** 2026-02-28
**Auditor scope:** All `.py` files in `poc/`
**Method:** Code-level review of every file, cross-referenced against CLAUDE.md, git log, and Sprint 1/2 fix history
**Perspective:** Attacker, stressed production server, confused new developer

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 5 |
| MEDIUM | 10 |
| LOW | 8 |
| **Total** | **24** |

The Sprint 1/2 fixes addressed the most dangerous issues. This second pass found no data-corruption-on-happy-path bugs, but uncovered thread-safety gaps in the display layer, an incomplete Sprint 2 lock fix, unbounded log files, and several robustness issues that would surface under sustained production load.

---

## 1. CRITICAL

### C1: `get_combined_heatmap()` reads live projection dicts outside `_projection_lock` after copy-on-read

**File:** `poc/entry_inference.py` lines 831, 840
**Confidence:** HIGH

The Sprint 2 M1 fix added copy-on-read under `_projection_lock` at lines 820-822. But lines 831 and 840 access the **live** `self.projected_long_liqs` and `self.projected_short_liqs` dicts directly — outside the lock — to build the union price set:

```python
# Line 820-822: Lock is held, copies made
with self._projection_lock:
    proj_long = {b.price: max(0.0, b.size_usd) for b in self.projected_long_liqs.values()}
    proj_short = {b.price: max(0.0, b.size_usd) for b in self.projected_short_liqs.values()}
# Lock released here

# Lines 831, 840: Live dicts accessed WITHOUT lock
all_long_prices = set(tape_long.keys()) | set(self.projected_long_liqs.keys())   # ← RACE
all_short_prices = set(tape_short.keys()) | set(self.projected_short_liqs.keys()) # ← RACE
```

**Call chain:** API thread → `LiquidationHeatmap.get_heatmap()` → `EntryInference.get_combined_heatmap()` → accesses live projection dicts. Meanwhile, WS thread → `_check_minute_rollover()` → `EntryInference.on_minute()` (line 297) → holds `_projection_lock` and mutates projection dicts.

**Impact:** The `all_long_prices` set may include prices from the *new* minute's projections that don't exist in the *old* `proj_long` copy. When `proj_long.get(price, 0)` returns 0 for these phantom prices, the combined value is pure tape at that bucket — subtly wrong but not a crash. In the opposite direction, if `on_minute` deletes a bucket between line 822 and 831, the price appears in `proj_long` but is missing from `all_long_prices`, so it's silently dropped from the combined output.

The same pattern exists in `get_combined_heatmap_display()` at lines 884-885 (copy under lock) and the subsequent iteration (not shown, but same structure).

**The same issue exists** in `liq_heatmap.py` line 196: `_estimate_entry_from_projection()` accesses `self.inference.projected_long_liqs` and `self.inference.projected_short_liqs` directly without acquiring `_projection_lock`. Called from `on_force_order` → WS thread path, while `on_minute` mutates these dicts from another thread context.

**Fix direction:** Use the already-copied `proj_long.keys()` and `proj_short.keys()` instead of the live dict keys:
```python
all_long_prices = set(tape_long.keys()) | set(proj_long.keys())
```
For `_estimate_entry_from_projection`, acquire `_projection_lock` or do copy-on-read.

---

## 2. HIGH

### H1: Display thread reads live processor attributes without lock

**File:** `poc/full_metrics_viewer.py` lines 990, 2123, 2126, 2144, 2172, 2189
**Confidence:** HIGH

`create_display()` (main thread at 4 FPS) reads `processor.symbol_prices`, `processor.liq_counts`, `processor.oi_by_symbol`, and `processor.exchange_last_seen` directly — **not** through the lock-protected `get_state()` deep copy. These attributes are written from the WS thread and OI poller thread under `self.lock`, but the reads in `create_display` never acquire it.

```python
# Main thread (create_display, no lock):
prices = processor.symbol_prices.get(sym, {})              # L2123
liq_events = sum(processor.liq_counts.get(sym, {}).values()) # L2126
agg_oi = processor.oi_by_symbol.get(sym, {}).get(...)        # L2144
age = now_ts - processor.exchange_last_seen.get(ex, 0.0)     # L2189

# WS thread (under self.lock):
self.exchange_last_seen[exchange] = time.time()  # L990 — actually OUTSIDE lock
```

Note: `exchange_last_seen` is updated at line 990, which is **before** `with self.lock:` at line 992. So it's written without *any* lock.

**Impact:** Display shows torn reads of multi-key dicts. Under CPython's GIL this is practically safe for individual `dict.get()` calls, but `sum(processor.liq_counts.get(sym, {}).values())` iterates a dict that could be mid-mutation, risking `RuntimeError: dictionary changed size during iteration`.

**Fix direction:** Either acquire `self.lock` in `create_display`, or copy these attributes into `FullMetricsState` in `get_state()`.

### H2: Zone persistence (`_save_to_disk`) serializes shared references outside lock

**File:** `poc/active_zone_manager.py` lines 551-577
**Confidence:** HIGH

The lock is held at lines 551-570 to build `zones_data`, but `tier_contributions` at line 562 is a **dict reference** shared with the live `ActiveZone` object. The lock is released at line 570, and `json.dump(data, f)` at line 575 iterates `tier_contributions` without the lock.

```python
with self._lock:
    zones_data.append({
        ...
        'tier_contributions': zone.tier_contributions  # ← shared reference
    })
# Lock released

json.dump(data, f, indent=2)  # ← iterates tier_contributions WITHOUT lock
```

If the main thread (via `_process_zone_update`) modifies `zone.tier_contributions` between lock release and `json.dump`, the serialization could raise `RuntimeError: dictionary changed size during iteration`.

Additionally:
- No `f.flush()` / `os.fsync()` before `os.replace()` (unlike `write_json_atomic`) — data loss on power failure
- No temp file cleanup in the `except` block — orphan `.tmp` files accumulate on repeated failures

**Fix direction:** Deep-copy `tier_contributions` inside the lock: `'tier_contributions': dict(zone.tier_contributions)`. Add `f.flush(); os.fsync(f.fileno())`. Add temp file cleanup in `except`.

### H3: 15 append-mode log files have NO rotation — grow unboundedly during multi-day runs

**Files:** `poc/liq_tape.py`, `poc/entry_inference.py`, `poc/liq_heatmap.py`, `poc/active_zone_manager.py`
**Confidence:** HIGH

Only 2 of 17 log files have rotation (and only at startup, not during runtime):
- `liq_calibrator_{SYM}.jsonl` — startup rotation only
- `liq_debug.jsonl`, `plot_feed.jsonl` — startup rotation only

The following 15 log files opened in append mode at startup and **never rotated**:

| File pattern | Count | Growth rate estimate |
|---|---|---|
| `liq_tape_{SYM}.jsonl` | 3 | ~200 bytes/event × events/hour |
| `liq_sweeps_{SYM}.jsonl` | 3 | ~300 bytes/sweep |
| `liq_inference_{SYM}.jsonl` | 3 | ~500 bytes/minute |
| `liq_debug_{SYM}.jsonl` | 3 | ~500 bytes/minute + periodic diagnostics |
| `liq_zones_{SYM}.jsonl` | 3 | ~300 bytes/zone lifecycle event |
| `liq_cycles_{SYM}.jsonl` | 3 | ~500 bytes/minute (3 × 1440 = 4320 entries/day) |

**24-hour estimate:** `liq_cycles` alone: 3 symbols × 1440 min × 500 bytes ≈ 2.1 MB/day. `liq_debug`: similar. `liq_inference`: similar. Combined: ~10-15 MB/day from periodic logs. Event-driven logs add more depending on market activity.

**7-day estimate:** ~70-100 MB from periodic logs alone. With active markets (500+ liqs/day), tape + sweep logs add ~20-50 MB/week per symbol. **Total: ~150-250 MB/week.**

**30-day estimate:** ~600 MB - 1 GB. Disk will fill on constrained deployments.

**Fix direction:** Add runtime log rotation — either periodic size checks (e.g., on each flush) or use Python's `logging.handlers.RotatingFileHandler` for the JSONL files.

### H4: API cache pollution via unbounded query parameter space

**File:** `poc/embedded_api.py` lines 765, 991, 1131, 1298, 1402
**Confidence:** MEDIUM

Cache keys include raw float/string query parameters without normalization or bounds checking:

```python
# Line 991 — any float value creates a unique cache key
cache_key = f"orderbook_heatmap?...&price_min={price_min}&price_max={price_max}"

# Line 1298 — raw symbol, any side string, any float min_weight
cache_key = f"liq_zones?symbol={symbol}&side={side}&min_leverage={min_leverage}..."
```

An attacker can generate unlimited unique cache keys by varying `price_min`, `price_max`, `min_notional`, `min_weight`, `side`, etc. The `ResponseCache._evict_expired()` runs every 100 `set()` calls and only removes entries older than 60s. Between evictions, each unique key stores a `JSONResponse` object (~10-50 KB).

**Sustained attack:** At 100 unique-key requests/second × 60s TTL × 50 KB/response = **300 MB steady-state memory** from cache alone.

Additionally, V3 zone cache keys use raw `symbol` instead of validated `symbol_short` (line 1298 vs validation at 1303), so `symbol=BTC`, `symbol=btc`, and `symbol=BTCUSDT` produce 3 separate cache entries for identical data.

**Fix direction:** Validate and normalize all query params before cache key construction. Round floats to fixed precision. Reject invalid `side` values. Add a hard cap on cache size (e.g., max 500 entries).

### H5: No re-entrancy guard on shutdown; double shutdown executes cleanup twice

**File:** `poc/full_metrics_viewer.py` lines 2506-2535
**Confidence:** MEDIUM

The signal handler (line 2506) and `finally` block (line 2529) both call the exact same cleanup sequence: `stop_event.set()`, `connector.stop()`, `oi_poller.stop()`, `rest_poller.stop()`, `_close_engines()`, `_stop_api_state()`. Every component's `stop()` and `close()` is called twice.

A rapid double Ctrl+C re-enters `signal_handler` while the first invocation is still in `_close_engines()`. If `heatmap_v2.close()` closes file handles, the second invocation will attempt to close already-closed handles (caught by try/except, but produces noisy error logs).

**Fix direction:** Add a `_shutdown_in_progress` flag at module level. Check it at the top of `signal_handler` and the `finally` block.

---

## 3. MEDIUM

### M1: `on_minute_snapshot` overwrites `current_weights` without `_enforce_disabled_tiers()`

**File:** `poc/liq_calibrator.py` lines 894-895
**Confidence:** HIGH

```python
self.current_ladder = ladder.copy()
self.current_weights = weights.copy()  # ← no _enforce_disabled_tiers() call
```

CLAUDE.md documents three mandatory call sites for `_enforce_disabled_tiers()`. This fourth mutation point is not covered. Between `on_minute_snapshot()` and the next `_run_calibration()`, `self.current_weights` may contain nonzero values for the disabled 10x tier.

**Impact:** `get_stats()` (line 2933) and `get_persisted_weights()` (line 2949) return unenforced weights. If the viewer uses `get_persisted_weights()` to feed the heatmap's `leverage_weights`, the disabled tier briefly participates in projection. The calibration math itself is unaffected (uses snapshot-local data).

**Fix direction:** Call `self._enforce_disabled_tiers()` after line 895.

### M2: Disabled tiers participate in soft attribution responsibility normalization

**File:** `poc/liq_calibrator.py` lines 1166-1175
**Confidence:** MEDIUM

The responsibility distribution `r(L)` is computed across ALL tiers, including the disabled 10x tier. The 10x tier absorbs a fraction of the responsibility mass (via the exponential distance kernel), diluting the signal that reaches neighboring enabled tiers (5x and 20x). The accumulated stats for the disabled tier are zeroed in `_run_calibration()`, but the dilution has already occurred.

**Impact:** Quantification depends on the typical distribution of liquidation events relative to implied levels. For events near the 10x implied level, the dilution could be 10-20% of signal lost from 5x/20x attribution. Produces slightly slower calibration convergence for those tiers.

**Fix direction:** Exclude `DISABLED_TIERS` from the responsibility normalization denominator in `_process_event()`.

### M3: Calibrator `events_window` deque has no `maxlen`

**File:** `poc/liq_calibrator.py` line 379
**Confidence:** MEDIUM

```python
self.events_window: deque = deque()  # no maxlen
```

Time-based cleanup (`_clean_old_events`, 15-minute window) limits growth under normal conditions. But during a flash crash with 1000+ liquidations/second, the deque could accumulate 90,000+ entries before cleanup runs (at the next `on_minute_snapshot` call, which is every 60 seconds).

Each `LiquidationEvent` is ~200 bytes → ~18 MB memory spike. Not catastrophic but unnecessary.

**Fix direction:** Add `maxlen=10000` (or a reasonable cap based on max expected events per window).

### M4: Non-Binance orderbook dicts accumulate stale price levels

**File:** `poc/full_metrics_viewer.py` lines 1033-1051
**Confidence:** MEDIUM

For Bybit/OKX depth messages, the diff-only path adds new levels but only removes them when `qty == 0`. Unlike the Binance path (line 1027: `book["bids"].clear()`), stale entries from previous updates are never cleared. If price moves significantly over 24 hours, hundreds of dead price levels accumulate.

Currently Bybit/OKX orderbook data is marked "future use" in CLAUDE.md, so this is a latent bug. It becomes real if ETH/SOL orderbook engines are enabled.

**Fix direction:** Clear bids/asks on each snapshot-style update, or add periodic cleanup of out-of-range levels.

### M5: Calibrator log rotation only fires at startup

**File:** `poc/liq_calibrator.py` lines 438-444
**Confidence:** HIGH

`_rotate_log_if_needed()` is called once in `__init__`. During a multi-day run, the per-symbol calibrator log files grow past the 200 MB threshold without being rotated. At typical rates (~10-50 MB/day per symbol), the 200 MB limit is reached in 4-20 days.

**Fix direction:** Check log file size periodically (e.g., every `_run_calibration()` cycle, which is every 15 minutes).

### M6: `_save_to_disk` in zone manager missing fsync and temp file cleanup

**File:** `poc/active_zone_manager.py` lines 572-582
**Confidence:** HIGH

Three issues compared to `write_json_atomic()`:
1. No `f.flush()` / `os.fsync()` before `os.replace()` — data in OS buffer, not on disk
2. No temp file cleanup on failure — `.tmp` files accumulate
3. `exc_info=True` not used in error log — no traceback

The viewer's `write_json_atomic()` at line 188 handles all three correctly.

**Fix direction:** Use the same pattern as `write_json_atomic`: flush + fsync + cleanup on failure.

### M7: `_write_debug_log` and `_write_plot_feed` silently swallow exceptions

**File:** `poc/full_metrics_viewer.py` lines 175-176, 184-185
**Confidence:** MEDIUM

```python
except Exception:
    pass  # Don't crash on log failures
```

Violates CLAUDE.md Rule #10 ("NEVER swallow exceptions silently in data pipelines"). If the log file becomes unwritable (disk full, permissions), all debug logging stops silently. Unlike data pipeline exceptions, these are diagnostic logs, but the silent failure masks disk-full conditions.

**Fix direction:** `except Exception as e: logger.warning("debug log write failed: %s", e)` — one-line fix.

### M8: `entry_inference.py` debug log accesses projection dict lengths outside lock

**File:** `poc/entry_inference.py` lines 415-416
**Confidence:** LOW

```python
"total_long_buckets": len(self.projected_long_liqs),
"total_short_buckets": len(self.projected_short_liqs)
```

These are accessed after the `with self._projection_lock:` block ends at line 394. The values may be slightly stale. Impact is diagnostic-only (debug log), so this is cosmetic.

**Fix direction:** Capture lengths inside the lock block.

### M9: Calibrator `on_minute_snapshot` with `src=0` produces garbage implied levels

**File:** `poc/liq_calibrator.py` lines 868, 1045, 1148-1151
**Confidence:** MEDIUM

If `src=0` is passed (theoretically prevented by the viewer's price checks, but not by the calibrator itself), all implied liquidation levels become 0.0, distances become meaningless, and responsibilities become uniform. The event still increments `total_events`, producing garbage calibration data.

The viewer has guards (`perp_close > 0` check), but the calibrator itself accepts any value. A defensive early return would be safer.

**Fix direction:** Add `if src <= 0: return` at the top of `on_minute_snapshot()`.

### M10: `/orderbook_heatmap_stats` and `/orderbook_heatmap_debug` leak filesystem paths

**File:** `poc/embedded_api.py` lines 1232, 1259
**Confidence:** MEDIUM

```python
stats["reconstructor"] = {"error": f"failed to read: {e}"}  # L1232 — exception may contain path
debug_info["ob_buffer_stats_error"] = str(e)                  # L1259 — ditto
```

Exception strings from `json.load()` or `PermissionError` include the full filesystem path. These are returned directly in API responses, leaking internal directory structure to any API consumer.

**Fix direction:** Sanitize or genericize error messages before including in API responses.

---

## 4. LOW

### L1: `side` parameter on `/liq_zones` accepts arbitrary strings

**File:** `poc/embedded_api.py` line 1285
**Confidence:** HIGH

No validation that `side` is `"long"`, `"short"`, or `None`. Any string (e.g., `side=attack`) passes to `zone_mgr.get_active_zones()`, returns empty results, and creates a unique cache entry. Functionally safe but misleading — returns `zones_count: 0` instead of a 400 error.

**Fix direction:** Validate `side in ("long", "short", None)` and return 400 for invalid values.

### L2: `normalize_liquidation` call outside per-event try/except

**File:** `poc/full_metrics_viewer.py` line 1243
**Confidence:** MEDIUM

```python
events = normalize_liquidation(exchange, data)  # ← outside try/except
for event in events:
    try:
        ...
    except Exception as e:
        ...
```

If `normalize_liquidation` itself raises (e.g., `data` is `None` or an unexpected type), the exception propagates up, potentially skipping the entire batch. The normalizer has its own internal exception handling, but a defensive guard here would be safer.

**Fix direction:** Wrap the `normalize_liquidation` call in its own try/except.

### L3: `_process_trades` assumes `data["q"]` exists when `data["p"]` exists

**File:** `poc/full_metrics_viewer.py` line 1199
**Confidence:** LOW

If Binance sends `"p"` but not `"q"`, `KeyError` propagates to the WS thread's top-level handler. Caught and logged, but the error message would be confusing ("KeyError: 'q'").

**Fix direction:** Add `if "p" not in data or "q" not in data: return`.

### L4: Logged entropy disagrees with safety-check entropy

**File:** `poc/liq_calibrator.py` line 2633 vs lines 2324-2334
**Confidence:** HIGH

The logged entropy in `_log_calibration` computes over ALL weights (including disabled tiers with 0.0), while the safety-check entropy correctly filters to enabled tiers and renormalizes. The logged value will be lower than the safety value. Someone reviewing logs would see entropy below the safety threshold but no trip — confusing for debugging.

**Fix direction:** Compute logged entropy over enabled tiers only, matching the safety check.

### L5: `_maybe_write_recon_stats` silently swallows exceptions

**File:** `poc/full_metrics_viewer.py` lines 922-923
**Confidence:** LOW

Same pattern as M7 but for orderbook reconstructor stats — purely diagnostic.

**Fix direction:** Add logger.warning.

### L6: `self.state.perp_markPrice` dynamically assigned, not in dataclass

**File:** `poc/full_metrics_viewer.py` line 1353
**Confidence:** LOW

`FullMetricsState` dataclass does not declare `perp_markPrice`. The attribute is set dynamically at line 1353. This works in Python but would confuse IDE type checking and new developers. No code currently reads it.

**Fix direction:** Add `perp_markPrice: float = 0.0` to `FullMetricsState`.

### L7: Log rotation timestamp collision on rapid restart

**File:** `poc/liq_calibrator.py` lines 66-68
**Confidence:** LOW

Rotated backup uses minute-precision timestamp: `liq_calibrator_BTC.202602281430.jsonl`. Two restarts within the same minute overwrite the backup. Low probability in practice.

**Fix direction:** Add seconds or a counter to the timestamp format.

### L8: REST poller `get_state()` returns live reference, not copy

**File:** `poc/rest_pollers.py` lines 498-500
**Confidence:** LOW

`get_state()` returns `self.state` directly. Any caller could mutate it. Currently, the only caller (`_on_rest_poller_update`) reads fields under `self.lock` and never mutates the returned object. Not a current bug but fragile.

**Fix direction:** Return a copy if thread safety is needed for future callers.

---

## 5. SPRINT 1/2 FIX REVIEW

### Fixes verified as correct:

| Sprint Fix | Status | Verification |
|---|---|---|
| **S1-C1** BTC depth fallback | CORRECT | `_check_minute_rollover` falls back to `symbol_prices["BTC"]["close"]` after 60s stale. Both paths tested. |
| **S1-C2** `write_json_atomic` logging | CORRECT | `logger.error(..., exc_info=True)` at line 202. Temp file cleaned up. |
| **S1-C3** Shutdown engine close | CORRECT but DOUBLE-CALLED — `_close_engines` runs from both signal handler and finally block. Each engine's `close()` is independently try/excepted, so double-close produces warning logs but no crash. |
| **S1-C5** OB input validation | CORRECT | `step > 0` and `0 < range_pct <= 1.0` validated. |
| **S1-H1** History load logging | CORRECT | Per-record WARNING, whole-file ERROR with traceback, summary INFO. |
| **S1-H3** OI poller snapshot lock | CORRECT | `_snapshot_lock` acquired in both write and read paths. |
| **S1-H5** `/liq_stats` deepcopy | CORRECT | `copy.deepcopy()` before mutation at line 895. |
| **S2-C4** Calibrator event drop log | CORRECT | Rate-limited WARNING before early return. |
| **S2-H2** WS backoff | CORRECT | Exponential with jitter, capped at 30s, resets on successful connection. |
| **S2-H4** `/oi` 503 on warmup | CORRECT | Returns 503 instead of 404. |
| **S2-H6** Thread stop join | CORRECT | `join(timeout=5)` with WARNING on timeout. |
| **S2-H9** API state stop | CORRECT | `_stop_api_state()` calls `state.stop()` with thread join. |
| **S2-M1** Projection lock | PARTIALLY CORRECT — see C1 above. The lock is correctly added and used for copy-on-read, but `get_combined_heatmap()` still accesses the live dicts outside the lock for the union key set. |
| **S2-M2** OI skip on all-fail | CORRECT | Early return with WARNING, no callback, previous snapshot preserved. |
| **S2-M8** API bind address | CORRECT | `API_HOST` env var with `"127.0.0.1"` fallback. |

### New issues introduced by Sprint fixes:

| Fix | New Issue | Severity |
|---|---|---|
| **S1-C3** Shutdown | Double-call from signal handler + finally block | H5 (above) |
| **S2-M1** Projection lock | Incomplete — live dict keys still accessed outside lock | C1 (above) |

---

## 6. 24-HOUR RUNTIME PROJECTION

| Component | After 24 hours | Concern level |
|---|---|---|
| **Memory (data structures)** | Stable. All dicts/deques are either time-bounded (15-30 min windows) or bounded by fixed key space. No unbounded growth detected. | NONE |
| **Memory (log file handles)** | 17+ open file handles held indefinitely. Not a memory issue but FD count concern on constrained systems. | LOW |
| **Memory (API cache)** | Bounded by TTL eviction (60s). Normal usage: <10 MB. Under attack: up to ~300 MB (see H4). | MEDIUM under attack |
| **Disk (log files)** | ~10-15 MB/day from periodic logs (cycles, debug, inference). ~5-20 MB/day from event-driven logs depending on market activity. Total: ~15-35 MB/day. | LOW for 24h, HIGH for 7+ days |
| **Disk (binary files)** | `ob_heatmap_30s.bin`: 4 KB × 2880 frames/day = ~11.5 MB/day. History bins: per-symbol, similar scale. | LOW (bounded by frame count cap of 1440) |
| **Disk (snapshot JSON)** | Overwritten atomically each minute. Constant size. | NONE |
| **Disk (temp files)** | `write_json_atomic` cleans up. `_save_to_disk` does NOT clean up `.tmp` files on failure — could accumulate 3 orphan files per failed save cycle (one per symbol). | LOW |
| **Decay correctness** | `0.995^1440 ≈ 0.00074` — after 24 hours, a zone's weight decays to 0.07% of original. Below `MIN_ZONE_WEIGHT=0.05`, so zones are properly culled. Intended behavior. | NONE |
| **Weight drift** | Weights renormalized every calibration cycle (15 min). Rounding to 6 decimal places on save, renormalized on load. No cumulative drift possible. | NONE |
| **Counter overflow** | `msg_counts` values reach ~8.6M at 100 msg/s. Python ints are arbitrary precision. No overflow. | NONE |

**Verdict:** The system is stable for 24-hour runs. The primary concern is disk usage from unrotated log files, which becomes significant after ~7 days.

---

## 7. DEPLOYMENT READINESS CHECKLIST

| Item | Status | Notes |
|---|---|---|
| **API bind address configurable** | PASS | `API_HOST` env var, `"127.0.0.1"` default |
| **No hardcoded localhost references** | PASS | `ob_heatmap.py` line 40 has `BINANCE_FUTURES_DEPTH_URL` pointing to `fapi.binance.com` — this is correct (external endpoint) |
| **Atomic writes for all persistent state** | PARTIAL FAIL | `write_json_atomic` is correct. `_save_to_disk` in `active_zone_manager.py` is missing fsync and cleanup (M6). |
| **Graceful shutdown** | PARTIAL FAIL | Works but double-executes cleanup (H5). Logs noise but no data loss. |
| **Thread safety for shared state** | PARTIAL FAIL | Display reads bypass lock (H1). Projection dict race (C1). Zone persistence race (H2). |
| **Input validation on all API endpoints** | PARTIAL FAIL | OB endpoints validated (S1-C5). `side` not validated (L1). Float params enable cache pollution (H4). |
| **Error responses don't leak internals** | FAIL | `/orderbook_heatmap_stats` and `/orderbook_heatmap_debug` leak filesystem paths (M10). |
| **Log rotation during runtime** | FAIL | All rotation is startup-only (H3, M5). |
| **Timezone handling** | PASS | All timestamps use `time.time()` (UTC epoch). No local timezone assumptions found. |
| **Filesystem path assumptions** | PASS | Uses `os.path.join`, `os.replace`, forward slashes. No Windows-specific paths. |
| **Environment variable documentation** | PARTIAL FAIL | `API_HOST` is documented in CLAUDE.md. No other env vars found. But there's no `.env.example` or startup validation. |
| **Network failure graceful degradation** | PASS | WS reconnect with exponential backoff. OI poller skips on all-exchange failure. REST poller has backoff. |
| **Disk-full behavior** | PARTIAL FAIL | `write_json_atomic` logs error and continues. `_save_to_disk` logs error and continues. But log file writes have bare `except: pass` (M7) — disk-full condition is invisible for debug logs. |
| **Process restart recovery** | PASS | Zones load from disk. Calibrator weights load from disk. History buffers load from binary files. All missing-file cases handled. |

---

## Dedup Notes — Sprint 1/2 Areas Verified as Resolved

The following Sprint 1/2 fix areas were verified in code and confirmed as properly resolved. None of these are re-reported:

- **S1-C1** BTC depth fallback — verified at `_check_minute_rollover` lines 1430-1457
- **S1-C2** Atomic write logging — verified at `write_json_atomic` line 202
- **S1-C5** OB input validation — verified at `/orderbook_heatmap` lines 948-951
- **S1-H1** History load error logging — verified in `LiquidationHeatmapBuffer._load_from_disk`
- **S1-H3** OI snapshot lock — verified at `oi_poller.py` lines 76, 192, 237
- **S1-H5** `/liq_stats` deepcopy — verified at `embedded_api.py` line 895
- **S2-C4** Calibrator event drop WARNING — verified in `_process_event`
- **S2-H2** WS exponential backoff — verified in all 3 connectors with reset on success
- **S2-H4** `/oi` 503 response — verified at `embedded_api.py`
- **S2-H6** Thread stop with join — verified at `oi_poller.py` line 279 and `rest_pollers.py` line 566
- **S2-H7** OB index-based grid — verified at `ob_heatmap.py` line 702
- **S2-H9** API state shutdown — verified at `full_metrics_viewer.py`
- **S2-M2** OI all-fail skip — verified at `oi_poller.py` lines 184-187
- **S2-M8** API bind address — verified with `API_HOST` env var
- **SOL FP grid fix** — verified index-based construction across all 8 files
- **OI base-to-USD conversion** — verified in `entry_inference.py` line 330
- **Independent normalization** — verified `_p99_scale` in `get_combined_heatmap`
- **Entropy false positive fix** — verified enabled-tier-only entropy in `_run_calibration`
- **Disabled tier weight drift fix** — verified `_enforce_disabled_tiers` in 3 documented locations
- **Bybit side convention fix** — verified `Buy→short`, `Sell→long` in normalizer
