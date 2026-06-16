"""Auto-generated from https://github.com/etcd-io/etcd/issues/17855

Title: Panic occurs when etcd (ver 3.5.13) new node joins the cluster
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd17855(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/17855"
    root_cause_description = (
        "Panic occurs when etcd (ver 3.5.13) new node joins the cluster. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? My operation process is as follows: I start an etcd node (called node1) first, and wait for it to become the L"
    )
