"""Auto-generated from https://github.com/etcd-io/etcd/issues/19700

Title: panic: runtime error: comparing uncomparable type map[string]interface {}
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd19700(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/19700"
    root_cause_description = (
        "panic: runtime error: comparing uncomparable type map[string]interface {}. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? use grpc client made by etcd resolver with `google.golang.org/grpc v1.71.0` cause panic ### What did you expect to"
    )
