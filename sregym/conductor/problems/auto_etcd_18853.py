"""Auto-generated from https://github.com/etcd-io/etcd/issues/18853

Title: Close of already closed channel causes a panic
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd18853(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/18853"
    root_cause_description = (
        "Close of already closed channel causes a panic. ### Bug report criteria - [X] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [X] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [X] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [X] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Organization I work for leverages etcd as a building block of a service, which complicates this matter a bit. There was"
    )
