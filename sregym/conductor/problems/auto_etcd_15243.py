"""Auto-generated from https://github.com/etcd-io/etcd/issues/15243

Title: `MemberList` doesn't work after adding a new member to one-node cluster
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd15243(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/15243"
    root_cause_description = (
        "`MemberList` doesn't work after adding a new member to one-node cluster. ### What happened? etcdadm automates bringing up a cluster, and has a test where it brings up a cluster node-by-node. That test regressed when we updated the client from 3.4 to 3.5, seemingly a reintroduction of #9949 ### What did you expect to happen? No regression ### How can we reproduce it (as minimally and precisely as possible)? https://github.com/kubernetes-sigs/etcdadm/pull/364 demonstrates the problem. The failing test is relatively simple: https://github.com/kubernetes-sigs/etcdadm/blob/master/test/e2e/cluster_phases.sh A failing run can be seen [here](https://github.com/kubernetes-sigs/etcdadm/actions/runs/4092492086/jobs/7057270193) ``` + docker exec etcdadm-1 /etcdadm/etcdadm join phase membership https://172.17.0.2:2379/ --name etcdadm-1 time=\"2023-02-04T16:4"
    )
