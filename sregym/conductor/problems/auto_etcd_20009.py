"""Auto-generated from https://github.com/etcd-io/etcd/issues/20009

Title: 3.6.0: server fails to start, nil pointer exception when re-using storage with `force-new-cluster`
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd20009(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/20009"
    root_cause_description = (
        "3.6.0: server fails to start, nil pointer exception when re-using storage with `force-new-cluster`. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Hi etcd Team! After etcd 3.6 has been released, I am evaluating it against my team's disaster recovery mechanisms. One"
    )
    crash_on_startup = True

    def setup_preconditions(self):
        pass  # TODO: self.app.run_reproducer("<command to enable the mode that causes the crash>")
