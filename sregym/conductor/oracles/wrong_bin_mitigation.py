from sregym.conductor.oracles.base import Oracle


class WrongBinMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        # Check if the deployment was updated to use the right binary
        expected_command = (
            "profile"  # Command dictates which binary will be ran, we want to run /go/bin/profile and not /go/bin/geo
        )

        try:
            deployment = kubectl.get_deployment(self.problem.faulty_service, namespace)
            containers = deployment.spec.template.spec.containers

            for container in containers:
                command = container.command or []
                if expected_command not in command:
                    print(f"[❌] Deployment for container '{container.name}' is using wrong binary: {command}")
                    # Compared against the command the injector replaced, so
                    # this is the injected fault observed directly.
                    return self.fail(
                        "fault_still_present",
                        container=container.name,
                        command=list(command),
                        expected=expected_command,
                    )

            print("[✅] Deployment is using the correct binary.")
            results["success"] = True
            return results

        except Exception as e:
            print(f"[ERROR] Exception during evaluation: {e}")
            return self.fail_from_exception(e)
