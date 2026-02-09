from sregym.conductor.oracles.base import Oracle


class GCCapacityDegradationMitigationOracle(Oracle):
    importance = 1.0

    def run_workload(self, problem, kubectl, namespace="default"):
        """Run workload and return workentry for agent trace saving."""
        problem.start_workload()
        job_name = problem.wrk.job_name
        kubectl.wait_for_job_completion(job_name=job_name, namespace=namespace, timeout=1000)
        workentries = problem.wrk.retrievelog()
        workentry = workentries[0] if workentries else None
        print(f"Workload Entry: {workentry}")
        return workentry

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        # Get all deployments in the namespace
        deployments = kubectl.list_deployments(namespace)
        all_correct = True

        for deployment in deployments.items:
            deployment_name = deployment.metadata.name
            containers = deployment.spec.template.spec.containers

            for container in containers:
                gogc_value = None

                # Check if GOGC environment variable is set
                if container.env:
                    for env_var in container.env:
                        if env_var.name == "GOGC":
                            gogc_value = env_var.value
                            break

                # GOGC should be "100" or unset (which defaults to 100)
                if gogc_value is not None and gogc_value != "100":
                    print(
                        f"❌ Deployment {deployment_name}, container {container.name} has GOGC={gogc_value} (expected 100 or unset)"
                    )
                    all_correct = False
                else:
                    status = "unset (default 100)" if gogc_value is None else gogc_value
                    print(f"✅ Deployment {deployment_name}, container {container.name} has GOGC={status}")

        results["success"] = all_correct
        print(f"Mitigation Result: {'Pass ✅' if all_correct else 'Fail ❌'}")

        return results
