"""Auto-generated from https://github.com/pingcap/tidb/issues/52628

Title: PITR: Run PITR for multiple times could lead to tiflash crash
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb52628(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/52628"
    root_cause_description = (
        "PITR: Run PITR for multiple times could lead to tiflash crash. ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) <!-- a step by step guide for reproducing the bug. --> 1. Backup snapshot and log using PITR 2. Restore the data within tso1 into a new cluster with tiflash instances by `br restore point` 3. Add tiflash replica(s) for the restored table(s) # or if the backup data contains tiflash replica, the tiflash replica will be added after step 2. 4. Restore the data within tso1...tso2 into the cluster by `br restore point` ### 2. What did you expect to see? (Required) Restore success and all instances run normally ### 3. What did you see instead (Required) When running step 4, TiFlash instances crash with backtrace like ``` [FATAL] [Exception.cpp:1"
    )
    reproducer = '1. Backup snapshot and log using PITR\n2. Restore the data within tso1 into a new cluster with tiflash instances by `br restore point`\n3. Add tiflash replica(s) for the restored table(s)\n4. Restore the data within tso1...tso2 into the cluster by `br restore point`'
    continuous_reproducer = True
    expected_output = 'Restore success and all instances run normally'
