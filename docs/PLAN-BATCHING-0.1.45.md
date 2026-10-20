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
