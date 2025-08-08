import asyncio
import atexit
import os
import shutil
import time
from json.decoder import JSONDecodeError

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.parser import ResponseParser
from srearena.conductor.problems.registry import ProblemRegistry
from srearena.service.kubectl import KubeCtl
from srearena.service.apps.registry import AppRegistry
from srearena.service.telemetry.prometheus import Prometheus
from srearena.utils.critical_section import CriticalSection
from srearena.utils.sigint_aware_section import SigintAwareSection
from srearena.utils.status import SessionPrint, SubmissionStatus


class Conductor:
    def __init__(self):
        self.agent = None
        self.agent_name = None
        self.parser = ResponseParser()
        self.problems = ProblemRegistry()
        self.sprint = SessionPrint()
        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()
        self.execution_start_time = None
        self.execution_end_time = None
        self.use_wandb = os.getenv("USE_WANDB", "false").lower() == "true"

        self.apps = AppRegistry()
        self.problem = None
        self.app = None
        self.detection_oracle = None
        self.problem_id = None
        self.submission_stage = None  # "noop", "detection", "localization", "mitigation", "done"
        self.results = {}

        self.strict_detection_mode = False  # With strict detection as True, if the agent doesn't
        # get the anomaly detection correct, it cannot advance to
        # later stages.

        # Dependency check
        self.dependency_check(["kubectl", "helm"])

    def dependency_check(self, binaries: list[str]):
        for binary in binaries:
            if shutil.which(binary) is None:
                raise RuntimeError(f"[❌] Required dependency '{binary}' not found. Please install {binary}.")

    def register_agent(self, agent, name="agent"):
        self.agent = agent
        self.agent_name = name

    async def run_problem(self):
        try:
            while self.submission_stage != "done":
                action = await self.ask_agent("")
                self.sprint.agent(action)
                env_response = await self.ask_env(action)
                self.sprint.service(env_response)
        except Exception as e:
            with CriticalSection():
                self.problem.recover_fault()
                atexit.unregister(self.exit_cleanup_and_recover_fault)
            raise e

        return self.results

    async def ask_agent(self, input: str):
        return await self.agent.get_action(input)

    async def ask_env(self, input: str):
        try:
            parsed = self.parser.parse(input)
        except Exception as e:
            return str(e)

        if parsed["api_name"] != "submit":
            return "[❌] Only `submit(...)` is supported."

        solution = parsed["args"][0] if parsed["args"] else None

        if self.submission_stage == "noop":
            results = self.detection_oracle.evaluate(solution)
            self.results["NOOP Detection"] = results
            if results.get("reason") == "Invalid Format":
                return "[⚠️] Invalid detection format. Please try again."
            self.submission_stage = "done"
            return "[✅] NO OP detection evaluated."

        if self.submission_stage == "detection":
            results = self.detection_oracle.evaluate(solution)
            self.results["Detection"] = results

            if results.get("reason") == "Invalid Format":
                return "[⚠️] Invalid detection format. Please try again."

            self.results["TTD"] = time.time() - self.execution_start_time

            if self.problem.localization_oracle:
                self.submission_stage = "localization"
            elif self.problem.mitigation_oracle:
                self.submission_stage = "mitigation"
            else:
                self.submission_stage = "done"

            if self.strict_detection_mode:
                if not results.get("success", False):
                    self.submission_stage = "done"
                    return "[❌] Incorrect detection. Ending evaluation."

            if results.get("success", False):
                if self.submission_stage == "done":
                    return "[✅] Detection successful. No further stages to evaluate."
                else:
                    return "[✅] Detection successful. Proceeding to next stage..."
            else:
                if self.submission_stage == "done":
                    return "[❌] Incorrect detection. No further stages to evaluate."
                else:
                    return "[❌] Incorrect detection. Proceeding anyway..."

        elif self.submission_stage == "localization":
            if not self.problem.localization_oracle:
                return "[⚠️] This problem does not support localization evaluation."

            results = self.problem.localization_oracle.evaluate(solution)
            self.results["Localization"] = results
            self.results["TTL"] = time.time() - self.execution_start_time

            if self.problem.mitigation_oracle:
                self.submission_stage = "mitigation"
            else:
                self.submission_stage = "done"

            if results.get("success", False):
                return "[✅] Localization successful. Proceeding..."
            else:
                return "[❌] Incorrect localization. Proceeding anyway..."

        if self.submission_stage == "mitigation":
            results = self.problem.mitigation_oracle.evaluate()
            self.results["Mitigation"] = results
            self.results["TTM"] = time.time() - self.execution_start_time
            self.submission_stage = "done"
            return SubmissionStatus.VALID_SUBMISSION

        return "[✅] Problem completed."

    async def start_problem(self):
        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)
        self.results = {}

        print(f"[Session Start] Problem ID: {self.problem_id}")

        self.deploy_app()

        # Phase 1: NO OP
        print("\n[NO OP Evaluation] System is healthy. Agent should detect no issue.")
        self.submission_stage = "noop"
        noop_results = await self.run_problem()
        print(f"NO OP Detection Result: {'✅' if noop_results.get('NOOP Detection', {}).get('success') else '❌'}")

        # Phase 2: Inject Fault
        print("[Injecting fault now...]")
        with CriticalSection():
            self.problem.inject_fault()
            atexit.register(self.exit_cleanup_and_recover_fault)

        # Phase 3: Faulty system
        self.submission_stage = "detection"
        fault_results = await self.run_problem()

        # Final cleanup
        self.execution_end_time = time.time()
        with CriticalSection():
            self.problem.recover_fault()
            atexit.unregister(self.exit_cleanup_and_recover_fault)

        self.undeploy_app()

        self.results.update(fault_results)
        return self.results
    
    def deploy_app(self):
        try:
            with SigintAwareSection():
                if not self.kubectl.get_service_deployment_status("metrics-server", "kube-system"):
                    print("Setting up metrics-server...")
                    self.kubectl.exec_command(
                        "kubectl apply -f "
                        "https://github.com/kubernetes-sigs/metrics-server/"
                        "releases/latest/download/components.yaml"
                    )
                    self.kubectl.exec_command(
                        "kubectl -n kube-system patch deployment metrics-server "
                        "--type=json -p='["
                        '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},'
                        '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}'
                        "]'"
                    )
                    self.kubectl.wait_for_ready("kube-system")  # metrics-server is deployed in kube-system
                else:
                    print("metrics-server already deployed. Skipping setup.")

                if not self.kubectl.get_namespace_deployment_status("openebs"):
                    print("Setting up OpenEBS...")
                    self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
                    self.kubectl.exec_command(
                        "kubectl patch storageclass openebs-hostpath "
                        '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
                    )
                    self.kubectl.wait_for_ready("openebs")
                    print("OpenEBS setup completed.")
                else:
                    print("OpenEBS already deployed. Skipping setup.")

                self.prometheus.deploy()

                self.app.delete()
                self.app.deploy()
                self.app.start_workload()
        except KeyboardInterrupt:
            print("\nImmediately terminating and Cleaning up...")
            atexit.register(self.exit_cleanup_and_recover_fault)
            raise SystemExit from None
        
        print("\nApp has successfully been deployed!")

    def undeploy_app(self):    
        self.app.cleanup()

        deployed_apps = self.get_deployed_apps()
        if len(deployed_apps) == 0:
            self.prometheus.teardown()
            self.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
            self.kubectl.exec_command("kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml")
            self.kubectl.wait_for_namespace_deletion("openebs")
        else:
            print("Other apps still running. Skipping OpenEBS and Prometheus deletion.")

        print("\nApp has successfully been undeployed!")

    def exit_cleanup_and_recover_fault(self):
        if self.problem:
            print("Recovering fault before exit...")
            try:
                self.problem.recover_fault()
            except JSONDecodeError:
                # CTRL+C before service is set up results in a JSONDecodeError
                print("Service has not been set up. Skipping fault recovery.")
            except RuntimeError:
                # When waiting for namespace deletion, console.status() is called and results in a RuntimeError
                pass

        self.undeploy_app()
        print("\nCleanup complete!")

    def get_deployed_apps(self):
        deployed_apps = []
        for app_name in self.apps.get_app_names():
            namespace = self.apps.get_app_metadata(app_name)["Namespace"]
            if self.kubectl.get_namespace_deployment_status(namespace):
                deployed_apps.append(app_name)

        return deployed_apps

def exit_cleanup_fault(conductor):
    conductor.exit_cleanup_and_recover_fault()
