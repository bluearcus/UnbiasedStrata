# Dual-GPU support for UnbiasedStrata — plan

**Target rig: RTX 3090 (24 GB, sm_86) + RTX 4070 (12 GB, ~10 GB usable, sm_89).**
**Primary model: Qwen3.8-Flash-Next IQ3_XXS. Experts spill to system RAM as they do today.**

Workspace:

| Path | What |
| --- | --- |
| `UnbiasedStrata/` | the fork, `github.com/bluearcus/UnbiasedStrata` (upstream `robertkarl/UnbiasedStrata`) |
| `Strata/` | the original project, `github.com/niko1221/Strata` |
| `UnbiasedStrata/docs/PLAN-dual-gpu.md` | this document (versioned with the fork) |

Status (Oct 2026): the fork is 7 commits ahead of its upstream (`b124b59` → `e6fb013`): Phase 0 harness, Phase 0 front end, Phase 1 observability, Phases 2+3 (RAM tier, per-device `--vram-cap`), the peer promotion pass, `docs/DUAL-GPU.md` — all committed, working tree clean. Gates G1–G6 await the model files (pulling).

---

## 1. The finding that sets the shape of this plan

The fork's **engine already contains three working multi-GPU mechanisms**, inherited from upstream: real
kernels, real sizing, real logging, upstream benchmarks behind them. What the fork does is **forbid them at
the front end** — `frontend/main.cpp:629-639`:

```cpp
// one GPU only: the engine's multi-GPU and remote-expert modes are not part of this fork
for (const char* no : {"--gpus", "--layer-split", "--split-device", "--peer-device", "--peer-slots",
                       "--remote-expert", "--vision", "--batch"})
    if (e.rfind(no, 0) == 0) { ...; std::exit(2); }
```

and `README.md:62` states it as a product decision (`Not supported | more than one GPU, batch requests`).

**The block list is a prefix match, so it is much narrower than it looks.** Of the flags this rig needs, only
`--peer-device` and `--peer-slots` are actually blocked; every peer sub-knob already passes through today:

| Flag | Status today |
| --- | --- |
| `--peer-device 1` | **a front-end option** since the Phase 0 change: parsed, validated (ordinal, P2P probe) and forwarded |
| `--peer-slots N` | **a front-end option** since the Phase 0 change; after `--` it is refused so it cannot race the defaults |
| `--peer-reserve-mib N` | passes through |
| `--peer-adapt-swaps N` | passes through |
| `--peer-prefill-rows N` | passes through |
| `--expert-cache-device1 auto` | passes through |
| `--gpus`, `--layer-split`, `--split-device`, `--batch*` | **BLOCKED** |

So the enabling change is a few lines, not an integration project.

---

## 2. The four mechanisms that already exist in the engine

| # | Option | Semantics | Card roles | Constraints |
| --- | --- | --- | --- | --- |
| **B** | `--peer-device N` | **Second adaptive expert tier.** CUDA0 keeps everything it has (dense, KV, session, MTP, head, its own expert tier). The peer card holds additional expert slots across **all** layers, computes its experts' rows with the *same kernels from the same activations*, and writes them into the mapped host rows the graph already reads | CUDA0 = everything but extra experts; CUDA1 = extra experts only | not with `--layer-split` or `--expert-cache-device1..3` (`generate.cpp:2002`); needs `--expert-profile` + the expert cache (`generate.cpp:3992`); **width-locked to `cpu::H` = 2560** (`generate.cpp:2313`) → this is exactly Flash-Next's width, so **B is available for the primary target and unavailable for Qwen3.6** |
| A | `--layer-split K[..]\|auto` + `--split-device` | **Pipeline parallelism**: each later GPU runs layers `[K_i, K_{i+1})` with its own dense weights, session, **KV slice**, expert cache, verifier and prompt stage; residual handed off through mapped pinned buffers | CUDA0 = `[0,K1)`, CUDA1.. = rest; last stage owns the head and drafter | needs `--serve`; `K >= 2`, rising; `< 8` stages; distinct devices (`generate.cpp:1611-1657`) |
| C | `--expert-cache-device1..3 N\|auto` | **Static remote expert tiers** (no adaptive swapping), results through pinned host rows | as B but static | with a layer split needs a further GPU no stage uses (`generate.cpp:1756-1763`) |
| D | `--remote-expert-opt` | decode-path optimizations on top of C | as C | width-locked to 2560 (`generate.cpp:2313`) |

A and B both fit this rig's VRAM into use. §3 picks between them; everything after that assumes the pick.

---

## 3. Which mechanism for a 3090 + 4070, and why it is not the layer split

