"""Auto-generated from https://github.com/pingcap/tidb/issues/55806

Title: TiDB Crash for nil pointer dereference tableInfo return by TableByID
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb55806(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/55806"
    root_cause_description = (
        "TiDB Crash for nil pointer dereference tableInfo return by TableByID. ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) <!-- a step by step guide for reproducing the bug. --> ``` panic: runtime error: invalid memory address or nil pointer dereference [recovered] panic: runtime error: invalid memory address or nil pointer dereference [signal SIGSEGV: segmentation violation code=0x1 addr=0x60 pc=0x534371a] goroutine 3936 [running]: github.com/pingcap/tidb/pkg/executor.(*ExecStmt).Exec.func1() /home/jenkins/agent/workspace/build-common/go/src/github.com/pingcap/tidb/pkg/executor/adapter.go:487 +0x508 panic({0x5beb6c0?, 0x99e8cf0?}) /usr/local/go/src/runtime/panic.go:920 +0x270 github.com/pingcap/tidb/pkg/executor.buildNoRangeIndexLookUpReader(0xc0dd7e2850, 0xc0dd7e9"
    )
