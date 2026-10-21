# The rig as a service — as deployed

**Deployed: Docker + compose.** Image `unbiased-strata:0.1.45` (5.35 GB), one container `strata-strata-1`,
published on `127.0.0.1:8080`, `restart: unless-stopped`, healthy. Design and rationale: `DEPLOY-DOCKER.md`.

```bash
cd /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
docker compose build          # ~19 min, sm_86;89, no tests
docker compose up -d          # XXS by default, ~2 min cold / ~30 s warm
docker compose logs -f        # the shim's peer line, the tier census, then "listening"
docker compose ps             # healthy only once /health answers
```

Quant is an env override, one line, same image:

```bash
STRATA_MODEL=/models/flash-next/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf \
STRATA_PACK=/models/pack-iq3-s docker compose up -d
docker compose up -d          # back to the default (XXS)
```

## What is measured, in the container

| | IQ3_XXS (default) | IQ3_S (override) | Host, for comparison |
| --- | --- | --- | --- |
| Slots (primary + peer) | 9,407 + 5,326 | 7,869 + 4,518 | 9,407 + 5,313 / same |
| Decode, streaming, 262K | **108.1 tok/s** | **97.9 tok/s** | 98.1 / 88.9 (5-run means) |
| VRAM while serving | 23,780 MiB (3090), 9,582 MiB (4070 incl. desktop) | — | same |
| Cold start | ~2 min | ~2 min | same |
| Warm restart (`compose restart`, page cache hot) | 32 s | 33-36 s | — |

GPU mapping inside the container is the host's: **device 0 = RTX 3090** (primary), **device 1 = RTX 4070** (peer,
2500 MiB reserved because it drives the display). `peer_p2p: false`, so the prompt path stays on the primary.

## Operating it

- **Health**: `curl -sS 127.0.0.1:8080/health` — `tiers` appears only when idle. `GET /` returns a 404
  (`File Not Found`) because no web UI is shipped; that is not an error.
- **Stop**: `docker compose stop` releases both cards (3090 → 2 MiB, 4070 → 254 MiB) and leaves no orphaned engine.
  The container exits **1** by design (the front end's supervisor reports "engine gone" so a service manager
  restarts both) and **134** (`terminate called without an active exception`) if a request was in flight — the cancel
  path aborts during teardown. Both are post-release; nothing is left holding VRAM.
- **Restart policy**: `unless-stopped`, so an engine failure (exit 1) is brought back, and an explicit stop stays
  stopped. `stop_grace_period: 120s` gives the engine time to unload ~47 GB.
- **Logs**: `docker compose logs` is the only log sink; the engine prints one tier line per request at startup only.

## Known warnings (pre-existing, not container artefacts)

- `only NNN MiB of VRAM free at the start of this request (below 512 MiB)` at 262K: the 3090 is nearly full after
  the KV and the expert cache. It appears in the host logs too (`/home/mike/models/bgen_*.log`). The engine's advice
  is `--vram-reserve-mib 832`; that would cost slots, and the measured decode (108 tok/s) shows no paging, so the
  shipped configuration keeps the reserve default and this note instead.
- A host `--gpus`-style deployment fails on this machine (toolkit/EGL packaging); compose uses `runtime: nvidia`
  with `NVIDIA_DRIVER_CAPABILITIES=compute,utility`. Do not "simplify" that away.

## Agent latency: where it comes from and what fixed it

Measured with this very pi session running on the rig (`PI_PROVIDER=strata`), prompt ~62k tokens, plus controlled
conversations up to 115k.

**Prefill is linear at ~2,300 tok/s** (115,068 tokens in 49.84 s; 76,531 in 33.29 s; 20,060 in 10.2 s), and it is
**not** cheaper at a smaller window - the window only changes how much fits. So a cold prompt read is the dominant
cost of a turn: ~26 s for 60k, ~50 s for 115k, ~44 s extrapolated for 100k.

**Warm turns are essentially free**, and `--conversation-cache-mib` is what makes that true across conversations:

| Conversation | Cold | Parked and restored |
| --- | ---: | ---: |
| 115,070 tokens | 50.58 s (cached 0) | **0.48 s** (cached 115,065) |
| 60,070 tokens | 26.41 s | restored in 153.6 ms |
| 20,060 tokens | 10.16 s | **0.44 s** (cached 20,055) |

Engine lines: `parked 115074 tokens in 251.1 ms`, `restored 115065 tokens (checkpoint) in 153.6 ms`,
`parked=4 bytes=5133959940 evictions=0`. Parking costs ~28 KB per parked token, so a 115k conversation is ~3.2 GB.
Without the flag the same switch cost a full re-read (measured: 49.89 s).

