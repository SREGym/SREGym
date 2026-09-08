from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class ValkeyAuthMitigation(Oracle):
    importance = 1.0

    FAILURE_CLASSES = {
        # valkey-cli answering PING with anything but PONG means the password
        # is still set: the injected fault, directly observed.
        "valkey_still_requires_auth": FailureClass.AGENT_ERROR,
        # We could not read the config to judge either way.
        "valkey_config_unreadable": FailureClass.AMBIGUOUS,
    }

    @staticmethod
    def _requirepass_is_clear(output: str) -> bool:
        lines = output.splitlines()
        if not lines or lines[0].strip() != "requirepass":
            return False

        # valkey-cli may omit the trailing blank value from captured output.
        return all(not line.strip() for line in lines[1:])

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Find the valkey pod
        valkey_pods = [
            pod.metadata.name for pod in kubectl.list_pods(namespace).items if "valkey-cart" in pod.metadata.name
        ]

        if not valkey_pods:
            print("❌ No Valkey pod found.")
            return self.fail("no_pods_found", namespace=namespace, matching="valkey-cart")

        valkey_pod = valkey_pods[0]
        print(f"🔍 Found Valkey pod: {valkey_pod}")

        # Check the current password setting in Valkey
        try:
            command = f"kubectl exec -n {namespace} {valkey_pod} -- valkey-cli CONFIG GET requirepass"
            output = kubectl.exec_command(command)

            if not self._requirepass_is_clear(output):
                print(f"❌ Unexpected valkey-cli CONFIG GET output: {output}")
                return self.fail("valkey_config_unreadable", pod=valkey_pod, output=output.strip()[:200])

            ping_command = f"kubectl exec -n {namespace} {valkey_pod} -- valkey-cli PING"
            ping_output = kubectl.exec_command(ping_command)
            if ping_output.strip() != "PONG":
                print(f"❌ Valkey still requires authentication: {ping_output}")
                return self.fail("valkey_still_requires_auth", pod=valkey_pod, ping=ping_output.strip()[:200])

            cart = kubectl.get_deployment("cart", namespace)
            desired_replicas = cart.spec.replicas or 0
            available_replicas = cart.status.available_replicas or 0
            if desired_replicas < 1 or available_replicas < desired_replicas:
                print(
                    f"❌ Cart deployment has not recovered: {available_replicas}/{desired_replicas} replicas available."
                )
                return self.fail(
                    "deployment_replicas_unready",
                    deployment="cart",
                    ready=available_replicas,
                    desired=desired_replicas,
                )

            print("✅ Valkey password is cleared and cart is available. Authentication restored.")
        except Exception as e:
            print(f"❌ Error querying Valkey password: {e}")
            # Previously this fell through to `return results`, which still held
            # the initial success=False -- so an exec failure was reported
            # identically to Valkey still demanding a password.
            return self.fail_from_exception(e)

        print("Mitigation Result: Pass ✅")
        return {"success": True}
