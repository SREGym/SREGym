#!/usr/bin/env bash
set -u
target=$1
ready_path=$2
client_budget=$3
connected=0
first=1
deadline=0
RESP=

read_reply() {
  local line count i body
  IFS= read -r -t 3 line <&3 || return 1
  line=${line%$'\r'}
  case ${line:0:1} in
    '+'|':') RESP=${line:1} ;;
    '-') RESP=${line:1}; return 1 ;;
    '$')
      count=${line:1}
      if (( count < 0 )); then RESP=; return 0; fi
      IFS= read -r -t 3 body <&3 || return 1
      RESP=${body%$'\r'}
      ;;
    '*')
      count=${line:1}
      for ((i=0;i<count;i++)); do read_reply || return 1; done
      ;;
    *) return 1 ;;
  esac
}

request() {
  printf '%s\r\n' "$*" >&3 || return 1
  read_reply
}

publish() {
  request CONFIG SET maxclients "$client_budget" || return 1
  request CLIENT KILL TYPE normal SKIPME yes || return 1
  deadline=0
}

trap '' PIPE
trap 'request CONFIG SET maxclients 10000 || true; exit 0' TERM INT
while :; do
  if (( connected == 0 )); then
    if exec 3<>"/dev/tcp/$target/6379"; then
      if request CLIENT SETNAME timeline-session-profile; then
        connected=1
      else
        exec 3>&-
      fi
    fi
    if (( connected == 0 )); then sleep 5; continue; fi
  fi
  if ! request CONFIG GET maxclients; then
    exec 3>&-
    connected=0
    if (( first == 0 )); then deadline=$((SECONDS+180)); fi
    sleep 5
    continue
  fi
  if (( first == 1 )); then
    if publish; then
      first=0
      printf 'ready\n' > "$ready_path"
    else
      exec 3>&-
      connected=0
    fi
  elif [[ $RESP != "$client_budget" ]]; then
    if (( deadline == 0 )); then deadline=$((SECONDS+180)); fi
    if (( SECONDS >= deadline )); then
      if ! publish; then exec 3>&-; connected=0; fi
    fi
  else
    deadline=0
  fi
  sleep 5
done
