# Fork strategy: keeping the rebase tax small

**The problem.** Upstream 0.1.45 moved fast and restructured twice: the front end is now a different program
(420 lines, llama-server option parsing, `--engine PATH`, engine flags after `--`) and the `STATS` protocol was
removed entirely (`/health` is now `{status, busy, service, loaded}`; the engine prints per-request lines like
`strata serve: expert tiers: ...` instead). Every future rebase re-pays for whatever we changed inside files
upstream also changes. Our current rebase cost ~4–6 days, most of it in two files.

**The rule to adopt.** *New files and wrappers are cheap forever; edits inside upstream files are rented.* Each
edit we keep should earn its place, and each should be findable by grep.

---

## 1. What to drop: `--vram-cap` on this branch (biggest saving)

Upstream 0.1.40 removed `--vram-cap` and its `vram_info()` wrapper. Re-adding it cost us the most invasive change
in the whole rebase: **26 call-site renames** across a 16,000-line file that upstream rewrites constantly.

But its purpose was **emulation** - pretending a card is smaller. On real hardware each card already reports its
own free VRAM, and the peer tier sizes itself from its own card's figure; the only production need on an
asymmetric rig (leave the desktop card some room) is `--peer-reserve-mib`, which exists upstream. Upstream even
ships `--vram-reserve-later-mib` for the same idea on layer splits.

**Recommendation: do not carry `--vram-cap` forward.** Keep `include/strata/core/vram_cap.hpp` and
`tests/core/vram_cap_test.cpp` on the 0.1.39 branch (they are correct and tested), and re-add the option only if
we ever need emulation again - in which case it is a self-contained patch to re-apply, not part of the main line.
Saving: ~100 lines plus 26 edits per rebase.

## 2. Observability: follow upstream's grain, do not revive `STATS`

The engine already prints, per request:

```
strata serve: expert tiers: GPU <hits> this request; since the start RAM <n> blobs, files <n> blobs <mb> MB read
strata serve: decode expert cache hit rate: ...
```

The front end already parses its own protocol lines; /health is assembled in
`frontend/strata_context.cpp:get_health`.

**Recommendation:** extend the *existing* line with the peer's fields (one `fprintf`, ~2 lines of code) and parse
it in `get_health`, rather than reintroducing a command protocol that upstream deleted. Peer numbers are
diagnostic, not control-plane: a log line is the right size of mechanism, and it cannot conflict with an
upstream protocol change because it adds no protocol.

If the counters must be machine-readable without log scraping, the second-cheapest option is a *new* single
command in the engine's command dispatch (a marked block) - but prefer the log line.

## 3. Front-end peer validation/defaults: a shim, not an edit

Everything we added to the old front end was **pre-flight policy**: check the peer ordinal, probe P2P before a
40 GB load, default the reserve, default `--ple-io direct` under a peer, choose the low-RAM tier on small hosts.
None of it needs to live inside upstream's new `main.cpp`.

**Recommendation:** ship a `strata-dual` wrapper (shell or ~100-line C++) that:

1. validates `--peer-device N` against `nvidia-smi -L` (or a tiny CUDA query tool),
2. probes P2P (`tools/peer_p2p_probe` - 30 lines, reuses the same calls), printing the verdict,
3. injects the defaults (reserve 2500, `--peer-prefill-rows 0` without P2P, `--ple-io direct` under a peer,
   the low-RAM flags below ~56 GiB) unless the user was explicit,
4. execs `unbiased-strata -m ... -- <engine flags>`.

Zero upstream-file edits, fully testable without a GPU, and it survives every rebase untouched. `STRATA_DUMP_EARGS`
becomes a `--dry-run` flag in the wrapper.

## 4. Keep the engine edits we cannot avoid, but mark them

We cannot avoid touching the engine for the peer tier: it is a feature upstream does not have. Current surface:

| Edit | Where | Size | Why it must stay |
| --- | --- | --- | --- |
| peer `open()` cap args + `slots()` + `nominate_for_primary()` | `peer_experts.{hpp,cpp}` | ~70 lines, mostly new code | the tier and the promotion pass *are* the fork's value |
| promotion hook (1 call) + P2P log line (1 `fprintf`) | `generate.cpp` | ~10 lines | wiring only |
| `--peer-*` options | already upstream | 0 | upstream kept them |

**Recommendation:** wrap each such insertion in `// STRATA-FORK-BEGIN <reason>` / `// STRATA-FORK-END`, so a
rebase is `grep -n STRATA-FORK src/ include/` and every conflict is localised. Keep the count small enough to
read in one screen.

## 5. Pin and schedule

- Record the rebased-on commit in this file (`rk/main 1fa9006b`, version 0.1.45) and re-check upstream quarterly,
  not per release.
- Rebase only when the measured gain is worth a day: the 0.1.39 → 0.1.45 move was worth 6–20%, which is a high
  bar; most releases will not be.
- Keep the old branch buildable until the new one passes the rig gates, so there is always a fallback.

## 6. Sequencing (what I would do, in order)

1. **Drop `--vram-cap`** from the rebase branch (section 1) - the tree gets simpler and the engine patch shrinks
   to the peer tier alone.
2. **Peer fields in the existing log line** (section 2, ~2 lines) and a `get_health` parse.
3. **`strata-dual` shim + P2P probe tool** (section 3), replacing the old front-end additions.
4. **Mark the remaining engine blocks** (section 4).
5. Re-run G1/G2/G4/G6 on the rebased tree with the shim, then merge to `main`.

Steps 1–4 are roughly one to two days; step 5 is an afternoon of the runs we already have scripted.
