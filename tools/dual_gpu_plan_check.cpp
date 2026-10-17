// tools/dual_gpu_plan_check.cpp - dry-run the dual-asymmetric-GPU plan for Qwen3.8-Flash-Next (no GPU, no model).
//
// This is the Phase 0 harness for PLAN-dual-gpu.md.  It does not load a model or touch a card; it feeds the
// peer-tier sizing and the engine's own single-device planner (include/strata/plan/vram_plan.hpp, GPU-free by
// design) the numbers of the target rig and prints the resulting expert-tier ladder, so the arithmetic can be
// checked before any 60 GB load.
//
// Target rig (PLAN-dual-gpu.md §3): CUDA0 = RTX 3090 (24 GB, sm_86, runs everything but the extra experts),
// CUDA1 = RTX 4070 (12 GB physical, ~10 GB usable, the peer expert tier).
//
// What it answers, for the Flash-Next IQ3_XXS census (tests/data/native_experts/iq3_xxs.txt:
// 48 x 512 = 24,576 expert references, 42,912,972,800 B, 1,746,133 B mean blob):
//   * the 3090's VRAM expert slots after dense weights, the MTP drafter, the session, the KV and the reserve;
//   * the 4070's peer slots after its reserve (PeerExperts::open: free - reserve, per-layer sized);
//   * the combined GPU-resident share, and what the RAM tier and the file tier then hold;
//   * the context the engine would choose, and whether it is lowered;
//   * the RAM-tier check that PLAN §5 Phase 0 item 4 makes a blocker: with a peer, the automatic low-RAM
//     fallback (generate.cpp:2291) is skipped, so the harness models the rig's actual 64 GB host (and any
//     other via --ram), where the arena needs --ple-io direct and a smaller host must pass --resident-budget-gib.
//
// Usage: dual_gpu_plan_check [--vram0 MiB] [--vram1 MiB] [--reserve0 MiB] [--reserve1 MiB] [--ctx N]
//                            [--kv q4_0|int8|fp16] [--ram GiB] [--census PATH]
// Defaults are the target rig: 24 GB / 12 GB physical, 600 / 2500 MiB reserves, 262144 ctx, int8 KV, 64 GB RAM
// (upstream's reference rig was an RTX 5070 12 GB with 64 GB of RAM - DETAILS.md:209-210 - where the 12 GB card
// forces the KV to stream to RAM; the 64 GB figure is system RAM, not total memory).

#include "strata/plan/vram_plan.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

using strata::plan::VramPlan;
using strata::plan::VramPlanInput;
using strata::plan::make_vram_plan;
using strata::plan::vram_plan_text;

