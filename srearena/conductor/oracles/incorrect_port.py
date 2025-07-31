from srearena.conductor.oracles.base import Oracle


class IncorrectPortAssignmentMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        env_var = self.problem.env_var
        expected_port = self.problem.correct_port
        results = {}

        # Fetch deployment
        deployment = kubectl.get_deployment(deployment_name, namespace)
        container = deployment.spec.template.spec.containers[0]

        found = False
        correct = False

        for e in container.env:
            if e.name == env_var:
                found = True
                value = e.value
                print(f"🔍 Found {env_var}={value}")
                parts = value.split(":")
                if len(parts) == 2 and parts[1] == expected_port:
                    correct = True
                else:
                    print(
                        f"❌ Incorrect port: expected {expected_port}, found {parts[1] if len(parts) == 2 else 'N/A'}"
                    )
                break

        if not found:
            print(f"❌ Env var {env_var} not found.")
        results["success"] = found and correct

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")
        return results
