#!/bin/bash
# IQ3_S vs IQ3_XXS on the rig: serve mode, 262K, temp 0.7, same prompt, 3 requests per arm.
# Arms: XXS single, XXS peer, S single, S peer.  Prints tok/s, draft acceptance and the tier line.
set -u
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
F=/home/mike/models/flash-next
BODY='{"model":"strata","messages":[{"role":"user","content":"Write a Python function that merges two sorted lists, with tests."}],"max_tokens":300,"temperature":0.7}'

arm() {   # $1 name  $2 model dir+file prefix  $3 pack  $4 peer(0/1)
  local name=$1 qdir=$2 pack=$3 peer=$4
  local log=/home/mike/models/bench_$name.log
  local margs=(--peer-device 1)
  [ "$peer" = 0 ] && margs=()
  ( cd "$U" && CUDA_DEVICE_ORDER=PCI_BUS_ID setsid nohup ./build/unbiased-strata \
      --model "$qdir-00001-of-00002.gguf" --pack "$pack" --mtp /home/mike/models/mtp --ctx 262144 \
      "${margs[@]}" -- --kv q4_0 --ple-io direct > "$log" 2>&1 </dev/null & )
  for i in $(seq 1 24); do sleep 15; grep -q "ready on" "$log" 2>/dev/null && break; done
  grep -q "ready on" "$log" || { echo "== $name: FAILED TO START"; tail -3 "$log"; return; }
  echo "== $name"
  grep -E "tiers at start" "$log" | head -1 | sed 's/.*"experts"/  tiers: {"experts"/' | cut -c1-150
  local tot=0 n=0 acc=0 off=0
  for r in 1 2 3; do
    curl -s --max-time 900 http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
         -d "$BODY" -o /tmp/bench_${name}_$r.json
    read -r tps a d <<<"$(python3 -c "
import json
d=json.load(open('/tmp/bench_${name}_$r.json')); t=d['timings']
print('%.2f %d %d' % (t['predicted_per_second'], t['draft_n_accepted'], t['draft_n']))" 2>/dev/null || echo '0 0 0')"
    printf '  run %d: %6.1f tok/s  drafts %s/%s\n' "$r" "$tps" "$a" "$d"
    tot=$(python3 -c "print($tot+$tps)"); n=$((n+1)); acc=$((acc+a)); off=$((off+d))
  done
  python3 -c "print('  %s: avg %.1f tok/s, acceptance %.1f%%' % ('$name', $tot/$n, 100.0*$acc/max($off,1)))"
  local fe; fe=$(pgrep -f "unbiased-strata --model" | head -1); [ -n "$fe" ] && kill -TERM "$fe"; sleep 20
}

arm xxs_single "$F/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS" /home/mike/models/pack-iq3-xxs 0
arm xxs_peer   "$F/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS" /home/mike/models/pack-iq3-xxs 1
arm s_single   "$F/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S"     /home/mike/models/pack-iq3-s   0
arm s_peer     "$F/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S"     /home/mike/models/pack-iq3-s   1
echo BENCH_DONE