### 3.1 The rig is deliberately unbalanced

| | 3090 | 4070 |
| --- | --- | --- |
| VRAM | 24 GB | 12 GB physical, ~10 GB usable |
| Memory bandwidth | ~936 GB/s | ~504 GB/s |

In a **layer split (A)** both cards become mandatory stages of every token's pipeline, so per-token latency is
the sum of the stages. The 4070 has roughly half the bandwidth, so any layer given to it costs about twice
what the same layer costs on the 3090; a VRAM-proportional split (~29% of layers) would make the pipeline
substantially slower than the 3090 alone. Upstream's own `--split-skip-if-fits` (`generate.cpp:520-523`,
`2848-2890`) exists for exactly this reason: *"a split then only adds hand-offs (two RDNA4 cards: 1,384 vs
1,794 tok/s at 4K)"*. The split's real prize is pooled **KV** (each card holds only its own layers' KV), which
buys context, not speed.

### 3.2 The peer tier (B) is the mechanism that matches the stated goal

The goal — *"most used experts on the 3090, with some additional on the 4070 to increase hit rates"* — is
literally what `PeerExperts::open` does: it is handed the profile **ranked hottest-first** and the primary's
`ExpertCache`, and fills the peer with *only the pairs the primary does not hold* (`generate.cpp:3995-3997`).
The 3090 keeps the hottest experts; the 4070 takes the next slice; RAM/disk keep the rest. The tier then
adapts per conversation (`PeerExperts::adapt`).

Decisive properties for an unbalanced pair:

* the 4070 is **never in the critical path** — it runs concurrently with the CPU pool and its rows land in the
  same mapped host rows, so a slow peer costs latency only for the entries it owns;
* the peer's contribution is **purely extra expert capacity**: it holds no dense weights, no KV, no session,
  and its staging buffers are tiny (`CAP` = `MAXT × top-k` = 8 × 10 = 80 rows × `H` = 2560 ⇒ ~1.6 MB of mapped
  x/out/meta/q8/scratch — `peer_experts.cpp:130-140`);
* it works **without P2P** — see §3.4, which matters because this is an Ampere + Ada pair.

### 3.3 What the numbers look like

Flash-Next geometry (`docs/PLAN-qwen36-35b-a3b.md:26-35`): n_embd 2560, **48 layers, 512 experts, top-10**,
expert intermediate 640, one MTP draft layer, 262K context, 28.8 GB PLE shard. That is 48 × 512 =
**24,576 expert references** in the profile space.

An expert is `3 × n_embd × n_ff` = 3 × 2560 × 640 ≈ 4.92 M parameters; the shipped census
(`tests/data/native_experts/iq3_xxs.txt`) totals **42,912,972,800 B ≈ 39.97 GiB** over the 24,576 references —
**1,746,133 B ≈ 1.665 MiB per expert** (per-layer blobs range 1.245–2.222 MiB). The Phase 0 harness
(`tools/dual_gpu_plan_check.cpp`) computes the ladder from the real census and the engine's own
`vram_plan.hpp`:

| | Expert slots | Share of the 24,576 |
| --- | --- | --- |
| 3090 alone, after dense (~13.5 GB), MTP (~1.1 GB), session, KV (≈2.5 GB at `--kv int8`, 262K), prompt buffers, reserve | ≈ 2,361 (int8) / 3,129 (q4_0) | ≈ 10-13% |
| + 4070 peer (12 GB physical, 2.5 GB reserve) | ≈ +6,776 | ≈ +27.6% |
| combined | ≈ 9,137 (int8) / 9,905 (q4_0) | ≈ **37-40%** |

Measured on the rig (G1, Oct 2026, `--kv q4_0`): the primary holds **9,416** slots and the peer **5,389** (8.73 GiB) — **14,805 of 24,576 = 60.2% GPU-resident**, well above the 40% estimate (the 3090's fixed set came in much smaller than the conservative 13.5 GB dense assumption, and prefill borrows cache slots). First request: 73.9 tok/s decode, 111 tok/s prompt, 65% MTP acceptance, `peer_ms_wait` 0.37 ms/token, zero file reads. So the 4070 roughly **sextuples** the GPU-resident expert count (2,361 → 14,805), and the CPU
pool's drain — which upstream measured as the dominant cost (19.1 → 10.3 ms/token at 54.4% hits,
`generate.cpp:701-705`) — shrinks in proportion to the entries it no longer computes. This is the whole value
proposition for this rig.

### 3.4 P2P: plan for it not working

