#!/usr/bin/env bash
# Fair full-text dense (max-pool chunks) over all 500 items, sharded.
set -u
cd /data/asys-mem
PY=.venv-memcmp/bin/python
export PYTHONPATH=/data/asys-mem:/data/asys-mem/eval_mem_compare
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
LOG=/data/asys-mem/eval_mem_compare/chunk_shards
mkdir -p "$LOG"
rm -f eval_mem_compare/results_chunked_shard*.json
pids=()
for start in 0 50 100 150 200 250 300 350 400 450; do
  end=$((start+50))
  $PY eval_mem_compare/run_chunked.py tmp/longmemeval_s.json eval_mem_compare/results_chunked.json \
      --start=$start --end=$end > "$LOG/shard_${start}.log" 2>&1 &
  pids+=($!)
done
echo "launched ${#pids[@]} chunked shards"
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=$((fail+1)); done
echo "chunked shards done, failures=$fail"
$PY eval_mem_compare/merge_generic.py 'eval_mem_compare/results_chunked_shard*.json' eval_mem_compare/results_chunked.json
echo "CHUNKED_ALL_DONE failures=$fail"
