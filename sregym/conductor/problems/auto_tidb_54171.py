"""Auto-generated from https://github.com/pingcap/tidb/issues/54171

Title: tikv crash due to index out of bound
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb54171(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/54171"
    root_cause_description = (
        "tikv crash due to index out of bound. ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) tikv crashed unexpectedly <!-- a step by step guide for reproducing the bug. --> ### 2. What did you expect to see? (Required) ### 3. What did you see instead (Required) index out of bounds: the len is 6 but the index is 6 [FATAL] [lib.rs:465] [\"index out of bounds: the len is 6 but the index is 6\"] [backtrace=\" 0: tikv_util::set_panic_hook::{{closure}}\\n at /home/jenkins/agent/workspace/build-common/go/src/github.com/pingcap/tikv/components/tikv_util/src/lib.rs:464:18\\n 1: std::panicking::rust_panic_with_hook\\n at /rustc/2faabf579323f5252329264cc53ba9ff803429a3/library/std/src/panicking.rs:626:17\\n 2: std::panicking::b"
    )
    crash_on_startup = True

    def setup_preconditions(self):
        pass  # TODO: self.app.run_reproducer("<command to enable the mode that causes the crash>")
