# Benchmarking our rig against Strata's published numbers — plan

**Question.** Is the UnbiasedStrata fork weak in performance, and how does this rig compare with the numbers the
original project quotes (90–180 tok/s and "100+" in places)?

**Short answer, from the data already read:** the "100+" figures are **dual-GPU layer splits with smaller or
pruned quants at 32K**, not single-GPU IQ3_XXS. The only like-for-like cell that exists is the 3090 run in
`Strata/bench/results/2026-09-29-rtx3090-epyc-milan/`. Everything below is about matching *that*.

---

## 1. What upstream actually publishes

`Strata/bench/results/2026-09-29-rtx3090-epyc-milan/matrix.md` — **1× RTX 3090, IQ3_XXS, engine 0.1.26, int8 KV,
MTP `--spec 4 --spec-min-p 0.5`, greedy, 256-token outputs, prompts from the Strata source tree, 3-run medians:**

| Context | Prompt tokens | Prompt tok/s | Decode median | TTFT | Acceptance |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1K | 1,135 | 869.7 | 93.0 | 1.33 s | 0.738 |
| 4K | 4,200 | 1,772.2 | 89.2 | 2.41 s | 0.763 |
| 32K | 31,602 | 2,160.4 | 90.6 | 14.83 s | 0.739 |
| 128K | 129,913 | 2,173.6 | 79.3 | 60.53 s | 0.723 |
| 262K | 259,589 | 1,725.6 | 67.3 | 152.10 s | 0.697 |

Same machine, 32K, **two 3090s via layer split**: IQ3_XXS K=24 → 96.7 median; IQ3_S K=24 → **106.3**;
Coder IQ1_M (half the experts) K=22 → **103.3**. `bench/results/2026-09-30-community-rtx-5090/README.md`:
RTX 5090 + IQ2_XS → 179.4 (4K) / 175.7 (32K) / 165.0 (128K).

So the published "100+" is: bigger GPU, or two GPUs, or a pruned/smaller model — never one 3090 on IQ3_XXS.

**Two corrections to our own past claims, from this data:** upstream's 262K cell used a **259,589-token prompt**
(decode 67.3), whereas our "262K" measurements used ~65-token prompts merely *with a 262K context window*
(84.5 tok/s). Our numbers and theirs were never the same measurement.

**Version gap to keep in mind:** `Strata/` is **0.1.45**; our fork is **0.1.39**. Six releases of upstream
optimisation (and features such as `--batch-mtp`) that our engine does not have.

---

## 2. Three questions, three experiments

| # | Question | Experiment | Cost |
| --- | --- | --- | --- |
| **E1** | Does our **rig** match upstream's 3090 rig? | Replicate the five cells above on our 3090 with the fork engine | ~1.5 h (short cells) + ~1.5 h (128K/262K) |
| **E2** | Is the **fork** slower than upstream? | Build `Strata/` 0.1.45 here and run the same cells (1K/4K/32K) with its engine | build ~15 min + ~40 min |
| **E3** | Does **two cards** help as much as their layer split? | Our peer config vs single at 32K, same cells (their split gets 90.6 → 96.7, +7%) | ~40 min |

E2 is the one that answers "is the unbiased fork poor": our commits touch the front end, `STATS`, `--vram-cap`
plumbing and the peer tier — **no kernel or decode-loop changes** — so parity is expected, and any gap is the
0.1.39→0.1.45 version delta, not the fork.

---

## 3. Method (must match theirs, or the comparison is worthless)

| Dimension | Their setting | Ours must be |
| --- | --- | --- |
| Model | IQ3_XXS, revision `ed59f920` | same files we already verified |
| KV | **int8** | `--kv int8` (we have been running `q4_0`, which inflates throughput) |
| Context | 524288-capable model, `--max-context 262144` | same |
| Prefill | `--prefill auto` | same |
| Speculation | MTP `--spec 4 --spec-min-p 0.5`, suffix drafting on | same defaults |
| Sampling | `temperature: 0`, `reasoning_effort: none` | same |
| Output cap | 256 tokens | same |
| Prompts | Strata source-tree corpus, ~1.1K / 4.2K / 31.6K / 129.9K / 259.6K tokens | build with **their** harness: `python3 bench/bench_vs_llama.py prompts --out DIR --tokenizer <pack>/tokenizer --corpus Strata/src/...`, extended for 128K/262K |
| Repeat | 3 runs, median; unique request IDs; no conversation reuse | same |
| Metrics | prompt tok/s, decode median, TTFT, acceptance | same fields, from our `/health` + `timings` |

Confounds to record alongside (not to fix): our 3090 is capped at **300 W** (theirs 280 W); our CPU is a
**10900X** (AVX-2 for the expert kernel, like their EPYC 7453 which has no AVX-512 either); we have **62 GiB RAM**
to their 165 GiB, so our experts are mmap'd while theirs may be resident; our engine is 0.1.39, theirs 0.1.26
(E1) or 0.1.45 (E2).

---

## 4. Expected outcome (so the result is interpretable either way)

- **E1**: we should land near 93 / 89 / 90 at 1K / 4K / 32K, and drop at 128K/262K as KV grows. Our earlier
  84.5 was with `q4_0` KV *and* trivial prompts, so it is not evidence of weakness in either direction.
- **E2**: parity is the prediction (same kernels). A gap would point at 0.1.39 vs 0.1.45, and the fix would be
  rebasing the fork rather than profiling the rig.
- **E3**: their layer split gains +7% at 32K; our peer gained ~0% at 262K with 65-token prompts. At 32K with
  32K-token prompts the CPU pool pressure is higher, so the peer may finally show a gain — or confirm that it
  is the wrong mechanism for throughput, which is already the documented conclusion.

---

## 5. Deliverable

A `bench/results`-shaped table in `docs/DUAL-GPU.md` with our five cells for fork and upstream, plus the peer arm
at 32K, and a one-line verdict on: *rig competitive? fork competitive? peer worth enabling?*
