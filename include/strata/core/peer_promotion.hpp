// include/strata/core/peer_promotion.hpp - promote hot peer-tier experts toward the faster card.
//
// The primary and peer VRAM tiers form a strict partition: the primary never admits what the peer holds and
// vice versa, so an expert that gets hot on the slower card stays there until it goes cold.  Startup profile
// rank puts the globally-hottest experts on the fast card, but a conversation that reshuffles popularity needs
// a way back up.  The rule: a peer-held expert whose heat ranks inside the top third of its layer's primary
// residents is nominated for promotion - the peer evicts it now (the CPU covers it meanwhile, like any swap),
// which makes it an ordinary primary candidate in the same adapt round, and the primary's gain-sorted machinery
// admits it if it beats a victim there.  Weights always come from the host source; the cards never copy
// residency between each other.
//
// Pure arithmetic, no CUDA calls: unit-tested by peer_promotion_test (tests/core/).
#pragma once

#include "strata/core/expert_cache.hpp"   // kNotResident; CUDA-free

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace strata::core {

/// One peer-held expert nominated for promotion to the primary tier in this adapt round.
struct Promotion {
    float gain;          ///< usage minus the layer's worst primary resident (the admission margin)
    int32_t layer, expert;
};

/// Selects up to `max_n` promotions, best gain first.  `usage`/`r0`/`r1` are the per-(layer, expert) heat and the
/// primary/peer residency tables; `last_forced` holds the adapt round each pair was last nominated in
/// (`kNotResident` = never).  Per layer: with n primary-held experts, the bar is the heat at rank n/3 (top
/// third); a peer-held, non-primary-held expert at heat u qualifies when u >= 2 (the admission floor), u is
/// strictly above the bar, u beats the layer's worst primary resident by 1.5 (the standard admission margin -
/// so the primary will actually take it this round instead of stranding it on the CPU), and its cooldown has
/// expired.  Layers with no primary-held experts are skipped: there is no ranking to compare against.
inline int select_promotions(const float* usage, const int32_t* r0, const int32_t* r1, int64_t n_layers,
                             int64_t n_expert, const int32_t* last_forced, int32_t round, int32_t cooldown_rounds,
                             int max_n, std::vector<Promotion>& out, float min_gain = 1.5f) {
    out.clear();
    if (max_n <= 0) return 0;
    std::vector<float> held;
    held.reserve((size_t) n_expert);
    for (int64_t l = 0; l < n_layers; ++l) {
        held.clear();
        const float* u = usage + l * n_expert;
        const int32_t* p0 = r0 + l * n_expert;
        const int32_t* p1 = r1 + l * n_expert;
        for (int64_t e = 0; e < n_expert; ++e)
            if (p0[e] >= 0) held.push_back(u[e]);
        if (held.empty()) continue;
        std::sort(held.begin(), held.end(), std::greater<float>());
        const float bar = held[(held.size() - 1) / 3];   // top-third boundary of the primary's residents
        const float worst = held.back();
        for (int64_t e = 0; e < n_expert; ++e) {
            if (p1[e] < 0 || p0[e] >= 0) continue;        // peer-held and not primary-held only
            const float heat = u[e];
            if (heat < 2.0f || !(heat > bar) || heat < worst + min_gain) continue;
            const int32_t lf = last_forced[l * n_expert + e];
            if (lf != kNotResident && round - lf < cooldown_rounds) continue;
            out.push_back({heat - worst, (int32_t) l, (int32_t) e});
        }
    }
    std::sort(out.begin(), out.end(), [](const Promotion& a, const Promotion& b) { return a.gain > b.gain; });
    if ((int) out.size() > max_n) out.resize((size_t) max_n);
    return (int) out.size();
}

}  // namespace strata::core
