# syntax=docker/dockerfile:1
#
# Two stages: build the fork (CUDA + CPU expert kernels) against the pinned llama.cpp tree, then ship only the
# binaries into the matching CUDA runtime image.  See docs/DEPLOY-DOCKER.md for why this is a source build.
#
# Build:  docker compose build
# Run:    docker compose up -d

ARG CUDA_VERSION=13.4.3
ARG UBUNTU=ubuntu24.04

FROM nvidia/cuda:${CUDA_VERSION}-devel-${UBUNTU} AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

# sm_86 = RTX 3090 (primary), sm_89 = RTX 4070 (peer).  Tests are off: they need an hour and are not runtime deps.
RUN cmake -S /src -B /build \
        -DCMAKE_BUILD_TYPE=Release \
        -DSTRATA_ENABLE_CUDA=ON \
        -DSTRATA_NATIVE_EXPERTS=ON \
        -DSTRATA_BUILD_TESTS=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="86;89" \
        -DSTRATA_GGML_DIR=/src/third_party/llama.cpp \
 && cmake --build /build --target strata unbiased-strata peer_p2p_probe -j "$(nproc)"

FROM nvidia/cuda:${CUDA_VERSION}-runtime-${UBUNTU}

ENV DEBIAN_FRONTEND=noninteractive
# bash/coreutils/gawk: the strata-dual shim; curl: the compose healthcheck.  nvidia-smi is injected by the runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils gawk curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /build/strata /build/unbiased-strata /build/peer_p2p_probe /opt/strata/
COPY tools/strata-dual /opt/strata/strata-dual
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /opt/strata/strata-dual /opt/strata/strata /opt/strata/unbiased-strata \
               /opt/strata/peer_p2p_probe /usr/local/bin/entrypoint.sh

# The shim finds its helpers here instead of guessing a build directory.
ENV STRATA_FRONTEND=/opt/strata/unbiased-strata \
    STRATA_P2P_PROBE=/opt/strata/peer_p2p_probe \
    CUDA_DEVICE_ORDER=PCI_BUS_ID \
    STRATA_MTP=/models/mtp \
    STRATA_CTX=262144 \
    STRATA_KV=q4_0 \
    STRATA_PORT=8080

EXPOSE 8080

# tini reaps the engine child if it outlives the front end; the entrypoint execs the shim so SIGTERM reaches it.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
