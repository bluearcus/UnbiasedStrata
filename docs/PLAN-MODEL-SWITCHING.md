# What is missing for model switching

**Question.** Two quants of the same model are on disk — IQ3_XXS (the agent config) and IQ3_S (the quality config) —
and the service can only ever serve one of them.  Switching means recreating the container with different environment
variables.  What would it take to make that a real, safe, discoverable switch?

Short answer: **the naming layer is now correct, the weights are interchangeable in a way nobody expected, and the
missing part is not in the front end at all — it is a reload path in the engine and a way for a client to learn which
model is loaded.**

---

## 1. The finding that reshapes the problem

The two GGUFs were compared key by key.  They are **identical except three metadata strings**:

| | IQ3_XXS | IQ3_S |
| --- | --- | --- |
| tensors | 1223 | 1223 |
| `general.architecture` | `qwen4exp` | `qwen4exp` |
| vocabulary | 248,320 tokens, sha1 `046b9ca4aef75d26` | **identical** |
| `tokenizer.ggml.pre` | `qwen35` | `qwen35` |
| bos / eos | 248044 / 248046 | 248044 / 248046 |
| chat template | 9993 chars, sha1 `a7e79f8fe37f381c` | **identical** |
| differing keys | `general.file_type` (23), `general.repo_url`, `general.tags` | (26), …, … |

The front end reads **vocabulary only, no tensors** (`strata_context::load` sets `mparams.vocab_only = true`), and
everything it derives from the GGUF — vocab, chat templates, `chat_params`, capability flags — is therefore the same for
both quants.

**Consequence: a quant switch does not touch the front end.**  Not its vocabulary, not its template, not its reasoning
plumbing, not the model-id check added in `1218969`.  Everything that has to change lives in the engine and its
artifacts.  That removes most of what makes model switching hard in llama.cpp (per-model context, per-model template,
per-model sampler config) from this particular switch.

---

## 2. What a switch actually consists of

Each part, and whether it survives a quant change:

| component | what it is | tied to the quant? | survives? |
| --- | --- | --- | --- |
| front-end vocab + template | `vocab_only` load from `-m` | **no** — byte-identical between the two | yes, untouched |
| `dense.bin` | 1,538,035,200 B of non-expert tensors in the pack | **yes** — one pack per model | no, replaced |
| `native_experts.txt` | absolute byte offsets into a *named* GGUF (`…IQ3_XXS-00001-of-00002.gguf`), 42,912,972,800 B for XXS against 50,292,326,400 B for S | **yes**, and it names its source file | no, replaced |
| `index.txt` | 1079 packed tensors | yes | no, replaced |
| `expert-profile.bin` | 196,632 B of learned tier state; the shim already passes `--expert-profile "$PACK/expert-profile.bin"` | per-pack | **per-quant: each pack carries its own**, so tier learning survives *per model* |
| `--ple-gguf` | shard 2, derived by string-substituting `00001-of-00002` → `00002-of-00002` in the model path | yes | follows the model automatically |
| expert slot plan | 9326 VRAM + 5338 peer slots today | sized from the model's experts and the VRAM budget | recomputed at startup |
| conversation cache | 8192 MiB of host RAM holding parked conversations | model-specific content | **lost** |
| adaptive tier learned this session | in-process | model-specific | lost unless `--expert-profile-save` wrote it |

A pack and a GGUF are **not interchangeable**: `native_experts.txt` records byte offsets inside one named file.  Point a
pack at the wrong GGUF and the offsets read the wrong weights.  Any switch mechanism must move the pair together.

---

## 3. What is missing

| # | Missing | Why it matters | Cost | Owner |
| --- | --- | --- | --- | --- |
| **S1** | **No switch command.**  Switching is a hand-typed `docker compose up -d` with two environment overrides | easy to get half right — model without pack, pack without the matching GGUF | small | assistant |
| **S2** | **No reload in the engine.**  The serve loop understands `GEN`/`GENI`, `BGEN`, `STOP`/`BSTOP`, `BYIELD`, `TIERS`, `VRAM`, `SAVE`/`RESTORE`, `QUIT`.  There is no load/unload command, and no way to release the expert arena and take another model's | this is the actual gap: without it "switch" can only mean "restart the process" | **large** | assistant, engine work |
| **S3** | **Nothing reports which model is loaded in a machine-readable way.**  `/v1/models` gives an id; `/health` gives id + context + tiers.  Neither says quant, pack, MTP, peer device, or shard set | a client cannot decide "switch to the other one" without guessing, and a UI cannot show what it is serving | small | assistant |
| **S4** | **Pi's view is static.**  `models.json` lists ids; the endpoint serves one.  Listing both quants means one of them always answers 400 — the picker offers a dead model | the picker is the natural place to switch, and today it cannot do it honestly | small (config), medium (if it becomes two endpoints) | you decide |
| **S5** | **The cost of switching to IQ3_S has never been measured.**  Only the IQ3_XXS numbers are known | the decision "is switching cheap enough to be a normal action" is currently unquantified | 10 minutes, engine work | you decide |
| **S6** | **Nothing preserves parked conversations across a switch** | this session costs 88 s of cold prefill to rebuild; that is the real price of a switch, not the model load | medium | parked |

