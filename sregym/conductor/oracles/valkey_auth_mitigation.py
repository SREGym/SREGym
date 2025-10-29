from sregym.conductor.oracles.base import Oracle


class ValkeyAuthMitigation(Oracle):
    importance = 1.0

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

            if "requirepass" in output:
                parts = output.strip().splitlines()
                if len(parts) == 2 and parts[1] == "":
                    results["success"] = True
                    print("✅ Valkey password is cleared. Authentication restored.")
                else:
                    print(f"❌ Valkey password still set: {parts[1]}")
            else:
                print(f"❌ Unexpected valkey-cli CONFIG GET output: {output}")
        except Exception as e:
            print(f"❌ Error querying Valkey password: {e}")

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")
        return results
