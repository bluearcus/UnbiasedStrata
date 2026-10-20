# Dual-GPU: the 3090 + 4070 peer tier

Target rig: RTX 3090 (24 GB, sm_86) as the primary + RTX 4070 (12 GB, sm_89) as the peer, 64 GB of host RAM,
Qwen3.8-Flash-Next (IQ3_XXS and the other GSQ-RCO quantizations: all 2560-wide, which the peer path requires).

The 4070 is a second **adaptive expert tier**, not a second pipeline stage: the hottest experts stay on the 3090,
the next slice lives on the 4070, the rest in RAM. Decode gains ~2.7x the GPU-resident experts (2,361 -> 9,137
slots at int8 KV, 37% of all experts); context stays bounded by the 3090, as before.

## The 0.1.45 CLI, and `strata-dual`

Upstream 0.1.45 replaced the front end: it parses llama-server options, takes `-m/--model`, and passes everything
after `--` to the engine, which is a **separate binary** (`--engine PATH`). The old form (`--model/--pack/--mtp`
with a self-exec engine) is gone.

`tools/strata-dual` is the fork's front-end policy, kept **outside** upstream's front end so a rebase costs
nothing. It assembles the engine's standard command from `--pack`/`--mtp`/`-m` and, with `--peer-device N`,
validates the ordinal, probes P2P before the load, and defaults the reserve (2500 MiB), `--peer-prefill-rows 0`
without P2P, `--ple-io direct` for a two-shard model, and the low-RAM tier below ~56 GiB. Explicit flags win.

```
build-rk/peer_p2p_probe 1          # p2p no        (the standalone probe the wrapper calls)
CUDA_DEVICE_ORDER=PCI_BUS_ID tools/strata-dual \
  --engine build-rk/strata -m /models/...-00001-of-00002.gguf --port 8080 \
  --pack /models/pack-iq3-xxs --mtp /models/mtp --peer-device 1 \
  -- --max-context 262144 --kv int8
```

`--dry-run` prints the command instead of running it. `/health` carries the tier census (see below) when the
server is idle.

## Launch

```
CUDA_DEVICE_ORDER=PCI_BUS_ID ./unbiased-strata \
  --model /models/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
  --pack  /models/pack-iq3-xxs \
  --mtp   /models/mtp \
  --ctx   262144 \
  --peer-device 1 \
  -- --kv q4_0 --ple-io direct
```

Notes:

* `CUDA_DEVICE_ORDER=PCI_BUS_ID` pins device 0 = the 3090 (check `nvidia-smi`: the 3090 must be first). Without
  it the ordinals depend on enumeration order and the peer can land on the wrong card.
* `--peer-device`, `--peer-reserve-mib`, `--peer-slots`, `--peer-adapt-swaps`, `--peer-prefill-rows` are
  **front-end options (before `--`)**. A `--peer-*` knob after `--` is refused so it cannot race the front
  end's defaults.
* No `--peer-*` line at all means single-GPU, exactly as before.
* `--ple-io direct` keeps the 28.8 GB PLE table on the SSD (upstream's own default). The front end already
  passes it by default under a peer; the explicit line above is belt and braces (it wins over the default
  anyway). Without it the fork's hard-coded `--ple-io ram` mlocks the table and a 64 GB host OOMs (~74 GiB
  of demand vs ~46 GiB on the SSD).
* `--kv q4_0` after `--` overrides the front end's own `--kv int8` (last-wins parser) and frees ~1.3 GiB of
  3090 VRAM for expert slots. Optional; int8 is the default.
