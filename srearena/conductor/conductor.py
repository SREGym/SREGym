import asyncio
import atexit
import inspect
import os
import time

from srearena.conductor.parser import ResponseParser
from srearena.conductor.problems.registry import ProblemRegistry
from srearena.service.kubectl import KubeCtl
from srearena.service.telemetry.prometheus import Prometheus
from srearena.utils.critical_section import CriticalSection
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
        self.problem_id = None
        self.submission_stage = "detection"  # → detection → localization → mitigation → done
        self.results = {}

    def register_agent(self, agent, name="agent"):
        self.agent = agent
        self.agent_name = name

    def init_problem(self, problem_id: str):
        self.execution_start_time = time.time()
        self.problem_id = problem_id
        self.problem = self.problems.get_problem_instance(problem_id)

        print(f"[Session Start] Problem ID: {problem_id}")
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

        with CriticalSection():
            self.problem.inject_fault()
            atexit.register(exit_cleanup_fault, prob=self.problem)

        if inspect.iscoroutinefunction(self.problem.start_workload):
            asyncio.create_task(self.problem.start_workload())
        else:
            self.problem.start_workload()

        return (
            "Problem loaded.",
            "Use submit(...) when ready.",
            {"submit(...)": "Submit your solution"},
        )

    async def ask_agent(self, input: str):
        return await self.agent.get_action(input)

    async def ask_env(self, input: str):
        # Parse input and ensure it's a submit(...) call
        try:
            parsed = self.parser.parse(input)
        except Exception as e:
            return str(e)

        if parsed["api_name"] != "submit":
            return "[❌] Only `submit(...)` is supported."

        solution = parsed["args"][0] if parsed["args"] else None

        # === Evaluate based on available oracles ===
        if self.submission_stage == "detection":
            if not hasattr(self.problem, "detection_oracle") or self.problem.detection_oracle is None:
                return "[⚠️] This problem does not support detection evaluation."

            results = self.problem.detection_oracle.evaluate(solution)
            self.results["Detection"] = results

            if results["Detection Accuracy"] == "Invalid Format":
                return "[⚠️] Invalid detection format. Please try again."

            self.results["TTD"] = time.time() - self.execution_start_time

            if not results.get("success", False):
                self.submission_stage = "done"
                return "[❌] Incorrect detection. Ending evaluation."

            # Advance to next available stage
            if hasattr(self.problem, "localization_oracle") and self.problem.localization_oracle is not None:
                self.submission_stage = "localization"
            elif hasattr(self.problem, "mitigation_oracle") and self.problem.mitigation_oracle is not None:
                self.submission_stage = "mitigation"
            else:
                self.submission_stage = "done"
                return "[✅] Detection successful. No further stages to evaluate."

            return SubmissionStatus.VALID_SUBMISSION

        elif self.submission_stage == "localization":
            if not hasattr(self.problem, "localization_oracle") or self.problem.localization_oracle is None:
                return "[⚠️] This problem does not support localization evaluation."

            results = self.problem.localization_oracle.evaluate(solution)
            self.results["Localization"] = results

            if (
                "Localization Accuracy" not in results
                or results.get("Localization Accuracy") == 0.0
                and not results.get("is_subset", False)
            ):
                if not isinstance(solution, (str, list)):
                    return "[⚠️] Invalid localization format. Please try again."
                if isinstance(solution, list) and not all(isinstance(x, str) for x in solution):
                    return "[⚠️] Invalid localization list contents. Please try again."

            self.results["TTL"] = time.time() - self.execution_start_time

            if hasattr(self.problem, "mitigation_oracle") and self.problem.mitigation_oracle is not None:
                self.submission_stage = "mitigation"
            else:
                self.submission_stage = "done"
                return "[✅] Localization complete. No mitigation required."

            return SubmissionStatus.VALID_SUBMISSION

        elif self.submission_stage == "mitigation":
            if not hasattr(self.problem, "mitigation_oracle") or self.problem.mitigation_oracle is None:
                return "[⚠️] This problem does not support mitigation evaluation."

            results = self.problem.mitigation_oracle.evaluate()
            self.results["Mitigation"] = results
            self.results["TTM"] = time.time() - self.execution_start_time
            self.submission_stage = "done"
            return SubmissionStatus.VALID_SUBMISSION

        elif self.submission_stage == "done":
            return "[✅] Problem completed."

    async def start_problem(self):
        instr = "Please take the next action"
        try:
            while self.submission_stage != "done":
                action = await self.ask_agent(instr)
                self.sprint.agent(action)
                env_response = await self.ask_env(action)
                self.sprint.service(env_response)

        except Exception as e:
            with CriticalSection():
                self.problem.recover_fault()
                atexit.unregister(exit_cleanup_fault)
            raise e

        self.execution_end_time = time.time()

        with CriticalSection():
            self.problem.recover_fault()
            atexit.unregister(exit_cleanup_fault)

        self.problem.app.cleanup()
        self.prometheus.teardown()

        self.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
        self.kubectl.exec_command("kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.wait_for_namespace_deletion("openebs")

        return {"results": self.results}


def exit_cleanup_fault(prob):
    print("Recovering fault before exit...")
    prob.recover_fault()
