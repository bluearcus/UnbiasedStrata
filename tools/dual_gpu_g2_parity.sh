#!/bin/bash
# tools/dual_gpu_g2_parity.sh - Gate G2 (revised) for PLAN-dual-gpu.md: logit/distribution parity of the peer tier.
#
# Runs the engine twice on the SAME token ids - once with --peer-device and once without - dumps one row of raw
# logits per position, and compares the two distributions.  Token identity at temperature 0 is NOT the gate: the
# integer matmul is exact (__dp4a, INT8xINT8->INT32) but the row is float after the per-32-block int32 sums meet
# their fp32 activation / fp16 weight scales and go through SiLU, and sm_86 vs sm_89 need not emit identical float
# code, so a 1-ULP difference flips an argmax and diverges the whole sequence.  What matters is whether the peer
# computes the same distribution - top-1 agreement and KL.
#
# Both runs pin residency (--adapt-every 100000): the adaptive tier is non-deterministic by design (and upstream
# documents it), and it would mask the signal we are measuring.
#
# Usage:
#   tools/dual_gpu_g2_parity.sh --model SHARD1 --pack DIR --mtp DIR --tokens 1,2,3 [options]
#     --max-new N        positions to generate (default 16)
#     --peer-device N    peer ordinal (default 1)
#     --reserve MIB      peer reserve (default 2500)
#     --extra "ARGS"     more engine args, for both runs
#     --oracle LIST      the teacher-forced continuation as comma-separated token ids.  Given this, no generation
#                        pass runs: the gate compares two runs on exactly the same inputs, which is what it should
#                        be measuring (a generated oracle makes the comparison depend on generation being
#                        deterministic across machine states, which it is not always under load).
#     --min-top1 F       gate: top-1 agreement (default 0.9: the peer replaces CPU (AVX-2) expert rows with
#                        CUDA rows, which is a bigger difference than ULP; positions routed entirely on card 0
#                        stay exactly equal, which is the sharp invariant to watch)
#     --max-mean-kl F    gate: mean KL pair||single in nats (default 0.1: peer-served experts are
#                      computed on CUDA where the single-GPU run used the AVX-2 CPU pool, so the two differ
#                      by more than ULP - positions whose routing stayed on card 0 come out exactly equal)
#     --keep             keep both dumps
# Writes <tmp>/peer.f32 and <tmp>/single.f32, prints the comparison, exits non-zero when a gate fails.
set -u
MODEL="" PACK="" MTP="" TOKENS="" MAXNEW=16 PEER=1 RESERVE=2500 EXTRA="" MINTOP1=0.9 MAXKL=0.1 KEEP=0 ORACLE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL=$2; shift 2;;        --pack) PACK=$2; shift 2;;
    --mtp) MTP=$2; shift 2;;            --tokens) TOKENS=$2; shift 2;;
    --max-new) MAXNEW=$2; shift 2;;     --peer-device) PEER=$2; shift 2;;
    --reserve) RESERVE=$2; shift 2;;    --extra) EXTRA=$2; shift 2;;
    --min-top1) MINTOP1=$2; shift 2;;   --max-mean-kl) MAXKL=$2; shift 2;;
    --keep) KEEP=1; shift;;              --oracle) ORACLE_ARG=$2; shift 2;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
[ -n "$MODEL" ] && [ -n "$PACK" ] && [ -n "$MTP" ] && [ -n "$TOKENS" ] || {
  echo "usage: $0 --model SHARD1 --pack DIR --mtp DIR --tokens 1,2,3 [--max-new N]" >&2; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BIN=${STRATA_BIN:-$ROOT/build/unbiased-strata}
[ -x "$BIN" ] || { echo "no engine at $BIN (set STRATA_BIN)" >&2; exit 2; }
TMP=$(mktemp -d /tmp/g2-parity.XXXXXX)
COMMON=(--engine --pack "$PACK" --native "$MODEL" --expert-profile "$PACK/expert-profile.bin"
        --expert-cache auto --mtp "$MTP" --max-context 4096 --kv q4_0 --ple-io direct
        --spec 4 --prefill 512
        --tokens "$TOKENS" --max-new "$MAXNEW" --adapt-every 100000)
# Flash-Next ships a second shard holding the PLE table: the engine reads it as --ple-gguf
case "$MODEL" in *-00001-of-00002.gguf) COMMON+=(--ple-gguf "${MODEL/00001-of-00002/00002-of-00002}");; esac
[ -n "$EXTRA" ] && read -r -a EXTRA_ARR <<< "$EXTRA" && COMMON+=("${EXTRA_ARR[@]}")