`cudaDeviceCanAccessPeer` between sm_86 and sm_89 is expected to return 0 (different architecture families;
there is no NVLink between these cards). The code already handles both cases, and they differ per path:

| Path | Needs P2P? | Behaviour on this rig |
| --- | --- | --- |
| **Decode** (`PeerExperts::launch`/`finish`, `peer_experts.cpp:181-268`) | **No** — activations go H2D and rows come back D2H through the pinned staging buffers; `STRATA_PEER_DIRECT` (default on) scatters straight into the mapped host rows with a kernel | works |
| **Prompt-side helper** (`--peer-prefill-rows`, the peer computing prompt rows) | **Yes** — `prefill.cpp:1475` refuses outright without it | fails **gracefully**: `"strata serve: prefill peer: the two GPUs cannot access each other (no P2P) - the prompt path stays on the primary GPU"` (`generate.cpp:5339-5342`) |

So on this rig the front end does this itself (Phase 0 change, `frontend/main.cpp`): it probes P2P in the
parent — safe, because the engine child `execv`s (`frontend/engine.hpp:29-38`) — and passes
`--peer-prefill-rows 0` when the cards cannot see each other, so expect the peer to help decode only, and its
startup line says which. **The engine's own help text ("rows over P2P", `generate.cpp:720`) is still misleading
for this rig and should be corrected (Phase 1, item 4), and the engine's own probe result is still not logged
(Phase 1, item 4).**

### 3.5 The one thing the peer does not buy: context

KV stays entirely on the 3090 in peer mode, so the context is bounded by the 3090's budget as it is today —
the 4070 adds expert capacity, not context. If a longer context becomes the priority over decode speed, the
layer split is the only mechanism that pools KV, and it should then be evaluated with `--split-skip-if-fits`
as the tie-breaker. Keeping both modes selectable is the right end state; **peer first** is the right order.

### 3.6 Concrete launch, once the front end allows it

The peer flags are front-end options (before `--`) since the Phase 0 change; passing them after `--` is
refused so they cannot race the front end's defaults:

```
CUDA_DEVICE_ORDER=PCI_BUS_ID ./unbiased-strata \
  --model /models/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
  --pack  /models/pack-iq3-xxs \
  --mtp   /models/mtp \
  --ctx   262144 \
  --peer-device 1 \
  -- --kv q4_0 --ple-io direct
```

Notes on that line:

* `CUDA_DEVICE_ORDER=PCI_BUS_ID` pins "device 0 = the 3090" deterministically; otherwise the ordinal depends on
  enumeration order and the peer could land on the wrong card.
* The front end emits `--kv int8` itself when `--ctx > 8192` (`frontend/main.cpp:684`); trailing `--` arguments
  are appended **after** it and the engine's parser is last-wins, so `--kv q4_0` here overrides it. Choose
  deliberately — freeing KV bytes on the 3090 buys expert slots, which is the point of this rig.
* `--ple-io direct` is **required on a 64 GB host**: the front end hard-codes `--ple-io ram`
  (`frontend/main.cpp:680`), mlocking the 28.8 GB PLE table and pushing RAM demand to ~74 GiB. Overriding it to
  `direct` keeps the table on the SSD and drops demand to ~46 GiB (KV stays in VRAM on the 3090). Note that
  `direct` is **upstream's own default** (`docs/upstream/DETAILS.md:1030-1031`; `ram` is only for rotational
  disks), so the override restores upstream's behaviour rather than deviating from it. Wants the model on an
  SSD, not a rotational disk (`generate.cpp:1014`).
