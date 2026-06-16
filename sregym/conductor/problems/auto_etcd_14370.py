"""Auto-generated from https://github.com/etcd-io/etcd/issues/14370

Title: Durability API guarantee broken in single node cluster
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd14370(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/14370"
    root_cause_description = (
        "Durability API guarantee broken in single node cluster. I observed the possibility of data loss and I would like the community to comment / correct me otherwise. Before explaining that, I would like to explain the happy path when user does a PUT <key, value>. I have tried to only necessary steps to focus this issue. And considered a single etcd instance. ==================================================================================== ----------api thread -------------- User calls etcdctl PUT k v It lands in v3_server.go::put function with the message about k,v Call delegates to series of function calls and enters v3_server.go::processInternalRaftRequestOnce It registers for a signal with wait utility against this keyid Call delegates further to series of function calls and enters raft/node.go::stepWithWaitOption(..mess"
    )
