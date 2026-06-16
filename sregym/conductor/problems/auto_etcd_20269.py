"""Auto-generated from https://github.com/etcd-io/etcd/issues/20269

Title: etcd panic on member rejoin after reset — tocommit(...) is out of range [lastIndex(...)]
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd20269(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/20269"
    root_cause_description = (
        "etcd panic on member rejoin after reset — tocommit(...) is out of range [lastIndex(...)]. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via security@etcd.io. - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? During negative testing of our etcd cluster, we simulate failure scenarios by renaming the etcd binary to something else. Which simulates that etcd member is unhea"
    )
