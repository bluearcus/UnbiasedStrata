// tests/core/peer_promotion_test.cpp - the peer-to-primary promotion rule (Phase 5-adjacent, GPU-free).
#include "strata/core/peer_promotion.hpp"

#include <cstdio>
#include <string>
#include <vector>

namespace {
int failures = 0;
void check(bool ok, const std::string& what) {
    if (!ok) { ++failures; std::printf("FAIL: %s\n", what.c_str()); }
}
constexpr int32_t NR = strata::core::kNotResident;
}  // namespace

int main() {
    using strata::core::Promotion;
    using strata::core::select_promotions;
    // one layer, six experts: primary holds 0..3 with descending heat, peer holds 4 and 5.
    // primary heats: 100 80 60 40 -> bar = sorted[(4-1)/3 = 1] = 80, worst = 40.
    const int64_t L = 1, N = 6;
    std::vector<float> usage{100, 80, 60, 40, 90, 10};
    std::vector<int32_t> r0{0, 1, 2, 3, NR, NR};
    std::vector<int32_t> r1{NR, NR, NR, NR, 7, 8};
    std::vector<int32_t> last_forced((size_t)(L * N), NR);
    std::vector<Promotion> out;

    // expert 4 (heat 90 > bar 80, beats worst 40 + 1.5) is nominated; expert 5 (heat 10) is not
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 100, 8, 96, out) == 1 &&
              out[0].expert == 4 && out[0].layer == 0,
          "a peer expert in the primary's top third is nominated");
    // below the bar: heat 70 < 80 stays on the peer even though it beats the worst resident + 1.5
    usage[4] = 70;
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 100, 8, 96, out) == 0,
          "a peer expert below the top-third bar is not nominated");
    // above the bar but not beating the worst resident + 1.5: plateau, nothing to gain
    usage[0] = usage[1] = usage[2] = usage[3] = 89;
    usage[4] = 90;   // > bar 89 (sorted[(4-1)/3=1] = 89) but 90 < 89 + 1.5
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 100, 8, 96, out) == 0,
          "a peer expert inside the margin plateau is not nominated");
    usage[0] = 100; usage[1] = 80; usage[2] = 60; usage[3] = 40; usage[4] = 90;
    // primary-held is never nominated, even at top heat
    r1[0] = 9;
    usage[0] = 200;
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 100, 8, 96, out) == 1 &&
              out[0].expert == 4,
          "a primary-held expert is never nominated");
    r1[0] = NR;
    usage[0] = 100;
    // cooldown: nominated at round 100, skipped at round 105, eligible again at 108
    last_forced[4] = 100;
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 105, 8, 96, out) == 0,
          "cooldown blocks immediate re-nomination");
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 108, 8, 96, out) == 1,
          "nomination re-arms after the cooldown");
    last_forced[4] = NR;
    // the per-round cap keeps the best gains: two hot peer experts, cap 1 takes heat 95 over 90
    usage[4] = 90; usage[5] = 95;
    r1[5] = 9;
    check(select_promotions(usage.data(), r0.data(), r1.data(), L, N, last_forced.data(), 200, 8, 1, out) == 1 &&
              out[0].expert == 5,
          "the cap keeps the best gain first");
    r1[5] = NR; usage[5] = 10;
    // a layer with no primary-held experts is skipped, not divided by zero
    std::vector<float> u2(N, 50);
    std::vector<int32_t> z0(N, NR), z1(N, 0);
    check(select_promotions(u2.data(), z0.data(), z1.data(), L, N, last_forced.data(), 200, 8, 96, out) == 0,
          "a layer with no primary residents is skipped");

    if (failures == 0) std::printf("peer_promotion_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
