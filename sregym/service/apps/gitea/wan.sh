set -eu
# The node's own interface toward the other region, whatever this node calls it.
first=${PEERS%% *}
DEV=$(ip -o route get "${first%/*}" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
echo "$DEV" > /tmp/wan-dev
tc qdisc del dev "$DEV" root 2>/dev/null || true
tc qdisc add dev "$DEV" root handle 1: prio bands 4 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
tc qdisc add dev "$DEV" parent 1:4 handle 40: netem delay "$DELAY"
for peer in $PEERS; do tc filter add dev "$DEV" parent 1:0 protocol ip prio 1 u32 match ip dst "$peer" flowid 1:4; done
trap 'tc qdisc del dev "$DEV" root; exit 0' TERM INT
while :; do sleep 3600 & wait $!; done
