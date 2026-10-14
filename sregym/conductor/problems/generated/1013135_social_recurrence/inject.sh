#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
mkdir -p "$ARTIFACT_DIR"
python3 - "$ARTIFACT_DIR/original-deployments.json" "$NAMESPACE" <<'PY'
import json, pathlib, subprocess, sys
state = pathlib.Path(sys.argv[1])
names = ["post-storage-service", "user-timeline-service", "user-mention-service",
         "user-service", "text-service", "compose-post-service"]
live = json.loads(subprocess.check_output(["kubectl", "-n", sys.argv[2],
                                          "get", "deployment", *names, "-o", "json"]))
def identities(objects):
    return {d["metadata"]["name"]: d["metadata"]["uid"] for d in objects["items"]}
try:
    saved = json.loads(state.read_text())
except (FileNotFoundError, ValueError):
    saved = None
if saved is None or identities(saved) != identities(live):
    state.write_text(json.dumps(live))
PY
kubectl -n "$NAMESPACE" rollout status deployment/media-mongodb --timeout=180s
kubectl -n "$NAMESPACE" exec -i deployment/media-mongodb -- bash -s <<'REMOTE'
set -euo pipefail
active=0
if [[ -r /tmp/content-cache-refresh.pid ]]; then
  read -r pid </tmp/content-cache-refresh.pid
  if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    args=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    if [[ $args == *'bash /tmp/content-cache-refresh.sh'* ]]; then
      group=$(awk '{print $5}' "/proc/$pid/stat")
      if [[ $group == "$pid" ]]; then
        kill -KILL -- "-$pid" 2>/dev/null || true
      else
        kill -KILL "$pid" 2>/dev/null || true
      fi
    fi
  fi
fi
if (( ! active )); then
cat >/tmp/content-cache-refresh.sh <<'PROGRAM'
#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
publish() {
  mongo --quiet --host post-storage-mongodb --eval '
    var c=db.getSiblingDB("post").post;
    c.find().sort({_id:-1}).limit(4096).forEach(function(d) {
      var key=d.post_id.toString().replace(/^NumberLong\("?/, "").replace(/"?\)$/, "");
      delete d._id;
      var fields=[];
      Object.keys(d).forEach(function(k) { fields.push([k,d[k]]); });
      print("post\t"+key+"\t"+tojsononeline(fields));
      print("user\t"+d.creator.username+":user_id\t"+tojsononeline([["user_id",d.creator.user_id],["username",d.creator.username]]));
    });
  ' >/tmp/content-cache-refresh.records.part || return 1
  sed -E 's/NumberLong\("?(-?[0-9]+)"?\)/\1/g' /tmp/content-cache-refresh.records.part >/tmp/content-cache-refresh.records || return 1
  rm -f /tmp/content-cache-refresh.records.part
  [[ -s /tmp/content-cache-refresh.records ]] || return 1
  local first first_user reply
  first=$(awk -F '\t' '$1=="post" {print $2; exit}' /tmp/content-cache-refresh.records)
  first_user=$(awk -F '\t' '$1=="user" {print $2; exit}' /tmp/content-cache-refresh.records)
  [[ -n $first && -n $first_user ]] || return 1
  exec 3<>/dev/tcp/post-storage-memcached/11211 || return 1
  awk -F '\t' '$1=="post" {printf "set %s 0 0 %d noreply\r\n%s\r\n", $2, length($3), $3} END {printf "version\r\n"}' /tmp/content-cache-refresh.records >&3
  IFS= read -r -t 10 reply <&3
  [[ $reply == VERSION* ]]
  exec 3<&-
  exec 3>&-
  exec 5<>/dev/tcp/user-memcached/11211 || return 1
  awk -F '\t' '$1=="user" {printf "set %s 0 0 %d noreply\r\n%s\r\n", $2, length($3), $3} END {printf "version\r\n"}' /tmp/content-cache-refresh.records >&5
  IFS= read -r -t 10 reply <&5
  [[ $reply == VERSION* ]]
  exec 5<&-
  exec 5>&-
  printf '%s\n%s\n' "$first" "$first_user" >/tmp/content-cache-refresh.cursor
  rm -f /tmp/content-cache-refresh.records
}
record_is_pairs() {
  local host=$1 key=$2 header word flags bytes body
  exec 4<>"/dev/tcp/$host/11211" || return 1
  printf 'get %s\r\n' "$key" >&4
  if ! IFS= read -r -t 3 header <&4; then
    exec 4<&-; exec 4>&-; return 1
  fi
  read -r word key flags bytes <<<"${header%$'\r'}"
  if [[ $word != VALUE || ! $bytes =~ ^[0-9]+$ ]]; then
    exec 4<&-; exec 4>&-; return 1
  fi
  if ! IFS= read -r -N "$bytes" -t 3 body <&4; then
    exec 4<&-; exec 4>&-; return 1
  fi
  exec 4<&-
  exec 4>&-
  [[ $body == '['* ]]
}
pairs_present() {
  local -a keys
  mapfile -t keys </tmp/content-cache-refresh.cursor || return 1
  record_is_pairs post-storage-memcached "${keys[0]:-}" &&
    record_is_pairs user-memcached "${keys[1]:-}"
}
if [[ ${1:-} == --publish ]]; then
  publish
  exit 0
fi
deadline=$((SECONDS + 130))
was_pairs=1
while :; do
  sleep 20
  now_pairs=0
  pairs_present && now_pairs=1
  if (( was_pairs && ! now_pairs )); then
    deadline=$((SECONDS + 130))
  fi
  was_pairs=$now_pairs
  if (( SECONDS >= deadline )); then
    if publish; then
      was_pairs=1
    fi
    deadline=$((SECONDS + 130))
  fi
done
PROGRAM
chmod 700 /tmp/content-cache-refresh.sh
fi
# Complete the publication before returning, including on repeated calls.
bash /tmp/content-cache-refresh.sh --publish
if (( ! active )); then
  setsid nohup bash /tmp/content-cache-refresh.sh </dev/null >/tmp/content-cache-refresh.log 2>&1 &
  pid=$!
  printf '%s\n' "$pid" >/tmp/content-cache-refresh.pid
  kill -0 "$pid"
fi
REMOTE
