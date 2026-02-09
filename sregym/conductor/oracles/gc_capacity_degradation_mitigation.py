from sregym.conductor.oracles.base import Oracle


class GCCapacityDegradationMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate if the GOGC environment variable has been restored to its default value (100)
        for all containers in all deployments.
        """
        print("== Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        results = {}
        mitigation_successful = True

        # Get all deployment names
        deployments_cmd = f"kubectl get deployments -n {namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        deployment_names = kubectl.exec_command(deployments_cmd).split()

        if not deployment_names:
            print("No deployments found in namespace")
            results["success"] = False
            return results

        for deployment_name in deployment_names:
            if not deployment_name:
                continue

            print(f"Checking deployment: {deployment_name}")

            # Get container count
            containers_cmd = f"kubectl get deployment {deployment_name} -n {namespace} -o jsonpath='{{.spec.template.spec.containers[*].name}}'"
            container_names = kubectl.exec_command(containers_cmd).split()

            for i, container_name in enumerate(container_names):
                # Check GOGC value
                gogc_check_cmd = f"kubectl get deployment {deployment_name} -n {namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env[?(@.name==\"GOGC\")].value}}'"
                gogc_value = kubectl.exec_command(gogc_check_cmd).strip()

                if gogc_value:
                    print(f"  Container {container_name}: GOGC={gogc_value}")
                    if gogc_value != "100":
                        print(f"  ❌ GOGC value is {gogc_value}, expected 100")
                        mitigation_successful = False
                else:
                    # If GOGC is not set, it uses the default (100), which is acceptable
                    print(f"  Container {container_name}: GOGC not set (using default 100)")

        results["success"] = mitigation_successful

        print(f"Mitigation Result: {'Pass ✅' if mitigation_successful else 'Fail ❌'}")

        return results
