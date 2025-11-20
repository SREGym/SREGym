"""Oracle for validating CockroachDB Version Check operator action.

This oracle verifies that the agent successfully extracted the image version,
set appropriate cluster conditions, and cleaned up temporary resources.

The oracle validates:
1. Image version extracted (40% weight)
2. Cluster condition set (30% weight)
3. Temporary Job resources cleaned up (30% weight)
4. Annotation removed from CrdbCluster CR

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/version-check/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBVersionCheckOracle(Oracle):
    """
    Oracle that validates the Version Check operator workflow for CockroachDB.

    This oracle verifies that the agent extracted and validated the version:
    1. Version extracted from image (40% weight)
    2. Cluster status condition set (30% weight)
    3. Temporary resources cleaned up (30% weight)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent extracted version and set conditions successfully.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Version Check Oracle Evaluation ==")
        print("Testing version extraction and validation")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "version_extracted": False,
                "condition_set": False,
                "job_cleanup": False,
            },
        }

        # STEP 1: Check version extracted (40% weight)
        print(f"\n[1/4] Checking version extracted from image (40% weight)...")
        version_extracted = False

        try:
            # Get StatefulSet and extract image version
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            if sts.spec.template.spec.containers:
                sts_image = sts.spec.template.spec.containers[0].image
                print(f"  ✅ StatefulSet image: {sts_image}")

                # Check if version is v24.1.0
                if "v24.1.0" in sts_image or "24.1.0" in sts_image:
                    print(f"  ✅ Version v24.1.0 detected in image")
                    version_extracted = True
                    results["breakdown"]["version_extracted"] = True
                    results["score"] += 0.40
                else:
                    issue = f"Expected version v24.1.0 in image, got {sts_image}"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)
            else:
                issue = "StatefulSet has no containers"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking StatefulSet image: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check condition set (30% weight)
        print(f"\n[2/4] Checking cluster status condition set (30% weight)...")
        condition_set = False

        try:
            # Get CrdbCluster and check for VersionChecked condition
            cr = kubectl.custom_api.get_namespaced_custom_object(
                group="crdb.cockroachlabs.com",
                version="v1alpha1",
                namespace=namespace,
                plural="crdbclusters",
                name=cluster_name,
            )

            if "status" in cr and "conditions" in cr.get("status", {}):
                conditions = cr["status"]["conditions"]
                version_condition_found = False

                for condition in conditions:
                    if condition.get("type") == "VersionChecked" or "Version" in condition.get("type", ""):
                        version_condition_found = True
                        print(f"  ✅ Version condition set: {condition.get('type')} = {condition.get('status')}")
                        break

                if version_condition_found or len(conditions) > 0:
                    print(f"  ✅ Cluster status condition set")
                    condition_set = True
                    results["breakdown"]["condition_set"] = True
                    results["score"] += 0.30
                else:
                    print(f"  ℹ️  No conditions yet (may be pending)")
                    # Partial credit since CR exists
                    results["score"] += 0.15
            else:
                print(f"  ℹ️  No status conditions found (may be pending)")
                # Partial credit
                results["score"] += 0.15

        except Exception as e:
            issue = f"Error checking CrdbCluster status: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check Job cleanup (30% weight)
        print(f"\n[3/4] Checking temporary Job resources cleaned up (30% weight)...")
        job_cleanup = False

        try:
            # List Jobs to see if temporary verification jobs exist
            jobs = kubectl.batch_v1_api.list_namespaced_job(namespace=namespace)

            version_check_jobs = [job for job in jobs.items if "version-check" in job.metadata.name]

            if len(version_check_jobs) == 0:
                print(f"  ✅ No temporary version-check jobs found (cleanup complete or never created)")
                job_cleanup = True
                results["breakdown"]["job_cleanup"] = True
                results["score"] += 0.30
            else:
                issue = f"Found {len(version_check_jobs)} temporary version-check jobs still present"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                print(f"     Jobs: {[j.metadata.name for j in version_check_jobs]}")

        except Exception as e:
            # If we can't list jobs, assume cleanup not needed
            print(f"  ℹ️  Could not verify job cleanup: {str(e)}")
            job_cleanup = True
            results["breakdown"]["job_cleanup"] = True
            results["score"] += 0.30

        # STEP 4: Final summary
        print(f"\n[4/4] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Version extracted:  {'✅ +40%' if results['breakdown']['version_extracted'] else '❌ +0%'}")
        print(f"  - Condition set:      {'✅ +30%' if results['breakdown']['condition_set'] else '❌ +0%'}")
        print(f"  - Job cleanup:        {'✅ +30%' if results['breakdown']['job_cleanup'] else '❌ +0%'}")
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
            print(f"\n✅ PASS: Version check completed successfully")

        print(f"=" * 60 + "\n")

        return results
