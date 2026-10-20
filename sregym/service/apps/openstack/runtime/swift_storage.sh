#!/bin/bash
# Run one Swift storage node: account, container and object servers, their
# replicators and updaters, and rsync for replication. If any process exits the
# container exits, and Kubernetes restarts it.
set -eu
for device in $DEVICES; do mkdir -p "/srv/node/$device"; done
mkdir -p /var/cache/swift /var/lock /var/run/swift
chown -R swift:swift /srv/node /var/cache/swift
until [ -s /etc/swift/object.ring.gz ] && [ -s /etc/swift/container.ring.gz ] && [ -s /etc/swift/account.ring.gz ]; do
  sleep 2
done
rsync --daemon --no-detach --config=/etc/swift/rsyncd.conf &
for kind in account container object; do
  swift-$kind-server /etc/swift/$kind-server.conf -v &
  swift-$kind-replicator /etc/swift/$kind-server.conf -v &
done
swift-container-updater /etc/swift/container-server.conf -v &
swift-object-updater /etc/swift/object-server.conf -v &
wait -n
echo "a Swift process exited; restarting the node" >&2
exit 1
