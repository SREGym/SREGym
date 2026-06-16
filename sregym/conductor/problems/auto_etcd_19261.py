"""Auto-generated from https://github.com/etcd-io/etcd/issues/19261

Title: etcdctl lease keep-alive & timetolive return success (0) even on failure
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd19261(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/19261"
    root_cause_description = (
        "etcdctl lease keep-alive & timetolive return success (0) even on failure. ### Bug report criteria - [ ] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [ ] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [ ] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [ ] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Calling `etcdctl lease keep-alive` or `timetolive` with an expired or non-existent lease id returns a status code of 0."
    )
    reproducer = 'etcdctl lease keep-alive 0bad1d0; echo $?'
    continuous_reproducer = True
