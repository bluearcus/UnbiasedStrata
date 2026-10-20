# Deploying the rig as a Docker Compose service — plan

Decisions already taken by the user: **Docker with compose**, not systemd. `docs/SERVICE.md` keeps the launch facts
and the sizing numbers; this file is the container-specific design.

## 1. Facts established on this host (not assumptions)

| Check | Result | Consequence for the design |
| --- | --- | --- |
| Docker / Compose | 29.9.1 / v5.6.0, NVIDIA runtime registered | usable as-is |
| `docker run --gpus all ...` | **fails**: toolkit tries to mount `/usr/lib/x86_64-linux-gnu/libnvidia-egl-wayland.so.1.1.21`, which this host does not ship (it has `libnvidia-egl-wayland2.so.1.0.2`, toolkit 1.20.1) | **do not use `--gpus` / compose `device_requests`** until the host packaging is fixed |
| `--runtime nvidia` + `NVIDIA_VISIBLE_DEVICES=all` + `NVIDIA_DRIVER_CAPABILITIES=compute,utility` | **works** - `nvidia-smi` in-container lists both cards | compose uses `runtime: nvidia` plus those two env vars |
| Binary deps (`ldd`) | `libcudart.so.13`, `libcublas.so.13`, `libcublasLt.so.13` | the runtime image must be `nvidia/cuda:13.4.x-**runtime**-ubuntu24.04` (the `base` images lack cuBLAS) |
| Build inputs | cmake, nvcc 13.4, g++, git, submodule `third_party/llama.cpp` pinned at `a6de9af3b` | multi-stage build from this tree; `-DCMAKE_CUDA_ARCHITECTURES="86;89"` |
| Model files | 132 GB under `/home/mike/models`; runtime needs shards + `pack-*` + `mtp` | one read-only bind mount at `/models` |
| Host RAM needed | ~46 GiB (XXS) / ~52 GiB (S) | no container memory limit, or a generous one; the page cache matters for `--ple-io direct` |
| Startup | ~2 min from NVMe | `healthcheck.start_period` must be minutes, not seconds |
| 4070 drives the display | peer reserve 2500 MiB | keep `--peer-reserve-mib 2500` (the shim's default) |

The `--gpus` failure is a host packaging bug (the toolkit expects an EGL soname the driver no longer ships). The
`runtime: nvidia` + `compute,utility` path avoids the graphics libs entirely, which is the correct capability set for
a compute container anyway. If `--gpus` is ever wanted, the host fix is `sudo apt install --reinstall
libnvidia-egl-wayland1` (or aligning the toolkit/driver versions) - a root change, not part of this plan.

## 2. Image design (multi-stage)

```
Dockerfile
  stage build   nvidia/cuda:13.4.3-devel-ubuntu24.04
                apt: build-essential cmake git curl
                COPY . /src            (the whole tree, submodule included)
                cmake -S /src -B /build -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON \
                      -DSTRATA_GGML_DIR=/src/third_party/llama.cpp -DSTRATA_BUILD_TESTS=OFF \
                      -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES="86;89"
                cmake --build /build --target strata unbiased-strata peer_p2p_probe -j N

  stage runtime nvidia/cuda:13.4.3-runtime-ubuntu24.04
                apt: ca-certificates curl bash coreutils gawk tini  (shim needs bash/awk; curl = healthcheck)
                COPY --from=build /build/strata /build/unbiased-strata /build/peer_p2p_probe /opt/strata/
                COPY tools/strata-dual /opt/strata/
                COPY docker/entrypoint.sh /usr/local/bin/strata-entrypoint
                ENV STRATA_FRONTEND=/opt/strata/unbiased-strata \
                    STRATA_P2P_PROBE=/opt/strata/peer_p2p_probe \
                    CUDA_DEVICE_ORDER=PCI_BUS_ID
                ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/strata-entrypoint"]
                # tini is belt-and-braces: the entrypoint already `exec`s the shim, so SIGTERM reaches the front
                # end, which sends QUIT.  tini only reaps the engine if it ever outlives its parent.
```

Why build inside the image rather than mount the host's `build-rk/`: reproducibility and a self-contained image are
worth one 15-20 minute build; a bind-mounted binary is only a good idea for iteration, and can be a second compose
override (`docker/compose.dev.yaml`) if wanted.

Options worth deciding at build time: pruning the image (the devel stage is thrown away; the runtime stage lands
around 3-4 GB with the CUDA runtime), and whether to embed the two `--engine`-adjacent helper scripts.

## 3. Entrypoint

A small shell script turns container env into the shim's arguments (the shim already assembles the engine flags and
the peer defaults - do not duplicate that logic):

