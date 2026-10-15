#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}

stop_publisher() {
  kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s <<'SH'
set -eu
for entry in /proc/[0-9]*/cmdline; do
  [ -r "$entry" ] || continue
  command_line=$(tr '\000' ' ' < "$entry" 2>/dev/null) || continue
  case "$command_line" in
    *"bash /tmp/cache-session-profile.sh "*)
      process=${entry#/proc/}; process=${process%/cmdline}
      [ "$process" -gt 1 ] || continue
      kill -TERM "$process" 2>/dev/null || true
      ;;
  esac
done
rm -f /tmp/cache-session-profile.sh /tmp/cache-session-profile.pid /tmp/cache-session-profile.log /tmp/cache-session-profile.ready
SH
}

set_client_budget() {
  local budget=$1 response attempt
  for attempt in {1..20}; do
    response=$(kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s -- "user-timeline-redis.$NAMESPACE.svc.cluster.local" "$budget" 2>&1 <<'SH'
set -eu
exec 3<>"/dev/tcp/$1/6379"
printf 'CONFIG SET maxclients %s\r\n' "$2" >&3
IFS= read -r -t 3 response <&3
response=${response%$'\r'}
printf '%s\n' "${response#+}"
SH
    ) || continue
    if [[ $response == OK ]]; then return 0; fi
  done
  printf '%s\n' "Unable to restore cache connection capacity: $response" >&2
  return 1
}

get_client_budget() {
  kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s -- "user-timeline-redis.$NAMESPACE.svc.cluster.local" <<'SH'
set -eu
exec 3<>"/dev/tcp/$1/6379"
printf 'CONFIG GET maxclients\r\n' >&3
IFS= read -r -t 3 header <&3
[[ ${header%$'\r'} == '*2' ]] || exit 1
IFS= read -r -t 3 length <&3
IFS= read -r -t 3 key <&3
IFS= read -r -t 3 length <&3
IFS= read -r -t 3 value <&3
printf '%s\n%s\n' "${key%$'\r'}" "${value%$'\r'}"
SH
}
