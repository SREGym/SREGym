from sregym.conductor.oracles.base import Oracle


class IncorrectImageMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, actual_images: dict = None):
        if actual_images is None:
            actual_images = {}
        super().__init__(problem)
        self.actual_images = actual_images

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_names = self.problem.faulty_service

        # The loop checks every faulty Deployment rather than stopping at the
        # first, so collect them all: which ones were left broken is more
        # useful than knowing that at least one was.
        still_wrong = {}
        for deployment_name in deployment_names:
            # Fetch the current deployment
            deployment = kubectl.get_deployment(deployment_name, namespace)
            container = deployment.spec.template.spec.containers[0]
            actual_image = container.image

            if actual_image == self.actual_images[deployment_name]:
                print(f"❌ Deployment {deployment_name} still using incorrect image: {actual_image}")
                still_wrong[deployment_name] = actual_image
            else:
                print(f"✅ Deployment {deployment_name} using correct image: {actual_image}")

        if still_wrong:
            # Compared against the exact image the injector wrote, so this is
            # the injected fault observed directly.
            return self.fail("fault_still_present", deployments=still_wrong)
        return {"success": True}
