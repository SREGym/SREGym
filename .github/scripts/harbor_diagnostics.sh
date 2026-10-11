#!/usr/bin/env bash
# Print the host summary for every trial under a Harbor jobs directory. With
# --logs, also print the end of each collected log, and the whole cluster state
# the backend records when setup fails.
# Usage: harbor_diagnostics.sh <jobs-dir> [--logs]
set -u
jobs_dir=$1

find "$jobs_dir" -name environment.txt -print0 | while IFS= read -r -d '' report; do
    echo "== ${report#"$jobs_dir"/}"
    grep '^summary:' "$report" || echo "(no summary)"
done

if [[ ${2:-} == --logs ]]; then
    find "$jobs_dir" -type f \( -name '*.log' -o -name status.json -o -name grade.json \) -print0 |
        while IFS= read -r -d '' log; do
            echo "== ${log#"$jobs_dir"/} (last 60 lines)"
            tail -n 60 "$log"
        done
    find "$jobs_dir" -type f -name cluster-state.txt -print0 |
        while IFS= read -r -d '' state; do
            echo "== ${state#"$jobs_dir"/}"
            cat "$state"
        done
fi
exit 0
