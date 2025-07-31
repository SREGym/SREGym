from srearena.conductor.oracles.base import Oracle


class IncorrectImageMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        results = {}

        # Fetch the current deployment
        deployment = kubectl.get_deployment(deployment_name, namespace)
        container = deployment.spec.template.spec.containers[0]
        actual_image = container.image

        if actual_image == "app-image:latest":
            print(f"❌ Deployment {deployment_name} still using incorrect image: {actual_image}")
            results["success"] = False
        else:
            print(f"✅ Deployment {deployment_name} using correct image: {actual_image}")
            results["success"] = True

        return results
