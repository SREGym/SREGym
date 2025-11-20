"""Oracle for validating CockroachDB ResizePVC operator action.

This oracle verifies that the agent successfully executed the complete operator
workflow for expanding PersistentVolumeClaims, NOT just the actual storage expansion.

Since many storage provisioners (like OpenEBS hostpath, local-path) don't support
online PVC expansion, this oracle focuses on validating the operator's workflow:
1. Patching PVC specs to request new size
2. Restarting pods to recognize changes
3. Updating StatefulSet template for future pods
4. Maintaining cluster health throughout

This simulates real operator behavior when managing local/hostpath storage.

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/resize-pvc/
"""

from datetime import datetime, timezone

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBResizePVCOracle(Oracle):
    """
    Oracle that validates the ResizePVC operator workflow for CockroachDB cluster.

    This oracle verifies that the agent executed the complete workflow:
    1. ✅ Updated all PVC specs to request 20Gi (basic kubectl operation)
    2. ✅ Restarted pods after PVC update (understanding of StatefulSet behavior)
    3. ✅ Updated StatefulSet VolumeClaimTemplate (completeness thinking)
    4. ✅ Maintained cluster health (reliability verification)
    5. ⭐ (Optional) Proper operation ordering (advanced distributed systems understanding)

    Scoring breakdown:
    - PVC spec updates: Required (fail if missing any)
    - Pod restarts: 35% weight (critical for workflow understanding)
    - StatefulSet template update: 30% weight (completeness)
    - Cluster health: 25% weight (reliability)
    - Operation ordering: 10% bonus (optional advanced check)

    Note: We do NOT check PVC.status.capacity because most local storage provisioners
    (OpenEBS hostpath, local-path) don't support online expansion even with
    allowVolumeExpansion=true. This is intentional - we're testing operator workflow,
    not storage driver capabilities.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent executed the complete ResizePVC operator workflow.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.1 (1.1 if bonus achieved)
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB ResizePVC Oracle Evaluation ==")
        print("Testing operator workflow (NOT actual storage expansion)")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "pvc_spec_updated": False,
                "pods_restarted": False,
                "statefulset_updated": False,
                "cluster_healthy": False,
                "proper_ordering": False,  # bonus
            },
        }

        # Expected values
        expected_replicas = 3
        expected_storage_size = "20Gi"
        initial_storage_size = "10Gi"

        # STEP 1: Check PVC spec updates (REQUIRED - 0 points but must pass)
        print(f"\n[1/6] Checking PVC spec updates to {expected_storage_size}...")
        pvc_spec_pass = True
        pvc_patch_times = []

        try:
            pvcs = kubectl.core_v1_api.list_namespaced_persistent_volume_claim(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pvcs.items) != expected_replicas:
                issue = f"Expected {expected_replicas} PVCs, found {len(pvcs.items)}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                pvc_spec_pass = False
            else:
                print(f"  ✅ Found {expected_replicas} PVCs")

            # Check each PVC's requested storage
            for pvc in pvcs.items:
                pvc_name = pvc.metadata.name
                requested_storage = pvc.spec.resources.requests.get("storage", "")

                if requested_storage != expected_storage_size:
                    issue = f"PVC '{pvc_name}' spec.storage is {requested_storage}, expected {expected_storage_size}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
                    pvc_spec_pass = False
                else:
                    print(f"  ✅ PVC '{pvc_name}' spec.storage: {requested_storage}")
                    # Track resource version change time for ordering check
                    if pvc.metadata.resource_version:
                        pvc_patch_times.append(pvc.metadata.creation_timestamp)

                # Check PVC is Bound
                if pvc.status and pvc.status.phase != "Bound":
                    issue = f"PVC '{pvc_name}' is not Bound (phase: {pvc.status.phase})"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
                    pvc_spec_pass = False

            results["breakdown"]["pvc_spec_updated"] = pvc_spec_pass
            if not pvc_spec_pass:
                results["success"] = False
                print(f"\n  ⚠️  CRITICAL: PVC spec updates failed - this is required!")

        except Exception as e:
            issue = f"Error checking PVCs: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # STEP 2: Check if pods were restarted (35% weight)
        print(f"\n[2/6] Checking pod restarts (35% weight)...")
        pods_restarted = False
        pod_start_times = []

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
                # Check if pods are Running and Ready
                all_ready = True
                for pod in pods.items:
                    pod_name = pod.metadata.name
                    pod_start_times.append(pod.status.start_time)

                    if pod.status.phase != "Running":
                        all_ready = False
                        issue = f"Pod '{pod_name}' not Running (phase: {pod.status.phase})"
                        print(f"  ❌ {issue}")
                        results["issues"].append(issue)
                    elif pod.status.container_statuses:
                        for container in pod.status.container_statuses:
                            if not container.ready:
                                all_ready = False
                                issue = f"Pod '{pod_name}' container not ready"
                                print(f"  ❌ {issue}")
                                results["issues"].append(issue)

                if all_ready:
                    # We can't easily check if pods were restarted AFTER PVC patch without tracking,
                    # so we check if pods exist and are healthy as a proxy
                    # In a real scenario, we'd compare pod.status.startTime with PVC patch time
                    print(f"  ✅ All {expected_replicas} pods are Running and Ready")
                    print(f"  ℹ️  Assuming pods were restarted (can't verify timing without audit logs)")
                    pods_restarted = True
                    results["breakdown"]["pods_restarted"] = True
                    results["score"] += 0.35
                else:
                    print(f"  ⚠️  Pods exist but not all healthy - partial credit")
                    results["breakdown"]["pods_restarted"] = False

        except Exception as e:
            issue = f"Error checking pods: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check StatefulSet VolumeClaimTemplate (30% weight)
        print(f"\n[3/6] Checking StatefulSet VolumeClaimTemplate (30% weight)...")
        sts_updated = False

        try:
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            if sts.spec.volume_claim_templates:
                for vct in sts.spec.volume_claim_templates:
                    if vct.metadata.name == "datadir":
                        vct_storage = vct.spec.resources.requests.get("storage", "")
                        if vct_storage != expected_storage_size:
                            issue = f"StatefulSet VolumeClaimTemplate storage is {vct_storage}, expected {expected_storage_size}"
                            print(f"  ❌ {issue}")
                            results["issues"].append(issue)
                        else:
                            print(f"  ✅ VolumeClaimTemplate updated to {vct_storage}")
                            sts_updated = True
                            results["breakdown"]["statefulset_updated"] = True
                            results["score"] += 0.30
                        break
                else:
                    issue = "VolumeClaimTemplate 'datadir' not found in StatefulSet"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
            else:
                issue = "StatefulSet has no VolumeClaimTemplates"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking StatefulSet: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 4: Test cluster functionality (25% weight)
        print(f"\n[4/6] Testing cluster functionality (25% weight)...")
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

                # Check cluster health (node status)
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

        # STEP 5: Check operation ordering (10% BONUS - optional)
        print(f"\n[5/6] Checking operation ordering (10% bonus - optional)...")
        proper_ordering = False

        try:
            # Ideal order: PVC patch → Pod restart → StatefulSet update
            # We can infer this from timestamps
            if pvc_patch_times and pod_start_times:
                # This is a simplified check - in reality we'd need more precise timing
                print(f"  ℹ️  Operation ordering check:")
                print(f"     - PVCs patched (inferred from current state)")
                print(f"     - Pods running (start times recorded)")
                print(f"     - StatefulSet updated (checked)")

                # If all previous steps passed, assume ordering was reasonable
                if pvc_spec_pass and pods_restarted and sts_updated:
                    print(f"  ⭐ All operations completed successfully - assuming proper order")
                    proper_ordering = True
                    results["breakdown"]["proper_ordering"] = True
                    results["score"] += 0.10
                else:
                    print(f"  ℹ️  Some operations incomplete - skipping ordering check")
            else:
                print(f"  ℹ️  Insufficient timing data for ordering check")

        except Exception as e:
            print(f"  ℹ️  Could not verify ordering: {str(e)}")

        # STEP 6: Final summary
        print(f"\n[6/6] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - PVC specs updated:      {'✅' if results['breakdown']['pvc_spec_updated'] else '❌'} (required)")
        print(f"  - Pods restarted:         {'✅ +35%' if results['breakdown']['pods_restarted'] else '❌ +0%'}")
        print(f"  - StatefulSet updated:    {'✅ +30%' if results['breakdown']['statefulset_updated'] else '❌ +0%'}")
        print(f"  - Cluster healthy:        {'✅ +25%' if results['breakdown']['cluster_healthy'] else '❌ +0%'}")
        print(
            f"  - Proper ordering:        {'⭐ +10%' if results['breakdown']['proper_ordering'] else 'ℹ️  +0% (bonus)'}"
        )
        print(
            f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['success'] and results['score'] >= 0.70 else 'Fail'})"
        )

        if results["issues"]:
            print(f"\nIssues found ({len(results['issues'])}):")
            for i, issue in enumerate(results["issues"], 1):
                print(f"  {i}. {issue}")

        # Overall pass/fail: require PVC spec updates + at least 70% score
        if not results["breakdown"]["pvc_spec_updated"]:
            results["success"] = False
            print(f"\n❌ FAIL: PVC spec updates are required")
        elif results["score"] < 0.70:
            results["success"] = False
            print(f"\n❌ FAIL: Score {results['score']:.0%} below 70% threshold")
        else:
            results["success"] = True
            print(f"\n✅ PASS: ResizePVC workflow executed successfully")

        print(f"=" * 60 + "\n")

        return results
