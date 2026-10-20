# Enabling batch slots in the front end — investigation and plan

**Goal.** Serve 2–4 concurrent conversations on the 3090 so that aggregate throughput passes the ~85 tok/s
single-stream ceiling measured in `PLAN-dual-gpu.md` (G6). Target: ≥110 tok/s aggregate with two slots at 262K.

**Status of the engine half: already done upstream.** The engine ships a complete batch implementation and protocol.
What is missing is the fork's *front end*: it serialises every request behind one mutex and blocks `--batch`.

---

## 1. What the engine already provides

| Piece | Where | State |
| --- | --- | --- |
| `--batch N` / `--slots N` (2..8) | `generate.cpp:670`, option at `1477` | present, opt-in, `--serve` only |
| Slot sessions carved per slot | `generate.cpp:3310-3325` | present (each slot gets its own session/KV; capped by VRAM) |
| Batch protocol: `BGEN <slot> <max_new> <ids>` → `BT <slot> <id>` … `BDONE <slot> <produced> <finish> <ms>` | `generate.cpp:488-493`, `6465`, `6472` | present |
| Per-slot cancel: `BSTOP <slot>` | `generate.cpp:6600+` | present |
| `--batch-mtp`: one MTP drafter per slot, sharing weights (+50 MiB each, 35–110 cache slots) | docs/BATCHING.md | present, opt-in |
| `--batch-groups G` (layer split pipelining only) | option at `1480` | present; irrelevant on one GPU |
| Conversation parking (`--conversation-cache-*`) | `generate.cpp:659`, 5623-5698 | present, independent of batch |
| Prompt read is serial, decode is batched | `generate.cpp:6688-6700` (`BGEN` reads the prompt through the usual path, then continues in the window) | present — **this shapes the front end design** |

**Upstream's own measurements** (docs/BATCHING.md): +31% to +39% total throughput with 2–4 clients on one GPU
(RTX PRO 5000); on a 2×A4000 layer split, `--batch-mtp` +15.4% against serial windows at 2 slots, with the
two slot drafters costing 35–110 expert-cache slots. One stream alone decodes the same with or without it.

So the expected shape on this rig: two streams at roughly the same per-stream latency as one, ~1.3–1.4× aggregate.

---

## 2. What the fork is missing (gap analysis)

| Gap | Evidence | Consequence |
| --- | --- | --- |
| Every request holds `Engine::mu` for its whole lifetime | `frontend/engine.hpp:159` — the comment literally says *"one request at a time: the engine holds one conversation"* | no concurrency at all today; `BGEN` cannot be driven |
| `Engine::generate()` is synchronous: writes `GEN`, reads lines until `DONE` | `frontend/engine.hpp:70-127` | the client has no line demultiplexer; `BT <slot>` lines from other slots would be read as this request's tokens |
| `--batch` is on the front-end block list | `frontend/main.cpp:640` (block list) | unreachable from the front end |
| No slot allocator, no queue, no fairness | — | needs writing |
| `PP` (prompt progress) and `ERR` lines are **not slot-tagged** | `generate.cpp:5789`, protocol doc `4766-4774` | the scheduler must know which request is in its prompt phase, and admit prompts one at a time |
| `/health` reports engine-wide counters only | `STATS` is global | per-slot observability needs a front-end-side table |
| Streaming path assumes one in-flight request | `frontend/main.cpp:425-460` (mutex held through the callback) | streaming must be per-slot, dispatched from the demuxer |

---

## 3. Elements, scale and risk

Scale is engineering effort for one person familiar with the codebase. Risk is the chance the element forces a
redesign or fails outright.

