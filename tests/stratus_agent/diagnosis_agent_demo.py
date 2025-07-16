import os

os.environ["PROVIDER_TOOLS"] = "openai"
os.environ["MODEL_TOOLS"] = "gpt-4o-mini"
os.environ["URL_TOOLS"] = "https://api.openai.com/v1"
os.environ["API_KEY_TOOLS"] = ""

import atexit
import logging
import subprocess
import socket
import time
from srearena.conductor import Conductor
from srearena.utils.critical_section import CriticalSection
from clients.langgraph_agent.stratus_agent.base_agent import BaseAgent
from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools
from clients.configs.stratus_config import diagnosis_agent_cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def wait_for_port(port, host="127.0.0.1", timeout=10):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"Port {port} on {host} not ready after {timeout} seconds")


def setup_port_forwarding():
    pf_jaeger = subprocess.Popen(
        ["kubectl", "port-forward", "svc/jaeger", "16686:16686", "-n", "test-hotel-reservation"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    try:
        wait_for_port(16686)
        print("Port-forwarding jaeger is ready!")
    except TimeoutError as e:
        print(e)
        pf_jaeger.terminate()
        pf_jaeger.wait()
        raise

    pf_prometheus = subprocess.Popen(
        ["kubectl", "port-forward", "svc/prometheus-server", "32000:80", "-n", "observe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    try:
        wait_for_port(32000)
        print("Port-forwarding prometheus is ready!")
    except TimeoutError as e:
        print(e)
        pf_prometheus.terminate()
        pf_prometheus.wait()
        raise

    return [pf_jaeger, pf_prometheus]


if __name__ == "__main__":
    llm = get_llm_backend_for_tools()

    diagnosis_agent = BaseAgent(llm, diagnosis_agent_cfg.copy(update={"max_tool_call": 3}))
    diagnosis_agent.build_agent()

    pid = "revoke_auth_mongodb-1"
    conductor = Conductor()
    problem = conductor.problems.get_problem_instance(pid)

    print(f"[Session Start] Problem ID: {pid}")

    print("Setting up metrics-server...")
    conductor.kubectl.exec_command(
        "kubectl apply -f "
        "https://github.com/kubernetes-sigs/metrics-server/"
        "releases/latest/download/components.yaml"
    )
    conductor.kubectl.exec_command(
        "kubectl -n kube-system patch deployment metrics-server "
        "--type=json -p='["
        '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},'
        '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}'
        "]'"
    )
    conductor.kubectl.wait_for_ready("kube-system")  # metrics-server is deployed in kube-system

    print("Setting up OpenEBS...")
    conductor.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
    conductor.kubectl.exec_command(
        "kubectl patch storageclass openebs-hostpath "
        '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
    )
    conductor.kubectl.wait_for_ready("openebs")
    print("OpenEBS setup completed.")

    conductor.prometheus.deploy()

    problem.app.delete()
    problem.app.deploy()
    problem.app.start_workload()
    pf_processes = setup_port_forwarding()

    # Phase 1: NO OP
    final_state = diagnosis_agent.run(problem.app.get_app_summary())
    logger.info(f"Final state: {final_state}")
    print(f"NO OP Detection Result: {'✅' if not final_state['ans']['detection'] else '❌'}")

    # Phase 2: Inject Fault
    print("[Injecting fault now...]")
    with CriticalSection():
        problem.inject_fault()
        atexit.register(conductor.exit_cleanup_and_recover_fault)

    # Phase 3: Faulty system
    final_state = diagnosis_agent.run(problem.app.get_app_summary())
    logger.info(f"Final state: {final_state}")
    print("--------------------**********************----------------------")
    print(f"Faulty Result: {'✅' if final_state['ans']['detection'] else '❌'}")
    print("--------------------**********************----------------------")

    # Final cleanup
    with CriticalSection():
        problem.recover_fault()
        atexit.unregister(conductor.exit_cleanup_and_recover_fault)

    for p in pf_processes:
        p.terminate()
        p.wait()

    problem.app.cleanup()
    conductor.prometheus.teardown()
    conductor.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
    conductor.kubectl.exec_command("kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml")
    conductor.kubectl.wait_for_namespace_deletion("openebs")
