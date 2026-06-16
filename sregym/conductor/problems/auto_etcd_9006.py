"""Auto-generated from https://github.com/etcd-io/etcd/issues/9006

Title: On master, key doesn't be deleted when bound lease expires
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd9006(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/9006"
    root_cause_description = (
        "On master, key doesn't be deleted when bound lease expires. step: - `etcdctl lease grant 30` - `etcdctl put a 123 --lease leaseid` - `etcdctl lease timetolive leaseid` and got `lease 4289604df903d409 granted with TTL(30s), remaining(-4s)` Lease expired but it and key still exists. Correct output of expired lease should be `lease 4289604df903d409 granted with TTL(0s), remaining(-1s)`"
    )
