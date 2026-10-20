#!/bin/bash
# Regime-shift test: warm in one expert space, then jump to a different one entirely, and watch whether the
# adaptive tier (and the promotion pass) help the transition.
#
#   phase A: code / systems English      (3 requests)
#   phase B: French lyrical + narrative   (3 requests)   <- a different expert space
#
# Arms: peer with promotion off, peer with promotion on (tight budget), single GPU (control).
# Per request it records decode tok/s and the tier counters (peer hits, nominated), so recovery is visible
# request by request rather than as one median.
set -u
F=/home/mike/models/flash-next
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
MODEL="$F/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
PACK=/home/mike/models/pack-iq3-xxs
OUT=/home/mike/models/shift.tsv
PORT=8080
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

ask() {  # $1 prompt -> prints "tok/s|accepted|offered|prompt_n"
  local prompt=$1 out=/tmp/shift_req.json
  python3 - "$prompt" > /tmp/shift_body.json <<'PY'
import json, sys
print(json.dumps({"messages": [{"role": "user", "content": sys.argv[1]}], "max_tokens": 300, "temperature": 0.7}))
PY
  curl -s --max-time 600 "http://127.0.0.1:$PORT/v1/chat/completions" -H "Content-Type: application/json" \
       --data-binary @/tmp/shift_body.json -o "$out"
  python3 - "$out" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1])); t = d.get("timings", {})
    print("%.1f|%d|%d|%d" % (t.get("predicted_per_second", 0), t.get("draft_n_accepted", 0),
                             t.get("draft_n", 0), t.get("prompt_n", 0)))
except Exception:
    print("0|0|0|0")
PY
}

tiers() {  # -> "peer_hits peer_held vram_held nominated"
  curl -s --max-time 10 "http://127.0.0.1:$PORT/health" | python3 -c "
import json,sys
try:
    t=json.load(sys.stdin).get('tiers',{})
    print('%s %s %s %s' % (t.get('peer_hits'),t.get('peer_held'),t.get('vram_held'),t.get('peer_nominated')))
except Exception:
    print('? ? ? ?')
"
}

arm() {  # $1 name, $2 env (KEY=V ...), $3 extra engine args
  local name=$1
  local envv=$2
  local extra=$3
  local log=/home/mike/models/shift_$name.log
  echo "== arm $name"
  ( cd "$U" && env $envv CUDA_DEVICE_ORDER=PCI_BUS_ID setsid nohup tools/strata-dual \
      --engine "$U/build-rk/strata" -m "$MODEL" --port $PORT --pack "$PACK" --mtp /home/mike/models/mtp \
      ${extra} -- --max-context 262144 --kv q4_0 > "$log" 2>&1 </dev/null & )
  local up=0
  for i in $(seq 1 40); do sleep 5; [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health)" = 200 ] && { up=1; break; }; done
  [ "$up" = 1 ] || { echo "  FAILED TO START"; tail -3 "$log"; return; }
  local phase p
  for phase in A B; do
    eval "prompts=(\"\${${phase}[@]}\")"
    local idx=0
    for p in "${prompts[@]}"; do
      local r; r=$(ask "$p"); local t; t=$(tiers)
      local tps=${r%%|*}
      echo -e "${name}\t${phase}\t$((++idx))\t${tps}\t$(echo "$r" | cut -d'|' -f2-4 | tr '|' '\t')\t$t" >> "$OUT"
      printf '   %s%d: %6s tok/s   tiers(peer_hits held vram held nominated): %s\n' "$phase" "$idx" "$tps" "$t"
    done
  done
  local fe; fe=$(pgrep -f "unbiased-strata --engine" | head -1); [ -n "$fe" ] && kill -TERM "$fe"
  sleep 15
}

echo -e "arm\tphase\treq\ttok_s\tacc\toff\tprompt_n\tpeer_hits\tpeer_held\tvram_held\tnominated" > "$OUT"
arm peer_off   ""                                                               "--peer-device 1"
arm peer_promo "STRATA_PEER_PROMOTE=1 STRATA_PEER_PROMOTE_MAX=8 STRATA_PEER_PROMOTE_GAIN=2" "--peer-device 1"
arm single     ""                                                               ""
echo SHIFT_DONE
