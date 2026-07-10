from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


class _RecordingKubeCtl:
    def __init__(self):
        self.commands = []

    def exec_command(self, command):
        self.commands.append(command)
        return "OK\n"


def test_rolling_update_recovery_uses_saved_original_and_waits_for_readiness():
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()

    injector.recover_rolling_update_misconfigured(["custom-service"])

    assert injector.kubectl.commands == [
        "kubectl delete deployment custom-service -n social-network",
        "kubectl apply -f /tmp/custom-service-orig.yaml -n social-network",
        "kubectl rollout status deployment/custom-service -n social-network --timeout=120s",
    ]
