#!/usr/bin/env bash
set -euo pipefail
declare -A TARGET_PIDS
for COMMAND_FILE in /proc/[0-9]*/cmdline; do
  PROCESS_ID="${COMMAND_FILE#/proc/}"
  PROCESS_ID="${PROCESS_ID%/cmdline}"
  [[ "$PROCESS_ID" != 1 ]] || continue
  while IFS= read -r -d '' ARGUMENT; do
    if [[ "$ARGUMENT" == /tmp/directory_refresh.js ]]; then
      TARGET_PIDS["$PROCESS_ID"]=1
      kill -STOP "$PROCESS_ID" 2>/dev/null || true
    fi
  done < "$COMMAND_FILE" 2>/dev/null || true
done
CHANGED=1
while [[ "$CHANGED" == 1 ]]; do
  CHANGED=0
  for STATUS_FILE in /proc/[0-9]*/stat; do
    if read -r PROCESS_ID PROCESS_NAME PROCESS_STATE PARENT_ID OTHER < "$STATUS_FILE" 2>/dev/null; then
      if [[ -v TARGET_PIDS[$PARENT_ID] && ! -v TARGET_PIDS[$PROCESS_ID] ]]; then
        TARGET_PIDS["$PROCESS_ID"]=1
        kill -STOP "$PROCESS_ID" 2>/dev/null || true
        CHANGED=1
      fi
    fi
  done
done
for PROCESS_ID in "${!TARGET_PIDS[@]}"; do
  kill -TERM "$PROCESS_ID" 2>/dev/null || true
  kill -CONT "$PROCESS_ID" 2>/dev/null || true
done
ACTIVE=1
ATTEMPT=0
while [[ "$ACTIVE" == 1 && "$ATTEMPT" -lt 100 ]]; do
  ACTIVE=0
  for PROCESS_ID in "${!TARGET_PIDS[@]}"; do
    if read -r PID_VALUE PROCESS_NAME PROCESS_STATE OTHER < "/proc/$PROCESS_ID/stat" 2>/dev/null; then
      [[ "$PROCESS_STATE" == Z ]] || ACTIVE=1
    fi
  done
  if [[ "$ACTIVE" == 1 ]]; then sleep 0.1; fi
  ATTEMPT=$((ATTEMPT + 1))
done
if [[ "$ACTIVE" == 1 ]]; then
  for PROCESS_ID in "${!TARGET_PIDS[@]}"; do kill -KILL "$PROCESS_ID" 2>/dev/null || true; done
fi
rm -f /tmp/directory_refresh.js /tmp/directory_refresh.pid /tmp/directory_refresh.log /tmp/directory_refresh.*.response
