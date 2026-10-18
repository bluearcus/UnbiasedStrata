// tests/core/vram_cap_test.cpp - the per-device --vram-cap parsing and clamp arithmetic (Phase 3, no GPU).
#include "strata/core/vram_cap.hpp"

#include <cstdio>
#include <string>

namespace {
int failures = 0;
void check(bool ok, const std::string& what) {
    if (!ok) { ++failures; std::printf("FAIL: %s\n", what.c_str()); }
}
}  // namespace

int main() {
    using strata::core::parse_vram_cap;
    using strata::core::vram_cap_mib_for;
    using strata::core::vram_capped_free;
    std::map<int, int64_t> caps;
    std::string err;

    // a bare number caps every card (the historical form still parses)
    check(parse_vram_cap("5722", caps, err) && caps.size() == 1 && caps[-1] == 5722, "bare MIB caps every card");
    check(vram_cap_mib_for(caps, 0) == 5722 && vram_cap_mib_for(caps, 1) == 5722, "bare MIB applies to each ordinal");

    // the asymmetric rig: 24 GB on card 0, 10 GB on card 1 (PLAN-dual-gpu.md Gate G5)
    check(parse_vram_cap("0:22000,1:10000", caps, err) && caps.size() == 2 && caps[0] == 22000 && caps[1] == 10000,
          "0:22000,1:10000 parses per ordinal");
    check(vram_cap_mib_for(caps, 0) == 22000 && vram_cap_mib_for(caps, 1) == 10000 && vram_cap_mib_for(caps, 2) == 0,
          "each ordinal keeps its own figure; an unlisted card is uncapped");

    // refusals: the front end validates the same shapes, so these are the engine's second line
    check(!parse_vram_cap("", caps, err), "empty is refused");
    check(!parse_vram_cap("0", caps, err), "a bare 0 is refused");
    check(!parse_vram_cap("0:abc", caps, err), "a non-number is refused");
    check(!parse_vram_cap("8:1000", caps, err), "ordinal 8 is out of range");
    check(!parse_vram_cap("0:0", caps, err), "MIB 0 is refused");
    check(!parse_vram_cap("0:1000,0:2000", caps, err), "a doubled device is refused");
    check(!parse_vram_cap("1000,0:2000", caps, err), "bare + pairs do not mix");
    check(!parse_vram_cap("0:1000,", caps, err), "a trailing comma is refused");

    // the clamp: a card of the cap with `used` in use by others shows cap - used (blocker excluded once)
    const uint64_t MiB = 1ull << 20;
    check(vram_capped_free(23000 * MiB, 500 * MiB, 22000, 0) == (22000 - 500) * MiB, "clamp = cap - used");
    check(vram_capped_free(100 * MiB, 500 * MiB, 22000, 0) == 100 * MiB, "a smaller free figure is untouched");
    // the blocker's own bytes must not count as `used` a second time
    const uint64_t blocker = (24576 - 22000) * MiB;
    check(vram_capped_free(blocker + 21500 * MiB, blocker + 500 * MiB, 22000, blocker) == 21500 * MiB,
          "the blocker is excluded from used exactly once");

    if (failures == 0) std::printf("vram_cap_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
