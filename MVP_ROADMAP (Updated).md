# TradeNet Quantum Terminal — MVP Roadmap
## Updated: Mar 2, 2026

---

## GOAL
260 people waiting. $13/mo. $36/mo infrastructure. Ship a professional terminal with predictive liquidation heatmaps nobody else offers.

---

## COMPLETED — 56 tasks + 6 bug fixes

### Phase 1: Backend Foundation ✅ (9 tasks)
Core engine architecture, EngineManager, per-symbol calibrator + heatmap + zone manager pipeline, embedded API server, snapshot pipeline, atomic writes.

### Phase 2: Multi-Exchange Aggregation ✅ (8 tasks)
Binance, Bybit, OKX WebSocket connectors. Liquidation normalizer (`liq_normalizer.py`). Multi-exchange OI poller. Per-symbol routing. Dynamic liquidation routing.

### Phase 2b: ETH/SOL Engine Parity ✅ (5 tasks)
Per-symbol engines for ETH/SOL. Per-symbol snapshot files, history buffers, binary files, log files. API serving per-symbol with validation. OB endpoints reject non-BTC.

### Phase 3 (partial): Frontend Aggregation ✅ (4 tasks + 1 bug fix)
AGGREGATED tickers in screener. Binance klines as reference for aggregated pairs. aggr-server WebSocket for trade data.

### Bug Fixes ✅ (6 resolved)
SOL FP grid (floating-point drift), BTC OI unit mismatch (base→USD conversion), tape/inference normalization (independent p99 scaling), calibrator entropy false positive (disabled tier exclusion), disabled tier weight drift (enforce after normalization), frontend live update stream fix.

### Phase A: Heatmap Quality ✅ (6 tasks)

| # | Task | Status | Notes |
|---|------|--------|-------|
| A1 | Partial sweep reduction | ✅ | `SWEEP_REDUCTION_FACTOR=0.70`, `SWEEP_MIN_BUCKET_USD=100` |
| A2 | OI reduction cap | ✅ | `MAX_OI_REDUCTION_PER_MINUTE=0.25` |
| A3a | Combined-source bucket-level boost | ✅ vestigial | Deployed, never fires — $430 structural gap between tape/inference buckets. Dead code kept for now. |
| A3a-diag | Near-overlap diagnostic logging | ✅ | Confirmed bucket-level wrong abstraction. $430 avg, 21.5 steps apart. |
| A3a-v2 | Combined-source CLUSTER-level boost | ✅ VALIDATED | Validated during Feb 28 Iran strike event. 2,623 force orders captured. `tape_nonzero_buckets: 72`, `exact_overlap_buckets: 47-55`, `cluster_combined: 2/2 (100%)`. Long liq cluster $59,840–$63,560, short liq cluster $63,920–$69,400. Bucket-level boost avg 50% intensity increase on boosted buckets. |
| A3a-v2 diag | Cluster-level diagnostic fields | ✅ | cluster_total, cluster_combined, cluster_combined_pct, example_boosted_clusters logged every 10 min. |

### Codebase Audit — Sprint 1: Production Safety ✅ (8 fixes)

| # | Fix | Status |
|---|-----|--------|
| C1 | BTC depth fallback (markPrice when OB stale >60s) | ✅ |
| C2 | `write_json_atomic()` error logging (no more bare except:pass) | ✅ |
| C3 | Shutdown cleanup (`heatmap_v2.close()` for all engines) | ✅ |
| C5 | OB endpoint `step` validation (400 on step≤0) | ✅ |
| H1 | History deserialization logging (corrupt frames counted) | ✅ |
| H3 | OI poller thread lock (`_snapshot_lock`) | ✅ |
| H5 | `/liq_stats` cache mutation fix (`deepcopy`) | ✅ |
| H2-lite | WebSocket error logging (9 except blocks, all 3 connectors) | ✅ |

### Codebase Audit — Sprint 2: Operational Visibility ✅ (9 fixes)

