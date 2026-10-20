#!/bin/bash
# GLM 5.3 Flash on the A6000 VM through the UnbiasedStrata 2 fork (fresh tree ~/us2-fresh/UnbiasedStrata).
#   serve-glm-a6000.sh reap50   REAP50-Q3_K_M (patrickbdevaney), 79 GB: experts fit VRAM + RAM on this box
#   serve-glm-a6000.sh 3.0bit   GSQ-RCO 3.0bit (neuralll), 114 GB: experts beyond VRAM + page cache read from disk
# Engine flags as docs/GLM.md's installer example (--max-context, --expert-profile seeded from the fork's data/).
# reasoning_effort low as the server default: the template treats anything but low/high as max (README, Known limits).
case "${1:-reap50}" in
  # reap50: NVMe copy removed 2026-10-20 to make room for 3.0bit; GLM_REAP50=/mnt/ds4/glm/<file> after copying it back
  reap50) DRAFT=""; M=${GLM_REAP50:-/mnt/models/glm/GLM-5.3-Flash-REAP50-Q3_K_M.gguf}; SEED=expert-profile-glm-reap50.txt; ALIAS=glm-5.3-flash-reap50-q3_k_m-a6000;;
  3.0bit) M=/mnt/ds4/glm/GLM-5.3-Flash-GSQ-RCO-3.0bit-q4kattn.gguf; SEED=expert-profile-glm.txt; ALIAS=glm-5.3-flash-3.0bit-a6000
          DRAFT="--draft-model /mnt/ds4/glm/glm-5.3-flash-draft-unsloth-621d456e.gguf";;   # the file has no draft block: tools/glm_draft_fetch.py block
  *) echo "reap50|3.0bit"; exit 2;;
esac
B=~/us2-fresh/UnbiasedStrata/build/bin
P=~/us2-glm/${1:-reap50}.profile
mkdir -p ~/us2-glm
[ -s "$P" ] || cp ~/us2-fresh/UnbiasedStrata/data/$SEED "$P"
[ -f "$M" ] || { echo "missing $M"; exit 1; }
export CUDA_MODULE_LOADING=EAGER
ulimit -l unlimited 2>/dev/null
echo "$(date -u +%FT%TZ) start ${1:-reap50} ctx ${GLM_CTX:-65536} port 18093" >> ~/us2-glm/starts.log
exec $B/unbiased-strata --engine $B/strata-poly -m "$M" --host 127.0.0.1 --port 18093 --alias $ALIAS \
  --sse-ping-interval 10 --chat-template-kwargs '{"reasoning_effort":"low"}' \
  -- --model "$M" --max-context ${GLM_CTX:-65536} --expert-profile "$P" $DRAFT
