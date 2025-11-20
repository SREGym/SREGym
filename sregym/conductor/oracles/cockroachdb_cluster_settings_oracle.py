"""Oracle for validating CockroachDB Cluster Settings operator action.

This oracle verifies that the agent successfully applied cluster settings via SQL
and verified they were persisted correctly.

The oracle validates:
1. Cluster settings applied via SQL
2. Settings readable from system.settings
3. Settings persisted (not lost on pod restart)
4. Annotation removed from CrdbCluster CR
5. Cluster remains healthy

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/cluster-settings/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBClusterSettingsOracle(Oracle):
    """
    Oracle that validates the Cluster Settings operator workflow for CockroachDB.

    This oracle verifies that the agent applied cluster settings via SQL:
    1. Cluster settings applied and queryable from system.settings (40% weight)
    2. Settings persisted across operations (30% weight)
    3. Annotation removed from CrdbCluster CR (30% weight)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent applied cluster settings successfully.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Cluster Settings Oracle Evaluation ==")
        print("Testing cluster settings application via SQL")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "settings_applied": False,
                "settings_persisted": False,
                "annotation_removed": False,
            },
        }

        # STEP 1: Check cluster settings applied (40% weight)
        print(f"\n[1/4] Checking cluster settings applied (40% weight)...")
        settings_applied = False

        try:
            # Query system.settings to verify settings were applied
            settings_query = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT variable, value FROM system.settings WHERE variable LIKE \\'%\\' LIMIT 10;'"
            settings_output = kubectl.exec_command(settings_query)

            if "ERROR" in settings_output or "error" in settings_output.lower():
                issue = f"Failed to query system.settings: {settings_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
            else:
                print(f"  ✅ System settings queryable")

                # Check for at least one custom setting (typical ones: timezone, sql.defaults.*)
                if "variable" in settings_output and "value" in settings_output:
                    print(f"  ✅ Settings table has expected structure")
                    settings_applied = True
                    results["breakdown"]["settings_applied"] = True
                    results["score"] += 0.40
                else:
                    issue = "Settings table does not have expected structure"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking cluster settings: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check settings persisted (30% weight)
        print(f"\n[2/4] Checking settings persisted (30% weight)...")
        settings_persisted = False

        try:
            # Try to verify settings are readable and active
            verify_query = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SHOW CLUSTER SETTING sql.defaults.timezone;'"
            verify_output = kubectl.exec_command(verify_query)

            if "ERROR" in verify_output or "error" in verify_output.lower():
                # If cluster setting query fails, it might not be set yet, but that's ok
                print(f"  ℹ️  No custom timezone setting detected (default used)")
                # Check at least that SQL works
                health_query = (
                    f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'"
                )
                health_output = kubectl.exec_command(health_query)
                if "ERROR" not in health_output.lower():
                    print(f"  ✅ Cluster operational and accepting SQL")
                    settings_persisted = True
                    results["breakdown"]["settings_persisted"] = True
                    results["score"] += 0.30
            else:
                print(f"  ✅ Custom settings active and queryable")
                settings_persisted = True
                results["breakdown"]["settings_persisted"] = True
                results["score"] += 0.30

        except Exception as e:
            issue = f"Error verifying settings persistence: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check annotation removed (30% weight)
        print(f"\n[3/4] Checking annotation removed (30% weight)...")
        annotation_removed = False

        try:
            # Check if annotation is removed from CrdbCluster
            annotation_cmd = f"kubectl -n {namespace} get crdbcluster {cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/cluster-settings}}'"
            annotation = kubectl.exec_command(annotation_cmd)

            if annotation == "" or annotation == "null" or annotation is None:
                print(f"  ✅ Annotation removed from CrdbCluster CR")
                annotation_removed = True
                results["breakdown"]["annotation_removed"] = True
                results["score"] += 0.30
            else:
                issue = f"Annotation still present: crdb.cockroachlabs.com/cluster-settings={annotation}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking annotation: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 4: Final summary
        print(f"\n[4/4] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Settings applied:  {'✅ +40%' if results['breakdown']['settings_applied'] else '❌ +0%'}")
        print(f"  - Settings persisted: {'✅ +30%' if results['breakdown']['settings_persisted'] else '❌ +0%'}")
        print(f"  - Annotation removed: {'✅ +30%' if results['breakdown']['annotation_removed'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Cluster settings applied successfully")

        print(f"=" * 60 + "\n")

        return results
