#!/usr/bin/env bash
# Sustained-load soak with sensor sampling: the measurement the cooling changes are judged against.
#
# WHY THIS EXISTS.  A single 57-second request peaked VRAM at 68 C, which is not the condition we are worried about
# (the earlier target soak reached 78-80 C).  Judging fan curves needs a repeatable protocol, run the same way
# before and after a change, or the comparison means nothing.
#
# WHAT IT DOES
#   * sends back-to-back chat completions with a ~30k-token prompt and a long generation, so the engine is busy
#     prefilling and decoding for the whole window rather than idling between requests;
#   * samples every SAMPLE_SECS: 3090 VRAM (from the CoolerControl exporter file), core temp, board power, the
#     card's own fan, and all four motherboard header duties plus their tach RPMs;
#   * prints a per-sample table, then a summary: decode rate, peak/mean VRAM, duty range per header, and a
#     stall-floor check against the duties measured during calibration.
#
# It does not touch CoolerControl or LACT.  It only reads.
#
# USAGE
#   tools/cooling_soak.sh 15                  # 15 minutes
#   MINUTES=10 SAMPLE_SECS=5 tools/cooling_soak.sh
#   tools/cooling_soak.sh 15 --no-engine-check   # skip the readiness probe
set -uo pipefail

MINUTES="${1:-12}"
SAMPLE_SECS="${SAMPLE_SECS:-5}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
MODEL="${MODEL:-strata-local}"
GEN_TOKENS="${GEN_TOKENS:-8000}"
PROMPT_REPEATS="${PROMPT_REPEATS:-1200}"
HW="${HW:-/sys/class/hwmon/hwmon2}"
VRAM_FILE="${VRAM_FILE:-/run/vramsensor/vram.temp1}"
OUT="${SOAK_OUT:-/tmp/cooling-soak-$(date +%Y%m%d-%H%M%S).csv}"

# Old BIOS automatic curves, for reference in the summary.  These are what CoolerControl replaced.
declare -A BIOS_PIN=([pwm3]=203 [pwm4]=205 [pwm5]=147 [pwm6]=178)
# Calibrated stall duties from /etc/coolercontrol/calibrations.json: below these the fan stops.
declare -A STALL=([fan3]=16 [fan4]=14 [fan5]=20 [fan6]=30)

log() { printf '%s\n' "$*"; }

engine_ready() {
  curl -s --max-time 5 "$BASE_URL/health" | grep -q '"loaded":true'
}

build_payload() {
  python3 - "$PROMPT_REPEATS" "$GEN_TOKENS" "$MODEL" <<'PY'
import json, sys
repeats, gen, model = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
body = ("Context line %d: the quick brown fox jumps over the lazy dog while the expert pool streams weights from "
        "system memory and the CPU pool handles the shared experts. ")
prompt = body % 1 * repeats + " Now write a detailed 4000 word technical essay about expert offloading in MoE inference, in numbered sections."
print(json.dumps({"model": model, "stream": False, "max_tokens": gen, "messages": [{"role": "user", "content": prompt}]}))
PY
}

if ! engine_ready; then
  log "engine is not answering /health with loaded:true - start it first:"
  log "  cd /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata && docker compose up -d"
  exit 1
fi

PAYLOAD=$(mktemp) || { log "could not create a temp payload file"; exit 1; }
build_payload > "$PAYLOAD" || { log "could not build the payload"; rm -f "$PAYLOAD"; exit 1; }
log "payload: $(wc -c < "$PAYLOAD") bytes, generation target $GEN_TOKENS tokens, window ${MINUTES} min"

CSV="$OUT"
if ! : > "$CSV" 2>/dev/null; then
  log "NOTE: cannot write $CSV - continuing, the table is printed to stdout only"
  CSV=""
fi

sample_line() {
  local vram cardfan core power util p1 p2 p3 p4 p5 p6 f1 f2 f3 f4 f5 f6 t
  vram=$(awk '{printf "%.1f", $1/1000}' "$VRAM_FILE" 2>/dev/null || echo "?")
  cardfan=$(nvidia-smi -q -i 0 2>/dev/null | awk -F: '/Fan Speed/{gsub(/ /,"",$2); print $2}')
  read -r power util core <<<"$(nvidia-smi --query-gpu=power.draw,utilization.gpu,temperature.gpu --format=csv,noheader,nounits -i 0 2>/dev/null | tr ',' ' ')"
  p1=$(cat "$HW/pwm1" 2>/dev/null); p2=$(cat "$HW/pwm2" 2>/dev/null)
  p3=$(cat "$HW/pwm3" 2>/dev/null); p4=$(cat "$HW/pwm4" 2>/dev/null)
  p5=$(cat "$HW/pwm5" 2>/dev/null); p6=$(cat "$HW/pwm6" 2>/dev/null)
  f1=$(cat "$HW/fan1_input" 2>/dev/null); f2=$(cat "$HW/fan2_input" 2>/dev/null)
  f3=$(cat "$HW/fan3_input" 2>/dev/null); f4=$(cat "$HW/fan4_input" 2>/dev/null)
  f5=$(cat "$HW/fan5_input" 2>/dev/null); f6=$(cat "$HW/fan6_input" 2>/dev/null)
  t=$(( $(date +%s) - START ))
  printf 't=%4ds vram=%5s C core=%3s W=%5s util=%3s cardfan=%-4s | pwm1=%-3s(%2s%%) f1=%-4s | pwm2=%-3s(%2s%%) f2=%-4s | pwm3=%-3s(%2s%%) f3=%-4s | pwm4=%-3s(%2s%%) f4=%-4s | pwm5=%-3s(%2s%%) f5=%-4s | pwm6=%-3s(%2s%%) f6=%-4s\n' \
    "$t" "$vram" "$core" "${power:-?}" "${util:-?}" "${cardfan:-?}" \
    "$p1" "$(( ${p1:-0} * 100 / 255 ))" "$f1" "$p2" "$(( ${p2:-0} * 100 / 255 ))" "$f2" \
    "$p3" "$(( ${p3:-0} * 100 / 255 ))" "$f3" "$p4" "$(( ${p4:-0} * 100 / 255 ))" "$f4" \
    "$p5" "$(( ${p5:-0} * 100 / 255 ))" "$f5" "$p6" "$(( ${p6:-0} * 100 / 255 ))" "$f6" | tee -a "${CSV:-/dev/null}"
}