| Env | Default | Meaning |
| --- | --- | --- |
| `STRATA_MODEL` | (required) | `/models/flash-next/IQ3_XXS/...-00001-of-00002.gguf` or the S equivalent |
| `STRATA_PACK` | (required) | `/models/pack-iq3-xxs` or `/models/pack-iq3-s` |
| `STRATA_MTP` | `/models/mtp` | draft head |
| `STRATA_CTX` | `262144` | `--max-context` |
| `STRATA_KV` | `q4_0` | `--kv` |
| `STRATA_PEER_DEVICE` | `1` | peer ordinal; empty disables the peer |
| `STRATA_PORT` | `8080` | listener |

The entrypoint binds **`--host 0.0.0.0`** inside the container (the host publishes to `127.0.0.1:8080`), and `exec`s
the shim so SIGTERM reaches it (the front end then sends `QUIT` to the engine - `stop_grace_period` must allow the
engine to unwind).

## 4. Compose sketch (only the fields that matter)

```yaml
services:
  strata:
    build: { context: . }
    image: unbiased-strata:0.1.45
    runtime: nvidia                     # NOT device_requests: see the --gpus note above
    environment:
      NVIDIA_VISIBLE_DEVICES: all
      NVIDIA_DRIVER_CAPABILITIES: compute,utility
      CUDA_DEVICE_ORDER: PCI_BUS_ID
      STRATA_MODEL: /models/flash-next/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf
      STRATA_PACK: /models/pack-iq3-xxs
    ports: ["127.0.0.1:8080:8080"]
    volumes: ["/home/mike/models:/models:ro"]
    ipc: host
    ulimits: { memlock: -1, stack: 67108864 }
    restart: unless-stopped
    stop_grace_period: 120s            # the engine unloads ~47 GB on QUIT; 60 s was too tight
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 300s                # the engine loads ~47 GB before it answers
```

Two further choices to make explicit:

- **Quant selection: one service, env override - not two profile services.** The first sketch here proposed
  `profiles: [xxs]` / `[s]`, which is unsafe: an unprofiled service always starts, so `--profile s up` would launch
  *both* engines and two models cannot share the VRAM. Instead `STRATA_MODEL`/`STRATA_PACK` are `${VAR:-default}`
  in one service, so the quant is a one-line override and the two-engines case cannot be expressed:
  `STRATA_MODEL=…IQ3_S… STRATA_PACK=/models/pack-iq3-s docker compose up -d`. Same image, no rebuild.
- **Auth.** Bind to loopback and put `api_key` in the run config / `STRATA_API_KEY` only if something off-host will
  reach it; the compose ports line already keeps it on the host.

## 5. Validation checklist (in order, each with the expected result)

1. `docker compose build` - image builds; note the runtime image size.
2. `docker compose up -d` then `docker compose logs -f` - the shim's `peer GPU 1, p2p no, reserve 2500 MiB` line, then
   `READY 262144 stop`.
3. `docker compose ps` - healthy only after ~2 min (`start_period` 300 s).
4. `curl 127.0.0.1:8080/health` - `tiers` present with `vram_slots ~9407`, `peer_slots ~5300`, `peer_p2p false`.
5. 40-token chat completion - non-empty content; then a 400-token generation: **~90-100 tok/s** (40-50 would mean a
   demoted tier or two slots).
6. Inside the container: `nvidia-smi` shows both cards, and the 3090 is device 0 (peer ordinal 1 must be the 4070).
7. `docker compose restart strata` - comes back without touching the desktop's compositor; the 4070's VRAM returns to
   its pre-service level (reserve honoured).