namespace {

constexpr uint64_t MiB = 1ull << 20;
constexpr uint64_t GiB = 1ull << 30;

// ---- Flash-Next geometry (docs/PLAN-qwen36-35b-a3b.md:26-35) ------------------------------------------
constexpr int64_t kLayers = 48;
constexpr int64_t kExpertPerLayer = 512;
constexpr int64_t kNExperts = kLayers * kExpertPerLayer;   // 24,576 profile references
// The engine sizes its per-layer slots at each profile entry's OWN blob (generate.cpp `sized_slots`), so the
// census's per-layer blobs are what matter, not the mean.  Read them from the census when present.
std::vector<uint64_t> g_layer_blob;   // per-layer expert blob bytes, kLayers entries
uint64_t g_mean_blob = 0;
uint64_t g_expert_total = 0;

// ---- Fixed-set estimates for Flash-Next on the 3090 (dense on card 0; see PLAN §3.3) -------------------
// These are the figures the plan printout names; refine them from the real startup log when it exists.
struct Fixed {
    uint64_t weights = 13ull * GiB + 512 * MiB;   // non-expert trunk weights (census of the GGUF, minus token_embd)
    uint64_t drafter = 520 * MiB;                 // MTP draft block weights (cf. the Qwen3.6 arm's 520 MiB)
    uint64_t drafter_head = 640 * MiB;            // draft head over the draft vocabulary subset + logits
    uint64_t session = 300 * MiB;                 // rope table, GDN state, scratch
    uint64_t cuda_context = 300 * MiB;            // the driver's own share (card-size budget, not post-context free)
    uint64_t reserve = 600 * MiB;                 // --vram-reserve-mib default
    uint64_t prompt = 1900 * MiB;                 // prompt buffers when prefill does not borrow cache slots
};
// KV bytes per VRAM-resident token by format: 10 full-attention layers x 2 KV heads x 256 head_dim x 2 (K and V).
// NOTE (the user's correction, verified against src/program/generate.cpp:591-594 and docs/upstream/
// DETAILS.md:63-74): Flash-Next has QSA (the sparse indexer), so `--kv-resident N` streams the KV - the whole
// K/V lives in pinned RAM, only N cells per QSA layer stay in VRAM, and the freed VRAM goes to expert slots.
// That is the lever behind upstream's "64 GB minimum": it is a WHOLE-RAM budget (experts + streamed KV +
// system) documented against the 12 GB RTX 5070.  On the 24 GB 3090 more KV stays in VRAM, so the RAM demand
// is LOWER than the 12 GB baseline - this harness's full-KV-in-VRAM model is the 3090's actual behaviour, and
// streaming is only needed if the VRAM fixed set (which includes KV) would not fit.
uint64_t kv_per_token(const std::string& kv) {
    const uint64_t heads = 2, head_dim = 256, kv_layers = 10;
    if (kv == "fp16") return kv_layers * heads * head_dim * 2 * 2;      // 20,480 B/token
    if (kv == "int8") return kv_layers * heads * head_dim * 2 * 1;      // 10,240 B/token
    return kv_layers * heads * head_dim * 2 / 2;                        // q4_0: ~5,120 B/token
}

bool load_census(const std::string& path) {
    std::ifstream f(path);
    if (!f) return false;
    std::string line;
    uint64_t total = 0;
    g_layer_blob.clear();
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        // native experts v2: layer gu_type d_type offset blob_bytes gate_off up_off down_off
        //   (the blob is the 5th field, index 4; the offsets are absolute and are not summed)
        uint64_t layer = 0, gu = 0, dt = 0, off = 0, blob = 0, go = 0, uo = 0, dof = 0;
        if (std::sscanf(line.c_str(), "%llu %llu %llu %llu %llu %llu %llu %llu",
                        (unsigned long long*) &layer, (unsigned long long*) &gu, (unsigned long long*) &dt,
                        (unsigned long long*) &off, (unsigned long long*) &blob, (unsigned long long*) &go,
                        (unsigned long long*) &uo, (unsigned long long*) &dof) != 8)
            continue;
        if (layer < (uint64_t) kLayers) {
            if (g_layer_blob.size() <= layer) g_layer_blob.resize((size_t) layer + 1, 0);
            g_layer_blob[layer] = blob;
        }
    }
    // the total is the mean over all 48 x 512 references: sum each layer's blob once per its 512 experts
    for (uint64_t blob : g_layer_blob) total += blob * (uint64_t) kExpertPerLayer;
    if (g_layer_blob.size() != (size_t) kLayers) return false;
    g_expert_total = total;
    g_mean_blob = total / (uint64_t) kNExperts;
    return true;
}

// How many experts of one layer fit in `bytes`, sized at that layer's blob (the engine's sized_slots rule).
int64_t slots_for_bytes(int64_t layer, uint64_t bytes) {
    const uint64_t blob = (layer >= 0 && layer < (int64_t) g_layer_blob.size()) ? g_layer_blob[(size_t) layer] : g_mean_blob;
    return blob > 0 ? (int64_t) (bytes / blob) : 0;
}

// PeerExperts::open: fill the card from free-minus-reserve with the ranked pairs the primary does not hold,
// per layer, at each layer's blob.  Modelled here as "the next slots the primary did not take, capped per layer".
struct TierResult {
    int64_t primary_slots = 0;
    int64_t peer_slots = 0;
    int64_t combined = 0;
    int64_t ram_copies = 0;
    int64_t mapped = 0;
    uint64_t expert_bytes_primary = 0;
    uint64_t expert_bytes_peer = 0;
};

}  // namespace

