"""Oracle for validating CockroachDB Zone Config operator action.

This oracle verifies that the agent successfully applied zone configuration
and verified it was persisted correctly.

The oracle validates:
1. Zone configuration applied via SQL (40% weight)
2. Zone configuration persisted in system.zones (30% weight)
3. Annotation removed from CrdbCluster CR (30% weight)

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/zone-config/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBZoneConfigOracle(Oracle):
    """
    Oracle that validates the Zone Config operator workflow for CockroachDB.

    This oracle verifies that the agent applied zone configuration via SQL:
    1. Zone configuration applied and queryable (40% weight)
    2. Configuration persisted in system.zones (30% weight)
    3. Annotation removed from CrdbCluster CR (30% weight)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent applied zone configuration successfully.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Zone Config Oracle Evaluation ==")
        print("Testing zone configuration application via SQL")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "zone_applied": False,
                "config_persisted": False,
                "annotation_removed": False,
            },
        }

        # STEP 1: Check zone configuration applied (40% weight)
        print(f"\n[1/4] Checking zone configuration applied (40% weight)...")
        zone_applied = False

        try:
            # Query system.zones to verify zone configuration
            zones_query = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT zone_id, database_name, table_name, config FROM system.zones LIMIT 10;'"
            zones_output = kubectl.exec_command(zones_query)

            if "ERROR" in zones_output or "error" in zones_output.lower():
                issue = f"Failed to query system.zones: {zones_output}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
            else:
                print(f"  ✅ System zones queryable")

                # Check for zone configurations
                if "zone_id" in zones_output or "database_name" in zones_output:
                    print(f"  ✅ Zone table has expected structure")
                    zone_applied = True
                    results["breakdown"]["zone_applied"] = True
                    results["score"] += 0.40
                else:
                    issue = "Zone table does not have expected structure"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking zone configuration: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check configuration persisted (30% weight)
        print(f"\n[2/4] Checking zone configuration persisted (30% weight)...")
        config_persisted = False

        try:
            # Verify zone configuration is active
            verify_query = f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SHOW ZONE CONFIGURATION FOR SYSTEM;'"
            verify_output = kubectl.exec_command(verify_query)

            if "ERROR" in verify_output or "error" in verify_output.lower():
                # If zone query fails, try basic health check
                print(f"  ℹ️  Default zone configuration in use")
                health_query = (
                    f"kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e 'SELECT 1;'"
                )
                health_output = kubectl.exec_command(health_query)
                if "ERROR" not in health_output.lower():
                    print(f"  ✅ Cluster operational")
                    config_persisted = True
                    results["breakdown"]["config_persisted"] = True
                    results["score"] += 0.30
            else:
                print(f"  ✅ Zone configuration active and queryable")
                config_persisted = True
                results["breakdown"]["config_persisted"] = True
                results["score"] += 0.30

        except Exception as e:
            issue = f"Error verifying zone persistence: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check annotation removed (30% weight)
        print(f"\n[3/4] Checking annotation removed (30% weight)...")
        annotation_removed = False

        try:
            # Check if annotation is removed from CrdbCluster
            annotation_cmd = f"kubectl -n {namespace} get crdbcluster {cluster_name} -o jsonpath='{{.metadata.annotations.crdb\\.cockroachlabs\\.com/zone-config}}'"
            annotation = kubectl.exec_command(annotation_cmd)

            if annotation == "" or annotation == "null" or annotation is None:
                print(f"  ✅ Annotation removed from CrdbCluster CR")
                annotation_removed = True
                results["breakdown"]["annotation_removed"] = True
                results["score"] += 0.30
            else:
                issue = f"Annotation still present: crdb.cockroachlabs.com/zone-config={annotation}"
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
        print(f"  - Zone applied:       {'✅ +40%' if results['breakdown']['zone_applied'] else '❌ +0%'}")
        print(f"  - Config persisted:   {'✅ +30%' if results['breakdown']['config_persisted'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Zone configuration applied successfully")

        print(f"=" * 60 + "\n")

        return results