### S2 detail: the reload path, and why it is the hard one

The engine loads once at startup and its whole architecture assumes that: the plan, the expert arena, the slot
allocation, the tier, the K/V geometry.  A reload needs all of those torn down and rebuilt, and it has to happen at a
point where no request is in flight.

Two building blocks already exist and point at how this could be done without redesigning the engine:

* **`--vram-elastic` + the `VRAM <reserve_mib>` command** (#533): the expert cache lives in physical segments via CUDA
  virtual-memory management and can be **resized between requests** — explicitly refused while batch slots are decoding
  (`VRAM: not while batch slots are decoding`).  That is exactly the "free the VRAM, at a safe point" primitive a reload
  needs.  It is not enabled on this rig.
* **`--expert-profile` / `--expert-profile-save`** (#477): learned tier state can be written between requests and read
  back at startup.  Already used per-pack by the shim, so a reload could restore each model's own learned tier.

What does *not* exist: any command to release the weights, re-plan slots, or re-`READY` a second model.  S2 is the item
that decides whether model switching is a feature or a restart.

**And the machinery is already in the tree, unbuilt.**  Upstream's multi-model server — `tools/server/server-models.cpp`,
with `models_max`, load/unload lists and requests that *wait for a model to load* ("a registered conv whose model is still
loading earns a retry") — is in the submodule and is **not compiled into this fork at all**:

```cmake
add_library(strata_llama_server STATIC
  server-http.cpp server-queue.cpp server-task.cpp
  server-common.cpp server-chat.cpp server-schema.cpp server-stream.cpp
  frontend/ui_stub/ui.cpp)          # no server-models.cpp, no server-context.cpp
```

So S2 is not "invent multi-model support".  It is "the fork replaced the file that has it, and would have to either use it
or reimplement the part of it that matters for a strata engine".  Which parts are reusable against an engine that owns its
own weights is the actual question, and it is answerable by reading `server-models.cpp` rather than by writing code.

### S6 detail: the re-prefill is forced by the weights, not by the restart

What the caches hold is **K/V state**, not prompt text.  K/V is computed *by the model's weights*, so changing weights
invalidates every cached conversation even though the tokens are identical.  A quant switch therefore costs a re-prefill
**by construction**, and no cache design can dodge it.

How expensive, measured on this conversation (the engine's own resume accounting):

```
prompt 229569 tokens = 229161 reused + 408 read in 1023 ms      6 checkpoints
prompt 225340 tokens = 221823 reused + 3517 read in 2927 ms     (a history rewrite costs the rewrite, not the lot)
after a restart:  prompt 195279 tokens, 88332 ms, nothing reused
```

So the warm path reads a few hundred tokens; cold reads ~2,200 tok/s, which is the 88 s.  The gap is the whole cost of a
switch, and note that **restarting into the *same* quant pays it too** — the caches are RAM (`--prompt-cache` is "~118 MB
of RAM each", `--conversation-cache-mib` is a "RAM budget"), so they die with the process whatever caused the death.

The only disk-persisted path is the engine's `SAVE`/`RESTORE` session files (which require `--prompt-cache > 0`), and
**the front end registers no route that reaches them** — `/health`, `/v1/health`, `/props`, `/models`, `/v1/models` and the
completion/tokenize routes are all there is.  Today a conversation cannot survive a restart at all.

**The design point that makes switching cheap in practice: cache per model, not per process.**  K/V is ~1.4 GB for a
196k-token conversation (12 full-attention layers at 48 blocks / `full_attention_interval 4`, 2 KV heads of 256+256, q4_0),
and the conversation cache budget here is 8192 MiB.  Keeping a cache *per quant* costs a couple of GB of a 62.5 GiB host,
and it means a switch back to a model you were just running restores instantly: only the **first** visit to a quant pays
the re-prefill.  Alternatives, worse to better:

| approach | re-prefill? | note |
| --- | --- | --- |
| today | always, completely | caches are RAM; a restart kills them whatever the cause |
| persist sessions to disk, restore same-model | avoided for a same-quant restart | needs an HTTP route that does not exist |
| persist and restore **across** quants | avoided | structurally plausible, semantically wrong, error unmeasured |
| cache per model, in RAM | avoided on the way back | ~1.4 GB each; the practical answer |
| background re-prefill while idle | paid, but hidden | the first turn after a switch is warm instead of 88 s cold |

### The one trap worth naming early

`SAVE`/`RESTORE` write a **session file** containing per-layer running state — the K/V.  Structurally the two quants
share geometry (same architecture, same vocabulary), so a session file from IQ3_XXS might load cleanly under IQ3_S.
That is not a reason to do it: the remembered attention state was computed by *different weights*, so the conversation
would continue on a memory the new model never produced.  A switch must rebuild the conversation, not transplant it.
(Structural compatibility is inferred from the metadata comparison above; the cross-quant restore itself has not been
attempted and should not be.)

---

## 4. What switching costs today (measured, and what is not measured)

From the last container recreate and the engine's own timing lines:

| | measured |
| --- | --- |
| container start → `/health` `loaded: true` | **15–16 s** |
| first request after the restart, 195,279-token prompt | **88,332 ms** prefill |
| the same conversation once parked | **591 ms** prefill, `reused=195,704` |
| decode, either way | ~95 tok/s, `drafts=226/333` |

So a switch today is roughly **15 s of outage plus ~88 s of the next long turn being cold** — about 100 s of degraded
service, dominated by the conversation cache being empty rather than by loading the model.  IQ3_S loads 50.3 GB of
experts against IQ3_XXS's 42.9 GB, so its share is expected to be larger; **S5 is the open measurement.**

Both quants cannot be resident at once, which is why "switch" cannot mean "keep both loaded": experts of 42.9 GB or
50.3 GB, 24 GB + 12 GB of VRAM, 62.5 GiB of RAM, **no swap**.

---

## 5. Options, cheapest first

**A. A switch script (no engine work).**  `tools/strata-switch [iq3-xxs|iq3-s]`: validate that the model file, its
shard 2, the pack and the MTP head all exist and match, recreate the container, poll `/health` until loaded, then print
the tier census and refuse loudly if a pack's `native_experts.txt` names a different GGUF than the one being started.
Turns a four-variable hand command into one command that cannot half-work.  This is the piece worth building now.

**B. Report what is loaded (no engine work).**  Add quant/file type, pack path, MTP path, peer device and the shard set
to `/health` and to `/v1/models` metadata.  Small, and it is the prerequisite for any UI or script that decides *which*
model to switch to — without it a client is guessing.

**C. Two named endpoints, one running (no engine work).**  Compose profiles giving `strata-xxs` on 8080 and `strata-s`
on 8081, only one up at a time, each with its own honest id.  Pi then has two real models to pick between and `/model`
becomes the switch — at the cost of the stopped one being unreachable rather than merely wrong.  This is the option that
actually fixes S4, and it is a deployment decision, not code.

**D. Hot reload in the engine (S2).**  The real feature: quiesce, free the expert arena via the elastic-segment path,
load the other pack and GGUF, re-plan, re-`READY`.  It would make switching take seconds instead of a restart plus a
cold 88-second prefill — except the conversation cache still has to be rebuilt, so it removes maybe a third of the
measured cost.  Given that, **D is not obviously worth its size**, and A + B + C capture most of the benefit for a
fraction of the work.

---

## 6. What is already right

Worth stating so it does not get undone by a rebase:

* The endpoint now **names its model and refuses the wrong one** (`1218969`): derived id, alias/path accepted, unknown
  id → `400 invalid_request_error` naming what is served.  `frontend/tests/e2e.sh` asserts it.
* The id **changes with the quant**, because it is derived from the file.  Under option C that is exactly what is wanted;
  under a hot-reload design (D) it becomes a problem — a name that changes under a live client is its own kind of lie,
  and D would need a stable id with the quant reported separately.  **Decide this before starting D.**
* `--expert-profile` is already per-pack, so per-model tier learning persists across restarts today.

---

## 7. Work estimate

### 7.1 What is already built (this is why the numbers below are small)

| piece | state | where |
| --- | --- | --- |
| K/V session files | **complete**: atomic write via temp file + rename + folder flush, free-space preflight, oversized/foreign files refused before allocation, streaming K/V read straight from the device without a host copy | `include/strata/core/conversation_file.hpp`, `session_save_reclaim.hpp` |
| Session *identity* binding | **complete**: `SessionFileIdentity { model, config }`, and *"a read with any other identity is refused before the payload is parsed"* | `conversation_file.hpp:25` |
| Engine `SAVE <path>` / `RESTORE <path>` | **complete**, with progress lines (`SESSION`, `SAVED`, `SERR`) | `generate.cpp:11480-11647` |
| Checkpoint selection for a save | **complete**, incl. freeing reconstructible host caches first (`--session-save-reclaim`) | `generate.cpp:11560-11605` |
| Front-end process control of the engine | **complete**: `fork`+`exec`, `stop()`, `quit()`, `can_stop()`, READY-until-timeout wait | `frontend/engine.hpp:70-160` |
| Front-end HTTP surface | **complete** for completions/tokenize; **no route** reaches SAVE/RESTORE | `strata_context.cpp:1419+` |

Two consequences worth stating plainly:

* **Cross-quant K/V transplant is already impossible by construction** - the identity hash covers the model files, so a
  file written under IQ3_XXS is refused under IQ3_S before it is even parsed.  My earlier worry about a conversation
  continuing on a memory the new model never produced is handled; nothing needs doing.
* **The engine does not have to be hot-reloaded to switch models.**  The front end already owns the engine process and
  already knows how to wait for `READY`.  Switching can be *front-end orchestration of an engine restart* - no engine
  change at all - which changes the estimate by an order of magnitude.

### 7.2 The two paths, sized

**Path A - front end orchestrates a restart (recommended).**

| step | work | effort |
| --- | --- | --- |
| A1 | child start via `posix_spawn` instead of `fork`: `fork()` in a live multi-threaded HTTP process is unsafe, `start()` even says *"must be called before any thread exists"* | **1-2 d**, low risk, ~60 lines |
| A2 | quiesce: a switching flag that answers `503` to new work, drain the task queue, then `stop()` + reap the child | 1 d, low risk |
| A3 | a model manifest and `/v1/models` from it: `/models/manifest.json`, entries of {id, gguf, pack, mtp, alias}; `/model/switch {model: id}`; `/health` reporting the live one | 1-2 d, low risk |
| A4 | SAVE before switch, RESTORE on the way back, keyed by conversation + model identity - reuses the engine commands that already exist | 2-3 d, medium risk (identity plumbing, file naming, preflight failures) |
| A5 | vocab/template reload after respawn (fast: `vocab_only`, ~1 s; identical between quants so mostly a no-op that must still work for a different model) | 1 d, low risk |
| A6 | tests: `e2e.sh` switch round trip against the fake engine, engine-side `SAVE`/`RESTORE` already has unit tests | 1-2 d |
| | **total** | **1.5-2 weeks** |

**Path B - hot reload inside the engine.**

The obstacle is not conceptual, it is that the load path is not a function:

```
main() at generate.cpp:2185   ->   READY at 10605
  8,420 lines of setup, 135 `auto` locals, 45 explicit allocation sites, all in one scope
```

Those locals own the weights, sessions, graphs, arenas, the tier and the plan.  A reload means turning 8.4k lines of
straight-line `main` body into something that can be constructed, destroyed and constructed again - and every one of those
135 locals has a lifetime that has to become explicit.

| step | effort | risk |
| --- | --- | --- |
| B1 | extract the init into a `ModelRuntime` struct/function (mechanical, invasive) | 2-3 wks | **high** - VRAM planning and graph capture are order-sensitive, and every rebase collides with it |
| B2 | teardown: release weights, sessions, graphs, both expert arenas, the CPU pool | 1 wk | high |
| B3 | reload protocol + quiesce + re-`READY` on the same pipes | 1 wk | medium |
| B4 | re-fill the tier from the other pack's profile; re-plan slots | 3-5 d | medium |
| | **total** | **5-8 wks** | compounding with every upstream rebase |

Path B buys: no process restart, so the HTTP listener and the client connection survive, and the switch takes seconds
instead of ~15 s.  It does **not** buy a warm conversation - the K/V is still invalidated by the weights, so the 88 s
re-prefill is avoided only by A4's save/restore, which Path A has anyway.

### 7.3 What a switch costs after each path

| | today | Path A | Path B |
| --- | --- | --- | --- |
| engine restart | yes (compose) | yes (~15 s) | no |
| HTTP listener / client connection | lost | **kept** (only in-flight work fails) | kept |
| conversation warm afterwards | no | **yes, if saved** | only with the same save/restore |
| code touched | - | front end only | front end + 8.4k-line engine init |

### 7.4 Recommendation

Build **A1-A3 and A6 first** (~1 week): a manifest, `/model/switch`, a safe respawn and the tests.  That alone turns
"switch" from a hand-typed compose command into one HTTP call, keeps the client connected, and makes `pi`'s picker
honest.  Then decide on **A4** (save/restore) with the measured cost in hand: it is the part that removes the 88 s, and
it is also the part with the only real correctness question (conversation identity across a save).

**Do not start Path B** unless something needs switching without a restart.  At 5-8 weeks against a shared init path that
every upstream rebase touches, it is the most expensive way to save 15 seconds.
