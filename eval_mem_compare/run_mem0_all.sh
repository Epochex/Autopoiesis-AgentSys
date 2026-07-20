#!/usr/bin/env bash
# Run the real mem0ai over all 500 LongMemEval items, sharded across parallel
# processes (each item is independent -> exact same result as one pass, merged).
set -u
cd /data/asys-mem
PY=.venv-memcmp/bin/python
export PYTHONPATH=/data/asys-mem:/data/asys-mem/eval_mem_compare
export OPENAI_API_KEY=sk-dummy-never-called
export MEM0_TELEMETRY=False
export TOKENIZERS_PARALLELISM=false
# 10 shards of 50 items; cap threads so 10 procs share 24 cores without thrash.
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
LOG=/data/asys-mem/eval_mem_compare/mem0_shards
mkdir -p "$LOG"
rm -f eval_mem_compare/results_mem0_shard*.json
rm -rf eval_mem_compare/qdrant_shards
pids=()
for start in 0 50 100 150 200 250 300 350 400 450; do
  end=$((start+50))
  $PY eval_mem_compare/run_mem0.py tmp/longmemeval_s.json eval_mem_compare/results_mem0.json \
      --start=$start --end=$end > "$LOG/shard_${start}.log" 2>&1 &
  pids+=($!)
done
echo "launched ${#pids[@]} shards: ${pids[*]}"
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=$((fail+1)); done
echo "all shards finished, failures=$fail"
ls -1 eval_mem_compare/results_mem0_shard*.json | wc -l | xargs echo "shard result files:"
$PY eval_mem_compare/merge_mem0.py 'eval_mem_compare/results_mem0_shard*.json' eval_mem_compare/results_mem0.json
echo "MEM0_ALL_DONE failures=$fail"
