#!/bin/sh
set -eu
mkdir -p /root/.ssh /etc/platform /var/log/platform /state
cp /bootstrap/authorized_keys /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
if [ ! -f /etc/platform/initialized ]; then
  cp /bootstrap/*.json /etc/platform/
  touch /etc/platform/initialized
fi
/usr/sbin/sshd -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no
case "$ROLE" in
  worker)
    # Delegate this container's private cgroup subtree to Nomad/Docker. The
    # cgroup v2 no-internal-process rule requires an empty parent first.
    if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
      mkdir -p /sys/fs/cgroup/init
      for pid in $(cat /sys/fs/cgroup/cgroup.procs); do
        echo "$pid" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
      done
      sed 's/\([^ ]*\)/+\1/g' /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
    fi
    dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 > /var/log/platform/docker.log 2>&1 &
    until docker info >/dev/null 2>&1; do sleep 1; done
    consul agent -config-file=/etc/platform/consul.json > /var/log/platform/consul.log 2>&1 &
    nomad agent -config=/etc/platform/nomad.json > /var/log/platform/nomad.log 2>&1 &
    ;;
  consul) consul agent -config-file=/etc/platform/consul.json > /var/log/platform/consul.log 2>&1 & ;;
  nomad) nomad agent -config=/etc/platform/nomad.json > /var/log/platform/nomad.log 2>&1 & ;;
  vault) vault server -config=/etc/platform/vault.json > /var/log/platform/vault.log 2>&1 & ;;
esac
# Keep SSH available for repair even when a managed daemon exits. Daemons can
# be restarted with their native command; process liveness is not task success.
# Keep the shell as the parent so it reaps the daemons it started. Exec'ing
# tail here would leave exited children as zombies under a non-reaping parent.
while true; do sleep 60 & wait "$!"; done
