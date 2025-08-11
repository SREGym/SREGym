import os

os.environ["PROVIDER"] = "openai"
os.environ["WATSONX_API_KEY"] = ""
os.environ["PROVIDER_TOOLS"] = "openai"
os.environ["MODEL_TOOLS"] = "gpt-4o-mini"
os.environ["URL_TOOLS"] = "https://api.openai.com/v1"
os.environ["API_KEY_TOOLS"] = (
    "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"
)

import atexit
import logging
import socket
import subprocess
import time

from clients.configs.stratus_config import diagnosis_agent_cfg
from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools
from clients.langgraph_agent.stratus_agent.base_agent import BaseAgent
from srearena.conductor import Conductor

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
        stderr=subprocess.DEVNULL,
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
        stderr=subprocess.DEVNULL,
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

    diagnosis_agent = BaseAgent(llm, diagnosis_agent_cfg.model_copy(update={"max_round": 3}))
    diagnosis_agent.build_agent()
    diagnosis_agent.save_agent_graph_to_png()

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
    if "detection" in final_state["ans"] and isinstance(final_state["ans"]["detection"], bool):
        print(f"NO OP Detection Result: {'✅' if not final_state['ans']['detection'] else '❌'}")
    else:
        print(f"NO OP Detection Result: '❌'; Invalid answer provided by the agent!")

    # Phase 2: Inject Fault
    print("[Injecting fault now...]")
    problem.inject_fault()
    atexit.register(conductor.exit_cleanup_and_recover_fault)

    # Phase 3: Faulty system
    final_state = diagnosis_agent.run(problem.app.get_app_summary())
    logger.info(f"Final state: {final_state}")
    if "detection" in final_state["ans"] and isinstance(final_state["ans"]["detection"], bool):
        print(f"Faulty Result: {'✅' if final_state['ans']['detection'] else '❌'}")
    else:
        print(f"Faulty Result: '❌'; Invalid answer provided by the agent!")

    # Final cleanup
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
