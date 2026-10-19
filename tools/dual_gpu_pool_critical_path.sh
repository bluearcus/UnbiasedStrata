#!/bin/bash
# Is the CPU expert pool on the critical path for a SINGLE serve stream?
# Serve at 262K, temp 0.7, 3 requests each at 9 workers (default) and 4 workers (44%).
# If throughput tracks worker count, the pool is critical (a faster kernel would pay).
# If throughput is flat, the pool is hidden behind GPU work (a faster kernel would not).
set -u
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
M=/home/mike/models/flash-next/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf
P=/home/mike/models/pack-iq3-xxs
T=/home/mike/models/mtp
BODY='{"model":"strata","messages":[{"role":"user","content":"Write a Python function that merges two sorted lists, with tests."}],"max_tokens":300,"temperature":0.7}'

arm() {
  local w=$1
  local log=/home/mike/models/crit_$w.log
  ( cd "$U" && CUDA_DEVICE_ORDER=PCI_BUS_ID setsid nohup ./build/unbiased-strata \
      --model "$M" --pack "$P" --mtp "$T" --ctx 262144 \
      -- --kv q4_0 --ple-io direct --pool-workers "$w" > "$log" 2>&1 </dev/null & )
  for i in $(seq 1 20); do sleep 15; grep -q "ready on" "$log" 2>/dev/null && break; done
  grep -q "ready on" "$log" || { echo "workers $w: FAILED TO START"; return; }
  local tot=0 n=0
  for r in 1 2 3; do
    curl -s --max-time 600 http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
         -d "$BODY" -o /tmp/crit_${w}_$r.json
    tps=$(python3 -c "
import json; d=json.load(open('/tmp/crit_${w}_$r.json')); print('%.2f' % d['timings']['predicted_per_second'])")
    echo "  workers $w run $r: $tps tok/s"
    tot=$(python3 -c "print($tot+$tps)"); n=$((n+1))
  done
  python3 -c "print('  workers $w: avg %.1f tok/s' % ($tot/$n))"
  local fe; fe=$(pgrep -f "unbiased-strata --model" | head -1); [ -n "$fe" ] && kill -TERM "$fe"; sleep 20
}

arm 9
arm 4
echo CRIT_DONE
