#!/bin/bash
# Plan section 4 run order step 6: strata-poly --check-batch 4 on the short set, both Qwen3.6 files. Each short
# prompt gives 4 prompts (the prompt minus its first 0..3 tokens), answered alone and then together; exit 1 on a
# differing token.
cd ~/us2-results/correctness || exit 1
M=/mnt/ds4/unbiased-strata/qwen36
for tag in qwen36-q3km:Qwen3.6-35B-A3B-UD-Q3_K_M.gguf qwen36-iq4xs:Qwen3.6-35B-A3B-UD-IQ4_XS.gguf; do
  name=${tag%%:*}; file=${tag#*:}; out=checkbatch/$name; mkdir -p $out
  for p in inputs/short/p*.prompt.ids; do
    seq=$(basename $p .prompt.ids)
    cmd="STRATA_FREEZE_EXPERTS=1 /home/rk/us2-build/build/bin/strata-poly --model $M/$file --max-context 8192 --tail-skip 0 --no-draft --lookup 0 --window 1 --tokens \$(cat $p) --batch 4 --check-batch 4 --max-new 64"
    echo "$cmd # > $out/$seq.log" >> $out/command.sh
    STRATA_FREEZE_EXPERTS=1 /home/rk/us2-build/build/bin/strata-poly --model $M/$file --max-context 8192 --tail-skip 0 --no-draft --lookup 0 --window 1 --tokens $(cat $p) --batch 4 --check-batch 4 --max-new 64 > $out/$seq.log 2>&1
    rc=$?
    echo "$name $seq: exit $rc; $(grep "^check-batch" $out/$seq.log | tr "\n" ";")"
  done
done
