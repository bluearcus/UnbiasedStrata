# Is a custom CPU expert kernel worth writing? — investigation plan

**Question.** The CPU expert pool is often the tail (0.1.39: 15.0 ms/round against a ~12.3 ms GPU path at 262K).
The engine runs the **AVX-2** kernels on this box because its fast path needs AVX-512 VNNI *and* VBMI, and the
i9-10900X has VNNI but **not VBMI**. Would a kernel written for VNNI-without-VBMI be worth it — and is the pool
even compute-bound, or is it memory-bandwidth-bound (as has been assumed in the past)?

Three hypotheses, which different measurements separate:

| | Hypothesis | Signature |
| --- | --- | --- |
| **H1** | compute-bound (AVX-2 instruction throughput) | single-thread time ≈ MACs / AVX-2 rate; achieved GB/s well below the read ceiling; scaling linear in cores until the core count |
| **H2** | bandwidth-bound (DDR4) | achieved GB/s plateaus at the system ceiling; extra threads buy nothing; single-thread GB/s ≈ ceiling/cores |
| **H3** | schedule-bound (too few independent jobs) | any thread count saturates, *and* a synthetic many-job workload scales while the real per-layer workload does not |

## 1. What we already know (and what it does not prove)

| Measurement | Result | What it rules out / leaves open |
| --- | --- | --- |
| Worker sweep, 0.1.39, 262K (generate mode) | 9 workers 12.19 ms/round, 16 → 12.64, 20 (SMT) → 23.37 | 9→16 flat is saturation of *something*; 20-thread collapse is execution-port/cache contention. Does not say which of H1/H2/H3 |
| Pool critical path, 0.1.45 serve, 262K | 9 workers 81.2 tok/s vs 4 workers 68.3 | the pool has ~47% slack at 262K (pool ≈ 6.6 ms against GPU ≈ 12.3 ms) |
| Engine's own row rate | `22.3 GB/s over the rows phases` | near the *plausible* DDR4 aggregate, but the kernel also does arithmetic; this number alone cannot separate H1 from H2 |
| ISA | AVX-2 only (no VBMI), VNNI present but unused by the i-quant path | a VNNI kernel is *possible*; whether it helps is the question |

**Thread scaling is a necessary but confounded test**: 9→16 being flat is consistent with all three hypotheses,
and the real workload only has ~3 distinct experts per layer, so scheduling granularity (H3) can masquerade as
bandwidth saturation. That is why the plan measures *achieved GB/s against a ceiling* and *single-thread kernel time
against the bytes it reads*, not just time vs thread count.

## 2. Experiments

### E1 — the bandwidth ceiling (30 min)
Parallel page-cached reads of the shard that holds the expert blobs, 1/2/4/8/10/16/20 streams:

```
for n in 1 2 4 8 10 16 20; do
  sync; /usr/bin/time -f "%e s" bash -c \
    "for i in \$(seq $n); do dd if=SHARD1 of=/dev/null bs=1M count=4096 iflag=fullblock & done; wait"
done
```
Metric: GB/s per configuration, and its plateau. That plateau is **the ceiling the pool can never exceed**.
(Optional cross-check: `mbw`/`stream`-style kernel if installed; `dd` from page cache is adequate and matches the
access pattern - sequential blob reads.)

### E2 — the kernel in isolation, single-threaded (1 h)
`native_expert_bench` already exists for exactly this (real GGUF rows, bit-exact vs a reference mode, timed, and
`groups >= 8` deliberately exceeds L2 so it is a memory-level benchmark):

```
build-rk/native_expert_bench SHARD1 LAYERS GROUPS TOKENS REFMODE MODE [ITERS]
```
Run with one group and with 16+ groups, several layers, and take:
- **time per expert row**, from which **achieved GB/s** (bytes read / time) and **MAC/s** (3 · n_embd · n_ff per row)
- compare achieved GB/s with E1's *single-stream* figure: if the kernel already runs at ~single-stream bandwidth,
  it is bandwidth-bound at one core, and H1 is dead for single-core but not for the pool as a whole.

