// tools/peer_p2p_probe.cu - does CUDA device 0 have direct (P2P) access to device N, both ways?
//
//   peer_p2p_probe N     prints "p2p yes" or "p2p no", exit 0; "p2p unknown: <reason>", exit 2 when the
//                        question cannot be answered (no CUDA, no such device).
//
// The engine probes this itself in PeerExperts::open and logs the verdict at startup; this standalone copy exists
// so the strata-dual wrapper can answer it BEFORE a 40 GB load and pick --peer-prefill-rows accordingly.
#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: peer_p2p_probe N\n"); return 2; }
    const int peer = std::atoi(argv[1]);
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess || count < 1) {
        std::printf("p2p unknown: no CUDA device is visible\n");
        return 2;
    }
    if (peer < 1 || peer >= count) {
        std::printf("p2p unknown: device %d is not visible (%d devices)\n", peer, count);
        return 2;
    }
    int fwd = 0, back = 0;
    if (cudaDeviceCanAccessPeer(&fwd, 0, peer) != cudaSuccess ||
        cudaDeviceCanAccessPeer(&back, peer, 0) != cudaSuccess) {
        (void) cudaGetLastError();
        std::printf("p2p unknown: the probe failed\n");
        return 2;
    }
    std::printf("p2p %s\n", (fwd && back) ? "yes" : "no");
    return 0;
}
