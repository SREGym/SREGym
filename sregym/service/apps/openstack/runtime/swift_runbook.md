# Swift ring operations

The builder files in this directory are the source of truth for the cluster's
rings. Every proxy and storage node installs the `*.ring.gz` served from here
within about ten seconds of it changing. `swift-ring-builder` writes a copy of
the builder and ring to `backups/` on every change, named by Unix time.

## Decommission a failed drive (DCOPS)

Find the drive's device id, remove that id, rebalance:

    swift-ring-builder object.builder search --ip <storage host> --device <name>
    swift-ring-builder object.builder remove d<id> --yes
    swift-ring-builder object.builder rebalance

In a search value `d<id>` is a device id and `/<name>` is a device name.
Record the change in `changes.log`.

## Inspect

    swift-ring-builder object.builder                 # current devices, balance
    swift-ring-builder backups/<time>.object.builder  # an earlier builder
    swift-ring-builder object.ring.gz                 # the ring being served
