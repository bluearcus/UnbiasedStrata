# Rebasing the fork onto upstream 0.1.45 — assessment and plan

**Why.** Measured on this rig, same pack/prompts/settings: upstream 0.1.45 decodes **+7.8% / +16.6% / +23.6%**
faster than our fork 0.1.39 at 1K / 4K / 32K (`docs/DUAL-GPU.md`, E2). The gap grows with context, and our
commit set contains **no kernel or decode-loop changes** — it is the six-release version gap.

**Target.** `robertkarl/UnbiasedStrata` @ `1fa9006b` (`rk/main`), version **0.1.45**, 59 commits ahead of our base
`b124b59`. Same repository layout as ours (`frontend/`, `Makefile`, `tools/`, `patches/`), so this is a genuine
rebase — unlike Niko1221/Strata, which is the product repo with a different tree.

---

## 1. What the trial cherry-pick showed

Cherry-picking our nine commits onto `rk/main` in a scratch worktree:

| Commit | Result |
| --- | --- |
| `b1c0eed` tools + CMake | **1 conflict**, trivial: upstream deleted the block where our test targets sat (`vram_plan_test` no longer exists upstream). Re-attach after `strata-plan`, which is where `strata_plan` is defined. |
| `077c4bf` front end peer options | **2 conflicts, 5 hunks**: `README.md` (1), `frontend/main.cpp` (4) |

The second conflict is the significant one: **upstream replaced the front end**. `rk/main:frontend/main.cpp` is
420 lines (ours is ~1,000), it parses llama-server options with `common_params_parse`, takes `-m/--model`, and
passes everything after `--` to the engine. Our peer block-list line is simply gone — there is nothing to unblock.

## 2. Engine state upstream (what helps and what hurts)

| Item | In `rk/main` | Consequence for us |
| --- | --- | --- |
| `--peer-device` and its knobs | **present** (17 references), same names | our peer tier ports onto the same option surface |
| `--batch-mtp` / `STRATA_BATCH_MTP` | **present** (37 references) | batching becomes worth revisiting *after* the rebase (see `PLAN-batching.md`) |
| Adaptive tier | upstream added a **non-blocking** variant ("swaps advance between verify windows"; help text says `0 (default) = the blocking tier`) | our promotion pass hooks the blocking `adapt()` lambda; must be re-checked against both modes |
| `--vram-cap` | **gone entirely** (`git grep vram_cap` → nothing) | our Phase 3 work (per-device cap + `vram_cap.hpp`) becomes a fork contribution ahead of upstream; re-add as-is |
| `peer_experts.hpp` / `.cpp` | still present; cpp changed 67 lines upstream | our `slots()` accessor and cap-clamp re-apply by hand |

## 3. Work breakdown

Our whole change set is **1,888 insertions / 67 deletions over 18 files**; the code part is **493 / 66 over 7
files**. That is what has to be carried across, not a rewrite of the fork.

| Step | Work | Effort | Risk |
| --- | --- | --- | --- |
| R1 | Branch from `rk/main`; carry CMake test wiring (3 targets) | 0.5 h | low |
| R2 | Re-apply `src/program/generate.cpp` changes by hand: `STATS` peer fields, per-device `--vram-cap` (option, blockers, `vram_info`, help), peer P2P log line, promotion hook in the adapt lambda, `--peer-*` help | 1–1.5 d | **medium** — `tier_stats` and the adapt path both moved/grew (9,266 lines changed upstream); the promotion call must be checked against the new non-blocking tier |
| R3 | Re-apply `peer_experts.cpp/.hpp`: `slots()`, cap+blocker arguments in `open()`, `nominate_for_primary()`, `last_forced_`/round state | 0.5 d | low–medium |
| R4 | Port new headers (`vram_cap.hpp`, `peer_promotion.hpp`) and their two tests + CMake | 0.5 d | low |
| R5 | **Front end on the new architecture**: re-add the peer flags, the parent-side CUDA probe (ordinal check + P2P verdict), the reserve/prefill defaults, `--ple-io direct` default under a peer, the low-RAM default, and `STRATA_DUMP_EARGS` | 1.5–2 d | **medium** — the new front end delegates parsing; our validation lives in a different place |
| R6 | Update `docs/DUAL-GPU.md`, the launch recipes and the tools to the new CLI (`-m/--model`, engine flags after `--`) | 0.5 d | low |
| R7 | Re-run the gates: `dual_gpu_plan_check`, `vram_cap_test`, `peer_promotion_test`, front-end tests, then G1/G2/G4/G6 on the rig | 1 d | low |

