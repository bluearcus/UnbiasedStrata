# Batching on 0.1.45: scope, routes, and what would decide it

**Where we stand.** Single-stream is ~98 tok/s (XXS) and ~89 (S) at 262K, peer on, promotion off. Batching is the
only remaining throughput lever, and unlike the 0.1.39 branch the **engine now supports it fully**.

## 1. Engine side: complete

| Piece | Where | State |
| --- | --- | --- |
| `--batch N` / `--slots N` (2..8) | `generate.cpp` option + help | present |
| `--batch-mtp` (a slot also verifies one MTP proposal per window) | option at `2473`, planning at `5024` | **present** (this is what 0.1.39 lacked) |
| `BGEN <slot> <max_new> <ids>` / `BGENI` | protocol dispatch | present |
| `BT <slot> <id>`, `BDONE <slot> <produced> <finish> <ms>` | per-slot output | present |
| `BSTOP <slot>`, `BYIELD` | per-slot cancel / yield | present |
| Per-slot session (own KV) | carved at start; logs "N slot sessions on CUDA0 (X GiB each)" | present |

## 2. Front end: nothing, and here is exactly what is missing

The 0.1.45 front end mirrors llama.cpp's server but keeps **one** slot:

| Gap | Evidence | Consequence |
| --- | --- | --- |
| One `Slot` instance (not an array) | `frontend/strata_context.cpp:48` `struct Slot` + a single `slot` member; `:243` reports `n_processing_slots = 1` | one request at a time |
| No `BT` / `BDONE` line kinds | `frontend/engine.hpp:55` `EngineLine::Kind` has TOKEN/PP/RESUME/REUSED/DONE/ERR/INFO/OTHER; `classify()` at `:347` maps `BT`/`BDONE` to OTHER | the protocol client cannot demux slots |
| Writes `GEN`, reads `T`/`DONE` | request path | single stream, global `STOP` for cancel |
| No `--parallel` / `-np` option | zero matches in `frontend/main.cpp` | nothing to size the slot pool with |

## 3. Two routes

**Route A - add slots to upstream's front end (~4-6 days).** Because `Slot` already mirrors llama.cpp's
`server_slot`, the work is structural rather than inventive: `std::vector<Slot>`, an admission loop (the engine
reads prompts serially, so admit when a slot is free), `BT`/`BDONE` dispatch by slot id, `BSTOP <slot>` for
cancellation, and `--parallel N` forwarded as the engine's `--batch N`. ~500 lines in one file plus `engine.hpp`.
Cost to the fork strategy: this edits rented upstream code, so it must be `STRATA-FORK`-marked and will conflict
on the next rebase - unless upstream adds it first, which their structure suggests they intend to.

**Route B - our own parallel front end (~1-2 weeks).** Keep `legacy-0.1.39`'s front end (our code, no upstream
surface) and teach it `BGEN`/`BT`/`BDONE`/`BSTOP`. Philosophically right, but the legacy front end is pinned to the
old llama.cpp API, so it needs porting to the current one first - more work than Route A, with no throughput
advantage.

**Recommendation: Route A, behind `--parallel N` (default 1).** Default 1 keeps today's behaviour exactly, the
slot machinery is where upstream put the structure for it, and the alternative costs more for the same result.

## 4. The arithmetic that decides whether it is worth doing at all

- **Upstream's measurements**: `--batch-mtp` at 2 slots is **+15.4%** aggregate against serial windows (2x A4000,
  layer split), and 2-4 clients on one GPU gave **+31% to +39%** total throughput (RTX PRO 5000).
- **Our cost**: each extra slot carves its own session + KV. At 262K the 0.1.39 probe logged **2.25 GiB per slot**;
  with `--kv q4_0` our KV is ~1.28 GiB, so expect ~1.5-2.0 GiB per extra slot. The expert cache is 13.8 GiB now;
  losing 2 GiB is ~1,200 slots (~13% of 9,407), i.e. more CPU-bound work per token.
- **Net expectation**: at 32K, clearly positive (cheap slots, GPU-bound). At **262K it is genuinely uncertain** -
  the gain in window efficiency may be eaten by the smaller expert cache. Measure both; do not assume.

## 5. Validation plan (the harnesses already exist)

1. `--batch 2 --batch-mtp` through the front end, two concurrent clients, 300-token outputs: aggregate and
   per-stream tok/s at 32K and 262K, against the single-stream soak (~98 XXS / ~89 S).
2. Expert cache size and `peer_slots` per configuration (`/health` tiers), to price the VRAM trade.
3. `BSTOP`: cancel one stream, confirm the other is unharmed and the slot is reusable.
4. Single-client-alone regression: must match today's single-stream numbers (`--parallel 1` untouched).
5. Peer x batch: untested upstream. If it misbehaves, batch without the peer (and note the trade: the peer is
   worth +8-11% at 4K/32K, batching maybe +15-39%).

## 6. Verdict

