"""Auto-generated from https://github.com/etcd-io/etcd/issues/17780

Title: Revision decreasing after panic during compaction
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd17780(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/17780"
    root_cause_description = (
        "Revision decreasing after panic during compaction. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Failure in https://github.com/etcd-io/etcd/actions/runs/8659974818 ![image](https://github.com/etcd-io/etcd"
    )