**Total ≈ 4–6 working days.** The risky items are R2 (large upstream diff around our edits) and R5 (rewritten
front end); everything else is mechanical.

## 4. Benefits beyond the measured speed

1. **+8–24% decode** immediately, before any tuning, all of it free of our own optimisation work.
2. **`--batch-mtp` exists** — the per-slot drafters whose absence made batching pointless; the batching
   conclusion in `docs/DUAL-GPU.md` was explicitly conditional on that feature.
3. **The non-blocking adaptive tier** may make our promotion pass cheaper (it currently runs inside the
   blocking adapt round).
4. Our `--vram-cap` work and the promotion pass become **ahead of upstream** — both portable back as PRs.

## 5. Costs and risks

- **User-visible CLI change.** The new front end takes llama-server options before `--` (`-m/--model`,
  `--host`, sampling defaults) and engine flags after it. Every recipe in `docs/DUAL-GPU.md`, the systemd
  examples and our benchmark scripts need updating (R6).
- **Two rewrites collide.** Our front-end validation is ~180 lines written against a file that no longer
  exists in that form; it is a fresh implementation, not a merge.
- **Behavioural drift to re-verify.** Upstream changed the adaptive tier and the VRAM planner; the peer's
  interaction with both must be re-measured, not assumed (G1/G2/G4 gates).
- **Nothing forces the rebase.** Staying on 0.1.39 costs 8–24% but is stable, tested, and documented.

## 6. Recommendation

**Do it**, in the order R1 → R3 → R4 → R2 → R5 → R6 → R7 (engine and headers first, because they are the parts
whose conflicts are mechanical, and they prove the rebase before the front-end rewrite lands). Gate each step
with the rig runs already scripted in `tools/`, so any regression is caught at the step that caused it.

If a full rebase is not wanted now, a cheaper intermediate exists: **port only the engine optimisations**
(`src/` and `include/`, leaving our front end in place) — that recovers the decode speed without touching the
CLI. It is not straightforward (the front end and engine share the line protocol, which also changed), but it
is the smaller of the two jobs and worth evaluating first.

---

## 7. Status after the first pass (branch `rebase-0.1.45`, commit `96b9f9c`)

Done: R1, R3, R4, and R2's engine work. The rebased tree builds (`build-rk/`), `vram_cap_test` and
`peer_promotion_test` pass, and the ported features work on the rig: `--vram-cap 0:22000,1:10000` held back
2,126 / 1,873 MiB per card, expert cache 9,248 slots, **peer 5,377 experts (8.67 GiB), combined 14,625 of 24,576
on the GPUs, p2p no**, and a generation completed.

**Performance check (same pack, prompts and settings, 3-run medians):**

| Cell | Fork 0.1.39 | Upstream 0.1.45 | **Rebased fork** | vs old fork |
| --- | --- | --- | --- | --- |
| 1K | 105.2 | 113.4 | **111.1** | +5.6% |
| 4K | 81.4 | 94.9 | **94.0** | +15.5% |
| 32K | 75.1 | 92.8 | **89.9** | +19.7% |

The rebased fork is within 1-3% of upstream (inside the +/-10 tok/s run spread) and 6-20% faster than our 0.1.39
branch. That confirms the deficit was the version gap alone, not the fork's changes.

Still open:

* **R5 - front-end peer validation on the new program.** The rewritten front end passes engine flags after `--`
  verbatim, so `--peer-device` and its knobs already work; what is missing is our validation and defaults
  (ordinal check, live P2P probe before the load, reserve 2500, `--peer-prefill-rows 0` without P2P, `--ple-io
  direct` under a peer, the low-RAM fallback below ~56 GiB, and the `STRATA_DUMP_EARGS` inspection hook).
* **Observability (was Phase 1).** Upstream 0.1.45 has **no STATS protocol** and a minimal `/health`
  (`status, busy, service, loaded`), so the peer counters need a new vehicle - either a new engine command
  consumed by `frontend/strata_context.cpp`'s `get_health`, or per-request stderr lines like the cache hit rate
  the engine already prints.
* **R6 - docs and scripts for the new CLI** (`-m/--model` before `--`, engine flags after; `--engine PATH`;
  no `--engine` self-exec). Every recipe in `docs/DUAL-GPU.md` and the benchmark/pull scripts still uses the
  0.1.39 form.
* **Gates G1/G2/G4/G6 re-run** on the rebased engine with the peer (the numbers above are single-GPU).