**Consequence for sub-agents:** interleaving conversations is now cheap (half a second, not fifty), even though
*simultaneous* serving (batching) is still not viable (`PLAN-BATCHING-0.1.45.md`). Alternating turns between agents
do not pay re-reads. That is a real change in the economics recorded in `DUAL-GPU.md`'s "where this stops".

**Window choice, 3-run medians on the same prompts:**

| Config | Slots (primary + peer) | Decode median |
| --- | --- | --- |
| 262144, conv cache, `--vram-reserve-mib 832` | 9,326 + 5,340 | **107.6 tok/s** |
| 262144, conv cache, default reserve | 9,407 + 5,326 | 105.3 (this container) / 108.1 (earlier) |
| 131072, conv cache, 832 reserve | 10,035 + 5,323 | 100.5 |
| 131072, conv cache, default reserve | 10,118 + 5,335 | 102.1 |

131K buys ~700 expert slots but decodes ~5% slower, and it caps the agent context (a 135k-token prompt is rejected:
`exceed_context_size_error: 135139 prompt tokens, context 131072`). **The window stays 262144.**

**IQ3_S cannot host the conversation cache**: with it loaded the host had **1,895 MiB available**, under the parking
floor, so nothing parked. IQ3_XXS is the agent configuration; IQ3_S is the "one long conversation, best quality"
configuration (`STRATA_MODEL=…IQ3_S… docker compose up -d`, 97.9 tok/s at 262K, 96.2 at 131K).

**Deploy bug found and fixed**: `STRATA_EXTRA_ENGINE_ARGS` was not in compose's `environment:`, so the first two
experiments ran with `conversation_cache_mib=0` and the flag silently did nothing. It is now forwarded and has a
default:

```
--conversation-cache-mib 8192 --conversation-cache-min-free-mib 2048 --vram-reserve-mib 832
```

The reserve is the engine's own advice for the 262K window (#2065, the "below 512 MiB free" warning): it costs ~80
expert slots and nothing measurable in decode.

## LAN access and naming

The service is published on **0.0.0.0:8080** on this host (`STRATA_BIND_ADDR=0.0.0.0` in `.env`, which is gitignored;
the repo default stays `127.0.0.1` so a fresh clone opens nothing).  Host name, LAN address and gateway for this
deployment are deliberately not recorded here - see the privacy note at the end of this section.

