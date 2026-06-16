"""Auto-generated from https://github.com/etcd-io/etcd/issues/14294

Title: etcdctl lease id json output bug
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd14294(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/14294"
    root_cause_description = (
        "etcdctl lease id json output bug. ### What happened? Getting a key attached to a lease with `-w json` results in what seems to be rounded lease id ``` ▶ etcdctl get /foo -w json | jq { \"header\": { \"cluster_id\": 3857049431318267400, \"member_id\": 5819150906786784000, \"revision\": 49, \"raft_term\": 3 }, \"kvs\": [ { \"key\": \"L2Zvbw==\", \"create_revision\": 49, \"mod_revision\": 49, \"version\": 1, \"value\": \"YmFy\", \"lease\": 2658674479976362000 } ], \"count\": 1 } ``` ### What did you expect to happen? I expected to see the correct lease id ``` ▶ etcdctl get /foo -w fields \"ClusterID\" : 3857049431318267579 \"MemberID\" : 5819150906786784485 \"Revision\" : 49 \"RaftTerm\" : 3 \"Key\" : \"/foo\" \"CreateRevision\" : 49 \"ModRevision\" : 49 \"Ve"
    )
    reproducer = 'etcdctl lease grant 7000000000\netcdctl put /foo bar --lease 24e5825a70632f8c\netcdctl get /foo -w json | jq'
    continuous_reproducer = True
