"""Auto-generated from https://github.com/etcd-io/etcd/issues/20716

Title: Campaign watch cancel causes adapter/in-process watch stream permanent blocking
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd20716(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/20716"
    root_cause_description = (
        "Campaign watch cancel causes adapter/in-process watch stream permanent blocking. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via security@etcd.io. - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? I have an etcd cluster with many C++ services connecting to it for leader election. Since the C++ client SDK ([etcd-cpp-apiv3](https://github.com/etcd-cpp-apiv3/et"
    )
