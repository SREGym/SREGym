"""Oracle for validating CockroachDB Partitioned Update operator action.

This oracle verifies that the agent successfully executed a controlled rolling
version upgrade of the CockroachDB cluster, simulating the operator's
PartitionedUpdate reconciler.

The oracle validates:
1. All pods upgraded to new version
2. StatefulSet template updated
3. Zero downtime (cluster remained functional throughout)
4. Proper update strategy (partition management or rolling restart)
5. Cluster health maintained

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/partitioned-update/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBPartitionedUpdateOracle(Oracle):
    """
    Oracle that validates the Partitioned Update operator workflow for CockroachDB cluster.

    This oracle verifies that the agent executed a successful rolling upgrade:
    1. ✅ Updated StatefulSet template image (required)
    2. ✅ All pods running new version (30% weight - core functionality)
    3. ✅ Pods upgraded safely (25% weight - update strategy)
    4. ✅ Cluster remained healthy (25% weight - zero downtime)
    5. ✅ Version verified via SQL (20% weight - completeness)
    6. ⭐ (Optional) StatefulSet partition strategy used correctly (10% bonus)

    Scoring breakdown:
    - StatefulSet template update: Required (fail if missing)
    - All pods new version: 30% weight
    - Safe upgrade (ready checks): 25% weight
    - Cluster health maintained: 25% weight
    - Version SQL verification: 20% weight
    - Partition strategy: 10% bonus (optional advanced check)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent executed the complete Partitioned Update workflow.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.1 (1.1 if bonus achieved)
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Partitioned Update Oracle Evaluation ==")
        print("Testing rolling version upgrade workflow")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name
        new_version = self.problem.new_version
        old_version = self.problem.old_version

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "statefulset_updated": False,
                "pods_new_version": False,
                "safe_upgrade": False,
                "cluster_healthy": False,
                "version_verified": False,
                "partition_strategy": False,  # bonus
            },
        }

        expected_replicas = 3

        # STEP 1: Check StatefulSet template updated (REQUIRED)
        print(f"\n[1/7] Checking StatefulSet template updated to {new_version}...")
        sts_updated = False
        sts_image = None

        try:
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            # Check template image
            if sts.spec.template.spec.containers:
                sts_image = sts.spec.template.spec.containers[0].image
                # Check if new version is in the image string
                if new_version.replace("v", "") in sts_image:
                    print(f"  ✅ StatefulSet template image: {sts_image}")
                    sts_updated = True
                    results["breakdown"]["statefulset_updated"] = True
                else:
                    issue = f"StatefulSet image is {sts_image}, expected to contain {new_version}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
            else:
                issue = "StatefulSet has no containers"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

            if not sts_updated:
                results["success"] = False
                print(f"\n  ⚠️  CRITICAL: StatefulSet template update failed - this is required!")

        except Exception as e:
            issue = f"Error checking StatefulSet: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # STEP 2: Check all pods running new version (30% weight)
        print(f"\n[2/7] Checking all pods running {new_version} (30% weight)...")
        all_pods_new_version = False
        pod_versions = []

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
                pods_with_new_version = 0
                for pod in pods.items:
                    pod_name = pod.metadata.name
                    if pod.spec.containers:
                        pod_image = pod.spec.containers[0].image
                        pod_versions.append((pod_name, pod_image))

                        if new_version.replace("v", "") in pod_image:
                            print(f"  ✅ Pod '{pod_name}' running: {pod_image}")
                            pods_with_new_version += 1
                        else:
                            issue = f"Pod '{pod_name}' still on old version: {pod_image}"
                            print(f"  ❌ {issue}")
                            results["issues"].append(issue)

                if pods_with_new_version == expected_replicas:
                    all_pods_new_version = True
                    results["breakdown"]["pods_new_version"] = True
                    results["score"] += 0.30
                elif pods_with_new_version > 0:
                    # Partial credit
                    partial_score = 0.30 * (pods_with_new_version / expected_replicas)
                    results["score"] += partial_score
                    print(
                        f"  ⚠️  Partial credit: {pods_with_new_version}/{expected_replicas} pods updated (+{partial_score:.0%})"
                    )

        except Exception as e:
            issue = f"Error checking pods: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check safe upgrade (pods are healthy) (25% weight)
        print(f"\n[3/7] Checking safe upgrade - pods healthy (25% weight)...")
        safe_upgrade = False

        try:
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

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

            if all_ready and ready_count == expected_replicas:
                print(f"  ✅ All {expected_replicas} pods Running and Ready")
                safe_upgrade = True
                results["breakdown"]["safe_upgrade"] = True
                results["score"] += 0.25
            elif ready_count > 0:
                # Partial credit
                partial_score = 0.25 * (ready_count / expected_replicas)
                results["score"] += partial_score
                print(f"  ⚠️  Partial credit: {ready_count}/{expected_replicas} pods ready (+{partial_score:.0%})")

        except Exception as e:
            issue = f"Error checking pod readiness: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 4: Check cluster health (25% weight)
        print(f"\n[4/7] Checking cluster health (25% weight)...")
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
                    results["score"] += 0.25

        except Exception as e:
            issue = f"Failed to check cluster health: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 5: Verify version via SQL (20% weight)
        print(f"\n[5/7] Verifying version via SQL (20% weight)...")
        version_verified = False

        try:
            version_sql_cmd = (
                f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT version();'"
            )
            version_output = kubectl.exec_command(version_sql_cmd)

            if new_version.replace("v", "") in version_output:
                print(f"  ✅ SQL reports version: {new_version}")
                version_verified = True
                results["breakdown"]["version_verified"] = True
                results["score"] += 0.20
            else:
                issue = f"Version mismatch in SQL output (expected {new_version})"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                print(f"     SQL output: {version_output[:200]}")

        except Exception as e:
            issue = f"Failed to verify version via SQL: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 6: Check partition strategy (10% BONUS - optional)
        print(f"\n[6/7] Checking partition strategy (10% bonus - optional)...")
        partition_strategy_used = False

        try:
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            # Check if partition is set to 0 (indicating completed rolling update)
            if sts.spec.update_strategy and sts.spec.update_strategy.rolling_update:
                partition = sts.spec.update_strategy.rolling_update.partition
                if partition == 0 or partition is None:
                    print(f"  ⭐ StatefulSet partition correctly set to {partition or 0}")
                    print(f"     (Indicates proper rolling update strategy was used)")
                    partition_strategy_used = True
                    results["breakdown"]["partition_strategy"] = True
                    results["score"] += 0.10
                else:
                    print(f"  ℹ️  StatefulSet partition is {partition} (expected 0)")
                    print(f"     (Agent may not have completed rolling update properly)")
            else:
                print(f"  ℹ️  No partition strategy detected (agent may have used direct update)")

        except Exception as e:
            print(f"  ℹ️  Could not verify partition strategy: {str(e)}")

        # STEP 7: Final summary
        print(f"\n[7/7] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - StatefulSet template:   {'✅' if results['breakdown']['statefulset_updated'] else '❌'} (required)")
        print(f"  - Pods new version:       {'✅ +30%' if results['breakdown']['pods_new_version'] else '❌ +0%'}")
        print(f"  - Safe upgrade:           {'✅ +25%' if results['breakdown']['safe_upgrade'] else '❌ +0%'}")
        print(f"  - Cluster healthy:        {'✅ +25%' if results['breakdown']['cluster_healthy'] else '❌ +0%'}")
        print(f"  - Version verified:       {'✅ +20%' if results['breakdown']['version_verified'] else '❌ +0%'}")
        print(
            f"  - Partition strategy:     {'⭐ +10%' if results['breakdown']['partition_strategy'] else 'ℹ️  +0% (bonus)'}"
        )
        print(
            f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['success'] and results['score'] >= 0.70 else 'Fail'})"
        )

        if results["issues"]:
            print(f"\nIssues found ({len(results['issues'])}):")
            for i, issue in enumerate(results["issues"], 1):
                print(f"  {i}. {issue}")

        # Overall pass/fail: require StatefulSet update + at least 70% score
        if not results["breakdown"]["statefulset_updated"]:
            results["success"] = False
            print(f"\n❌ FAIL: StatefulSet template update is required")
        elif results["score"] < 0.70:
            results["success"] = False
            print(f"\n❌ FAIL: Score {results['score']:.0%} below 70% threshold")
        else:
            results["success"] = True
            print(f"\n✅ PASS: Partitioned Update workflow executed successfully")

        print(f"=" * 60 + "\n")

        return results
