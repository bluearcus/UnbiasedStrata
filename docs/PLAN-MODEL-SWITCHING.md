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

### S6 detail: the one trap worth naming early

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