**There is no authentication.**  Everything except `/health` and `/v1/health` is open to anything that can reach the
port, and this host runs no firewall (Docker's published-port rules would sit ahead of one anyway).  That is a decision,
not an oversight - `STRATA_API_KEY` would need three lines in `docker/entrypoint.sh` to turn llama.cpp's key check on,
and it was deliberately left out.

**What has to be true for a name to work here.**  Each of these was learned the hard way, and all of them apply to any
deployment of this service:

1. **The name and the address must agree, or the record rots.**  Pin the host's address - a reservation the router
   actually honours, or static configuration on the host - in the same change that adds the DNS record.  A lease that
   moves silently leaves the name pointing at an address nothing owns, and the reverse lookup rots with it.
2. **A reservation only works if it is keyed to an identity that appears on the wire.**  Where a LAN address lives on a
   bridge, a macvlan, or a container interface, the physical NIC's burned-in MAC is in no ARP or DHCP frame at all and a
   reservation for it is inert.  A Linux bridge also generates its own MAC at creation, so with STP off it can come up
   with a different address on every boot.  Check `ip -d link show` and `bridge link show` on the host before trusting a
   reservation, and pin the virtual interface's address if that is where the IP lives.
3. **The DHCP server and the DNS server are different things here.**  The router serves DHCP and does not publish leases
   into DNS; the local DNS server is not the DHCP server; a recursive resolver (Unbound) holds no local zone data.  So
   the DNS record is something you create, not a mirror of the lease table.  Bare short names need a DHCP search domain
   to expand and the router hands out none - use the FQDN.
4. **Use a zone from RFC 8375 (`home.arpa`), and mark the zone local.**  A private name under an unreserved zone such as
   `.home` answers when it is known, but an *unknown* name under it is forwarded upstream and times out instead of
   returning NXDOMAIN - so every typo of an internal name leaks to the public resolvers and hangs the client.  Naming
   the zone and making it local are two settings, not one (dnsmasq `local=/home.arpa/`).
5. **Advertise exactly one DNS server over DHCP.**  With a public resolver as the second option a client may pick it,
   and then the private name does not resolve and content filtering is bypassed as well.
6. **Do not rely on `.local`.**  Avahi with no `allow-interfaces` restriction advertises container addresses, so
   `getent hosts <name>.local` can return a docker bridge IP rather than the LAN address.

**Verify from another machine** (the host should answer in both directions, and the service should be reachable by name):

```
dig +short <host> @<local-dns>
dig +short -x <host-address> @<local-dns>
curl -s http://<host>:8080/v1/models | head -c 120
```

**Privacy note.**  Host names, LAN addresses, MAC addresses, NIC details, reservations and DNS records are deliberately
kept out of this repository - it is public.  They belong in the private network-admin project.

## The model id, and why the endpoint now refuses the wrong one

The service hosts **one** model, so the OpenAI `model` field used to be decorative: every value was accepted and served by
whatever happened to be loaded, including `gpt-4o` and the empty string.  The response then reported the *file path* as
the model.  Both are now fixed at the front end (`frontend/strata_context.cpp`, `frontend/main.cpp`):

* **The id is derived from the model file**: `file_stem`, minus `.gguf`, minus llama.cpp's `-NNNNN-of-NNNNN` split-shard
  suffix.  Today that is `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS`, and it changes when the quant changes, which is the point
  - a name that stayed constant while the weights changed would be the original lie in a friendlier font.
  `--alias` / `STRATA_MODEL_ALIAS` overrides it when a shorter name is wanted.
* **Unknown ids are refused** with `400 invalid_request_error` naming what the server does serve.  A missing or empty
  `model` is still accepted: llama.cpp's own clients omit it, and the tests in `frontend/tests` name the file.
* **Alias, derived name and full path all answer.**  The three are one model; the check exists to stop a *different* one
  being silently substituted, not to make clients guess the spelling.

Observed behaviour, live:

| request | result |
| --- | --- |
| `model: Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS` | 200 |
| no `model` field | 200 |
| `model: gpt-4o` | **400** `unknown model "gpt-4o"; this server serves ...` |
| `reasoning_effort: "banana"` | **400** `Unexpected reasoning effort banana. Supported types are xhigh (default), medium, and low.` |
| `reasoning_effort: "none"` | 200, no reasoning block |

`frontend/tests/e2e.sh` asserts the first three, so a rebase that loses this behaviour fails the suite rather than
quietly returning to being decorative.

Switching between quants is a separate problem from naming, and it is written up in **`docs/PLAN-MODEL-SWITCHING.md`**:
the two GGUFs turn out to differ in nothing but three metadata strings, so a quant switch does not touch the front end
at all - what is missing is a reload path in the engine, a machine-readable report of what is loaded, and a switch
command.

## Thinking control: what the engine does with it

`reasoning_effort` reaches the chat template untouched, and **the template defines the accepted set**: the GGUF's
`tokenizer.chat_template` accepts exactly `xhigh`, `medium`, `low`, remaps `high` to `xhigh`, and raises for anything
else.  `enable_thinking: false` (or `reasoning_effort: none`, which llama.cpp maps to it) removes the reasoning block.

Measured reasoning lengths on a fixed arithmetic prompt, greedy, 180-token cap:

| effort | reasoning characters |
| --- | --- |
| `none` | 0 |
| `low` | 408 |
| `medium` | 372 |
| `high` | 145 |
| `xhigh` | 137 |
| *(absent - template default `xhigh`)* | 199 |

**Effort does not order monotonically on this model**: `low` produced the longest trace and `xhigh` one of the shortest.
The template only supplies instruction strength, and the model's compliance with it varies.  So "low effort" is not a
reliable way to make this model think less - it is a *nudge*, and the only reliable switch is off (`none`) versus on.
Recorded rather than papered over, because a UI that offers five levels implies an ordering the model does not honour.

For `pi`: `~/.pi/agent/models.json` now carries `reasoning: true`, `compat.supportsReasoningEffort: true` and a
`thinkingLevelMap` of `off→none, minimal→low, low→low, medium→medium, high→high`.  `minimal` maps to `low` because the
template has no such string and an unmapped level would send one the template rejects.

## Sizing reference (unchanged from the plan)

| | IQ3_XXS | IQ3_S |
| --- | --- | --- |
| Host RAM for experts + engine | ~46 GiB | ~52 GiB (the host's ceiling; IQ4-class does not fit) |
| Startup | ~2 min cold | ~2 min cold |

## Where the knowledge lives

| Doc | Contents |
| --- | --- |
| `docs/DEPLOY-DOCKER.md` | image design, the compose fields that matter, the `--gpus` host bug, validation checklist |
| `docs/COOLING.md` | the GDDR6X baseline, the LACT VRAM-keyed fan curve, and the CoolerControl setup for the case fans |
| `docs/DUAL-GPU.md` | the measurement record: matrix, soaks, rate curve, shift test, "where this stops and why" |
| `docs/FORK-STRATEGY.md` | thin-fork policy, `STRATA-FORK` markers, why the shim exists |
| `docs/PLAN-*.md` | rebase, CPU-kernel and batching plans (the latter two are parked) |
| `tools/` | `strata-dual` (the launch policy) and every probe that produced the numbers |

`main` is on 0.1.45 with the fork's work; `legacy-0.1.39` is the old line; `dual-gpu-0.1.45` tags the state.
