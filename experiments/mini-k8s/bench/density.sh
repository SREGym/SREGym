#!/usr/bin/env bash
# Tier-1 density: deploy hotel-reservation copies (real processes) in batches
# on one mini cluster and record host memory and CPU after each batch.
#
# usage: density.sh OUT.csv N1 N2 ...   (cumulative environment counts)
set -euo pipefail

OUT=$1; shift
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE/../../.."

used_mib() { awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf "%.0f", (t-a)/1024}' /proc/meminfo; }
cpu_busy() {  # cores busy over 20 s
  read -r _ u1 n1 s1 i1 w1 q1 sq1 st1 _ < /proc/stat; sleep 20
  read -r _ u2 n2 s2 i2 w2 q2 sq2 st2 _ < /proc/stat
  local busy=$(( (u2+n2+s2+q2+sq2+st2) - (u1+n1+s1+q1+sq1+st1) ))
  echo "scale=2; $busy / 100 / 20" | bc
}

echo "envs,pods,host_used_mib,cpu_cores_busy,deploy_s" > "$OUT"
echo "0,$(kubectl get pods -A --no-headers | wc -l),$(used_mib),$(cpu_busy),0" | tee -a "$OUT"
have=0
for target in "$@"; do
  t0=$(date +%s)
  uv run python "$HERE/multi_env.py" $((target - have)) --start "$have" 2>&1 | grep -E "^ENV|Error" || true
  have=$target
  sleep 30  # let mongo/consul settle before measuring
  echo "$target,$(kubectl get pods -A --no-headers | wc -l),$(used_mib),$(cpu_busy),$(( $(date +%s) - t0 ))" | tee -a "$OUT"
done
uv run python "$HERE/multi_env.py" "$have" --check-only 2>&1 | grep -E "^CHECK" | sort | uniq -c | sort -rn | head
