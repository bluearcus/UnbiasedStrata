#!/bin/bash
# Does batching pay at 32K and at 262K?  Drives the engine's own BGEN protocol with two slots and --batch-mtp,
# and the same prompts single-stream, at both window sizes.  No front end, no peer (isolate batching).
#
#   bgen_probe.sh   -> /home/mike/models/bgen.tsv, then BGEN_DONE
set -u
F=/home/mike/models/flash-next
U=/home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata
MODEL="$F/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
PACK=/home/mike/models/pack-iq3-xxs
MTP=/home/mike/models/mtp
OUT=/home/mike/models/bgen.tsv
echo -e "window\tmode\tslot0_tok_s\tslot1_tok_s\taggregate\tslots" > "$OUT"
P1=$(cat /tmp/prompt_4200.txt); P2=$(cat /tmp/prompt_b_4200.txt)

run() {  # window, mode
  local W=$1 mode=$2
  local log=/home/mike/models/bgen_${W}_${mode}.log fifo=/tmp/bgen_$$.fifo
  rm -f "$fifo"; mkfifo "$fifo"
  local extra=""
  [ "$mode" = batch ] && extra="--batch 2 --batch-mtp"
  ( cd "$U" && CUDA_DEVICE_ORDER=PCI_BUS_ID ./build-rk/strata --serve --pack "$PACK" --native "$MODEL" \
      --expert-profile "$PACK/expert-profile.bin" --expert-cache auto --mtp "$MTP" --max-context "$W" \
      --kv q4_0 --prefill auto --spec 4 --ple-io direct $extra \
      --ple-gguf "${MODEL/00001-of-00002/00002-of-00002}" < "$fifo" > "$log" 2>&1 ) &
  local epid=$!
  exec 3>"$fifo"
  local i up=0
  for i in $(seq 1 60); do sleep 5; grep -q '^READY' "$log" 2>/dev/null && { up=1; break; }; done
  if [ "$up" = 0 ]; then echo -e "${W}\t${mode}\t-\t-\tSTART_FAILED\t-" >> "$OUT"; exec 3>&-; kill $epid 2>/dev/null; return; fi
  local slots; slots=$(grep -m1 -oE "expert cache [0-9]+ slots" "$log" | grep -oE "[0-9]+" | head -1)
  local t0; t0=$(date +%s.%N)
  if [ "$mode" = single ]; then
    printf 'GEN 300 %s\n' "$P1" >&3
    for i in $(seq 1 300); do sleep 1; grep -q '^DONE ' "$log" && break; done
  else
    printf 'BGEN 0 300 %s\n' "$P1" >&3
    printf 'BGEN 1 300 %s\n' "$P2" >&3
    for i in $(seq 1 300); do sleep 1; [ "$(grep -c '^BDONE ' "$log")" -ge 2 ] && break; done
  fi
  local t1; t1=$(date +%s.%N)
  printf 'QUIT\n' >&3; exec 3>&-; wait $epid 2>/dev/null
  python3 - "$W" "$mode" "$log" "$t0" "$t1" "$slots" >> "$OUT" <<'PY'
import re, sys
W, mode, log, t0, t1, slots = sys.argv[1:7]
text = open(log, encoding="utf-8", errors="replace").read()
wall = float(t1) - float(t0)
if mode == "single":
    m = re.search(r"^DONE (\d+) (\d+) [\d.]+ ([\d.]+) ", text, re.M)
    if not m:
        print("%s\t%s\t-\t-\tNO_DONE\t%s" % (W, mode, slots)); sys.exit()
    tok, dms = int(m.group(1)), float(m.group(3))
    print("%s\t%s\t%.1f\t-\t%.1f\t%s" % (W, mode, tok / (dms / 1000.0), tok / (dms / 1000.0), slots))
else:
    ms = [(int(a), float(b)) for a, b in re.findall(r"^BDONE (\d+) (\d+) \w+ ([\d.]+)", text, re.M)] if False else []
    rows = re.findall(r"^BDONE (\d+) (\d+) (\w+) ([\d.]+)", text, re.M)
    if len(rows) < 2:
        print("%s\t%s\t-\t-\tNO_BDONE(%d)\t%s" % (W, mode, len(rows), slots)); sys.exit()
    prod = [int(r[1]) for r in rows]; dms = [float(r[3]) for r in rows]
    fps = [p / (d / 1000.0) for p, d in zip(prod, dms)]
    agg = sum(prod) / (max(dms) / 1000.0)
    print("%s\t%s\t%.1f\t%.1f\t%.1f\t%s" % (W, mode, fps[0], fps[1] if len(fps) > 1 else 0.0, agg, slots))
PY
  rm -f "$fifo"
}

run 32768   single
run 32768   batch
run 262144  single
run 262144  batch
echo BGEN_DONE
