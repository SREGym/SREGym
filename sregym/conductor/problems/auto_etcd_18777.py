"""Auto-generated from https://github.com/etcd-io/etcd/issues/18777

Title: raftexample deletes nodes and adds old nodes abnormally
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd18777(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/18777"
    root_cause_description = (
        "raftexample deletes nodes and adds old nodes abnormally. ### Bug report criteria - [X] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [X] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [X] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [X] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? raftexample Q1: Deleting the data on the deleted machine is useless, will replay synced data after restart. All mach"
    )
