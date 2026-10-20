#!/bin/bash
# Full promotion trial: REPEATS cold runs of the regime-shift sequence per arm.
# Each repeat restarts the engine, warms in domain A (code/systems English) and then jumps to domain B
# (French lyrical + gothic-novel analysis); B1 is the first request after the jump.
set -u
F=/home/mike/models/flash-next
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
MODEL="$F/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
PACK=/home/mike/models/pack-iq3-xxs
OUT=/home/mike/models/trial.tsv
PORT=8080
REPEATS=${REPEATS:-3}
export STRATA_FRONTEND=$U/build-rk/unbiased-strata
export STRATA_P2P_PROBE=$U/build-rk/peer_p2p_probe

A=(
 "Write a Python implementation of a red-black tree with insert and delete, and explain the invariants you maintain."
 "Explain how a B-tree differs from a skip list for on-disk indexes, with the trade-offs for write amplification."
 "Write SQL DDL for a double-entry ledger with postings and explain the query plan for a monthly trial balance."
)
B=(
 "Écris un passage lyrique en français sur une rivière à l'aube, environ trois cents mots, dans un registre poétique."
 "Summarise the plot conventions of the nineteenth-century gothic novel, with concrete examples from two novels."
 "Describe in evocative prose the smell, sound and light of a summer storm arriving over a wheat field."
)

ask() {
  python3 - "$1" > /tmp/trial_body.json <<'PY'
import json, sys
print(json.dumps({"messages": [{"role": "user", "content": sys.argv[1]}], "max_tokens": 300, "temperature": 0.7}))
PY
  curl -s --max-time 600 "http://127.0.0.1:$PORT/v1/chat/completions" -H "Content-Type: application/json" \
       --data-binary @/tmp/trial_body.json -o /tmp/trial_req.json
  python3 - <<'PY'
import json
try:
    d = json.load(open("/tmp/trial_req.json")); t = d.get("timings", {})
    print("%.1f" % t.get("predicted_per_second", 0))
except Exception:
    print("0")
PY
}

start_engine() {  # $1 env, $2 extra args, $3 log
  ( cd "$U" && env $1 CUDA_DEVICE_ORDER=PCI_BUS_ID setsid nohup tools/strata-dual \
      --engine "$U/build-rk/strata" -m "$MODEL" --port $PORT --pack "$PACK" --mtp /home/mike/models/mtp \
      $2 -- --max-context 262144 --kv q4_0 > "$3" 2>&1 </dev/null & )
  local i
  for i in $(seq 1 60); do
    sleep 5
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health)" = 200 ] && return 0
  done
  return 1
}

stop_engine() {
  local fe; fe=$(pgrep -f "unbiased-strata --engine" | head -1)
  [ -n "$fe" ] && kill -TERM "$fe"
  sleep 12
}

echo -e "arm\trepeat\tphase\treq\ttok_s" > "$OUT"
for arm in "off::" "promo:STRATA_PEER_PROMOTE=1 STRATA_PEER_PROMOTE_MAX=2 STRATA_PEER_PROMOTE_GAIN=3:"; do
  name=${arm%%:*}; rest=${arm#*:}; envv=${rest%%:*}; extra="--peer-device 1"
  for r in $(seq 1 "$REPEATS"); do
    log=/home/mike/models/trial_${name}_$r.log
    if ! start_engine "$envv" "$extra" "$log"; then echo "  $name repeat $r: FAILED TO START"; continue; fi
    for phase in A B; do
      eval "prompts=(\"\${${phase}[@]}\")"
      idx=0
      for p in "${prompts[@]}"; do
        tps=$(ask "$p"); idx=$((idx+1))
        echo -e "${name}\t${r}\t${phase}\t${idx}\t${tps}" >> "$OUT"
        printf '  %s r%d %s%d: %s\n' "$name" "$r" "$phase" "$idx" "$tps"
      done
    done
    stop_engine
  done
done
echo TRIAL_DONE