8. Failure behaviour: stop the service while a request is in flight (`stop_grace_period`) - no orphaned engine, no
   VRAM left held.
9. Reboot test - `restart: unless-stopped` brings it up; the models volume must be mounted before the engine starts
   (it is, by definition of a bind mount).

## 6. Risks and open questions

- **Host packaging bug** (`libnvidia-egl-wayland.so.1.1.21`): the compose design routes around it, but a host upgrade
  or toolkit reinstall could change which path works. Keep the `runtime: nvidia` + capabilities approach and record
  the symptom in the doc so it is not re-diagnosed later.
- **CUDA version coupling**: the image is built with 13.4 toolkits against a 595.x driver; a driver downgrade would
  break the image, and a newer driver is fine (backwards compatible). Pin the image tag, not `latest`.
- **Memory accounting**: no container memory limit means an engine failure can still pressure the host (we lost a
  terminal to an OOM once). Consider `mem_limit` around 56g as a guard - but note the page cache is what makes
  `--ple-io direct` and mmap expert reads fast, so a hard limit that counts page cache could hurt more than help;
  measure before setting it.
- **The desktop shares the 4070**: reserve 2500 MiB is the knob; if the desktop gets busier, the peer's slots shrink
  (it logs the figure at start).
- **Model files must be on an SSD** for `--ple-io direct` (the shim defaults it under a peer). A rotational disk would
  make prompt reads pathological - the engine warns about this itself.
- **Build time in-container** (~15-20 min for sm_86;89 on 16 threads) and image size (~3-4 GB runtime stage) are the
  practical annoyances; a prebuilt image pushed to a registry would remove the rebuild from every deploy.

## 7. Order of work

1. Write `docker/entrypoint.sh`, `Dockerfile`, `.dockerignore`, `compose.yaml` (nothing but files).  Done; the
   deviations from the first sketch above are deliberate and recorded.
2. Build; fix only build errors (no tuning).
3. Bring it up; run the validation checklist end to end.
4. Add the `s` profile and re-run items 3-5 for it.
5. Record the deployed configuration in `docs/SERVICE.md` (replacing the systemd framing) and note the `--gpus` host bug.

## 8. As built — results of the validation checklist

| Step | Result |
| --- | --- |
| `docker compose build` | **~19 min**, image 5.35 GB (devel stage discarded) |
| `up -d`, logs | shim: `peer GPU 1, p2p no, reserve 2500 MiB`; engine: `peer GPU 1: 5324 experts, 8.60 GiB … 14731 of 24576 on the GPUs`; then `listening on http://0.0.0.0:8080` |
| health | healthy; tiers `vram_slots 9407`, `peer_slots 5326`, `peer_p2p false`, ctx 262144 |
| chat + throughput | 385-token streaming reply: **108.1 tok/s** decode (host mean 98.1), TTFT 1.16 s |
| GPU mapping | container device 0 = RTX 3090 (23,780 MiB used), device 1 = RTX 4070 (9,582 MiB incl. desktop) |
| `compose restart` | healthy again in **32 s** (page cache warm); slots unchanged |
| quant override (`STRATA_MODEL`/`STRATA_PACK` = IQ3_S) | recreated in place, healthy after 36 s, slots 7,869 + 4,518, **97.9 tok/s** decode; back to XXS with plain `up -d` |
| stop with a request in flight | VRAM released (3090 2 MiB, 4070 254 MiB), no orphaned process; container exit **134** (`terminate called without an active exception`) |
| idle stop | exit **1** — by design: the front end's supervisor returns non-zero when the engine goes away, so a service manager restarts both |
| reboot | not performed (would interrupt the user's session); `restart: unless-stopped` is set and both stop paths are clean of orphans |

Two findings for the record:

* **Exit codes are intentionally non-zero.** `frontend/main.cpp` returns `engine_died ? 1 : 0`, so a stop is not a
  clean `0`. `restart: unless-stopped` distinguishes engine failure (restart) from an explicit stop (stay down).
* **Exit 134 on the in-flight cancel path** is a `std::terminate` during teardown, after VRAM is released. Worth
  reporting upstream rather than patching here - it cannot leave a half-held card.
