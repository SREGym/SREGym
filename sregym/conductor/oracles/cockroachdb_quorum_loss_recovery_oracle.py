"""Oracle for validating CockroachDB Quorum Loss Recovery operator action.

This oracle verifies that the agent successfully recovered from catastrophic
quorum loss using unsafe recovery procedures.

The oracle validates:
1. Quorum restored (cluster responsive)
2. Cluster responsive to queries
3. Cluster scaled back up to 3 nodes

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/quorum-loss-recovery/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBQuorumLossRecoveryOracle(Oracle):
    """
    Oracle that validates quorum loss recovery.

    Weighted scoring breakdown:
    - quorum_restored: 50% - Cluster recovered and responsive
    - cluster_responsive: 30% - SQL queries working
    - scaled_up: 20% - All 3 nodes back online

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully recovered from quorum loss.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Quorum Loss Recovery Oracle Evaluation ==")
        print("Testing recovery from catastrophic quorum loss")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"quorum_restored": False, "cluster_responsive": False, "scaled_up": False},
        }

        # STEP 1: Check if quorum was restored (50% weight)
        print(f"\n[1/3] Checking if quorum was restored (50% weight)...")
        quorum_restored = False

        try:
            # Try to connect to cluster
            test_cmd = (
                f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SELECT 1;" 2>/dev/null'
            )
            test_output = kubectl.exec_command(test_cmd)

            if "1" in test_output or ("ERROR" not in test_output and len(test_output.strip()) > 0):
                print(f"  ✅ Cluster responsive (quorum restored)")
                quorum_restored = True
                results["breakdown"]["quorum_restored"] = True
                results["score"] += 0.50
            else:
                issue = f"Cluster still not responsive"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not verify quorum restoration: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 2: Check if cluster is responsive (30% weight)
        print(f"\n[2/3] Checking cluster responsiveness (30% weight)...")
        cluster_responsive = False

        try:
            # Test SQL connectivity and node status
            sql_test_cmd = f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SHOW databases;" 2>/dev/null'
            sql_output = kubectl.exec_command(sql_test_cmd)

            if "defaultdb" in sql_output or len(sql_output.strip()) > 5:
                print(f"  ✅ SQL queries responsive")

                # Check node status
                node_status_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure --format=csv 2>/dev/null"
                node_status_output = kubectl.exec_command(node_status_cmd)

                lines = node_status_output.strip().split("\n")
                live_nodes = 0
                for line in lines:
                    if line and not line.startswith("id,"):
                        fields = line.split(",")
                        if len(fields) >= 5:
                            is_live = fields[4].strip().lower() == "true"
                            if is_live:
                                live_nodes += 1

                if live_nodes >= 1:
                    print(f"  ✅ Cluster health verified ({live_nodes} live nodes)")
                    cluster_responsive = True
                    results["breakdown"]["cluster_responsive"] = True
                    results["score"] += 0.30

        except Exception as e:
            issue = f"Could not verify cluster responsiveness: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 3: Check if cluster scaled back to 3 nodes (20% weight)
        print(f"\n[3/3] Checking if cluster scaled back to 3 nodes (20% weight)...")
        scaled_up = False

        try:
            # Check pod count
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            pod_count = len(pods.items)
            if pod_count == 3:
                print(f"  ✅ All 3 pods restored")
                scaled_up = True
                results["breakdown"]["scaled_up"] = True
                results["score"] += 0.20
            elif pod_count > 1:
                partial_score = 0.20 * (pod_count / 3)
                results["score"] += partial_score
                print(f"  ⚠️  Partial: {pod_count}/3 pods restored (+{partial_score:.0%})")
            else:
                issue = f"Expected 3 pods, found {pod_count}"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not verify pod count: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Quorum restored:     {'✅ +50%' if results['breakdown']['quorum_restored'] else '❌ +0%'}")
        print(f"  - Cluster responsive:  {'✅ +30%' if results['breakdown']['cluster_responsive'] else '❌ +0%'}")
        print(f"  - Scaled up:           {'✅ +20%' if results['breakdown']['scaled_up'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Quorum loss recovery completed successfully")

        print(f"=" * 60 + "\n")

        return results
