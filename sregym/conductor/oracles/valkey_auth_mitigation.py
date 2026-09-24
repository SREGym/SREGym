from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete


class ValkeyAuthMitigation(Oracle):
    importance = 1.0

    FAILURE_CLASSES = {
        # A password value or explicit authentication error confirms the fault.
        "valkey_still_requires_auth": FailureClass.AGENT_ERROR,
        # We could not read the config to judge either way.
        "valkey_config_unreadable": FailureClass.AMBIGUOUS,
        "valkey_ping_failed": FailureClass.AMBIGUOUS,
    }

    @staticmethod
    def _requirepass_is_clear(output: str) -> bool:
        lines = output.splitlines()
        if not lines or lines[0].strip() != "requirepass":
            return False

        # valkey-cli may omit the trailing blank value from captured output.
        return all(not line.strip() for line in lines[1:])

    @staticmethod
    def _authentication_error(output: str) -> bool:
        response = output.strip().removeprefix("(error) ")
        return response.startswith(("NOAUTH ", "WRONGPASS "))

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
            output = kubectl.exec_command_checked(command, timeout=30)

            if not self._requirepass_is_clear(output):
                lines = output.splitlines()
                if self._authentication_error(output) or (lines and lines[0].strip() == "requirepass"):
                    print("❌ Valkey still requires authentication")
                    return self.fail("valkey_still_requires_auth", pod=valkey_pod)
                print(f"❌ Unexpected valkey-cli CONFIG GET output: {output}")
                return self.fail("valkey_config_unreadable", pod=valkey_pod, output=output.strip()[:200])

            ping_command = f"kubectl exec -n {namespace} {valkey_pod} -- valkey-cli PING"
            ping_output = kubectl.exec_command_checked(ping_command, timeout=30)
            if ping_output.strip() != "PONG":
                print(f"❌ Valkey PING failed: {ping_output}")
                reason = (
                    "valkey_still_requires_auth" if self._authentication_error(ping_output) else "valkey_ping_failed"
                )
                return self.fail(reason, pod=valkey_pod, ping=ping_output.strip()[:200])

            cart = kubectl.get_deployment("cart", namespace)
            desired_replicas = cart.spec.replicas or 0
            available_replicas = cart.status.available_replicas or 0
            if desired_replicas < 1:
                print("❌ Cart deployment is scaled to zero")
                return self.fail("required_deployment_scaled_to_zero", deployment="cart")
            if not deployment_rollout_complete(cart):
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
            return self.fail_from_exception(e)

        print("Mitigation Result: Pass ✅")
        return {"success": True}