# Teacher forcing is what makes the comparison meaningful.  Plain autoregressive decode diverges: one flipped
# argmax and every later position is conditioned on a different prefix, which reads as a KL of 12+ nats and says
# nothing about the peer's arithmetic.  So the single-GPU run first produces a continuation, and BOTH runs are then
# held to that same continuation (--spec-oracle + --teacher-force), which puts identical prefixes under every
# compared position.
ORACLE=$TMP/oracle.txt
if [ -n "$ORACLE_ARG" ]; then
  printf '%s' "$ORACLE_ARG" > "$ORACLE"
  echo "oracle: given ($(awk -F, '{print NF}' "$ORACLE") tokens)"
else

# pass 1: a reference continuation from the single-GPU run (greedy, static residency)
"$BIN" "${COMMON[@]}" > "$TMP/gen.log" 2>&1 || {
  echo "single-GPU generation failed; see $TMP/gen.log" >&2; tail -5 "$TMP/gen.log" >&2; exit 1; }
python3 - "$TMP/gen.log" "$ORACLE" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"^output\s*:\s*([0-9 ]+)$", text, re.M)
if not m:
    sys.exit("no 'output  : <ids>' line in the generation log")
ids = m.group(1).split()
open(sys.argv[2], "w").write(",".join(ids))
print("oracle: %d tokens" % len(ids))
PY
fi

# --teacher-force refuses a drafter: the point is identical prefixes, not speed
COMMON_NOMTP=()
for ((i = 0; i < ${#COMMON[@]}; ++i)); do
  case "${COMMON[i]}" in --mtp) ((i++));; *) COMMON_NOMTP+=("${COMMON[i]}");; esac
done
COMMON=("${COMMON_NOMTP[@]}" --spec-oracle "$ORACLE" --teacher-force)
echo "== single GPU (teacher-forced) =="
CUDA_DEVICE_ORDER=PCI_BUS_ID "$BIN" "${COMMON[@]}" --dump-logits "$TMP/single.f32" > "$TMP/single.log" 2>&1 \
  || { echo "single-GPU run failed; see $TMP/single.log" >&2; tail -5 "$TMP/single.log" >&2; exit 1; }
echo "== peer GPU $PEER (teacher-forced, reserve ${RESERVE} MiB) =="
CUDA_DEVICE_ORDER=PCI_BUS_ID "$BIN" "${COMMON[@]}" --peer-device "$PEER" --peer-reserve-mib "$RESERVE" \
  --peer-prefill-rows 0 --dump-logits "$TMP/peer.f32" > "$TMP/peer.log" 2>&1 \
  || { echo "peer run failed; see $TMP/peer.log" >&2; tail -5 "$TMP/peer.log" >&2; exit 1; }

# the dump's header is two int32: n_vocab, then rows.  The comparison tool reads the engine side with its header
# and the reference side as raw rows, so the single-GPU dump is copied without its 8-byte header.
NVOCAB=$(python3 -c "import struct,sys; d=open(sys.argv[1],'rb').read(8); print(struct.unpack('<2i',d)[0])" "$TMP/peer.f32")
tail -c +9 "$TMP/single.f32" > "$TMP/single.raw.f32"
echo "== comparison (n_vocab $NVOCAB) =="
set +e
python3 "$HERE/qwen36_compare_logits.py" "$TMP/peer.f32" "$TMP/single.raw.f32" --n-vocab "$NVOCAB" \
        --min-top1 "$MINTOP1" --max-mean-kl "$MAXKL"
RC=$?
set -e
echo "dumps: $TMP/peer.f32 $TMP/single.f32"
[ "$KEEP" -eq 0 ] && [ "$RC" -ne 0 ] && echo "(kept because the gate failed)"
[ "$KEEP" -eq 0 ] && [ "$RC" -eq 0 ] && rm -rf "$TMP"
exit "$RC"