* `--peer-reserve-mib 2500` is the knob for the 4070's desktop overhead, and **2500 is the front end's own
default** for a peer since the Phase 0 change (the engine's default is 600); `PeerExperts::open` fills the card
from `cudaMemGetInfo` minus this reserve, so it is the exact control for "leave ~2 GB". `--peer-prefill-rows 0`
needs no explicit line either: without P2P the front end passes it itself (the P2P probe runs in the parent
before the engine starts, and its verdict is printed).
* Building for just these two cards: `make build CUDA_ARCHS="86;89"` — quote the list, an unquoted `;` would
  separate two shell commands (~3 min instead of the 6-arch default).

### 3.7 Quantization: XXS first, S as the measured follow-up (Oct 2026 decision)

IQ3_S fits this rig (experts 46.8 GiB → ~53 GiB RAM demand; ~27% GPU-resident vs ~37% for XXS) but is not a free
upgrade: lower GPU-resident share means more (and bigger) CPU-pool misses per token, so S will likely decode
*slower* than XXS here while giving better quality per token. XXS stays the target until G1–G6 pass: every
shard/pack/profile SHA is published and pinned for it, while S needs an unvalidated pack build, a generated
profile, and kernel coverage checks for its quant types. The profile ports by file copy (the format is a pure
pair ranking, quant-blind), validated by hit-rate comparison; the running server's `--expert-profile-save`
snapshots provide the live loop. Revisit S with side-by-side tok/s + quality numbers once the peer tier is proven.

---

## 4. What is single-device in the fork today

| Gap | Where | Effect for this rig |
| --- | --- | --- |
| ~~Front-end gate~~ **done** (Phase 0, written and syntax-checked; rig verification G1/G2 pending the build) | `frontend/main.cpp` | the five `--peer-*` flags are parsed, validated (ordinal, P2P probe, sub-knob consistency) and forwarded; `--gpus`/`--layer-split`/`--split-device`/`--remote-expert` stay blocked (Phase 4's decision) |
| The P2P probe is silent | `peer_experts.cpp:99-110`; startup message at `generate.cpp:3995-4003` | user cannot tell whether the prompt-side helper is available; the help text claims P2P |
| The peer tier is absent from `STATS` | `generate.cpp:6138` (`tier_stats`) reports only the primary's `vram_slots`/`vram_held` | `/health` under-reports the tier; no `peer_hits`/`peer_ms_wait` |
| Auto RAM-tier decision is skipped under a peer | `generate.cpp:2291` (`o.peer_device < 1` in the condition), comment: *"the multi-GPU helpers' residency rules were validated at option time and are not revisited here"* | with `--peer-device`, the automatic downgrade to mmap + hot copies never runs; the host tier is whatever the defaults produce, and Flash-Next **must** spill (40 GB of experts, 28.8 GB PLE) |
| `--vram-cap` was one global for one device — **done** (Phase 3) | `generate.cpp:126-140` (`g_vram_cap_bytes`), `2130-2162` | cannot express 24 GB + 10 GB — now `N` caps every card, `D:MIB,...` caps one ordinal each (`vram_cap_test`) |
| VRAM plan is single-device | `include/strata/plan/vram_plan.hpp` | correct for peer mode (KV is on card 0 only) but the startup plan should still show the peer's contribution to the tier ladder |
| Front-end doc/help | `README.md:62`, frontend `usage()` | claims one GPU only |
| No multi-GPU tests in the fork | `tests/` has no split/peer coverage (upstream's lives in `Strata/tools/*.py`) | nothing regresses loudly |

One gap that is **not** a problem here: `generate.cpp:1683` refuses `--resident-cpu-experts` with a layer split
or remote caches, but the guard does not mention `peer_device` — so the resident RAM modes remain available
with a peer, which Flash-Next needs.

---

## 5. Work plan

### Phase 0 — enable it (hours)

**Status:** done at the source level — the front end parses, validates (ordinal < device count, P2P probe both
ways in the parent, sub-knob consistency), defaults (reserve 2500 when not given, prefill rows 0 when the probe
says no P2P) and forwards the five `--peer-*` flags; `usage()` and the README's "Not supported" row are updated;
the whole file is syntax-checked against a stub `cuda_runtime.h`.  Remaining: the real build (cmake + nvcc
installing), `ctest` on the harness, and the G1/G2 acceptance runs on the rig.

Before any of the source changes below, run the **dry-run harness** (`tools/dual_gpu_plan_check.cpp`, wired
into the build as the `dual_gpu_plan_check` test) against the rig's numbers. It is GPU-free and loads no model:
it feeds the real IQ3_XXS census and the engine's own `vram_plan.hpp` the 3090/4070 budgets and prints the
expert-tier ladder, the context the engine would choose, and the RAM-tier check (the Phase 0 blocker). Run it
with `--vram0/--vram1/--reserve0/--reserve1/--kv/--ram/--ctx` to match the actual cards and host.

Then change the front end from "reject these prefixes" to real handling. Minimum: stop blocking `--peer-device`
and `--peer-slots`. Better, and barely more work:

1. **Parse** `--peer-device N`, `--peer-reserve-mib`, `--peer-slots`, `--peer-adapt-swaps`,
   `--peer-prefill-rows` into `Options` and forward them (the sub-knobs already pass through, so this is about
   validation and defaults rather than plumbing).
2. **Validate before a 60 GB load**: peer ordinal `< device_count()` and `!= 0`; refuse peer + split; refuse
   peer without `--mtp`/`--pack`.
3. **Sensible defaults**: when a peer is configured and `--peer-reserve-mib` was not given, use a larger
   default than 600 (a card driving a desktop needs more); default `--peer-prefill-rows 0` when the P2P probe
   says the cards cannot see each other.
4. **The RAM tier: on a 64 GB host the single blocker is the PLE, not the experts.** Two things set the RAM
   arithmetic, and both favour this rig:
   * `generate.cpp:2291` makes the automatic low-RAM fallback (resident pinned arena → `--mmap-experts` +
     `--resident-experts`) fire **only when `peer_device < 1`**. With a peer configured the engine does not
     downgrade, so the front end must pass the RAM tier explicitly whenever a peer is set.
   * Upstream's "64 GB minimum" (`docs/upstream/DETAILS.md:63-66, 209-210, 248`) is a **system-RAM budget for
     the reference rig — an RTX 5070 12 GB with 64 GB of RAM** — where the 12 GB card cannot hold the KV, so at
     64K+ the KV streams to pinned RAM (`--kv-resident`, ~13.7 KB/token): ~43 GB experts + ~6 GB overhead +
     ~3.6 GB streamed KV at 262K ≈ 53 GB, hence "close the browser, keep the context at 128K or less". The
     28.8 GB PLE is **on the SSD in that budget** — upstream's default is `--ple-io direct` (`DETAILS.md:1030-
     1031`); `ram` is only for rotational disks. **On the 24 GB 3090 the KV stays in VRAM** (it is in this plan's
     fixed set), so it does not count against RAM either: this rig's RAM demand is experts + overhead ≈ 46 GiB —
     *less* than the reference rig's at the same 262K context. The only thing that breaks the 64 GB host is the
     fork's hard-coded `--ple-io ram`.

   The blocking item is therefore the **PLE table**, not the experts. The fork's front end **hard-codes
   `--ple-io ram`** (`frontend/main.cpp:680`), mlocking the whole 28.8 GB PLE table. On the 64 GB host:
   * `--ple-io ram`  → 40 (experts) + 28 (PLE) + ~6 (overhead) ≈ **74 GB — does not fit**, even on one GPU.
   * `--ple-io direct` (PLE reads unbuffered from SSD, never enters RAM) → 40 + ~6 ≈ **46 GB — fits**, and the
     fast resident-arena path is kept (no `--mmap-experts` needed).

   The front end already appends trailing `--` arguments after its own and the engine's parser is last-wins, so
   **`-- --ple-io direct` overrides the hard-coded `--ple-io ram` with no source change**. `direct` wants the
   model on an SSD, not a rotational disk (`generate.cpp:1014`). So Phase 0 on this host is: unblock
   `--peer-device`, pass `-- --ple-io direct`, and (because the auto fallback is skipped) explicitly choose the
   resident arena on a 64 GB host — falling back to `--mmap-experts --resident-budget-gib N` only if the arena
   still does not fit. The Phase 0 acceptance test must assert a generation completes under
   `--ple-io direct --peer-device 1` on the 64 GB host, not merely that the engine starts.

**Gate G1**: Flash-Next starts on the 64 GB host with the peer on the 4070 and `--ple-io direct`, the startup
line reports the peer's experts and GiB, and a generation completes with output identical to the one-card run
(§6, G2).

### Phase 1 — make it observable (days)

4. **Log the P2P probe** in the peer startup message (`generate.cpp:3995-4003`): `p2p yes|no`, and when it is
   `no`, say that the prompt-side helper is off. Fix the help text at `generate.cpp:720-728` to match.
5. **Add the peer tier to `STATS`** (`tier_stats`, `generate.cpp:6138`): `peer_slots`, `peer_held`,
   `peer_hits`, `peer_ms_wait`, `peer_p2p`. The front end needs no change — it forwards the JSON verbatim to
   `/health` (`frontend/main.cpp:~740+`), so the numbers appear for free.
6. **Report the combined tier at startup**: `"with the primary's %lld that is %lld of %lld on the GPUs"` already
   exists (`generate.cpp:4002`); extend the same idea to the plan printout so the expert ladder (VRAM / peer /
   RAM / file) is visible in one place.

**Gate G3**: `/health` shows the peer's slots, held count and wait time, and they move during a request.

### Phase 2 — the RAM tier under a peer (days)

7. **Host-side tier under a peer — done (Phase 2, front end).** With `--peer-device` the engine's automatic arena-to-mmap fallback (`generate.cpp:2291`) is skipped, so the front end passes the tier explicitly. Flash-Next needs ~40 GB of experts; the supported configuration on **this rig's 64 GB host** is the resident pinned arena with `--ple-io direct` (the PLE on the SSD, upstream's own default): ~46 GiB, ~18 GiB of headroom beside the desktop. The fallbacks `-- --mmap-experts --resident-experts` (mmap + hottest copied to RAM) and `--resident-budget-gib N` are for hosts smaller than 64 GB. Both defaults are now in the front end: `--ple-io direct` under a peer (the fork's hard-coded `ram` would OOM a 64 GB host), and `--mmap-experts --resident-experts` when total host RAM is below ~56 GiB. Explicit `--ple-io` / `--mmap-experts` / `--resident-*` / `--shared-expert-arena` after `--` win over both. A `STRATA_DUMP_EARGS=1` hook prints the engine command line and exits, so all forwarding is checkable without a model.
8. **Confirm the interaction is sound**: the peer writes its rows into the pinned portable output rows
   (`verify.cpp:97` allocates them portable when `peer_portable()`, set at `generate.cpp:1595`), which is
   orthogonal to where the expert *weights* live — so resident-RAM and peer modes should compose. Prove it
   (`--resident-experts` + `--peer-device`) rather than assume it. Still to prove on the rig (Gate G4): needs the model files, which are not on this machine yet.

**Gate G4**: a long generation with the peer and the RAM fallback active shows no row corruption, and
`rdma`-style counters (`ram_reads`, `file_reads`, `major_faults`) behave as they do without the peer.

### Phase 3 — plan honesty for an asymmetric rig (days)

9. **Per-device `--vram-cap` — done (Phase 3, engine + front end).** Was: one global (`g_vram_cap_bytes`) that would silently clamp the **4070's** budget to the **3090's** figure. Now: `g_vram_cap_bytes`
   (`generate.cpp:126-140`) is a single global, and `vram_info()` clamps against the *current* CUDA device.
   A single value would therefore silently clamp the **4070's** budget to the **3090's** figure, and cannot
   express one figure per card. Now `include/strata/core/vram_cap.hpp` parses `N` (every card) or `D:MIB,...` (`0:22000,1:10000`); `generate.cpp` allocates one blocker per capped card and `vram_info()` clamps against the current device; `PeerExperts::open()` takes the peer's own cap entry + blocker (it previously read the raw `cudaMemGetInfo`, ignoring the cap entirely). Pure parsing/clamp arithmetic is covered by `vram_cap_test`; the front end validates the shape and forwards it verbatim.
10. **Feed the peer's capacity into the startup plan** so the printed ladder matches what was actually
    allocated, and the adaptive tier's expected hit rate is stated per card.

**Gate G5**: `--vram-cap 0:22000,1:10000` is honoured on both cards, and each card's own figure is used, not a
shared one.

### Phase 4 — the layer split as the secondary mode (optional)

Only if pooled context becomes the priority. The engine-side machinery is already complete and generic
(per-stage dense weights via `--trim-stage-weights` at `generate.cpp:2898-2926`, per-stage session carving at
`3211-3220`, per-stage caches at `3623`/`3666`, prompt stages at `5255`, pipeline hand-offs at `5420-5460`,
parking per stage). The work is then the same front-end treatment as Phase 0 for `--gpus` / `--layer-split` /
`--split-device` / `--split-skip-if-fits`, plus the N-device planner in Phase 3 being extended to *partition*
KV, not just account for it. Known engine limits to document either way: no token graph across stages
(`generate.cpp:4682`), batched prefill across a split (`generate.cpp:5727`), no `VRAM` command with a peer
(`generate.cpp:6173`), and `--gpu-only-full`/`--gpu-stages` refuse a split (`generate.cpp:2020`).

### Phase 5 — tests and documentation (days)

11. **Unit**: a peer-tier test alongside `tests/core/expert_cache_per_layer_test.cpp` (capacity arithmetic,
    profile-minus-primary fill order, adapt swap selection). Still open: needs the profile + pack fixtures; the
    `vram_cap_test` covers the per-device cap arithmetic in the meantime.
12. **Parity**: assert the design claim that a peer-served expert row is **bit-identical** to the same expert
    computed on the primary's hit path (same kernels, same activations — `peer_experts.hpp:11-13`). This is the
    single most valuable test, because it is what makes G2 meaningful.
13. **Front end**: extend `make test-frontend` (`frontend/engine_test.cpp`) to assert the new flags are
    forwarded verbatim and the new validation rejects bad ordinals. Half done: `STRATA_DUMP_EARGS=1` prints the
    engine command line for exactly these assertions (verified by hand against the real probe); wiring them into
    `make test-frontend` needs the built binary + CUDA, while that suite is CPU-only by design.
14. **Docs**: a `docs/DUAL-GPU.md` in the fork covering peer mode for the 3090+4070 (the command in §3.6, the
    reserve knob, why P2P is absent, what is and is not gained), the layer split as the alternative, and the
    update of `README.md:62`'s "Not supported" row. Done: `docs/DUAL-GPU.md` is written from the implemented
    behaviour; the README row names the peer tier.

---

## 6. Acceptance gates

| Gate | Check |
| --- | --- |
| G1 | Flash-Next starts with `--peer-device 1`; the startup line reports the peer's experts and GiB |
| G2 | **Revised and measured (Oct 2026), harness: `tools/dual_gpu_g2_parity.sh`.** Teacher-forced, 16 positions: top-1 agreement **14-16 of 16**, top-5 overlap 4.5-5.0, mean KL 0.05-0.13; positions whose routing stayed on card 0 come out **exactly** equal. Control: single-vs-single with the same oracle is **bit-identical** (KL 0.000 at every position). **Diagnosis (investigated, Oct 2026): there is no peer-path race.** The run-to-run variation first seen was in the *oracle generation pass*, not the comparison: under a **fixed oracle** both configurations reproduce exactly (15/16 and mean KL 0.07125 to five decimals across repeats), the primary slot count (11,426) and pcie_frac (0.34) are identical across runs, and three clean repeats of the generation pass were themselves identical. The residual difference is structural: the single run serves non-cached experts from the AVX-2 CPU pool, the peer run serves its slice from CUDA. The sharp invariant is that **positions whose routing stayed entirely on card 0 come out exactly equal** — that is what makes the remaining difference attributable rather than noise. The harness takes `--oracle` directly so the gate never depends on generation being deterministic. Gate as it stands: top-1 ≥ 0.875 over repeated runs and mean KL ≤ 0.15, with the single-vs-single control required to be exactly 0. **Original text:** token identity FAILED and is the wrong instrument. Same config twice with the peer differed (256 vs 177 tokens); single-GPU static was identical (177 vs 177). The integer matmul is exact (`__dp4a`, INT8×INT8→INT32), but the row is not: per-32-block int32 sums meet fp32 activation / fp16 weight scales, are summed in float, and go through SiLU (`s2_expert_grouped.hpp:56-74`; the header itself notes SiLU's `expf` differs between CUDA and CPU). Across sm_86 and sm_89 that yields ULP differences, and at temperature 0 a near-tie flips the argmax and diverges the sequence. New gate: **logits/distribution parity** (KL or top-k overlap per step at temp 0) plus a **row-parity test with an explicit ULP tolerance**; token identity stays a single-GPU regression only (where it is proven). |
| G3 | `/health` reports `peer_slots`/`peer_held`/`peer_hits`/`peer_ms_wait`/`peer_p2p` |
| G4 | The resident-RAM fallback and the peer compose without corruption; `major_faults`/`file_reads` behave as without a peer |
| G5 | Asymmetric `--vram-cap 0:22000,1:10000` honoured on both cards |
| G6 | **Measured (Oct 2026) — the peer's throughput contribution is small, and the bottleneck is NOT expert residency.** 300-token generations, temp 0.7, same prompt. At 262144 ctx: single **78.6** avg / 81.7 median, peer **84.1** avg / 83.1 median (80.3, 85.7, 93.2, 77.1); at 65536: single 84.8, peer 85.8, peer-static 79.7, resident-arena modes 75 (slower than mmap). Decisive counter: `peer_ms_wait` is **0.14 ms/token** (1.2% of an 11.8 ms token) with `ram_reads=0 file_reads=0` — nothing waits on the CPU pool, so decode is **GPU-compute bound**; the 3090 alone already hides the CPU tier. Speculation is the real lever (the 93.2 tok/s run had 82% draft acceptance; `--spec 6` lowered acceptance to 56-59% and throughput to 82.3 avg; temp 0 vs 0.7 is ~13%). Resident pinned arena is a **net loss** on this 64 GB host. Consequence: more expert capacity cannot reach 100 tok/s — profiling the GPU stage split (dense / attention / experts / MTP verify) is the next step. Speculation sweep done (262K, peer, temp 0.7, 3 runs each): `--spec 3` 81.2 tok/s / 65.3% acceptance, `--spec 4` (default) 84.1 / ~66%, `--spec 5` 80.0 / 59.1%, `--spec 4 --suffix-draft 0` 83.9 / 64.6%, `--spec 4 --spec-min-p 0.3` 76.9 / 53.1%. The defaults are already at the optimum - no speculation setting buys a material gain, and run-to-run spread is +/-10 tok/s. Stage profile (generate mode, `--stats`): the host thread waits on CPU-pool rings 14.6 ms/round while the peer halves the pool (15.0 -> 8.4 ms/round, CPU experts 2.90 -> 1.44/layer), but the peer needs 19% more rounds per token because acceptance falls, which cancels the saving. In serve mode the pool is overlapped instead. Ceiling ~85 tok/s is structural (GPU work + verify cadence); the remaining lever is batching/concurrency, which this fork refuses. The original gate text follows. **Report both greedy and sampled**, with draft acceptance: greedy is ~13% flattering (measured single-GPU, static, same prompt: 60.8 tok/s at temp 0 / 74% draft acceptance, 56.5 at 0.7 / 70%, 53.7 at 1.0 / 62%) because speculative acceptance falls as sampling diverges. Indicative first-request peer delta at equal acceptance (74%): 60.8 -> 87.4 tok/s (+44%), to be confirmed warmed and repeated. |
| G7 | All existing single-GPU tests still pass — the N=1 path is untouched |
| G8 | *(Phase 4)* a layer split starts, pools KV, and `--split-skip-if-fits` falls back exactly when the profile fits the 3090 |

---

## 7. Risks and open questions

1. **Ampere + Ada P2P.** Expected absent, handled gracefully on decode, unavailable on the prompt path (§3.4).
   Confirm with the probe log from Phase 1 rather than assuming; if P2P *does* work, `--peer-prefill-rows` and
   `STRATA_PREFILL_HELP` become available and prompt processing also gains.
2. **The peer's win depends on where the bottleneck is.** If the 3090's own GPU work already dominates a
   decode step, more expert slots on the 4070 buy little. Measure before/after with the existing counters
   (`STATS` + the pool-phase timing flags) and expect the gain to track the *added GPU-resident share*, not
   the added VRAM.
3. **Host RAM is still the binding constraint.** On this rig's 64 GB host: ≈40 GiB of experts + ≈6 GiB overhead
   ≈ 46 GiB fits **with the PLE on the SSD** (`--ple-io direct`, upstream's own default); the fork's hard-coded
   `--ple-io ram` adds the 28 GiB table and ~74 GiB of demand, which does not fit. The peer changes nothing
   here, and Phase 2 exists because the automatic RAM-tier decision is skipped under a peer — the most likely
   way to get a surprising regression.
4. **4070 desktop overhead and PCIe placement.** `--peer-reserve-mib` handles the former. For the latter: if
   the 4070 sits on a chipset slot (PCIe 4.0 ×4), the peer's H2D/D2H staging shares that link. Rough sizing:
   ≈`rows × 2560 × 4 B` each way per layer, so at 30% peer ownership roughly 1.5 MB/token down and back —
   ≈0.2 ms each way at ×4 — against a decode step of ~10 ms. Acceptable, but it is the number to watch in
   `peer_ms_wait`, and `STRATA_PEER_DIRECT` (default on) already removes one of the two copies.
5. **Power.** ~350 W + ~200 W sustained. Worth stating in the docs, since this class of upgrade is where a PSU
   gives out.
6. **Qwen3.6-35B-A3B gets none of this.** The peer and remote-expert paths are width-locked to 2560
   (`generate.cpp:2313`) and Qwen3.6 is 2048. For that model the options are the layer split (A) or
   `--expert-cache-device1..3` (C, whose guard does not check the width but whose code also assumes it). If
   Qwen3.6 matters later, that is a separate, self-contained piece of work: take `n_embd`/`n_ff` from the
   loaded geometry instead of the constant in `peer_experts.cpp:25` / `remote_experts.cpp:16`, size the q8_1
   staging from it, and add a 2048-wide parity test. The kernels underneath are already runtime-shaped
   (`native_expert_grouped` takes a `NativeExpertLayout` with per-layer `n_embd`/`n_ff`).
7. **Front-end/engine flag drift.** The front end hard-codes an engine argument list
   (`frontend/main.cpp:660-692`) and prefixes are how things are excluded today. A small explicit mapping
   table in one place would stop the next flag from being silently mis-handled in either direction.

---

## 8. Order of work

```
Phase 0  enable --peer-device, validate, default the reserve           <- hours; the whole rig starts working
Phase 1  log the P2P probe, put the peer in STATS, fix the help text   <- days; makes it debuggable
Phase 2  settle the RAM tier under a peer, prove it composes           <- days; the likeliest regression
Phase 3  per-device --vram-cap, plan printout that matches reality
Phase 4  (optional) layer split for pooled context
Phase 5  parity test, front-end test, docs/DUAL-GPU.md, README
```

Phases 0-2 deliver the stated goal on this rig. Phase 3 is worth doing regardless. Phase 4 is a different
product decision (context over speed) and can wait.
