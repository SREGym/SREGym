"""Auto-generated from https://github.com/pingcap/tidb/issues/51301

Title: tidb crash because of logging grpc error  
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb51301(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/51301"
    root_cause_description = (
        "tidb crash because of logging grpc error . ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) tidb crash because of [logging grpc err](https://github.com/pingcap/tidb/blob/v6.5.7/util/topsql/reporter/pubsub.go#L136), check https://github.com/grpc/grpc-go/blob/v1.51.0/internal/status/status.go#L150, there is a related grpc issue: https://github.com/grpc/grpc-go/issues/6204. It's hard to reproduce, but we needs to figure out a way to fix it. ![img_v3_028a_a5c0aba3-54e1-4509-a1dd-2695cbcd17bg](https://github.com/pingcap/tidb/assets/7493273/3ddd71ae-e073-45b9-aed7-2cad18f63954) <!-- a step by step guide for reproducing the bug. --> ### 2. What did you expect to see? (Required) ### 3. What did you see instead (Required) ### 4. What is your Ti"
    )
