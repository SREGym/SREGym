#!/usr/bin/env bash
set -uo pipefail
trap 'exit 0' TERM INT
while true; do
  if result=$(mongo --host 127.0.0.1 profile-db --quiet /etc/catalog/retention.js); then
    printf '%s\n' "$result"
    while IFS='=' read -r marker key; do
      if [[ "$marker" == expired_catalog_key ]]; then
        if exec 3<>/dev/tcp/memcached-profile/11211; then
          printf 'delete %s\r\n' "$key" >&3
          IFS= read -r reply <&3 || true
          exec 3<&- 3>&-
        else
          printf 'catalog cache invalidation unavailable\n' >&2
        fi
      fi
    done <<< "$result"
    [[ "${1:-}" == once ]] && exit 0
  elif [[ "${1:-}" == once ]]; then
    exit 1
  fi
  sleep 10
done
