#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}

holder=$(kubectl -n "$NAMESPACE" get pods -l service=media-mongodb -o jsonpath='{.items[0].metadata.name}')
[[ -n "$holder" ]]

kubectl -n "$NAMESPACE" exec -i "$holder" -- bash -s <<'REMOTE'
set -euo pipefail
utility=/tmp/timeline-checkpoint-refresh.sh
pidfile=/tmp/timeline-checkpoint-refresh.pid
if [[ -f "$pidfile" ]]; then
  read -r oldpid < "$pidfile"
  if [[ "$oldpid" =~ ^[0-9]+$ ]] && kill -0 "$oldpid" 2>/dev/null; then
    commandline=$(tr '\0' ' ' < "/proc/$oldpid/cmdline" 2>/dev/null || :)
    if [[ "$commandline" == *"bash $utility"* ]]; then
      bash "$utility" --once
      exit 0
    fi
  fi
fi
cat > "$utility" <<'UTILITY'
#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C
redis_request() {
  local destination=$1
  shift
  local connection argument reply count length i
  exec {connection}<>"/dev/tcp/$destination/6379" || return 1
  printf '*%d\r\n' "$#" >&"$connection"
  for argument in "$@"; do
    printf '$%d\r\n%s\r\n' "${#argument}" "$argument" >&"$connection"
  done
  IFS= read -r reply <&"$connection" || return 1
  reply=${reply%$'\r'}
  case ${reply:0:1} in
    :|+) printf '%s\n' "${reply:1}" ;;
    '*')
      count=${reply:1}
      for ((i=0; i<count; i++)); do
        IFS= read -r length <&"$connection" || return 1
        [[ ${length:0:1} == '$' ]] || return 1
        IFS= read -r reply <&"$connection" || return 1
        printf '%s\n' "${reply%$'\r'}"
      done
      ;;
    *) printf '%s\n' "$reply" >&2; exec {connection}<&-; exec {connection}>&-; return 1 ;;
  esac
  exec {connection}<&-
  exec {connection}>&-
}
inventory='
local keys = {}
local cursor = "0"
repeat
  local page = redis.call("SCAN", cursor, "COUNT", 512)
  cursor = page[1]
  for _, key in ipairs(page[2]) do
    if string.match(key, "^%d+$") and redis.call("TYPE", key).ok == "zset" then
      table.insert(keys, key)
    end
  end
until cursor == "0"
return keys
'
program='
local count = 0
for _, key in ipairs(KEYS) do
  local kind = redis.call("TYPE", key).ok
  if string.match(key, "^%d+$") and (kind == "zset" or kind == "none") then
    local inner = "0"
    repeat
      local entries = redis.call("ZSCAN", key, inner, "MATCH", "-*", "COUNT", 128)
      inner = entries[1]
      for i = 1, #entries[2], 2 do
        local member = entries[2][i]
        if string.match(member, "^%-%d%d%d%d%d%d%d%d%d%d%d%d%d$") then
          redis.call("ZREM", key, member)
        end
      end
    until inner == "0"
    for lane = 0, 127 do
      local checkpoint = string.format("%.0f", -tonumber(ARGV[1]) - lane)
      redis.call("ZADD", key, tonumber(ARGV[2]) + lane, checkpoint)
    end
    count = count + 1
  end
end
return count
'
refresh() {
  local epoch ceiling destination
  local -a users
  epoch=$(date +%s%3N)
  ceiling=$((epoch + 86400000))
  mapfile -t users < <(redis_request user-timeline-redis EVAL "$inventory" 0)
  ((${#users[@]} > 0)) || return 0
  for destination in home-timeline-redis user-timeline-redis; do
    redis_request "$destination" EVAL "$program" "${#users[@]}" "${users[@]}" "$epoch" "$ceiling"
  done
}
if [[ ${1:-} == --once ]]; then
  refresh
  exit $?
fi
while :; do
  refresh >/dev/null 2>&1 || :
  sleep 180
done
UTILITY
chmod 700 "$utility"
# The first publication completes before returning; the utility then stays resident.
bash "$utility" --once
nohup bash "$utility" </dev/null >/dev/null 2>&1 &
printf '%s\n' "$!" > "$pidfile"
REMOTE
