import time

from srearena.conductor.oracles.base import Oracle


class ImbalanceMitigationOracle(Oracle):
    importance = 1.0
    RETRIES = 5

    def __init__(self, problem):
        super().__init__(problem)

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_names = self.problem.faulty_services
        results = {}

        results["success"] = True

        for deployment_name in deployment_names:
            for _ in range(self.RETRIES):
                usage_dict = kubectl.get_pod_cpu_usage(namespace)

                usages = []
                for pod_name, cpu_usage in usage_dict.items():
                    if deployment_name in pod_name and "frontend-proxy" not in pod_name:
                        usages.append(int(cpu_usage))
                max_usage = max(usages)
                if len(usages) == 1:
                    print("Wait the top info to be ready...")
                    time.sleep(10)
                    continue
                average_others = (sum(usages) - max_usage) / (len(usages) - 1)

                if max_usage > average_others * 3:
                    print(
                        f"❌ Deployment {deployment_name} still not balanced (max usage: {max_usage}, average others: {average_others})"
                    )
                    results["success"] = False
                    return results

                time.sleep(10)  # wait for variation

        print(f"✅ Deployment {deployment_name} balanced")
        results["success"] = True
        return results