### E3 — the pool with controlled job counts (1 h)
The confound in E3 is job granularity. Measure the pool's scaling on a workload with **many independent experts**
(many layers × many experts at once) versus the real per-layer pattern, using the engine's own numbers
(`--pool-workers` sweep, `--stats`) and, if needed, `native_expert_bench` at high `groups`:
- if the many-job workload scales with threads while the real one does not → **H3**, and the fix is scheduling or
  layout, not a kernel;
- if both plateau at the same GB/s → **H2**;
- if both scale but below the E1 ceiling → **H1**.

### E4 — SMT and clock sanity (15 min)
`--pool-workers` 9 (one per physical core) vs 18/20 (SMT): already measured as a collapse on 0.1.39; repeat on
0.1.45 to confirm and to see whether the achieved GB/s *falls* with SMT (a bandwidth-bound kernel would not lose
throughput to SMT the way an execution-port-bound one does).

### E5 — prototype, only if E1–E4 point at H1 (2–4 days)
Take the gate/up dot from `src/kernels/cpu/iq_avx2.cpp` (556 lines; the quant formats are the ones the census names:
IQ3_XXS uses gate/up types 16/17 and down 20/42, IQ3_S uses 18/22 and 20/42 — so four gate/up formats, two down)
and add a `vpdpbusd` path, unpacking the 2/3-bit codes with shifts as the AVX-2 code already does. Validate with
`expert_parity` / `iq_avx2_parity` (bit-exactness against the reference), then re-run E2 and E3.

## 3. Decision rule

| Outcome | Reading | Action |
| --- | --- | --- |
| E2 achieved GB/s ≈ E1 single-stream ceiling, and E3 plateaus at E1's aggregate | **H2, bandwidth-bound** | do not write the kernel; the only lever is reading fewer bytes |
| E2 well under the ceiling, E3 scales with threads up to the core count | **H1, compute-bound** | write the VNNI prototype (E5); expect 1.5–2.5x on the pool |
| E3's many-job workload scales but the real one does not | **H3, schedule/layout-bound** | fix granularity (per-layer slotting, batching of jobs), not the kernel |
| Mixed | the pool is compute-bound at low thread counts and bandwidth-bound at high ones | a VNNI kernel helps only where it runs with few threads busy, i.e. marginal |

## 4. The payoff gate (why this must be decided before writing code)

Even a 2.5x faster pool is worth **~0-5% on single-stream** at 262K, because the pool has 47% slack there
(`docs/DUAL-GPU.md`). The real prize is that it would make **batching** viable: our 2-slot probe lost 29% at 32K
because two slots push the pool past the GPU path. A pool 2x faster would put it back under, and batching's window
sharing could then pay.

So the honest order of value is: **kernel -> batching viability -> throughput**, not "kernel -> throughput". If the
answer to E1–E4 is H1, the combined work is 2-3 weeks with a plausible 1.5-1.8x aggregate on two concurrent
streams; if H2, both projects are dead and the ~98 tok/s single-stream figure is the machine's ceiling.

## 5. Risks and confounds

- **Page cache vs storage**: E1 and E2 must read experts that are already cached (the 43 GB shard does not fit in
  62 GB alongside everything else) - pin the layers used, or accept and report the measured read rate.
- **Turbo/thermal**: 10-core AVX-2 sustained load downclocks; record `--pool-workers 9` against 9. Record package
  power if possible, and run the thread sweep twice.
- **Two quant formats**: IQ3_S's blobs are ~17% larger and use different gate/up types (18/22 against 16/17), so a
  kernel must cover both to be useful for the quality configuration.
- **Bit-exactness is non-negotiable**: the verify window's text depends on the rows (`native_expert_bench` asserts
  it). Any new kernel must pass `expert_parity` against the AVX-2 reference, not merely look close.

## 6. Order and cost

| Step | Cost | Decision it unlocks |
| --- | --- | --- |
| E1 + E2 | half a day | H1 vs H2 for one core and for the kernel |
| E3 + E4 | half a day | H3 ruled in or out; pool scaling explained |
| E5 prototype | 2-4 days | whether VNNI actually delivers, measured |
| Batching re-probe on top | 1 day | whether the aggregate gain exists |

E1-E4 fit in a single working day and answer the question in the title. E5 is only worth starting if they point at H1.
