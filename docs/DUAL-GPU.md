# Dual-GPU: the 3090 + 4070 peer tier

Target rig: RTX 3090 (24 GB, sm_86) as the primary + RTX 4070 (12 GB, sm_89) as the peer, 64 GB of host RAM,
Qwen3.8-Flash-Next (IQ3_XXS and the other GSQ-RCO quantizations: all 2560-wide, which the peer path requires).

The 4070 is a second **adaptive expert tier**, not a second pipeline stage: the hottest experts stay on the 3090,
the next slice lives on the 4070, the rest in RAM. Decode gains ~2.7x the GPU-resident experts (2,361 -> 9,137
slots at int8 KV, 37% of all experts); context stays bounded by the 3090, as before.

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

## Upward tier movement (promotion pass)

The two VRAM tiers never exchange residency directly. Instead, each adapt round opens with a promotion pass:
a peer-held expert whose heat ranks inside the **top third** of its layer's primary residents is evicted to CPU
coverage, which makes it an ordinary primary candidate in the same round — and the primary's gain-sorted
admission pulls it up to the faster card when it beats a victim there. Weights always come from the host
source. Three guards: the nominee must also beat the layer's worst primary resident by the standard 1.5
margin (so it is actually admitted, not stranded on the CPU), at most `adapt_swaps` nominations per round,
and an 8-round cooldown before re-nomination. `peer_nominated` in `/health` counts the cumulative upward
moves. Net effect: the fast card converges back toward holding the hottest experts even when the conversation
drifts off-profile, without a restart.

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
