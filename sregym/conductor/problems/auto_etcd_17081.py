"""Auto-generated from https://github.com/etcd-io/etcd/issues/17081

Title: Wrong raft messages may cause etcd panic
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd17081(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/17081"
    root_cause_description = (
        "Wrong raft messages may cause etcd panic. ### Bug report criteria - [X] This bug report is not security related, security issues should be disclosed privately via security@etcd.io. - [X] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [X] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [X] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Multiple etcd servers repeatedly panic with a message \"tocommit(4432450) is out of range [lastIndex(4432444)]. Was the raft log corrupted, truncated, or lost?\""
    )
