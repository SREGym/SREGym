import time

from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_remote_os import RemoteOSFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class KubeletCrash(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.rollout_services = ["frontend", "frontend-proxy", "currency"]
        self.injector = RemoteOSFaultInjector()

        super().__init__(app=self.app, namespace=self.namespace)

        # note from JC after talking to Bohan:
        # We could consider adding an oracle later, but it's not trivial where localization should go
        # Same with mitigation, this is done with a script to kill the kubelet daemon.
        # Maybe we could implement an oracle later to check for the status of the kubelet daemon?
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_kubelet_crash()
        # rollout the services to trigger the failure
        for service in self.rollout_services:
            print(f"Rolling out {service}...")
            self.kubectl.trigger_rollout(deployment_name=service, namespace=self.namespace)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_kubelet_crash()
        for service in self.rollout_services:
            print(f"Rolling out {service}...")
            self.kubectl.trigger_rollout(deployment_name=service, namespace=self.namespace)
