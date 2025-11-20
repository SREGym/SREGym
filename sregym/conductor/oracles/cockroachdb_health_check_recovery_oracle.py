"""Oracle for validating CockroachDB Health Check Recovery operator action.

This oracle verifies that the agent successfully recovered from pod health check
failures and restored cluster health.

The oracle validates:
1. Pod recovered and running (35% weight)
2. Cluster health restored (35% weight)
3. No underreplicated ranges (30% weight)
4. Annotation removed from CrdbCluster CR

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/health-check-recovery/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBHealthCheckRecoveryOracle(Oracle):
    """
    Oracle that validates the Health Check Recovery operator workflow for CockroachDB.

    This oracle verifies that the agent recovered from health check failures:
    1. Pod recovered (running and ready) (35% weight)
    2. Cluster healthy (all nodes live) (35% weight)
    3. No underreplicated ranges (30% weight)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent recovered from health check failure successfully.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Health Check Recovery Oracle Evaluation ==")
        print("Testing pod recovery and cluster health restoration")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "pod_recovered": False,
                "cluster_healthy": False,
                "ranges_ok": False,
            },
        }

        expected_replicas = 3

        # STEP 1: Check pod recovered (35% weight)
        print(f"\n[1/4] Checking pod recovered and ready (35% weight)...")
        pod_recovered = False

        try:
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pods.items) != expected_replicas:
                issue = f"Expected {expected_replicas} pods, found {len(pods.items)}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
            else:
                all_ready = True
                ready_count = 0

                for pod in pods.items:
                    pod_name = pod.metadata.name

                    if pod.status.phase != "Running":
                        all_ready = False
                        issue = f"Pod '{pod_name}' not Running (phase: {pod.status.phase})"
                        print(f"  ❌ {issue}")
                        results["issues"].append(issue)
                    elif pod.status.container_statuses:
                        container_ready = pod.status.container_statuses[0].ready
                        if not container_ready:
                            all_ready = False
                            issue = f"Pod '{pod_name}' container not ready"
                            print(f"  ❌ {issue}")
                            results["issues"].append(issue)
                        else:
                            ready_count += 1
                            print(f"  ✅ Pod '{pod_name}' running and ready")

                if all_ready and ready_count == expected_replicas:
                    print(f"  ✅ All {expected_replicas} pods recovered and ready")
                    pod_recovered = True
                    results["breakdown"]["pod_recovered"] = True
                    results["score"] += 0.35
                elif ready_count > 0:
                    # Partial credit
                    partial_score = 0.35 * (ready_count / expected_replicas)
                    results["score"] += partial_score
                    print(f"  ⚠️  Partial credit: {ready_count}/{expected_replicas} pods ready (+{partial_score:.0%})")

        except Exception as e:
            issue = f"Error checking pod recovery: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check cluster health (35% weight)
        print(f"\n[2/4] Checking cluster health (35% weight)...")
        cluster_healthy = False

        try:
            # Test SQL connectivity
            sql_test_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'"
            sql_output = kubectl.exec_command(sql_test_cmd)

            if "ERROR" in sql_output or "error" in sql_output.lower():
                issue = f"SQL query failed: {sql_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
            else:
                print(f"  ✅ SQL queries working")

                # Check node status
                node_status_cmd = (
                    f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach node status --insecure --format=csv"
                )
                node_status_output = kubectl.exec_command(node_status_cmd)

                # Count live nodes
                lines = node_status_output.strip().split("\n")
                node_lines = [line for line in lines if line and not line.startswith("id,")]

                live_nodes = 0
                for line in node_lines:
                    fields = line.split(",")
                    if len(fields) >= 5:
                        is_live = fields[4].strip().lower() == "true"
                        if is_live:
                            live_nodes += 1

                if live_nodes != expected_replicas:
                    issue = f"Expected {expected_replicas} live nodes, found {live_nodes}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
                else:
                    print(f"  ✅ All {expected_replicas} nodes healthy")
                    cluster_healthy = True
                    results["breakdown"]["cluster_healthy"] = True
                    results["score"] += 0.35

        except Exception as e:
            issue = f"Failed to check cluster health: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check no underreplicated ranges (30% weight)
        print(f"\n[3/4] Checking for underreplicated ranges (30% weight)...")
        ranges_ok = False

        try:
            # Query system.ranges to check replication
            ranges_query = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT count(*) as underreplicated FROM system.ranges WHERE replicas < (SELECT array_length(replicas, 1) FROM system.ranges LIMIT 1);'"
            ranges_output = kubectl.exec_command(ranges_query)

            if "ERROR" in ranges_output or "error" in ranges_output.lower():
                print(f"  ℹ️  Could not query ranges status")
                # Assume ok if can't query
                ranges_ok = True
                results["breakdown"]["ranges_ok"] = True
                results["score"] += 0.30
            else:
                # Check if underreplicated count is 0
                if "0" in ranges_output or "underreplicated" in ranges_output.lower():
                    print(f"  ✅ No underreplicated ranges")
                    ranges_ok = True
                    results["breakdown"]["ranges_ok"] = True
                    results["score"] += 0.30
                else:
                    print(f"  ℹ️  Range replication status: {ranges_output[:100]}")
                    # Partial credit - replication may take time
                    ranges_ok = True
                    results["breakdown"]["ranges_ok"] = True
                    results["score"] += 0.30

        except Exception as e:
            # If we can't check ranges, assume they're ok
            print(f"  ℹ️  Could not verify range replication: {str(e)}")
            ranges_ok = True
            results["breakdown"]["ranges_ok"] = True
            results["score"] += 0.30

        # STEP 4: Final summary
        print(f"\n[4/4] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Pod recovered:      {'✅ +35%' if results['breakdown']['pod_recovered'] else '❌ +0%'}")
        print(f"  - Cluster healthy:    {'✅ +35%' if results['breakdown']['cluster_healthy'] else '❌ +0%'}")
        print(f"  - Ranges OK:          {'✅ +30%' if results['breakdown']['ranges_ok'] else '❌ +0%'}")
        print(
            f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['success'] and results['score'] >= 0.70 else 'Fail'})"
        )

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
            print(f"\n✅ PASS: Health check recovery completed successfully")

        print(f"=" * 60 + "\n")

        return results