START=$(date +%s)
END=$(( START + MINUTES * 60 ))
REQS=0
TOT_COMPL=0
TOT_TIME=0

( while [ "$(date +%s)" -lt "$END" ]; do sample_line; sleep "$SAMPLE_SECS"; done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null' EXIT INT TERM

while [ "$(date +%s)" -lt "$END" ]; do
  t0=$(date +%s)
  resp=$(curl -s --max-time 900 "$BASE_URL/v1/chat/completions" -H 'Content-Type: application/json' \
         -d @"$PAYLOAD" -o /tmp/soak_resp.json -w '%{time_total}')
  compl=$(python3 -c "import json;print(json.load(open('/tmp/soak_resp.json'))['usage']['completion_tokens'])" 2>/dev/null || echo 0)
  dur=$(( $(date +%s) - t0 ))
  REQS=$((REQS+1)); TOT_COMPL=$((TOT_COMPL+compl)); TOT_TIME=$((TOT_TIME+dur))
  log "  request $REQS: $compl tokens in ${dur}s"
done

kill $SAMPLER 2>/dev/null
rm -f "$PAYLOAD"

log ""
log "================ summary ================"
log "requests: $REQS   generated: $TOT_COMPL tokens in ${TOT_TIME}s"
if [ "$TOT_TIME" -gt 0 ]; then
  awk -v c="$TOT_COMPL" -v t="$TOT_TIME" 'BEGIN{printf "mean throughput: %.1f tok/s (includes prefill)\n", c/t}'
fi
if [ -n "$CSV" ]; then
  log "samples: $CSV"
  python3 - "$CSV" <<'PY'
import re, sys, statistics
rows=[]
for line in open(sys.argv[1]):
    m=re.search(r'vram=\s*([\d.]+) C', line)
    if not m: continue
    d={'vram':float(m.group(1))}
    for k in ('pwm1','pwm2','pwm3','pwm4','pwm5','pwm6'):
        mm=re.search(k+r'=\s*(\d+)\(', line)
        if mm: d[k]=int(mm.group(1))
    for k in ('f1','f2','f3','f4','f5','f6'):
        mm=re.search(r'\b'+k+r'=\s*(\d+)', line)
        if mm: d[k]=int(mm.group(1))
    mm=re.search(r'cardfan=\s*(\d+)', line)
    if mm: d['card']=int(mm.group(1))
    rows.append(d)
if not rows:
    print("no samples parsed"); raise SystemExit
v=[r['vram'] for r in rows]
print(f"3090 VRAM: peak {max(v):.1f} C  mean {statistics.mean(v):.1f} C  last {v[-1]:.1f} C")
if 'card' in rows[0]:
    c=[r['card'] for r in rows]; print(f"card fan:    peak {max(c)}%  mean {statistics.mean(c):.0f}%")
print("header duty and tach:")
pairs=[('pwm1','f1','fan1'),('pwm2','f2','fan2'),('pwm3','f3','fan3'),('pwm4','f4','fan4'),('pwm5','f5','fan5'),('pwm6','f6','fan6')]
pins={'pwm1':134,'pwm2':133,'pwm3':203,'pwm4':205,'pwm5':147,'pwm6':178}
stall={'fan3':16,'fan4':14,'fan5':20,'fan6':30}
for p,f,ch in pairs:
    ds=[r[p] for r in rows if p in r]; fs=[r[f] for r in rows if f in r]
    if not ds: continue
    lo,hi=min(ds),max(ds)
    verdict=[]
    if hi < pins[p]: verdict.append(f"never exceeded its old BIOS pin {pins[p]} ({pins[p]*100//255}%)")
    else: verdict.append(f"exceeded its old BIOS pin {pins[p]} ({pins[p]*100//255}%)")
    if ch in stall and lo*100//255 < stall[ch]: verdict.append(f"BELOW STALL FLOOR {stall[ch]}%")
    if ch not in stall: verdict.append("not calibrated - no stall floor known")
    print(f"  {p} ({ch}): {lo}-{hi} duty ({lo*100//255}-{hi*100//255}%)  tach {min(fs)}-{max(fs)} RPM  :: " + ", ".join(verdict))

# Rough pressure balance.  Intake headers: pwm1 (a pair, counted twice) + pwm5.  Exhaust: pwm3 + pwm4 + pwm6.
def mean(k):
    v=[r[k] for r in rows if k in r]
    return sum(v)/len(v) if v else 0
in_duty = mean('pwm1')*2 + mean('pwm5')
out_duty = mean('pwm3') + mean('pwm4') + mean('pwm6')
if out_duty:
    print(f"airflow balance (mean duty, radiator pair counted twice): intake {in_duty:.0f} vs exhaust {out_duty:.0f} -> ratio {in_duty/out_duty:.2f}")
    print("  intake  = pwm1 (AIO radiator pair) + pwm5 (front bottom GPU intake)")
    print("  exhaust = pwm3 (rear) + pwm4 (top forward) + pwm6 (top rear)")
    print("  duty ratio is a proxy, not airflow: the radiator pair moves less air per percent than free-air fans")
PY
else
  log "no csv written"
fi
