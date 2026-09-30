#!/bin/sh
set -eu

# Modern containerd can inherit a billion-descriptor soft limit. MySQL 5.7
# allocates bookkeeping from that limit, even for --verbose --help, and OOMs.
# Match Percona's configured open_files_limit without raising smaller limits.
nofile_limit="$(ulimit -n)"
if [ "$nofile_limit" = unlimited ] || [ "$nofile_limit" -gt 655360 ]; then
    ulimit -S -n 655360
fi

exec "$@"
