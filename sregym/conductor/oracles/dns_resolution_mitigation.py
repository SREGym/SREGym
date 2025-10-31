from sregym.conductor.oracles.base import Oracle


class DNSResolutionMitigationOracle(Oracle):

    importance = 1.0

    def evaluate(self) -> dict:
        print("== DNS Resolution Mitigation Check ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service

        service_names = [svc.metadata.name for svc in kubectl.list_services(namespace).items]

        if faulty_service == None:
            faulty_service = service_names[0]

        # Get the service's selector
        command = f"kubectl get service {faulty_service} -n {namespace} -o jsonpath='{{.spec.selector}}'"
        selector_output = kubectl.exec_command(command).strip()

        # Parse selector to label query
        selector_dict = eval(selector_output)
        label_selector = ",".join(f"{k}={v}" for k, v in selector_dict.items())

        # Get pod names using the selector
        command = f"kubectl get pods -n {namespace} -l {label_selector} -o jsonpath='{{.items[*].metadata.name}}'"
        pod_names = kubectl.exec_command(command).strip().split()

        target_pod = pod_names[0]

        if not pod_names:
            print("❌ No running pod found for the faulty service(s).")
            return {"success": False}
        else:

            failing = []

            for svc in service_names:
                try:
                    command = (
                        f"kubectl exec -n {namespace} {target_pod} -- getent hosts {svc}.{namespace}.svc.cluster.local"
                    )
                    output = kubectl.exec_command(command)
                    is_success = "exit code" not in output

                    if not is_success:
                        failing.append(svc)
                        print(f"[❌] Failed DNS Resolution: {svc}")
                    else:
                        print(f"[✅] Successfully resolved DNS for {svc}")

                except Exception as e:
                    print(f"Error execing getent hosts in pod {target_pod}: {e}")
                    failing.append(svc)

        if failing:
            print(
                f"[❌] Faulty Service: {faulty_service} | Failed DNS Resolutions from target pod {target_pod}: {', '.join(failing)}"
            )
            return {"success": False}

        print(f"[✅] All service names resolved inside target pod {target_pod}")
        return {"success": True}
