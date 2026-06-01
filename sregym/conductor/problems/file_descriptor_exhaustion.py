import json
import subprocess
import textwrap
from kubernetes import client
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.conductor.problems.base import Problem
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

class FileDescriptorExhaustion(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "frontend"
        self.forced_ulimit = 20

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=f"{self.namespace}",
            description=(
                f"The {self.faulty_service} deployment is encountering file descriptor exhaustion. "
                f"The current limit (ulimit -n {self.forced_ulimit}) is insufficient for the deployment, "
                f"causing the 'Too many open files' error."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    def _deploy_connection_flooder(self):
        apps_v1 = client.AppsV1Api()
    
        script = textwrap.dedent("""\
            import socket, time
            target = "frontend.hotel-reservation.svc.cluster.local"
            port = 5000
            held_sockets = []

            for i in range(1500):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setblocking(False)
                    s.connect_ex((socket.gethostbyname(target), port))
                    held_sockets.append(s)
                except Exception as e:
                    pass
                time.sleep(0.01) 
            
            while True:
                time.sleep(30)
        """)

        container = {
            "name": "flooder",
            "image": "python:3.12-alpine",
            "command": ["sh", "-c", 'ulimit -n $(ulimit -Hn); exec python -c "$SCRIPT"'],
            "env": [{"name": "SCRIPT", "value": script}]
        }

        deployment = {
            "metadata": {"name": "fd-flooder", "labels": {"app": "fd-flooder"}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "fd-flooder"}},
                "template": {
                    "metadata": {"labels": {"app": "fd-flooder"}},
                    "spec": {"containers": [container]}
                }
            }
        }
    
        apps_v1.create_namespaced_deployment(self.namespace, deployment)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace = self.namespace)
        injector.inject_fd_exhaustion(
            microservices=[self.faulty_service],
            entrypoint_cmd=f"{self.faulty_service}",
            limit = 1024
        )
        self._deploy_connection_flooder()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_fd_exhaustion(
            microservices=[self.faulty_service],
            entrypoint_cmd=f"{self.faulty_service}"
        )
        try:
            client.AppsV1Api().delete_namespaced_deployment("fd-flooder", self.namespace)
        except Exception:
            pass
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")