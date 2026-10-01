#!/usr/bin/env bash
# Print the SREGym sidecar's host summary for every trial under a Harbor jobs
# directory. With --logs, also print the end of each collected sidecar log.
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
fi
exit 0