| # | Fix | Status |
|---|-----|--------|
| C4 | Calibrator event drop warning (rate-limited) | ✅ |
| H2-full | WebSocket exponential backoff (1s→30s, jitter, reset on success) | ✅ |
| H4 | `/oi` warmup returns 503 (not 404) | ✅ |
| H6 | Thread join on shutdown (OI poller + REST poller) | ✅ |
| H7 | OB heatmap grid fix (Rule #16, index-based) | ✅ |
| H9 | API refresh thread joined on shutdown | ✅ |
| M1 | EntryInference projection lock (copy-on-read) | ✅ (incomplete — see Sprint 3.5 C1) |
| M2 | OI poller skip-on-total-failure (no stale 0.0) | ✅ |
| M8 | API bind address configurable (`API_HOST` env var) | ✅ |

**Note:** Sprint 2's H2-full backoff has a behavioral weakness — resets on TCP connect instead of proven data flow. Tracked as Sprint 3.5 H-Backoff.

---

## CURRENT ENGINE STATE (verified Mar 2, 2026)

- **Backend:** 3 exchanges (Binance/Bybit/OKX), 3 symbols (BTC/ETH/SOL), real per-symbol liq heatmaps
- **OI:** 3-exchange aggregation (Binance + Bybit + OKX per symbol)
- **OB heatmap:** Binance only (not aggregated). Multi-exchange OB is Phase F (post-launch).
- **Calibrator:** healthy, event drop warning added
- **Heatmap:** tape + inference producing data, independent normalization working. **2-day soak confirms production-quality visual output** — distinct intensity bands, clear zone structure, on par with Coinglass. History accumulating properly with decay working as designed.
- **A3a-v2:** VALIDATED during live geopolitical event (US/Israel strike Iran, Feb 28 2026). 2,623 force orders captured. Cluster boost firing at 100% cluster match rate with 47-55 exact bucket overlaps.
- **Zone Manager:** zones creating, reinforcing, sweeping, expiring normally
- **Audit status:** 17/29 first-pass findings fixed. 16 second-pass findings identified. Sprint 3.5 Tier 1 prompt sent to Claude Code (in progress).
- **Display/logic boundary:** verified clean by 5 independent auditors (1 Claude Code + 4 ChatGPT Codex)
- **Known limitation:** Notional USD values undercount real exposure by ~5-10x (only captures runtime OI deltas, not pre-existing inventory). Phase E calibration planned.

---

## WHAT'S LEFT — Ordered by Launch Impact

---

## Audit: Sprint 3.5 — Pre-Deployment Hardening (from second-pass audit)

### Tier 1 — Must fix before deployment (4 fixes) ⏳ IN PROGRESS

| # | ID | Finding | Fix | Status |
|---|-----|---------|-----|--------|
| 1 | C1 | Projection lock incomplete (Sprint 2 regression). Live dict keys read outside lock. `_estimate_entry_from_projection()` bypasses lock entirely. No nested locks. | Use `proj_long.keys()` from copy. Add lock to `_estimate_entry_from_projection`. No nested lock acquisition. | ⏳ Prompt sent |
| 2 | C2 | Unbounded grid allocation DoS. Two independent caps needed. | Request-side: `MAX_GRID_BUCKETS=10000`, reject if exceeded. Validate `price_max > price_min`. Disk-side: cap `n_buckets` from header, validate remaining bytes vs `struct.calcsize()`. | ⏳ Prompt sent |
| 3 | H-WS | Malformed WS payload tears down stream. Only catches JSONDecodeError. | Wrap callback in `except Exception` per-message, log and continue. Do NOT touch backoff counter on per-message errors. | ⏳ Prompt sent |
| 4 | H-Cache | Response cache unbounded cardinality. ~300MB under sustained attack. | `MAX_CACHE_ENTRIES=500` LRU. Key normalization: symbol `strip().upper()[:10]`, floats `round(x, 2)`, side validation at endpoint. Hash keys >200 chars via md5. Reject empty/invalid symbols with 400 at endpoint before cache. | ⏳ Prompt sent |

### Tier 2 — Should fix before deployment (6 fixes) ⬜ NEXT

| # | ID | Finding | Fix |
|---|-----|---------|-----|
| 5 | H-Display | Display thread reads live processor dicts without lock | Copy attrs into `get_state()` or acquire lock in `create_display` |
| 6 | H-Zone | Zone persistence serializes shared reference outside lock. Missing fsync. | `dict(zone.tier_contributions)` inside lock, add fsync + cleanup |
| 7 | H-Backoff | WS backoff resets on connect, not on proven data flow | Move reset to after first valid message received |
| 8 | H-Shutdown | Double shutdown executes cleanup twice | Add `_shutdown_in_progress` flag |
| 9 | M-Side | Unknown liquidation side defaults to short | Validate explicitly, drop/log invalid |
| 10 | M-Calibrator | `on_minute_snapshot` overwrites `current_weights` without `_enforce_disabled_tiers()` | Add `_enforce_disabled_tiers()` after line 895 |

### Tier 3 — Fix before beta (6 fixes) ⬜ LATER

| # | ID | Finding | Fix |
|---|-----|---------|-----|
| 11 | M-Tape | Tape event deque maxlen collapses during 500+/min bursts | Time-based pruning or larger adaptive cap |
| 12 | M-Logs | 15 JSONL log files grow unbounded (~1 GB/month) | Periodic size check or RotatingFileHandler |
| 13 | M-Events | Calibrator `events_window` deque has no maxlen | Add `maxlen=10000` |
| 14 | M-Disabled | Disabled tiers participate in soft attribution normalization | Exclude DISABLED_TIERS from denominator |
| 15 | M-Paths | Debug endpoints leak filesystem paths | Sanitize error messages |
| 16 | M-src0 | Calibrator accepts `src=0`, produces garbage | Add `if src <= 0: return` |

---

## Audit: Sprint 3 — Code Quality (before beta)

| # | ID | Finding | Fix |
|---|-----|---------|-----|
| 1 | M4 | Factory default ETH step mismatch (2.0 vs 1.0) | Align constant |
| 2 | M5 | Magic `weight * 100000` in V3 zone heatmap | Extract to named constant |
| 3 | M7 | Calibrator tolerance docs say 5, code enforces 4 | Align CLAUDE.md |
| 4 | M9 | `active_zone_manager.__del__` bare except | Add logging |
| 5 | M10 | `ob_heatmap.py` unconditional `print()` | Replace with logger |
| 6 | M11 | Silent exception swallowing in 4+ locations | Add logging |
| 7 | M12 | `DEFAULT_STEP` constant shadowing | Rename or remove import |
| 8 | M13 | Binary header comments say 48 bytes, struct is 44 | Fix comments |
| 9 | D1-D6 | CLAUDE.md updates (6 items) | Batch update |
| 10 | L1-L4 | Dead code, unused imports, orphaned files | Batch cleanup |

**Note:** M6 (cache key normalization) now covered by Sprint 3.5 H-Cache fix.

---

## PHASE A (remaining): Heatmap Quality

| # | Task | Status | Notes |
|---|------|--------|-------|
| A5 | Verification soak test | ⬜ SOFT PASS | Feb 28 Iran event + 2-day continuous run served as de facto stress test. 2,623 force orders during event, heatmap held up. 2-day soak shows production-quality output on par with Coinglass. Formal soak criteria still worth verifying quantitatively under normal conditions. |
| A4 | TAU_PCT_BASE tuning | ⬜ CONDITIONAL | Only if soak test reveals issues |

**A5 acceptance criteria (hard gate — no endless tuning):**
- Median combined-zone lifespan > 20 minutes
- Combined-zone swept rate > 40%
- < 30% of strong zones (>$50k notional) disappearing before first price touch
- Visible strong zones survive minor wicks
- Subjectively: "variable but sticky," not "bands chasing price"

---

## PHASE B: Visual Polish — Ship Quality

**Launch blockers:**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B1 | Colored cell backgrounds on footprint | Single biggest visual upgrade — professional footprint chart | Frontend |
| B4 | Fix CVD + OI CVD accumulation bugs | Data correctness — CVD drift, OI CVD midnight reset | Frontend |
| B5 | Fix bar stats timeframe mismatch | Data correctness — switching timeframes shows stale data | Frontend |
| B6 | Connect liq data to bar stats Short Liq / Long Liq rows | Currently zeros — backend has data, frontend needs to consume | Frontend + Backend |
| B7 | Fix multi-pane heatmap bug | Only one pane displays heatmap at a time | Frontend |
| B8 | Normalization visibility floor | Strong zones (notional > threshold) must always render at minimum 10-15% intensity regardless of p99 scaling. Prevents extreme events from crushing weaker zones off screen. Discovered during Feb 28 event. | Backend display wrapper |
| B9 | Swept zone visual persistence | Swept zones dim to 30% or shift color instead of vanishing instantly. Shows the zone was correct. | Frontend rendering |

**Nice-to-have (cut if time tight):**

| # | Task | Impact | Repo |
|---|------|--------|------|
| B2 | POC row highlighting | Professional standard | Frontend |
| B3 | K/M number formatting | Readability | Frontend |

---

## PHASE C: Deployment

| # | Task | Notes |
|---|------|-------|
| C1 | Configurable API base URL (frontend) | Hardcoded `127.0.0.1:8899` in `liq_heatmap.rs:15` and `orderbook_heatmap.rs:22` |
| C2 | Deploy backend to DigitalOcean Droplet 2 | No geo-blocking on droplet. `API_HOST=0.0.0.0` (M8 done). |
| C3 | nginx + SSL on backend droplet | Reverse proxy, TLS, rate limiting. Rate limiting alone neutralizes many second-pass audit findings. |
| C3.5 | Route Bybit REST through proxy | Bybit geo-blocked in US. Frontend needs proxy route. Backend on droplet unaffected. |
| C4 | Frontend: build release binary (.exe) | `cargo build --release` |
| C5 | Health checks + monitoring | `/health` exists. Need external uptime check + crash alert. |
| C6 | End-to-end test: frontend → remote backend | All overlays, OI, zones over network |
| C7 | Private beta: 3-5 trusted users | Real users, real feedback, real load |

---

## PHASE D: Brand & First Impression (parallel with B/C)

| # | Task | Notes |
|---|------|-------|
| D1 | TradeNet branding (logo, about) | Logo in header, about screen |
| D2 | Login screen polish | Functional but basic |
| D3 | Color scheme refinement | Dark theme needs professional polish |
| D4 | Sidebar cleanup | Remove dev artifacts |
| D5 | Toolbar/header cleanup | Remove debug elements |

---

## LAUNCH 🚀

---

## TASK COUNT

| Phase | Total | Done | Remaining |
|-------|-------|------|-----------|
| Phase 1: Backend Foundation | 9 | 9 | 0 |
| Phase 2: Multi-Exchange | 8 | 8 | 0 |
| Phase 2b: ETH/SOL Parity | 5 | 5 | 0 |
| Phase 3 (partial): Frontend Aggr | 5 | 5 | 0 |
| Bug Fixes | 6 | 6 | 0 |
| Phase A: Heatmap Quality | 8 | 6 | 2 (A5 soft pass + A4 conditional) |
| Audit Sprint 1 | 8 | 8 | 0 |
| Audit Sprint 2 | 9 | 9 | 0 |
| Audit Sprint 3 | 10 | 0 | 10 |
| Audit Sprint 3.5 Tier 1 | 4 | 0 | 4 (in progress) |
| Audit Sprint 3.5 Tier 2 | 6 | 0 | 6 |
| Audit Sprint 3.5 Tier 3 | 6 | 0 | 6 |
| Phase B: Visual Polish | 9 | 0 | 9 (7 blockers + 2 nice-to-have) |
| Phase C: Deployment | 8 | 0 | 8 |
| Phase D: Brand | 5 | 0 | 5 |
| **TOTAL** | **106** | **56** | **50** |

**Critical path to launch (blockers only):**

| Phase | Blocker Tasks |
|-------|---------------|
| Sprint 3.5 Tier 1 | 4 (in progress) |
| Sprint 3.5 Tier 2 | 6 |
| Phase B blockers | 7 |
| Phase C | 8 |
| Phase D | 5 |
| **Total blockers to launch** | **30** |

**Progress: 56 tasks complete. ~30 blocker tasks to launch.**

---

## DEPLOYMENT READINESS (current state)

| Item | Status | Blocks |
|------|--------|--------|
| API bind configurable | ✅ PASS | — |
| Query parameter hardening | ❌ FAIL | Sprint 3.5 C2, H-Cache |
| WS malformed payload resilience | ❌ FAIL | Sprint 3.5 H-WS |
| Thread safety (all shared state) | ❌ FAIL | Sprint 3.5 C1, H-Display, H-Zone |
| Graceful degradation (exchange failure) | ✅ PASS | — |
| Shutdown behavior | ⚠️ PARTIAL | Sprint 3.5 H-Shutdown |
| 24h memory stability | ⚠️ PARTIAL | H-Cache under attack |
| Log rotation during runtime | ❌ FAIL | Sprint 3.5 M-Logs |
| Timezone handling | ✅ PASS | — |
| Filesystem cross-platform | ✅ PASS | — |
| Process restart recovery | ✅ PASS | — |

**After Sprint 3.5 Tier 1+2: deployment-ready for trusted beta behind nginx.**
**After Sprint 3.5 Tier 3 + Sprint 3: deployment-ready for public exposure.**

---

## DISPLAY vs LOGIC BOUNDARY (architectural invariant)

Verified clean by 5 independent auditors (1 Claude Code + 4 ChatGPT Codex).

**Raw (logic/calibrator/zones):**
- `get_combined_heatmap()` → zone logic, calibrator input, diagnostic logging
- `get_heatmap()` → calibrator `on_minute_snapshot` (line ~1741), `EngineManager.get_snapshot()`
- `get_api_response()` → called only by display wrapper internally

**Display (snapshots/UI only):**
- `get_combined_heatmap_display()` → bucket-level boost (vestigial, never fires)
- `get_heatmap_display()` → V1/V2 snapshot intensity arrays
- `get_api_response_display()` → V2 snapshot + cluster-level boost (A3a-v2)

**Rule: future logic consumers MUST call raw versions. NEVER the display wrappers.**
**Rule: NEVER read self.projected_*_liqs outside _projection_lock — always use the locked copy.**

---

## KNOWN ISSUES (tracked, not blocking launch)

| Issue | Severity | Notes |
|-------|----------|-------|
| Bybit geo-blocked without VPN | DEV ONLY | Droplet has no geo-restrictions. Germany VPN for local dev. |
| Proxy latency (~1.2s/kline) | LOW | Frontend kline loading slow through proxy. Direct Binance 399ms. Phase C. |
| OB heatmap Binance-only | KNOWN | Not aggregated. Multi-exchange OB is Phase F (post-launch). |
| depth_band missing for ETH/SOL | DEFERRED | Needs per-symbol OB engines. Not MVP. |
| Hyperliquid excluded | DEFERRED | No public liquidation WebSocket stream. |
| 10x leverage tier disabled | KNOWN | -$62K median miss. Excluded from calibration. Monitor post-launch. |
| Agent signals disabled | DEFERRED | Re-enable post-launch. |
| A3a bucket-level boost vestigial | COSMETIC | Never fires. Dead code cleanup in Sprint 3. |
| Normalization crushes weaker zones during extreme events | KNOWN | B8 fix planned. Discovered during Feb 28 Iran strike event. |
| Swept zones vanish before trader can see confirmation | KNOWN | B9 fix planned. Sweep reduction + decay erases zones as price confirms them. |
| Sprint 2 backoff resets on connect not data flow | KNOWN | Sprint 3.5 H-Backoff fix planned. Behavioral weakness, not regression. |
| Notional USD values undercount real exposure (~5-10x) | KNOWN | Engine only captures OI deltas during runtime, missing pre-existing position inventory. Also missing exchanges beyond Binance/Bybit/OKX (Bitget, Gate, MEXC, etc). Tooltip shows ~$500K where real exposure is likely $3-20M. Phase E calibration task: compare displayed notional vs actual force order volume when zones sweep, derive correction factor. |

---

## MILESTONES & VALIDATION EVENTS

| Date | Event |
|------|-------|
| Feb 27, 2026 | Sprint 1 (8 fixes) + Sprint 2 (9 fixes) shipped. A3a-v2 deployed, first cluster detected. |
| Feb 28, 2026 | **A3a-v2 VALIDATED.** US/Israel strike Iran (Operation Shield of Judah). Engine captured 2,623 force orders. Heatmap showed exact liq structure — long cluster $59.8K–$63.5K, short cluster $63.9K–$69.4K. Side-by-side comparison with competitor showed massive quality gap. Second-pass audit (5 agents) completed, Sprint 3.5 plan created, Tier 1 prompt sent. |
| Mar 2, 2026 | **2-day soak confirmed.** Liq heatmap running continuously since Feb 28. Visual output on par with Coinglass — distinct intensity bands, proper decay, readable zone structure. Notional USD undercounting (~5-10x) identified and tracked for Phase E calibration. |

---

## POST-LAUNCH

**Phase E:** Feature depth — shared data fetching, data service layer, OB noise filter, OHLC bar, candle spacing, volume profile labels, bar stats colors, bid/ask flip, DoM/T&S fixes. **Notional USD calibration** — compare predicted zone notional vs actual force order volume on sweep events, derive per-symbol correction factor to scale displayed USD values closer to real market exposure.

**Phase F:** Aggregated OB heatmap for ETH/SOL — per-symbol orderbook engines, multi-exchange OB.

**Phase G:** ML/AI integration — regime classification → cascade probability → smart alerts.

**Phase H:** Competitive features — stacked imbalances, OI candles, liq bubbles, absorption, synced cursors, TPO, VWAP, alerts, multi-layout.

---

## ARCHITECTURE QUICK REFERENCE

**Backend** (`moonstreamprocess`): Python, FastAPI. Entry: `poc/full_metrics_viewer.py --api`. Port 8899.
- EngineManager → per-symbol EngineInstance (BTC, ETH, SOL)
- Each: LiquidationCalibrator + LiquidationHeatmap (LiquidationTape + EntryInference) + ActiveZoneManager
- BTC additionally: OrderbookReconstructor + OrderbookAccumulator (Binance only)
- 3 exchanges: Binance, Bybit, OKX. Normalized via `liq_normalizer.py`
- OI polled every 15s from 3 exchanges, aggregated per symbol
- API_HOST env var for bind address (default 127.0.0.1, set 0.0.0.0 for production)

**Frontend** (`tradenet-terminal`): Rust, Iced 0.13+. Cargo workspace.
- Canvas-based rendering, GPU-accelerated via wgpu
- Liq heatmap: 1s live + 10s history poll to backend API
- OB heatmap: 30s live + 60s history poll
- Klines/trades: Binance via proxy.tradenet.org
- Aggregated tickers: AGGREGATED:BTCUSDT, AGGREGATED:ETHUSDT, AGGREGATED:SOLUSDT

**Infrastructure:** $36/mo. 3 subscribers covers it. 260 waiting.

**Commands:**
- Start backend: `cd poc && python full_metrics_viewer.py --api`
- Start frontend: `cargo run` (dev) or `cargo build --release` (prod)
- Check health: `curl http://127.0.0.1:8899/health`
- Check boost: `Select-String -Path "poc\liq_debug_BTC.jsonl" -Pattern "combined_zone_boost" | Select-Object -Last 1`
- Check force orders: `(Invoke-RestMethod "http://127.0.0.1:8899/liq_stats?symbol=BTC").total_force_orders`
