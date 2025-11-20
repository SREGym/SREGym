"""Oracle for validating CockroachDB Multi-Region Setup operator action.

This oracle verifies that the agent successfully configured multi-region
topology with proper locality and pod spreading.

The oracle validates:
1. Topology configured
2. Pods spread across different nodes
3. Locality settings applied

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/multi-region-setup/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBMultiRegionSetupOracle(Oracle):
    """
    Oracle that validates multi-region topology setup.

    Weighted scoring breakdown:
    - topology_configured: 40% - Multi-region config set
    - pods_spread: 30% - Pods on different nodes
    - locality_set: 30% - Locality flags applied

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully configured multi-region setup.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Multi-Region Setup Oracle Evaluation ==")
        print("Testing multi-region topology configuration")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"topology_configured": False, "pods_spread": False, "locality_set": False},
        }

        expected_replicas = 3

        # STEP 1: Check topology configuration (40% weight)
        print(f"\n[1/3] Checking topology configuration (40% weight)...")
        topology_configured = False

        try:
            # Check StatefulSet for anti-affinity configuration
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            if sts.spec.template.spec.affinity:
                if sts.spec.template.spec.affinity.pod_anti_affinity:
                    print(f"  ✅ Pod anti-affinity configured")
                    topology_configured = True
                    results["breakdown"]["topology_configured"] = True
                    results["score"] += 0.40
                else:
                    issue = f"No pod anti-affinity rules found"
                    print(f"  ⚠️  {issue}")
            else:
                issue = f"No affinity rules configured"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check topology configuration: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 2: Check if pods are spread (30% weight)
        print(f"\n[2/3] Checking pod spread across nodes (30% weight)...")
        pods_spread = False

        try:
            pods = kubectl.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name=cockroachdb,app.kubernetes.io/instance={cluster_name}",
            )

            if len(pods.items) != expected_replicas:
                issue = f"Expected {expected_replicas} pods, found {len(pods.items)}"
                print(f"  ⚠️  {issue}")
            else:
                node_names = set()
                for pod in pods.items:
                    if pod.spec.node_name:
                        node_names.add(pod.spec.node_name)

                if len(node_names) >= 2:  # At least 2 different nodes
                    print(f"  ✅ Pods spread across {len(node_names)} different nodes")
                    pods_spread = True
                    results["breakdown"]["pods_spread"] = True
                    results["score"] += 0.30
                else:
                    issue = f"Pods not properly spread (only {len(node_names)} node(s))"
                    print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check pod spread: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 3: Check locality settings (30% weight)
        print(f"\n[3/3] Checking locality settings (30% weight)...")
        locality_set = False

        try:
            # Check if locality is set in StatefulSet command or env
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            if sts.spec.template.spec.containers:
                container = sts.spec.template.spec.containers[0]
                locality_found = False

                # Check command for locality flag
                if container.command:
                    command_str = " ".join(container.command)
                    if "--locality" in command_str:
                        print(f"  ✅ Locality flag configured in startup command")
                        locality_found = True

                # Check env variables
                if container.env:
                    for env_var in container.env:
                        if "LOCALITY" in env_var.name or "ZONE" in env_var.name or "REGION" in env_var.name:
                            print(f"  ✅ Locality environment variable set: {env_var.name}")
                            locality_found = True

                if locality_found:
                    locality_set = True
                    results["breakdown"]["locality_set"] = True
                    results["score"] += 0.30
                else:
                    issue = f"No locality settings found"
                    print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check locality settings: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Topology configured: {'✅ +40%' if results['breakdown']['topology_configured'] else '❌ +0%'}")
        print(f"  - Pods spread:         {'✅ +30%' if results['breakdown']['pods_spread'] else '❌ +0%'}")
        print(f"  - Locality set:        {'✅ +30%' if results['breakdown']['locality_set'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Multi-region topology configured successfully")

        print(f"=" * 60 + "\n")

        return results
