# Swift ring operations

The builder files in this directory are the source of truth for the cluster's
rings. Every proxy and storage node installs the `*.ring.gz` served from here
within about ten seconds of it changing. `swift-ring-builder` writes a copy of
the builder and ring to `backups/` on every change, named by Unix time.

## Decommission a failed drive (DCOPS)

    TARGET=z<zone>-<storage host>:6200
    swift-ring-builder object.builder remove $TARGET/<device> --yes
    swift-ring-builder object.builder rebalance

Record the change in `changes.log`.

## Inspect

    swift-ring-builder object.builder                 # current devices, balance
    swift-ring-builder backups/<time>.object.builder  # an earlier builder
    swift-ring-builder object.ring.gz                 # the ring being served