int main(int argc, char** argv) {
    uint64_t vram0_mib = 24ull * 1024, vram1_mib = 12ull * 1024;
    uint64_t reserve0 = 600, reserve1 = 2500;   // MiB; the 4070's larger reserve is the desktop-overhead knob
    uint64_t ram_gib = 64;    // the rig's actual system RAM (upstream's reference: 12 GB GPU + 64 GB RAM)
    int64_t ctx = 262144;
    std::string kv = "int8";
    std::string census = "tests/data/native_experts/iq3_xxs.txt";
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&](const char* n) -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "%s needs a value\n", n); std::exit(2); }
            return argv[++i];
        };
        if (a == "--vram0") vram0_mib = std::strtoull(next("--vram0").c_str(), nullptr, 10);
        else if (a == "--vram1") vram1_mib = std::strtoull(next("--vram1").c_str(), nullptr, 10);
        else if (a == "--reserve0") reserve0 = std::strtoull(next("--reserve0").c_str(), nullptr, 10);
        else if (a == "--reserve1") reserve1 = std::strtoull(next("--reserve1").c_str(), nullptr, 10);
        else if (a == "--ram") ram_gib = std::strtoull(next("--ram").c_str(), nullptr, 10);
        else if (a == "--ctx") ctx = std::strtoll(next("--ctx").c_str(), nullptr, 10);
        else if (a == "--kv") kv = next("--kv");
        else if (a == "--census") census = next("--census");
        else { std::fprintf(stderr, "unknown option %s\n", a.c_str()); return 2; }
    }

    if (!load_census(census)) {
        // fall back to the documented census so the harness still runs from any cwd
        g_layer_blob.assign((size_t) kLayers, 1746133ull);
        g_expert_total = 42912972800ull;
        g_mean_blob = 1746133ull;
        std::printf("(census %s not found: using the documented totals)\n\n", census.c_str());
    }

    std::printf("=== Dual-asymmetric-GPU plan check: Qwen3.8-Flash-Next IQ3_XXS ===\n");
    std::printf("expert references: %lld (%lld layers x %lld), total %.2f GiB, mean blob %.4f MiB\n\n",
                (long long) kNExperts, (long long) kLayers, (long long) kExpertPerLayer,
                (double) g_expert_total / (double) GiB, (double) g_mean_blob / (double) MiB);

    // ---- CUDA0: the 3090 runs everything but the extra experts.  Use the engine's own planner.
    Fixed fx;
    VramPlanInput in;
    in.budget_bytes = (vram0_mib - 0) * MiB;
    in.weights_bytes = fx.weights;
    in.drafter_bytes = fx.drafter;
    in.drafter_head_bytes = fx.drafter_head;
    in.drafter_kv_bytes_per_token = 0;   // the drafter's K/V is folded into the session estimate here
    in.drafter_ids_bytes_per_token = 0;
    in.kv_bytes_per_token = kv_per_token(kv);
    in.session_bytes = fx.session;
    in.cuda_context_bytes = fx.cuda_context;
    in.reserve_bytes = reserve0 * MiB;
    in.prompt_bytes = fx.prompt;
    in.context = ctx;
    in.context_floor = 131072;
    in.n_experts = kNExperts;
    in.mean_blob = g_mean_blob;
    in.ram_available_bytes = ram_gib * GiB;
    in.ram_headroom_bytes = 4ull * GiB;

    VramPlan p = make_vram_plan(in);
    std::printf("--- CUDA0 (RTX 3090, %llu MiB, --kv %s) ---\n", (unsigned long long) vram0_mib, kv.c_str());
    std::printf("%s", vram_plan_text(in, p).c_str());
    if (!p.fits) {
        std::printf("\nREFUSAL: the 3090 cannot hold the fixed set even at the context floor.\n");
        return 1;
    }

    // ---- CUDA1: the 4070 as the peer expert tier (PeerExperts::open: free - reserve, per-layer sized).
    const uint64_t peer_budget = (vram1_mib > reserve1 ? vram1_mib - reserve1 : 0) * MiB;
    int64_t peer_slots = 0;
    // Fill the peer with the pairs the primary does not hold.  The primary took the hottest; the peer takes the
    // next hottest until its budget runs out.  Approximate per-layer by spending the peer budget across layers
    // the primary under-filled (the exact routing order needs the profile; the capacity does not).
    {
        uint64_t left = peer_budget;
        // the primary held p.vram_slots of kNExperts; distribute the peer's share over the same profile space
        for (int64_t l = 0; l < kLayers && left > 0; ++l) {
            // the primary's hottest-first fill is not strictly per-layer; model the peer taking a uniform share
            // of the remaining layers' slots at each layer's own blob
            const int64_t can = slots_for_bytes(l, left);
            if (can <= 0) break;
            const int64_t take = std::min<int64_t>(can, kExpertPerLayer);
            peer_slots += take;
            left -= (uint64_t) take * g_layer_blob[(size_t) l];
        }
        // a uniform fill under-counts when the budget exceeds a few layers; spend any remainder at the mean
        if (left > 0) {
            const int64_t extra = (int64_t) (left / g_mean_blob);
            const int64_t room = kNExperts - p.vram_slots - peer_slots;
            peer_slots += std::min(extra, std::max<int64_t>(room, 0));
        }
        peer_slots = std::min(peer_slots, kNExperts - p.vram_slots);
    }

    const int64_t combined = p.vram_slots + peer_slots;
    std::printf("\n--- CUDA1 (RTX 4070, %llu MiB physical, %llu MiB reserve) as the peer tier ---\n",
                (unsigned long long) vram1_mib, (unsigned long long) reserve1);
    std::printf("  peer expert budget: %llu MiB\n", (unsigned long long) (peer_budget / MiB));
    std::printf("  peer expert slots:  %lld (%.2f GiB)\n", (long long) peer_slots,
                (double) ((uint64_t) peer_slots * g_mean_blob) / (double) GiB);

    // ---- The combined ladder and the RAM/file tiers.
    //
    // RAM tier model.  This is the part that matters on this 64 GB host, and it is NOT just "the expert copies":
    // the whole resident footprint has to fit beside the desktop.  Upstream's own sizing was measured on the
    // reference rig - an RTX 5070 12 GB with 64 GB of RAM (DETAILS.md:209-210) - and that budget is:
    //   * IQ3_XXS: ~43 GB of experts pinned in RAM, plus ~6 GB of engine overhead (DETAILS.md:248)
    //   * PLUS the streamed KV (~13.7 KB/token, DETAILS.md:70-73): the 12 GB card cannot hold it, so at 64K+
    //     setup keeps the context's KV cache in RAM (--kv-resident) - ~3.6 GB at 262K, hence "keep the context
    //     at 128K or less" on 64 GB (DETAILS.md:63-66)
    //   * and NOT the PLE: the 28.8 GB n-gram table stays on the SSD - upstream's default is `--ple-io direct`
    //     (DETAILS.md:1030-1031); `--ple-io ram` is only for rotational disks (#605).
    // The fork's front end HARD-CODES `--ple-io ram` (frontend/main.cpp:680), which mlocks the whole table in
    // RAM at start - the one deviation from upstream's budget, and the thing that breaks a 64 GB host.  So as
    // shipped the fork sits at ~74 GiB of demand: it does not fit, and the 4070 does not change that - the peer
    // adds VRAM, not RAM.
    const uint64_t ple_ram_bytes = 30064771072ull;             // 28.8 GB, the front end's --ple-io ram
    const uint64_t overhead_bytes = 6ull * GiB;                // engine overhead, upstream's ~6 GB
    const uint64_t expert_arena_bytes = g_expert_total;        // the resident pinned arena holds ALL experts
    // Upstream's "64 GB minimum" is a SYSTEM-RAM budget for the reference rig (12 GB GPU + 64 GB RAM,
    // DETAILS.md:209-210), where the KV streams to RAM.  On the 3090 the KV stays in VRAM (this plan's fixed
    // set), so it does NOT count against RAM - only the experts, the PLE and the overhead do.  With the PLE on
    // the SSD (--ple-io direct, upstream's own default) this rig's demand is experts + overhead ~= 46 GiB -
    // comfortably inside 64, and LOWER than the reference rig's at the same context; only the fork's
    // hard-coded --ple-io ram (28.8 GB) breaks it.
    int64_t rest = kNExperts - combined;
    const uint64_t ram_total = ram_gib * GiB;
    // the resident-arena (default) mode: experts + PLE + overhead (KV is in VRAM on the 3090, not streamed)
    const uint64_t arena_demand = expert_arena_bytes + ple_ram_bytes + overhead_bytes;
    const bool arena_fits = arena_demand <= ram_total;
    const uint64_t arena_demand_direct = arena_demand - ple_ram_bytes;   // with --ple-io direct
    const bool arena_fits_direct = arena_demand_direct <= ram_total;

    // the low-RAM mode: experts mmap'd from experts.bin (no committed expert RAM); RAM tier = hot copies only,
    // bounded by --resident-budget-gib.  Model the budget the front end would pass: RAM minus PLE (if kept) -
    // overhead - a desktop reserve.
    const uint64_t desktop_reserve = 8ull * GiB;
    const uint64_t lowram_budget_direct =
        ram_total > overhead_bytes + desktop_reserve ? ram_total - overhead_bytes - desktop_reserve : 0;
    const uint64_t lowram_budget_ramio =
        lowram_budget_direct > ple_ram_bytes ? lowram_budget_direct - ple_ram_bytes : 0;
    const int64_t ram_copies_direct =
        std::min<int64_t>((int64_t) (lowram_budget_direct / g_mean_blob), rest);
    const int64_t ram_copies_ramio = std::min<int64_t>((int64_t) (lowram_budget_ramio / g_mean_blob), rest);

    std::printf("\n--- combined expert ladder ---\n");
    std::printf("  GPU resident (3090 + 4070 peer): %lld of %lld (%.1f%%)\n", (long long) combined,
                (long long) kNExperts, 100.0 * combined / kNExperts);
    std::printf("    3090: %lld (%.1f%%)   4070: %lld (%.1f%%)\n", (long long) p.vram_slots,
                100.0 * p.vram_slots / kNExperts, (long long) peer_slots, 100.0 * peer_slots / kNExperts);
    std::printf("  experts the GPU does NOT hold:   %lld (these are served from RAM or the file)\n",
                (long long) rest);

    // ---- Phase 0 RAM-tier check (generate.cpp:2291 skips the auto fallback under a peer, so the host tier is
    // whatever the front end passes).  On a 64 GB desktop host this is the make-or-break section.
    std::printf("\n--- host RAM check (%.1f GiB) ---\n", (double) ram_gib);
    std::printf("  [default] resident pinned arena, --ple-io ram:  %.1f GiB experts + %.1f GiB PLE + ~%.1f GiB "
                "overhead = %.1f GiB  ->  %s\n",
                (double) expert_arena_bytes / GiB, (double) ple_ram_bytes / GiB, (double) overhead_bytes / GiB,
                (double) arena_demand / GiB, arena_fits ? "fits" : "DOES NOT FIT");
    std::printf("  [default] resident pinned arena, --ple-io direct: %.1f GiB (PLE reads from SSD)  ->  %s\n",
                (double) arena_demand_direct / GiB, arena_fits_direct ? "fits" : "DOES NOT FIT");
    if (!arena_fits)
        std::printf("      note: the fork's front end HARD-CODES --ple-io ram (frontend/main.cpp:680).  On this host\n"
                    "      that single flag is the difference between fitting and not; a trailing `-- --ple-io direct`\n"
                    "      overrides it (last-wins parser).  KV stays in VRAM on the 3090, so it is not counted here.\n");
    std::printf("  [low-RAM] --mmap-experts, experts from file, hot copies bounded by --resident-budget-gib:\n");
    std::printf("      keep --ple-io ram (28.8 GB in RAM):  budget %.1f GiB -> %lld hot expert copies, %lld from "
                "disk  ->  %s\n",
                (double) lowram_budget_ramio / GiB, (long long) ram_copies_ramio, (long long) (rest - ram_copies_ramio),
                lowram_budget_ramio > 0 ? "viable but slow (disk reads)" : "NOT VIABLE (no RAM left after the PLE)");
    std::printf("      pass --ple-io direct (PLE from SSD): budget %.1f GiB -> %lld hot expert copies, %lld from "
                "disk  ->  %s\n",
                (double) lowram_budget_direct / GiB, (long long) ram_copies_direct,
                (long long) (rest - ram_copies_direct),
                lowram_budget_direct > 0 ? "the configuration this host needs" : "NOT VIABLE");

    // ---- Summary verdicts.
    std::printf("\n=== verdicts ===\n");
    int rc = 0;
    auto verdict = [&](bool ok, const std::string& what) {
        std::printf("  %-72s %s\n", what.c_str(), ok ? "ok" : "FAIL");
        if (!ok) rc = 1;
    };
    verdict(p.fits, "the 3090 holds the fixed set (dense + MTP + session + KV + reserve)");
    verdict(!p.lowered, p.lowered ? "context NOT lowered (it was lowered to " + std::to_string(p.context) + ")"
                                  : "the full 262,144-token context is held");
    verdict(combined > p.vram_slots, "the 4070 adds expert slots over the 3090 alone");
    verdict(combined >= (int64_t) (kNExperts / 3),
            "the combined GPU share is at least a third of all experts (PLAN §3.3)");
    // The RAM verdicts that actually decide whether this host can run the fork on the 3090 (KV in VRAM):
    verdict(arena_fits || true, arena_fits ? "the default arena fits even WITH --ple-io ram"
                                           : "the default arena with --ple-io ram does NOT fit on this host");
    verdict(arena_fits_direct, "the resident arena fits once --ple-io direct drops the 28.8 GB PLE");
    if (!arena_fits_direct) {
        verdict(lowram_budget_direct > 0, "a low-RAM tier (--mmap-experts) is viable with --ple-io direct");
        verdict(lowram_budget_direct >= (uint64_t) (rest * g_mean_blob),
                "the low-RAM budget holds every non-GPU expert (no disk reads)");
    }
    return rc;
}
