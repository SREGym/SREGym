from sregym.conductor.oracles.base import Oracle


class ValkeyAuthMitigation(Oracle):
    importance = 1.0

    @staticmethod
    def _requirepass_is_clear(output: str) -> bool:
        lines = output.splitlines()
        if not lines or lines[0].strip() != "requirepass":
            return False

        # valkey-cli may omit the trailing blank value from captured output.
        return all(not line.strip() for line in lines[1:])

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        results = {"success": False}

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Find the valkey pod
        valkey_pods = [
            pod.metadata.name for pod in kubectl.list_pods(namespace).items if "valkey-cart" in pod.metadata.name
        ]

        if not valkey_pods:
            print("❌ No Valkey pod found.")
            return results

        valkey_pod = valkey_pods[0]
        print(f"🔍 Found Valkey pod: {valkey_pod}")

        # Check the current password setting in Valkey
        try:
            command = f"kubectl exec -n {namespace} {valkey_pod} -- valkey-cli CONFIG GET requirepass"
            output = kubectl.exec_command(command)

            if not self._requirepass_is_clear(output):
                print(f"❌ Unexpected valkey-cli CONFIG GET output: {output}")
                return results

            ping_command = f"kubectl exec -n {namespace} {valkey_pod} -- valkey-cli PING"
            ping_output = kubectl.exec_command(ping_command)
            if ping_output.strip() != "PONG":
                print(f"❌ Valkey still requires authentication: {ping_output}")
                return results

            cart = kubectl.get_deployment("cart", namespace)
            desired_replicas = cart.spec.replicas or 0
            available_replicas = cart.status.available_replicas or 0
            if desired_replicas < 1 or available_replicas < desired_replicas:
                print(
                    f"❌ Cart deployment has not recovered: {available_replicas}/{desired_replicas} replicas available."
                )
                return results

            results["success"] = True
            print("✅ Valkey password is cleared and cart is available. Authentication restored.")
        except Exception as e:
            print(f"❌ Error querying Valkey password: {e}")

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")
        return results