| # | Element | Scale | Risk | Notes and mitigation |
| --- | --- | --- | --- | --- |
| B1 | Unblock `--batch`/`--slots`/`--batch-mtp` in the front end; forward to the engine; validate `2..min(8, fitted)` against the VRAM plan | **0.5 d** | **Low** | Same pattern as the peer flags (Phase 0). Reuse the existing "engine said N slots fit" log line to cap. |
| B2 | Async protocol client: request struct with slot, line demuxer (`BT`/`BDONE`/`PP`/`ERR`/`DONE`), `BGEN`/`BSTOP` writers | **2–3 d** | **High** | This is the core. The line reader is shared: one reader thread fans lines out by slot. Non-slot-tagged lines (`PP`, `ERR`, `REUSED`) bind to the single request currently in its prompt phase — so the scheduler must guarantee at most one prompting request at a time. Needs careful shutdown/EOF semantics and the existing `child_watch` integration. |
| B3 | Scheduler: slot pool, admission queue, two phases (prompting → batched decode), fairness/backpressure | **2 d** | **High** | Prompts serialise; decodes batch. A long prompt blocks admission of others (upstream has the same property). Mitigations: first-come-first-served admission, bounded queue, reject with 429 past the limit, and keep the single-stream fast path when only one request is active. |
| B4 | Per-slot streaming: interleaved `BT` deltas, per-request chat parser, per-request cancellation (`BSTOP`), drain-on-client-disconnect | **1.5–2 d** | **Medium** | The chat parser is already per-request (`StreamParse`), so this is plumbing — but cancellation currently assumes "the" request (STOP). Must become per-slot or it will kill the wrong stream. |
| B5 | VRAM/memory budget: per-slot session + KV, batch buffers, optional per-slot drafter | **0.5 d + measurement** | **Low–Medium** | At 262K `--kv q4_0` a slot's KV is ≈1.28 GiB ⇒ ~740 fewer expert slots (of 9,416, −8%). `--batch-mtp` adds ~50 MiB + 35–110 cache slots per slot. Expect slots to fall to ~8,000 at 2 slots. Must log the derived cap and refuse to start if the plan does not fit. |
| B6 | Peer tier × batch (this rig's actual deployment) | **2–3 d** | **High (unknown)** | **Upstream never measured `--batch` with `--peer-device`.** Interactions to validate: peer promotion/adapt rounds across batch windows; `peer_ms_wait` under multi-token windows; `--peer-prefill-rows` with serial prompts; `VRAM` command already refuses while batch decodes. Fallback is well-defined: batch without the peer, or peer without batch, both already work. |
| B7 | Conversation parking × batch (multi-turn agentic use) | **1–2 d** | **Medium** | Parking is per-conversation and RAM-budgeted; with slots it must be keyed per slot. Optional for a first release (parking is off by default). |
| B8 | Observability: per-slot counters in `/health` (tokens, tok/s, acceptance, queue depth, wait time) | **1 d** | **Low** | Front-end-side table; the engine's `BDONE` line already carries produced/finish/ms. |
| B9 | Tests: fake-engine protocol tests (no model), scheduler unit tests, cancellation, and a load harness | **2 d** | **Medium** | `make test-frontend` exists and runs without CUDA/model — extend it with a fake engine that emits interleaved `BT`/`BDONE`. Then a rig load test: N clients × 300 tokens, aggregate tok/s and per-stream latency. |
| B10 | Docs: `docs/BATCHING-FRONTEND.md` or a section in the fork README + `--parallel` front-end option | **0.5 d** | **Low** | Mirror upstream's `"parallel": N` naming so configs transfer. |

**Totals:** ~12–17 working days for a solid first release (B1–B5, B8–B10 ≈ 9–11 d; peer × batch validation B6
another 2–3 d; parking B7 optional).

---

## 4. Expected benefit on this rig

| Scenario | Aggregate tok/s (est.) | Basis |
| --- | --- | --- |
| 1 stream, today | 85 | measured (G6) |
| 2 streams, batch, no peer | ~110–120 | upstream +31–39% on one GPU; our expert slots drop ~8% |
| 2 streams, batch + `--batch-mtp` | ~115–125 | upstream's batch-mtp adds +15% at 2 slots |
| 2 streams, batch + peer | unknown | B6; the peer frees CPU-pool capacity, which batch also consumes |
| 4 streams | ~140–160 (geometry permitting) | 4 slots cost ~5 GiB of KV at 262K; expect ~7,000 expert slots |

Per-stream latency will rise slightly (a window carries every slot's token), which is the expected trade for
aggregate throughput.

---

## 5. Gates

| Gate | Check |
| --- | --- |
| B-G1 | Two concurrent requests both complete, both streams stream correctly, no cross-talk between parsers |
| B-G2 | Aggregate throughput ≥1.25× single-stream at two clients (target ≥110 tok/s) |
| B-G3 | `BSTOP` cancels exactly one slot; the other stream is unharmed |
| B-G4 | A single client alone decodes at the same speed as before batching (no regression when idle) |
| B-G5 | VRAM plan refuses to start when `--batch N` does not fit; logs the derived slot count |
| B-G6 | Peer × batch: either passes the same gates with `--peer-device 1`, or is documented as unsupported with a clean refusal |

---

## 6. Open questions

1. **Peer × batch is unmeasured upstream** — the largest unknown. Cheap probe before committing: enable the
   engine's `--batch 2` directly (bypassing the front end, using `BGEN` over stdin) and run two scripted
   conversations against the pipe. That answers B6 in an afternoon without writing any front-end code.
2. **Prompt admission serialisation** with long agentic prompts (100K+) may dominate: a second client waits for the
   first prompt to finish reading. Upstream's pipelined groups help only with a layer split. If it matters, a
   "prompt-first" scheduler policy (admit prompts ahead of decode) is a small follow-up.
3. **CPU pool under batch**: our pool is AVX-2 limited (no VBMI on the 10900X). Batch windows route more expert
   rows per window (deduplicated across slots), so the pool's per-window work grows — this is the element most
   likely to erode the expected gain on *this* CPU, and it is measurable in the same probe as (1).