* `--peer-reserve-mib` defaults to 2500 (the engine's 600 assumes a dedicated card; the 4070 drives a desktop).
  `--peer-slots` defaults to what fits (~6,800). `--peer-prefill-rows` defaults to 0 without P2P (this rig:
  sm_86 + sm_89 have no P2P, so the prompt path always stays on the 3090 and the peer helps decode only).

## What is and is not gained

* Gained: GPU-resident expert share (the CPU pool's drain shrinks in proportion); nothing else moves: dense
  weights, KV, session, MTP and the prompt path stay on the 3090.
* Not gained: context (KV is not pooled — for pooled context the layer split is the alternative, not
  implemented in this fork), prompt speed (no P2P on this pair).
* Cost: ~350 W + ~200 W sustained; make sure the PSU has the headroom. If the 4070 sits on a chipset PCIe
  4.0 x4 slot its staging shares that link (~1.5 MB/token each way at 30% peer ownership — fine against a
  ~10 ms decode step; watch `peer_ms_wait` in `/health`).

## Upward tier movement (promotion pass) — opt-in, measured HARMFUL, default off

The promotion pass moved peer-held experts to the primary when their heat ranked in the top third of that
layer's primary residents. Two designs were built and measured at 262K, peer on, 3-run medians:

| Design | XXS 1K / 4K / 32K | S 1K / 4K / 32K |
| --- | --- | --- |
| Evict-first (evict, then let the primary's selection decide; losers bounce back) | 108.8 / 84.6 / 84.2 | 87.7 / 68.0 / 67.9 |
| **Coordinated** (nominate without evicting; only the admitted ones leave the peer) | 100.1 / 86.1 / 81.6 | 82.7 / 68.1 / 68.7 |
| **Disabled** (upstream behaviour) | **106.5 / 99.0 / 100.8** | **96.7 / 86.8 / 80.2** |
| Upstream engine, same settings (reference) | 105.7 / 99.2 / 106.9 | 94.0 / 83.1 / 84.9 |

**Why it cannot pay here, in hindsight:** a promotion is a *lateral* move - the expert is already resident in
VRAM, merely on the slower card - and it is paid for by **demoting a primary resident to the CPU pool**. Both
cards being VRAM means residency does not improve; only bandwidth asymmetry could justify it, and on this rig the
peer is not bandwidth-starved (`peer_ms_wait` was 0.14 ms/token). The coordinated design removed the bounce but
not the demotion, which is why it did not recover the loss either.

**The rate is the dominant variable.** The pass runs every `--adapt-every` windows (default 4) and was allowed up
to `--adapt-swaps` (96) promotions per pass; the soak logs show it averaging ~18 per pass. Sweeping the budget at
32K (XXS, peer on, 3 runs each) gives a clean monotone curve:

| Promotions per pass | Heat margin | Decode |
| --- | --- | --- |
| 0 (off) | - | **101.9** |
| 2 | 3.0 | 98.2 |
| 8 | 2.0 | 92.1 |
| 96 (the old default) | 1.5 | 80.9 |

Each promotion is paid for by demoting a primary resident to the CPU pool, and on this rig there is no bandwidth
gain to trade for it, so the cost is monotone in the rate and any rate is negative. `STRATA_PEER_PROMOTE_MAX`
(default 2) and `STRATA_PEER_PROMOTE_GAIN` (default 3.0) exist for the case the mechanism was written for - a peer
card much slower than the primary, where `peer_ms_wait` is large - not for this rig.

So the mechanism is kept behind `STRATA_PEER_PROMOTE=1` (the code is small and correct, and a peer with a much
slower card or a large `peer_ms_wait` is the case it was written for) and **off by default**. With it off, the
fork's peer arm matches upstream's within noise and beats single-GPU by 8-11% at 4K/32K.

## Observability

* Startup prints the probe verdict (`peer GPU 1: no P2P ...`) and the engine logs `p2p yes|no` with what it
  means for the prompt path, plus the peer's experts/GiB and the combined GPU-resident count.
* `GET /health` (the engine's `STATS`) carries `peer_slots`, `peer_held`, `peer_hits`, `peer_ms_wait`,
  `peer_p2p` alongside the existing tiers. `STATS` without a peer reports zeros and `false`.
* `STRATA_DUMP_EARGS=1` prints the engine command line the front end built and exits before starting
  anything — for checking flag forwarding without loading a model.

## The asymmetric `--vram-cap`

`--vram-cap N` caps every card (a 1080 Ti as 6 GB: 5722); `--vram-cap 0:22000,1:10000` caps one ordinal each
(24 GB on the 3090's share, 10 GB on the 4070's). Each card's free figures are clamped to its own cap and a
blocker holds the rest back, so each card is sized as its own smaller card — including the peer tier, which
sizes itself from its own entry. Unlisted ordinals are uncapped.

## Small hosts

Below ~56 GiB of total host RAM the front end adds `--mmap-experts --resident-experts` under a peer itself
(the engine's automatic fallback is skipped with a peer): experts stay mapped from the file and the hottest
are copied to RAM by profile rank. Explicit `--mmap-experts` / `--resident-*` / `--shared-expert-arena` /
`--ple-io` after `--` override both defaults. On the 64 GB rig neither fallback triggers.

## What stays single-GPU

`--gpus` / `--layer-split` / `--split-device` (a layer split would make the 4070 a mandatory pipeline stage at
~half the 3090's bandwidth — slower decode for pooled KV, a different product decision) and `--remote-expert*`
stay refused at the front end. Qwen3.6-35B-A3B (2048-wide) is refused by the engine's width guard under a peer;
its multi-GPU path is the split, not this tier.

## Batching: measured, and on hold

`tools/dual_gpu_batch_probe.sh` drives the engine's own serve protocol (`BGEN`/`BT`/`BDONE`) with
`--batch 2 --peer-device 1` and two 200-token conversations, no front end involved. Measured on this rig
(262K, q4_0, peer on, real prose prompts):

| Arm | Per stream | Aggregate |
| --- | --- | --- |
| One request (baseline) | 89.8 tok/s | 89.8 tok/s |
| `--batch 2` | 42.5 / 44.4 tok/s | **84.9 tok/s** |
| `--batch 2` (+`STRATA_BATCH_MTP=1`) | 43.1 / 45.2 tok/s | **86.2 tok/s** |

The engine half works exactly as documented (`--batch: 2 slot sessions on CUDA0 (2.25 GiB each)`, interleaved
`BT` lines, per-slot `BDONE`), but **batching buys nothing here**: aggregate throughput is unchanged while
each stream halves. The mechanism: batching amortises *shared GPU work* (dense weights, head, shared expert,
and any expert two streams both routed). On this rig the binding resource is the **CPU expert pool**, which is
AVX-2 limited (the 10900X has AVX-512 VNNI but not VBMI, so the fast kernels are unavailable), and two
unrelated conversations share almost no experts - so the pool's work doubles and aggregate stays flat.

`STRATA_BATCH_MTP` did not engage in this configuration (no per-slot drafter line); even a best case of +15%
on top would leave it below the single-stream rate per user.

**Consequence:** the ~12-17 day front-end batching project (`PLAN-batching.md`, held unversioned) is not
justified on this hardware. It becomes worth revisiting only if the pool stops being the bottleneck:
a CPU with AVX-512 VBMI (Ice Lake / Zen 4+), or a quantization small enough that the experts fit in VRAM
(Q2_0 / IQ2_XS at ~34 GB is still larger than 34 GB of VRAM minus KV, so not today).

### `--batch-mtp` does not exist in this fork

The engine here is 0.1.39; per-slot drafters arrived upstream at 0.1.43+, and `grep -rn "BATCH_MTP|batch_mtp"
src/ include/` finds nothing. That matters more than it looks: speculation is what amortises GPU work per
window, and one stream already gets it.

| Config | Tokens per window | Window | Aggregate |
| --- | --- | --- | --- |
| single stream + MTP (`--spec 4`, ~70% acceptance) | ~1.9 | ~22 ms | ~85 tok/s |
| `--batch 2`, no per-slot drafting | 2.0 (1 per slot) | ~23.6 ms | ~85 tok/s |

Without per-slot drafters, batching *replaces* the strong amortisation (speculation) with a weak one (expert
sharing, near zero between unrelated conversations): same aggregate, half the speed per user.

### Throughput conclusions for the current architecture on this rig - nothing to do

| Path | Aggregate | Work | Confidence |
| --- | --- | --- | --- |
| today: single stream, peer on, 262K | **~85 tok/s** | - | measured |
| + VNNI-without-VBMI CPU kernel only | ~85 (no change) | 1-2 weeks | high (pool has 47% slack) |
| + `--batch 2` only | ~85 aggregate, ~42 per user | 1-2 weeks front end | measured |
| + backport per-slot drafting (`--batch-mtp`) | ~110-150? | weeks of engine work (per-slot drafters sharing weights, per-slot K/V rings, window geometry) | **low** - unmeasured here |
| ... plus a faster pool to feed it | higher again | + 1-2 weeks | medium |

Reaching 100+ tok/s therefore needs per-slot speculation backported from a newer upstream **and** the VNNI
kernel (3.8 tokens per window roughly doubles today's pool work). That is a months-scale project with a
low-confidence payoff on this GPU/CPU pair, not the front-end-only work the batching plan scoped.

**Verdict: stop optimising throughput on this rig.** ~85 tok/s single-stream is the architecture's limit here;
the peer tier remains valuable as headroom (CPU independence), not as speed. Revisit only with a VBMI-capable
CPU, a smaller quantization, or a batched-and-speculating engine.

## Is the CPU pool on the critical path? (it depends on the regime)

`tools/dual_gpu_pool_critical_path.sh` starves the pool of workers and measures the throughput response
(single-stream serve, 262K, peer on, temp 0.7):

| Workers | Throughput |
| --- | --- |
| 9 (default) | 81.2 tok/s avg |
| 4 (44%) | 68.3 tok/s avg |

A 56% cut costs only 16%, so with `T = max(GPU, pool)` the fit is **pool ~6.6 ms/token against GPU ~12.3 ms**:
the pool runs ~47% ahead of the GPU and has slack. **A faster CPU kernel would therefore gain nothing for
single-stream serving** on this rig, even though the AVX-2 fallback is genuinely 2-3x slower than the
VBMI path it replaces.

The two regimes where the pool *is* the limit, and where a faster kernel would pay:

| Regime | Pool/token | GPU/token | Limiter |
| --- | --- | --- | --- |
| single stream, serve, real prompt (~70% acceptance) | ~6.6 ms | ~12.3 ms | GPU |
| single stream, generate mode, degenerate prompt (30% acceptance: more rounds/token) | ~8.1 ms (15.0 ms/round) | ~11.5 ms | pool per round |
| `--batch 2` (two unrelated conversations share almost no experts) | ~13.2 ms | ~12.3 ms | **pool** |

So the kernel work and the batching work are complements, not alternatives: alone, each is worth ~nothing on
this hardware (single stream is GPU-bound; batching without a faster pool just halves each stream). Together
they would make two streams GPU-bound again, i.e. roughly 2x the aggregate of one stream.

## IQ3_S vs IQ3_XXS on this rig (measured)

`tools/dual_gpu_quant_ab.sh`: serve mode, 262K, `--kv q4_0`, temp 0.7, one prompt, 3 requests per arm, all four
arms in one session. The S pack was built with upstream's `iq_pack.py` and validated against the shipped S
census (`tests/data/native_experts/iq3_s.txt`): 48 layers, 0 blob mismatches, 50.292 GB both ways. The
expert profile was a straight copy of XXS's (the format is a pair ranking, quant-blind).

| Arm | GPU-resident experts | tok/s | Acceptance |
| --- | --- | --- | --- |
| XXS, single | 9,454 (38%) | **84.5** | 69.1% |
| XXS, peer | 14,752 (60%) | 81.9 | 59.4% |
| S, single | 7,878 (32%) | 68.7 (61.5 / 73.3 / 71.3) | 66.0% |
| S, peer | 12,409 (50%) | **73.5** (73.2 / 73.1 / 74.0) | 69.1% |

What this establishes:

* **S costs ~19% throughput alone, ~13% with the peer** - the predicted direction (bigger blobs, fewer slots,
  more CPU work) at a smaller magnitude than feared.
* **The peer's value grows with quantization size**: 0% for XXS (-3%, inside noise), **+7% for S**, and it also
  **removes the tail** - S alone spread 61.5-73.3 tok/s, S with the peer 73.1-74.0. That is the CPU pool coming
  near the critical path at S's blob sizes, exactly the mechanism the peer exists to absorb.
* S at 73.5 tok/s with the peer is still ~13% below XXS's 84.5, so S is a **quality** purchase, not a speed one.

## Soak on the rebased engine (0.1.45): both quants, real context

Through `tools/strata-dual`, peer on, `--max-context 262144`, `--kv q4_0`, five varied prompts of 400 output tokens
each (mix of explanation, code, design notes), `/health` read between requests:

| Quant | tok/s per request | mean | vram slots | peer slots | p2p | ram/file reads |
| --- | --- | --- | --- | --- | --- | --- |
| IQ3_XXS | 88.1, 85.8, 86.5, 91.6, 84.1 | **87.2** | 9,407 | 5,339 (21.7%) | no | 0 / 0 |
| IQ3_S | 79.2, 76.6, 74.8, 81.9, 75.0 | **77.5** | 7,869 | 4,534 | no | 0 / 0 |

Both ran stable: tiers constant across requests (no slot drift, no OOM), `peer_hits` climbing (21k to 33k), no file
reads, and the only `error` strings in the logs were the engine's benign `upload no error, sync no error` verify
lines. The promotion pass settles at or near the peer's slot count (`nominated` 5,193 of 5,339 for XXS; 4,534 of
4,534 for S), i.e. it re-sorts the tier over the first few requests and then stops.

For comparison, the same quant on the 0.1.39 branch measured 81.9 (XXS peer) and 73.5 (S peer): the rebase is worth
~6% here and more at short context, where it is ~114 tok/s.
