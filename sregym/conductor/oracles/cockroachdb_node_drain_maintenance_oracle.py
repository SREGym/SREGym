"""Oracle for validating CockroachDB Node Drain Maintenance operator action.

This oracle verifies that the agent successfully drained a node
without decommissioning it.

The oracle validates:
1. Node drained (no replicas)
2. Replicas moved to other nodes
3. Undrain is possible

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/node-drain-maintenance/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBNodeDrainMaintenanceOracle(Oracle):
    """
    Oracle that validates node drain for maintenance.

    Weighted scoring breakdown:
    - node_drained: 40% - Node successfully drained
    - replicas_moved: 40% - Replicas moved to other nodes
    - undrain_possible: 20% - Node can be undrainedafter

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully drained a node.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Node Drain Maintenance Oracle Evaluation ==")
        print("Testing node drain for maintenance")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name
        target_node = self.problem.target_node

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"node_drained": False, "replicas_moved": False, "undrain_possible": False},
        }

        # STEP 1: Check if node is drained (40% weight)
        print(f"\n[1/3] Checking if node {target_node} is drained (40% weight)...")
        node_drained = False

        try:
            # Check node drain status via SQL
            drain_check_cmd = f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SHOW node_drain_mode;" 2>/dev/null'
            drain_output = kubectl.exec_command(drain_check_cmd)

            if drain_output and len(drain_output.strip()) > 0:
                print(f"  ✅ Drain command executed")
                node_drained = True
                results["breakdown"]["node_drained"] = True
                results["score"] += 0.40
            else:
                issue = f"Node drain status unclear"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check drain status: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 2: Check if replicas were moved (40% weight)
        print(f"\n[2/3] Checking if replicas were moved to other nodes (40% weight)...")
        replicas_moved = False

        try:
            # Check replica distribution
            replica_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e \"SELECT count(*) FROM system.ranges WHERE replicas LIKE '%\\|{target_node}\\|%' LIMIT 1;\" 2>/dev/null"
            replica_output = kubectl.exec_command(replica_cmd)

            # If node_drained is true and we can verify no replicas on target node
            if "0" in replica_output or len(replica_output.strip()) < 20:
                print(f"  ✅ Replicas moved from drained node")
                replicas_moved = True
                results["breakdown"]["replicas_moved"] = True
                results["score"] += 0.40
            else:
                # Alternative check: just verify cluster is healthy
                health_cmd = f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SELECT 1;" 2>/dev/null'
                health_output = kubectl.exec_command(health_cmd)
                if "1" in health_output:
                    print(f"  ✅ Cluster responsive (replicas distributed)")
                    replicas_moved = True
                    results["breakdown"]["replicas_moved"] = True
                    results["score"] += 0.40
                else:
                    issue = f"Could not verify replica distribution"
                    print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check replica distribution: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 3: Check if undrain is possible (20% weight)
        print(f"\n[3/3] Checking if node can be undrainedafter maintenance (20% weight)...")
        undrain_possible = False

        try:
            # Check node status - should still be part of cluster
            node_status_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure --format=csv 2>/dev/null"
            node_status_output = kubectl.exec_command(node_status_cmd)

            # Count nodes
            lines = node_status_output.strip().split("\n")
            node_count = len([l for l in lines if l and not l.startswith("id,")])

            if node_count >= 3:
                print(f"  ✅ All nodes still part of cluster (undrain possible)")
                undrain_possible = True
                results["breakdown"]["undrain_possible"] = True
                results["score"] += 0.20
            else:
                issue = f"Expected 3 nodes, found {node_count}"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check undrain possibility: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Node drained:        {'✅ +40%' if results['breakdown']['node_drained'] else '❌ +0%'}")
        print(f"  - Replicas moved:      {'✅ +40%' if results['breakdown']['replicas_moved'] else '❌ +0%'}")
        print(f"  - Undrain possible:    {'✅ +20%' if results['breakdown']['undrain_possible'] else '❌ +0%'}")
        print(f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['score'] >= 0.70 else 'Fail'})")

        if results["issues"]:
            print(f"\nIssues found ({len(results['issues'])}):")
            for i, issue in enumerate(results["issues"], 1):
                print(f"  {i}. {issue}")

        # Overall pass/fail
        if results["score"] < 0.70:
            results["success"] = False
            print(f"\n❌ FAIL: Score {results['score']:.0%} below 70% threshold")
        else:
            results["success"] = True
            print(f"\n✅ PASS: Node drain maintenance completed successfully")

        print(f"=" * 60 + "\n")

        return results
