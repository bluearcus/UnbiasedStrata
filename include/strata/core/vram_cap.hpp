// include/strata/core/vram_cap.hpp - per-device VRAM caps (--vram-cap N | D:MIB,...).
//
// PLAN-dual-gpu.md Phase 3, item 9: one global cap cannot express an asymmetric rig (24 GB on card 0, 10 GB on
// card 1) - a single figure would silently clamp the 4070's budget to the 3090's.  The cap is therefore one MiB
// figure per CUDA ordinal (key -1 = every card), parsed once from the --vram-cap string and queried against the
// current CUDA device wherever a free figure is read (the engine's vram_info, the peer tier's open).
//
// Pure arithmetic, no CUDA calls: unit-tested by vram_cap_test (tests/core/).
#pragma once

#include <cstdint>
#include <map>
#include <string>

namespace strata::core {

// Parses --vram-cap: `N` (every card) or `D:MIB[,D:MIB...]` (per-ordinal, e.g. `0:22000,1:10000`).  On success
// fills `caps` (key -1, or the ordinals) and returns true; on failure returns false with `err` naming the cause.
inline bool parse_vram_cap(const std::string& s, std::map<int, int64_t>& caps, std::string& err) {
    caps.clear();
    if (s.empty()) { err = "--vram-cap needs MIB or D:MIB,..."; return false; }
    size_t i = 0;
    const auto need_num = [&](long& v) -> bool {
        if (i >= s.size() || s[i] < '0' || s[i] > '9') return false;
        v = 0;
        while (i < s.size() && s[i] >= '0' && s[i] <= '9') v = v * 10 + (s[i++] - '0');
        return true;
    };
    while (true) {
        long d = 0;
        if (!need_num(d) || d < 0) { err = "--vram-cap needs MIB or D:MIB,..."; return false; }
        if (i < s.size() && s[i] == ':') {
            if (d > 7) { err = "--vram-cap: device ordinal out of range 0..7"; return false; }
            ++i;
            long m = 0;
            if (!need_num(m) || m <= 0) { err = "--vram-cap: D:MIB needs MIB > 0"; return false; }
            if (caps.count((int) d)) { err = "--vram-cap: device listed twice"; return false; }
            caps[(int) d] = (int64_t) m;
        } else {
            if (d <= 0) { err = "--vram-cap needs MIB > 0"; return false; }
            if (!caps.empty()) { err = "--vram-cap: a bare MIB cannot be combined with D:MIB"; return false; }
            caps[-1] = (int64_t) d;
        }
        if (i >= s.size()) break;
        if (s[i] != ',') { err = "--vram-cap needs MIB or D:MIB,..."; return false; }
        ++i;
        if (i >= s.size()) { err = "--vram-cap: trailing comma"; return false; }
    }
    if (caps.count(-1) && caps.size() > 1) { err = "--vram-cap: a bare MIB cannot be combined with D:MIB"; return false; }
    return !caps.empty();
}

/// The cap in MiB for CUDA ordinal `dev` (the -1 entry covers every card; 0 = uncapped).
inline int64_t vram_cap_mib_for(const std::map<int, int64_t>& caps, int dev) {
    auto it = caps.find(dev);
    if (it != caps.end()) return it->second;
    it = caps.find(-1);
    return it != caps.end() ? it->second : 0;
}

/// The clamped free figure: what a card of `cap_mib` would show free with `used_b` in use by others, given the
/// `blocker_b` this process holds back on that card (the blocker already took total - cap out of free, so counting
/// it in `used` as well subtracts it twice - the --vram-cap 5722 double-count the single-cap code already knew).
inline uint64_t vram_capped_free(uint64_t free_b, uint64_t used_b, uint64_t cap_mib, uint64_t blocker_b) {
    if (cap_mib <= 0) return free_b;
    const uint64_t cap = (uint64_t) cap_mib << 20;
    const uint64_t used = used_b > blocker_b ? used_b - blocker_b : 0;
    const uint64_t room = cap > used ? cap - used : 0;
    return free_b > room ? room : free_b;
}

}  // namespace strata::core