Worth doing **if** aggregate throughput matters more than per-stream latency, and **after** deciding the 262K VRAM
trade with a measurement rather than a guess. The cheapest first step is not the front end at all: drive the
engine's `BGEN` protocol directly from a script with two slots and `--batch-mtp`, and measure at 32K and 262K. That
answers "is there a win here" in an afternoon, before any front-end work.

---

## 7. Measured: batching does not pay here, and the VRAM cost is not the reason

`tools/dual_gpu_bgen_probe.sh` drives the engine's own protocol (no front end, no peer) with the same prompts
single-stream and as two slots with `--batch-mtp`, at both window sizes. The engine confirms the feature engaged:
`--batch: 2 slot sessions on CUDA0`, `--batch-mtp: 2 slot drafters on CUDA0 ... sharing the solo drafter's weights`.

| Window | Mode | slot 0 | slot 1 | **aggregate** | expert slots |
| --- | --- | ---: | ---: | ---: | ---: |
| 32K | single | 94.8 | - | **94.8** | 10,679 |
| 32K | 2 slots + batch-mtp | 33.8 | 51.2 | **67.7** (-29%) | 10,150 |
| 262K | single | 83.6 | - | **83.6** | 9,446 |
| 262K | 2 slots + batch-mtp | 25.0 | 36.8 | **50.0** (-40%) | 6,436 |

Two things this settles:

1. **The 32K row rules out the VRAM explanation.** At 32K a slot session is 0.38 GiB, so the cache barely moved
   (10,679 -> 10,150) - and the aggregate still fell 29%. The loss is intrinsic to batching on this machine, not a
   consequence of a smaller expert cache.
2. **At 262K the VRAM cost is real but secondary**: 2.25 GiB per slot, cache 9,446 -> 6,436 (-32%), on top of the
   same intrinsic loss.

**Why**, consistently with everything else measured here: the CPU expert pool is the tail (the 10900X runs the
AVX-2 kernels), so two slots multiply the tail's work while sharing only the GPU-side work; and each window then
carries two sessions' rows, two drafters' proposals and the staging that goes with them. On a configuration where
the pool is *not* the tail (a quantization small enough for all experts to live in VRAM, or a much faster CPU) the
balance would change - that is the case upstream's +15%/+31-39% numbers describe.

**Decision: no front-end batching work.** The 4-6 day slot implementation would buy a 29-40% *regression* here.
The probe cost an afternoon and is kept as the instrument for the day the configuration changes (Q2_0/IQ2_XS, or a
VBMI-capable CPU).

---

## 8. The implementation already exists — steal it, do not invent it

**PolyStrata (VecSzn/PolyStrata, MIT, HEAD `c54f0ea`) has working batch-slot serving in Python**, and this repo's own
`PLAN.md` records that v1 deliberately dropped it: *"Replicas, CPU affinity, `--batch` slot scheduling (`BGEN`) |
drop in v1. One request at a time, queued. Engine `--batch` stays in the tree, 'present, unmeasured'"*
(`PLAN.md:90`, citing `serve/replicas.py`, `server.py:1572-1900`).

The reference implementation in `serve/server.py` (7,804 lines; ~318 lines touch slots):

| Concern | Reference |
| --- | --- |
| per-slot line routing (`BT `/`BDONE ` by slot id) | `server.py:1013` |
| GEN / BGEN control-line reader with heartbeats | `server.py:1291` |
| BDONE into the request's figures | `server.py:1381`, `1387`, `1401` |
| slot release after a consumer leaves (BSTOP) | `server.py:1406` |
| park / reap orphan slots | `server.py:1449`, `1468` |
| admission (`pick_slot`) and `slots_view` | `server.py:1905`, `1950` |
| slot save/restore actions | `server.py:3613` |
| slot count from the engine | `server.py:738` |

Reference tests to mirror: `serve/test_parallel.py` (877 lines), `test_slots.py` (449), `test_quiet_cancel.py` (226),
`test_slot_alloc.py` (108) — about **1,660 lines of behaviour** that says exactly what the C++ port must do.

**Two routes, in order of preference:**

1. **Port the reference when batching is worth doing** (i.e. after the CPU-kernel question says the pool can stop
   being the tail - `docs/PLAN-CPU-KERNEL.md`). Scope: ~300-500 lines of logic in `frontend/strata_context.cpp` +
   `engine.hpp`, plus mirrored tests. Much better specified than "add slots" from scratch; MIT means we can copy
   with attribution.
2. **Wait for upstream.** `PLAN.md` drops it for v1 rather than rejecting it, and the engine half is complete, so
   PolyStrata/UnbiasedStrata may reinstate it. Worth re-checking their `serve/` at each rebase before writing
   anything.

Either way the *engine* needs nothing: `--batch`, `--batch-mtp`, `BGEN`/`BT`/`BDONE`/`BSTOP` are all present and now
measured (`-29% at 32K` without a faster pool, section 7).
