#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
target="${1:?Redis deployment required}"
operation="${2:?GET or SET required}"
value="${3:-}"
# Use the native wire protocol; no database client installation is required.
kubectl exec -i -n "$NAMESPACE" "deployment/$target" -- bash -s -- "$operation" "$value" <<'RESPONSE'
set -euo pipefail
operation="$1"
value="$2"
exec 7<>/dev/tcp/127.0.0.1/6379
read_line() {
  IFS= read -r -t 10 line <&7
  line="${line%$'\r'}"
}
case "$operation" in
  GET)
    printf '*3\r\n$6\r\nCONFIG\r\n$3\r\nGET\r\n$21\r\nmin-replicas-to-write\r\n' >&7
    read_line; [[ "$line" == '*2' ]]
    read_line; [[ "$line" == '$21' ]]
    read_line; [[ "$line" == min-replicas-to-write ]]
    read_line; [[ "$line" == '$'* ]]
    read_line
    [[ "$line" =~ ^[0-9]+$ ]]
    printf '%s\n' "$line"
    ;;
  SET)
    [[ "$value" =~ ^[0-9]+$ ]]
    printf '*4\r\n$6\r\nCONFIG\r\n$3\r\nSET\r\n$21\r\nmin-replicas-to-write\r\n$%s\r\n%s\r\n' "${#value}" "$value" >&7
    read_line
    [[ "$line" == +OK ]]
    printf 'OK\n'
    ;;
  *) exit 1 ;;
esac
exec 7>&-
RESPONSE
