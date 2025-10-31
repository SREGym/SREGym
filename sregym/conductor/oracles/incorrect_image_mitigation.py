from sregym.conductor.oracles.base import Oracle


class IncorrectImageMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, actual_images: dict = {}):
        super().__init__(problem)
        self.actual_images = actual_images

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_names = self.problem.faulty_service
        results = {}

        results["success"] = True

        for deployment_name in deployment_names:
            # Fetch the current deployment
            deployment = kubectl.get_deployment(deployment_name, namespace)
            container = deployment.spec.template.spec.containers[0]
            actual_image = container.image

            if actual_image == self.actual_images[deployment_name]:
                print(f"❌ Deployment {deployment_name} still using incorrect image: {actual_image}")
                results["success"] = False
            else:
                print(f"✅ Deployment {deployment_name} using correct image: {actual_image}")

        return results
