#!/bin/bash
# Batch probe: does --batch work with --peer-device, and what does it buy on this rig?
# Drives the engine's own serve protocol (BGEN/BT/BDONE) over a FIFO - no front end involved.
# Arms: (1) one request, (2) two batched, (3) two batched with --batch-mtp.
set -u
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
M=/home/mike/models/flash-next/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf
P=/home/mike/models/pack-iq3-xxs
T=/home/mike/models/mtp
P1=7734,264,12654,709,421,78161,1330,10300,11140,11,440,6813,13
P2=814,20139,1204,20340,8404,17830,15089,28109,4138,321,3069,2706,41930,12482,13
BASE="--engine --serve --pack $P --native $M --expert-profile $P/expert-profile.bin --expert-cache auto \
--mtp $T --max-context 262144 --kv q4_0 --ple-io direct --prefill 512 --spec 4 \
--ple-gguf ${M/00001-of-00002/00002-of-00002} --peer-device 1"

arm() {   # $1 = name, $2 = batch count, $3 = extra args, $4 = "one" | "two"
  local name=$1 bcount=$2 extra=$3 mode=$4 envv=${5:-}
  local log=/home/mike/models/probe_$name.log fifo=/tmp/probe_$name.fifo
  rm -f "$fifo"; mkfifo "$fifo"
  ( cd "$U" && env $envv CUDA_DEVICE_ORDER=PCI_BUS_ID ./build/unbiased-strata $BASE --batch $bcount $extra < "$fifo" > "$log" 2>&1 ) &
  local epid=$!
  exec 3>"$fifo"                      # keep the pipe open for the whole run
  for i in $(seq 1 60); do sleep 5; grep -q "ready on\|READY" "$log" 2>/dev/null && break; done
  local t0; t0=$(date +%s.%N)
  if [ "$mode" = one ]; then
    printf 'GEN 200 %s\n' "$P1" >&3
  else
    printf 'BGEN 0 200 %s\n' "$P1" >&3
    printf 'BGEN 1 200 %s\n' "$P2" >&3
  fi
  # batch: the prompt phase ends with a plain DONE (first token), the window phase with BDONE per slot
  for i in $(seq 1 150); do
    sleep 2
    if [ "$mode" = two ]; then
      got=$(grep -c '^BDONE ' "$log" 2>/dev/null || true); [ "${got:-0}" -ge 2 ] && break
    else
      got=$(grep -c '^DONE ' "$log" 2>/dev/null || true); [ "${got:-0}" -ge 1 ] && break
    fi
  done
  local t1; t1=$(date +%s.%N)
  printf 'QUIT\n' >&3; exec 3>&-; wait $epid 2>/dev/null
  echo "=== arm $name (batch=$bcount $extra, $mode)"
  grep -E "^BDONE |^DONE " "$log" | cat
  python3 - "$t1" "$t0" <<PY
import re,sys
t1,t0=float(sys.argv[1]),float(sys.argv[2]); wall=t1-t0
text=open("$log",encoding="utf-8",errors="replace").read()
prod=[int(m) for m in re.findall(r"^BDONE \d+ (\d+) ", text, re.M)] or [int(m) for m in re.findall(r"^DONE (\d+) ", text, re.M)]
bt=[int(m) for m in re.findall(r"^BT (\d+) ", text, re.M)]
if bt: prod = [int(m)+1 for m in re.findall(r"^BDONE \d+ (\d+) ", text, re.M)] or prod
tot=sum(prod)
print("  produced per stream: %s, aggregate %d tokens in %.1f s wall = %.1f tok/s" % (prod, tot, wall, tot/max(wall,1e-9)))
PY
  rm -f "$fifo"
}

arm single 1 "" one                # solo baseline (batch 1 is disabled by the engine)
arm batch2 2 "" two
arm batch2mtp 2 "" two STRATA_BATCH_MTP=1
echo PROBE_DONE
