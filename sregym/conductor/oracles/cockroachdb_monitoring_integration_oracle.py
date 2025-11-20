"""Oracle for validating CockroachDB Monitoring Integration operator action.

This oracle verifies that the agent successfully created ServiceMonitor
for Prometheus integration with CockroachDB.

The oracle validates:
1. ServiceMonitor created
2. Metrics endpoint accessible

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/monitoring-integration/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBMonitoringIntegrationOracle(Oracle):
    """
    Oracle that validates monitoring integration setup.

    Weighted scoring breakdown:
    - servicemonitor_created: 50% - ServiceMonitor CRD created
    - metrics_accessible: 50% - Metrics endpoint working

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully set up monitoring.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Monitoring Integration Oracle Evaluation ==")
        print("Testing ServiceMonitor creation and metrics access")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"servicemonitor_created": False, "metrics_accessible": False},
        }

        # STEP 1: Check if ServiceMonitor was created (50% weight)
        print(f"\n[1/2] Checking if ServiceMonitor was created (50% weight)...")
        servicemonitor_created = False

        try:
            # Check for ServiceMonitor resource
            sm_cmd = f"kubectl -n {namespace} get servicemonitor -o jsonpath='{{.items[*].metadata.name}}' 2>/dev/null"
            sm_output = kubectl.exec_command(sm_cmd)

            if sm_output and len(sm_output.strip()) > 0:
                print(f"  ✅ ServiceMonitor found: {sm_output}")
                servicemonitor_created = True
                results["breakdown"]["servicemonitor_created"] = True
                results["score"] += 0.50
            else:
                issue = f"No ServiceMonitor resource found"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not check ServiceMonitor: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 2: Check if metrics endpoint is accessible (50% weight)
        print(f"\n[2/2] Checking metrics endpoint accessibility (50% weight)...")
        metrics_accessible = False

        try:
            # Try to access metrics endpoint
            metrics_cmd = f"kubectl -n {namespace} exec {cluster_name}-0 -- curl -s http://localhost:8080/_status/metrics/prometheus 2>/dev/null | head -20"
            metrics_output = kubectl.exec_command(metrics_cmd)

            if "cockroach" in metrics_output.lower() or "metric" in metrics_output.lower() or "#" in metrics_output:
                print(f"  ✅ Metrics endpoint responding with Prometheus format")
                metrics_accessible = True
                results["breakdown"]["metrics_accessible"] = True
                results["score"] += 0.50
            else:
                # Try alternative endpoint
                try:
                    metrics_cmd_alt = f"kubectl -n {namespace} exec {cluster_name}-0 -- curl -s http://localhost:8080/_status/metrics 2>/dev/null | head -10"
                    metrics_output_alt = kubectl.exec_command(metrics_cmd_alt)
                    if len(metrics_output_alt.strip()) > 0:
                        print(f"  ✅ Metrics endpoint accessible")
                        metrics_accessible = True
                        results["breakdown"]["metrics_accessible"] = True
                        results["score"] += 0.50
                    else:
                        issue = f"Metrics endpoint not responding"
                        print(f"  ⚠️  {issue}")
                except Exception:
                    issue = f"Could not access metrics endpoint"
                    print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not check metrics endpoint: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(
            f"  - ServiceMonitor created: {'✅ +50%' if results['breakdown']['servicemonitor_created'] else '❌ +0%'}"
        )
        print(f"  - Metrics accessible:     {'✅ +50%' if results['breakdown']['metrics_accessible'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Monitoring integration configured successfully")

        print(f"=" * 60 + "\n")

        return results
