"""Auto-generated from https://github.com/etcd-io/etcd/issues/16666

Title: --experimental-wait-cluster-ready-timeout causing stale response to linearizable read
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd16666(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/16666"
    root_cause_description = (
        "--experimental-wait-cluster-ready-timeout causing stale response to linearizable read. While working and discussing https://github.com/etcd-io/etcd/pull/16658 with @ahrtr I spotted one issue. Etcd going back in time during bootstrap. The good news is that the current reproduction limit the issue to v3.6 release. Impact on older releases is still under investigation. As described in https://github.com/etcd-io/etcd/pull/16658#discussion_r1341346778 graceful shutdown via SIGTERM allows etcd flushing it's database to disk, however SIGKILL will mean that data on disk might be older than in memory state of etcd. While bootstrapping etcd will catch up on changes it has forgotten by replaying WAL. Etcd might go back in time if it started serving data before it caught up to state before the kill. But this doesn't happen right? Unfortunately it's possible. In https://github.com/"
    )
