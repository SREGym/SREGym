"""Auto-generated from https://github.com/etcd-io/etcd/issues/16002

Title: Incorrect configuration in the new etcd member can bring down the etcd cluster (RCA attached)
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd16002(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/16002"
    root_cause_description = (
        "Incorrect configuration in the new etcd member can bring down the etcd cluster (RCA attached). ### What happened? ### Summary While running chaos tests against a cloud based Kubernetes service offering, one of the experiments was to manipulate the DNS resolutions to see how new control plane members behave. During the testing, an interesting issue faced was that all the existing etcd members of the backend etcd cluster went into a crashLoopBackoff state with an error \"failed to find remote peer in cluster\" with a non existent remote peer id. It is probably OK if the new member could not join the cluster, but in this case all the members started crashing and hence brought down the whole etcd cluster ### Etcd version Eventhough this was seen with etcd v3.5.3, this is unlikely to be version specific as this was seen in other versions as well ### Details 1. Whe"
    )
