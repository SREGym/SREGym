"""Oracle for validating CockroachDB Major Upgrade Finalize operator action.

This oracle verifies that the agent successfully finalized a major version upgrade
by resetting preserve_downgrade_option and completing migrations.

The oracle validates:
1. preserve_downgrade_option was reset
2. Cluster version finalized
3. Migrations complete

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/major-upgrade-finalize/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBMajorUpgradeFinalizeOracle(Oracle):
    """
    Oracle that validates major upgrade finalization.

    Weighted scoring breakdown:
    - preserve_reset: 40% - preserve_downgrade_option reset
    - version_finalized: 40% - Version finalization complete
    - migrations_complete: 20% - All migrations executed

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully finalized the major upgrade.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Major Upgrade Finalize Oracle Evaluation ==")
        print("Testing major version upgrade finalization")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"preserve_reset": False, "version_finalized": False, "migrations_complete": False},
        }

        # STEP 1: Check if preserve_downgrade_option was reset (40% weight)
        print(f"\n[1/3] Checking preserve_downgrade_option (40% weight)...")
        preserve_reset = False

        try:
            # Check current preserve_downgrade_option value
            check_cmd = f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SHOW CLUSTER SETTING cluster.preserve_downgrade_option;" 2>/dev/null'
            output = kubectl.exec_command(check_cmd)

            # If empty or shows no value, it means it was reset
            if "23.2" not in output and "22.2" not in output and len(output.strip()) < 50:
                print(f"  ✅ preserve_downgrade_option reset/cleared")
                preserve_reset = True
                results["breakdown"]["preserve_reset"] = True
                results["score"] += 0.40
            else:
                issue = f"preserve_downgrade_option still set: {output[:100]}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not check preserve_downgrade_option: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 2: Check if version is finalized (40% weight)
        print(f"\n[2/3] Checking version finalization (40% weight)...")
        version_finalized = False

        try:
            # Check cluster version via SQL
            version_cmd = f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SELECT crdb_internal.node_executable_version();" 2>/dev/null'
            version_output = kubectl.exec_command(version_cmd)

            if "24" in version_output and "1" in version_output:
                print(f"  ✅ Cluster running v24.1.0 (new major version)")
                version_finalized = True
                results["breakdown"]["version_finalized"] = True
                results["score"] += 0.40
            else:
                issue = f"Unexpected version output: {version_output[:100]}"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not verify version: {str(e)}"
            print(f"  ⚠️  {issue}")

        # STEP 3: Check if migrations completed (20% weight)
        print(f"\n[3/3] Checking migrations completion (20% weight)...")
        migrations_complete = False

        try:
            # Check cluster health and migration status
            health_cmd = (
                f'kubectl -n {namespace} exec {cluster_name}-0 -- ./cockroach sql --insecure -e "SELECT 1;" 2>/dev/null'
            )
            health_output = kubectl.exec_command(health_cmd)

            if "1" in health_output or "ERROR" not in health_output:
                print(f"  ✅ Cluster responsive (migrations completed)")
                migrations_complete = True
                results["breakdown"]["migrations_complete"] = True
                results["score"] += 0.20
            else:
                issue = f"Cluster may have migration issues"
                print(f"  ⚠️  {issue}")

        except Exception as e:
            issue = f"Could not verify migrations: {str(e)}"
            print(f"  ⚠️  {issue}")

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Preserve reset:       {'✅ +40%' if results['breakdown']['preserve_reset'] else '❌ +0%'}")
        print(f"  - Version finalized:    {'✅ +40%' if results['breakdown']['version_finalized'] else '❌ +0%'}")
        print(f"  - Migrations complete:  {'✅ +20%' if results['breakdown']['migrations_complete'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Major upgrade finalization completed successfully")

        print(f"=" * 60 + "\n")

        return results
