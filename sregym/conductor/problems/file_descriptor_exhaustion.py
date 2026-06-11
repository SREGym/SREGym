import contextlib
import resource
import socket
import subprocess
import threading
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.fd_exhaustion import FDMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FileDescriptorExhaustion(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "frontend"
        self.forced_ulimit = 1024

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=f"{self.namespace}",
            description=(
                f"The {self.faulty_service} deployment is experiencing file descriptor exhaustion. "
                f"An external load generator is flooding the service with ~1,100 simultaneous TCP connections. "
                f"Because the container's open file limit (ulimit -n) is {self.forced_ulimit}, "
                f"it cannot handle this volume of traffic, causing the pod to drop connections and log "
                f"'too many open files' errors. The root cause is the insufficient ulimit value of {self.forced_ulimit}."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = FDMitigationOracle(problem=self)
        self.stop_flooder = threading.Event()
        self.flooder_thread = None

    def background_flooder(self):
        print("\n[Flooder] Starting flooder background thread...")
        config.load_kube_config()
        v1 = client.CoreV1Api()

        current_pod = None
        pf_process = None
        local_port = None
        addr = None

        while not self.stop_flooder.is_set():
            try:
                all_pods = v1.list_namespaced_pod(self.namespace)
                frontend_pods = [
                    p
                    for p in all_pods.items
                    if p.metadata.name.startswith(f"{self.faulty_service}-") and p.status.phase == "Running"
                ]
                if not frontend_pods:
                    time.sleep(2)
                    continue
                new_pod = frontend_pods[0].metadata.name
            except ApiException:
                time.sleep(2)
                continue

            if new_pod != current_pod:
                if pf_process:
                    pf_process.terminate()
                    pf_process.wait(timeout=2)
                pf_process = subprocess.Popen(
                    ["kubectl", "port-forward", f"pod/{new_pod}", "-n", self.namespace, ":5000"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                local_port = None
                for line in iter(pf_process.stdout.readline, ""):
                    if "Forwarding from 127.0.0.1:" in line:
                        local_port = int(line.split("127.0.0.1:")[1].split()[0])
                        break
                if not local_port:
                    pf_process.terminate()
                    time.sleep(2)
                    continue
                addr = socket.getaddrinfo("127.0.0.1", local_port, type=socket.SOCK_STREAM)[0][4]
                current_pod = new_pod
                self.pf_process = pf_process

            cycle_size = 1100
            hold_seconds = 5
            last_success = time.time()

            while not self.stop_flooder.is_set():
                try:
                    v1.read_namespaced_pod(current_pod, self.namespace)
                except ApiException:
                    break
                try:
                    test_sock = socket.socket()
                    test_sock.settimeout(1.0)
                    test_sock.connect(addr)
                    test_sock.close()
                    last_success = time.time()
                except Exception:
                    if time.time() - last_success > 5:
                        break
                held = []
                for _ in range(cycle_size):
                    if self.stop_flooder.is_set():
                        break
                    s = socket.socket()
                    s.settimeout(0.5)
                    try:
                        s.connect(addr)
                        held.append(s)
                    except (TimeoutError, OSError):
                        s.close()
                    time.sleep(0.002)

                hold_end = time.time() + hold_seconds
                while time.time() < hold_end and not self.stop_flooder.is_set():
                    time.sleep(0.5)

                for s in held:
                    with contextlib.suppress(Exception):
                        s.close()
                time.sleep(2)

            time.sleep(1)

        if pf_process and pf_process.poll() is None:
            pf_process.terminate()
            pf_process.wait(timeout=2)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_fd_exhaustion(
            microservices=[self.faulty_service], entrypoint_cmd=f"{self.faulty_service}", limit=self.forced_ulimit
        )
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if hard != resource.RLIM_INFINITY:
                resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            else:
                resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
        except Exception as e:
            print(f"[Flooder] Warning: Could not raise local FD limit: {e}")

        self.stop_flooder.clear()
        self.flooder_thread = threading.Thread(target=self.background_flooder, daemon=True)
        self.flooder_thread.start()

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_fd_exhaustion(microservices=[self.faulty_service], entrypoint_cmd=f"{self.faulty_service}")

        if self.flooder_thread and self.flooder_thread.is_alive():
            print("Stopping background FD flooder thread...")
            self.stop_flooder.set()
            self.flooder_thread.join(timeout=5)

        if hasattr(self, "pf_process") and self.pf_process and self.pf_process.poll() is None:
            print("Closing port-forward tunnel...")
            self.pf_process.terminate()
            try:
                self.pf_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.pf_process.kill()

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
