import asyncio
import atexit
import os
import time
from json.decoder import JSONDecodeError

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.parser import ResponseParser
from srearena.conductor.problems.registry import ProblemRegistry
from srearena.service.kubectl import KubeCtl
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

        self.problem = None
        self.detection_oracle = None
        self.problem_id = None
        self.submission_stage = None  # "noop", "detection", "localization", "mitigation", "done"
        self.results = {}

    def register_agent(self, agent, name="agent"):
        self.agent = agent
        self.agent_name = name

    async def run_problem(self):
        try:
            while self.submission_stage != "done":
                instr = "Please take the next action"
                action = await self.ask_agent(instr)
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
            self.submission_stage = "done"
            return "[✅] NO OP detection evaluated."

        if self.submission_stage == "detection":
            results = self.detection_oracle.evaluate(solution)
            self.results["Detection"] = results

            if results.get("reason") == "Invalid Format":
                return "[⚠️] Invalid detection format. Please try again."

            self.results["TTD"] = time.time() - self.execution_start_time

            if not results.get("success", False):
                self.submission_stage = "done"
                return "[❌] Incorrect detection. Ending evaluation."

            if self.problem.localization_oracle:
                self.submission_stage = "localization"
            elif self.problem.mitigation_oracle:
                self.submission_stage = "mitigation"
            else:
                self.submission_stage = "done"
                return "[✅] Detection successful. No further stages to evaluate."

            return SubmissionStatus.VALID_SUBMISSION

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
        self.detection_oracle = DetectionOracle(self.problem)
        self.results = {}

        # Common setup
        print(f"[Session Start] Problem ID: {self.problem_id}")
        print("Setting up OpenEBS...")
        self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.exec_command(
            'kubectl patch storageclass openebs-hostpath -p \'{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )
        self.kubectl.wait_for_ready("openebs")
        print("OpenEBS setup completed.")

        self.prometheus.deploy()
        self.problem.app.delete()
        self.problem.app.deploy()
        self.problem.app.start_workload()

        # Phase 1: NO OP
        print("\n[NO OP Evaluation] System is healthy. Agent should detect no issue.")
        self.submission_stage = "noop"
        noop_results = await self.run_problem()
        print(f"NO OP Detection Result: {'✅' if noop_results.get('NOOP Detection', {}).get('success') else '❌'}")

        # Phase 2: Inject Fault
        print("[Injecting fault now...]")
        with CriticalSection():
            self.problem.inject_fault()
            atexit.register(exit_cleanup_fault, prob=self.problem)

        # Phase 3: Faulty system
        self.submission_stage = "detection"
        fault_results = await self.run_problem()

        # Final cleanup
        self.execution_end_time = time.time()
        with CriticalSection():
            self.problem.recover_fault()
            atexit.unregister(exit_cleanup_fault)

        self.problem.app.cleanup()
        self.prometheus.teardown()
        self.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
        self.kubectl.exec_command("kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.wait_for_namespace_deletion("openebs")

        self.results.update(fault_results)
        return self.results

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

            self.problem.app.cleanup()

        self.prometheus.teardown()

        self.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
        self.kubectl.exec_command("kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml")

        print("\nCleanup complete!")


def exit_cleanup_fault(conductor):
    conductor.exit_cleanup_and_recover_fault()
