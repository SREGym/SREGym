#!/usr/bin/env bash
# Report the host facts that decide whether nested Docker and KIND can run here:
# kernel, cgroups, privilege and user-namespace isolation, kernel modules,
# limits and registry reachability. Every probe is bounded and best effort, so
# the report also works on hosts where setup is about to fail. It never prints
# environment variables, which can hold credentials.
set -u

section() { printf '\n[%s]\n' "$1"; }
kv() { printf '%-26s %s\n' "$1:" "$2"; }
first_line() { head -n 1 "$1" 2>/dev/null || echo unavailable; }

section host
kv kernel "$(uname -r)"
kv architecture "$(uname -m)"
kv cpus "$(nproc 2>/dev/null || echo unknown)"
kv memory "$(awk '/^MemTotal:/ {printf "%.1f GiB", $2 / 1048576}' /proc/meminfo 2>/dev/null)"
docker_dir=/var/lib/docker
kv docker_data "$(df -hP "$docker_dir" 2>/dev/null | awk 'NR == 2 {print $4 " free of " $2}') ($(stat -fc %T "$docker_dir" 2>/dev/null || echo unknown))"

section isolation
if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
    cgroup_version=2
    kv cgroup "v2: $(cat /sys/fs/cgroup/cgroup.controllers)"
    kv cgroup_memory_max "$(first_line /sys/fs/cgroup/memory.max)"
    kv cgroup_cpu_max "$(first_line /sys/fs/cgroup/cpu.max)"
else
    cgroup_version=1
    kv cgroup "v1 (KIND worker nodes have failed to start on cgroup v1 hosts)"
    kv cgroup_memory_max "$(first_line /sys/fs/cgroup/memory/memory.limit_in_bytes)"
fi
kv cgroup_path "$(first_line /proc/self/cgroup)"
cap_eff=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
# CAP_SYS_ADMIN is bit 21.
if (( (16#$cap_eff >> 21) & 1 )); then sys_admin=yes; else sys_admin=no; fi
if awk '$5 == "/sys" {exit ($6 ~ /^rw/) ? 0 : 1}' /proc/self/mountinfo; then sys_mount=rw; else sys_mount=ro; fi
seccomp=$(awk '/^Seccomp:/ {print ($2 == 0 ? "disabled" : $2 == 2 ? "filtering" : $2)}' /proc/self/status)
# Docker's --privileged grants CAP_SYS_ADMIN, disables seccomp and mounts /sys read-write.
if [[ $sys_admin == yes && $seccomp == disabled && $sys_mount == rw ]]; then privileged=yes; else privileged=no; fi
kv privileged "$privileged (CAP_SYS_ADMIN $sys_admin, seccomp $seccomp, /sys $sys_mount, CapEff $cap_eff)"
uid_map=$(awk '{$1 = $1; print}' /proc/self/uid_map 2>/dev/null | head -n 1)
if [[ $uid_map == "0 0 4294967295" ]]; then userns=host; else userns="mapped ($uid_map)"; fi
kv user_namespace "$userns"
if grep -q sysboxfs /proc/self/mountinfo 2>/dev/null; then sysbox=yes; else sysbox=no; fi
kv sysbox "$sysbox"
kv apparmor "$(tr -d '\0' < /proc/self/attr/current 2>/dev/null || echo unavailable)"

section kernel
# Kernel features SREGym's cluster relies on. Containers cannot load modules,
# so a feature built as a module must already be loaded by the host.
kernel_config=$( (zcat /proc/config.gz || cat "/boot/config-$(uname -r)") 2>/dev/null)
missing=()
for entry in overlay:OVERLAY_FS br_netfilter:BRIDGE_NETFILTER nf_tables:NF_TABLES ip_tables:IP_NF_IPTABLES \
    iptable_nat:IP_NF_NAT nf_conntrack:NF_CONNTRACK ip_set:IP_SET xt_set:NETFILTER_XT_SET \
    ipip:NET_IPIP vxlan:VXLAN sch_netem:NET_SCH_NETEM; do
    module=${entry%%:*}
    config=$(grep -m 1 "^CONFIG_${entry#*:}=" <<<"$kernel_config" | cut -d= -f2)
    if grep -q "^$module " /proc/modules 2>/dev/null || [[ -d /sys/module/$module/sections ]]; then
        status=loaded
    elif [[ $config == y ]]; then
        status="built in"
    elif [[ $config == m ]]; then
        status="module not loaded (load it on the host)"
        missing+=("$module")
    elif [[ -n $kernel_config ]]; then
        status="not in this kernel"
        missing+=("$module")
    elif [[ -d /sys/module/$module ]]; then
        status="built in"
    else
        status="unknown (not loaded; kernel config unavailable)"
        missing+=("$module?")
    fi
    kv "$module" "$status"
done
for device in /dev/kmsg /dev/fuse /dev/net/tun /dev/loop-control; do
    if [[ -e $device ]]; then kv "device $device" present; else kv "device $device" absent; fi
done
for sysctl in fs.inotify.max_user_instances fs.inotify.max_user_watches kernel.keys.maxkeys \
    kernel.pid_max net.ipv4.ip_forward vm.overcommit_memory; do
    kv "sysctl $sysctl" "$(first_line "/proc/sys/${sysctl//.//}")"
done

section egress
# Registries and chart repositories that SREGym problems pull from at setup.
hosts=(
    mirror.gcr.io registry-1.docker.io ghcr.io registry.k8s.io quay.io
    github.com raw.githubusercontent.com charts.chaos-mesh.org openebs.github.io
    prometheus-community.github.io grafana.github.io kubernetes.github.io
    open-telemetry.github.io jaegertracing.github.io opensearch-project.github.io helm.elastic.co
)
reachable=0
if [[ ${SREGYM_ENV_REPORT_EGRESS:-1} == 1 ]]; then
    tmp=$(mktemp -d)
    for host in "${hosts[@]}"; do
        # Any HTTP status means the host answered; 000 means it did not.
        curl -sS -o /dev/null -m 8 -w '%{http_code}' "https://$host/" >"$tmp/$host" 2>/dev/null &
    done
    wait
    for host in "${hosts[@]}"; do
        code=$(cat "$tmp/$host" 2>/dev/null)
        if [[ -n $code && $code != 000 ]]; then
            kv "$host" "reachable (HTTP $code)"
            reachable=$((reachable + 1))
        else
            kv "$host" unreachable
        fi
    done
    rm -rf "$tmp"
    egress="$reachable/${#hosts[@]} hosts reachable"
else
    egress="not checked"
fi

section summary
summary="kernel $(uname -r), cgroup v$cgroup_version, privileged $privileged, user namespace $userns, sysbox $sysbox"
summary+=", $(nproc 2>/dev/null) CPUs, $(awk '/^MemTotal:/ {printf "%.0f GiB", $2 / 1048576}' /proc/meminfo)"
summary+=", kernel features unavailable: ${missing[*]:-none}, egress: $egress"
echo "summary: $summary"
