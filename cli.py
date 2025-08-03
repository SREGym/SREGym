"""SREArena CLI client."""

import asyncio
import atexit

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from srearena.conductor import Conductor, exit_cleanup_fault
from srearena.service.shell import Shell
from srearena.utils.sigint_aware_section import SigintAwareSection

WELCOME = """
# SREArena
- Type your commands or actions below.
- Use `exit` to quit the application.
- Use `start <problem_id>` to begin a new problem.
"""

TASK_MESSAGE = """{prob_desc}
You are provided with the following APIs to interact with the service:

{telemetry_apis}

You are also provided an API to a secure terminal to the service where you can run commands:

{shell_api}

Finally, you will submit your solution for this task using the following API:

{submit_api}

At each turn think step-by-step and respond with your action.
"""


class HumanAgent:
    def __init__(self, conductor):
        self.session = PromptSession()
        self.console = Console(force_terminal=True, color_system="auto")
        self.conductor = conductor
        self.pids = self.conductor.problems.get_problem_ids()
        self.completer = WordCompleter(self.pids, ignore_case=True, match_middle=True)

    def display_welcome_message(self):
        self.console.print(Markdown(WELCOME), justify="center")
        self.console.print()

    def display_context(self, problem_desc, apis):
        self.shell_api = self._filter_dict(apis, lambda k, _: "exec_shell" in k)
        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(apis, lambda k, _: "exec_shell" not in k and "submit" not in k)

        stringify_apis = lambda apis: "\n\n".join([f"{k}\n{v}" for k, v in apis.items()])

        self.task_message = TASK_MESSAGE.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            shell_api=stringify_apis(self.shell_api),
            submit_api=stringify_apis(self.submit_api),
        )

        self.console.print(Markdown(self.task_message))

    def display_env_message(self, env_input):
        if not env_input:
            return
        self.console.print(Panel(env_input, title="Environment", style="white on blue"))
        self.console.print()

    async def set_problem(self, problem_name=None):
        # user_input = await self.get_user_input(completer=self.completer)
        # user_input = "start k8s_target_port-misconfig"
        user_input = f"start {problem_name}"

        if user_input.startswith("start"):
            try:
                _, problem_id = user_input.split(maxsplit=1)
            except ValueError:
                self.console.print("Invalid command. Please use `start <problem_id>`")
                return

            self.conductor.problem_id = problem_id.strip()
            self.completer = None
            self.session = PromptSession()

        else:
            self.console.print("Invalid command. Please use `start <problem_id>`")

    async def get_action(self, env_input):
        self.display_env_message(env_input)
        # user_input = await self.get_user_input()
        user_input = "submit('')"

        if not user_input.strip().startswith("submit("):
            try:
                output = Shell.exec(user_input.strip())
                self.display_env_message(output)
            except Exception as e:
                self.display_env_message(f"[❌] Shell command failed: {e}")
            return await self.get_action(env_input)

        return f"Action:```\n{user_input}\n```"

    async def get_user_input(self, completer=None):
        loop = asyncio.get_running_loop()
        style = Style.from_dict({"prompt": "ansigreen bold"})
        prompt_text = [("class:prompt", "SREArena> ")]

        with patch_stdout():
            try:
                with SigintAwareSection():
                    input = await loop.run_in_executor(
                        None,
                        lambda: self.session.prompt(prompt_text, style=style, completer=completer),
                    )

                    if input.lower() == "exit":
                        raise SystemExit

                    return input
            except (SystemExit, KeyboardInterrupt, EOFError):
                if self.conductor.submission_stage != "detection":
                    atexit.register(exit_cleanup_fault, conductor=self.conductor)
                raise SystemExit from None

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

import argparse
import yaml

def parse_args():
    parser = argparse.ArgumentParser(description="SREArena CLI 实验参数")
    parser.add_argument("--problem", type=str, default="k8s_target_port-misconfig", help="problem name")
    parser.add_argument("--app", type=str, default="social_network", help="app name")
    parser.add_argument("--method", type=str, default="original", help="method name")
    # parser.add_argument("--master", type=str, default=None, help="Enable debug mode")
    # parser.add_argument("--worker1", type=str, default=None, help="Enable debug mode")
    # parser.add_argument("--worker2", type=str, default=None, help="Enable debug mode")
    return parser.parse_args()

def load_inventory(path="./scripts/ansible/inventory.yml"):
    with open(path, "r") as f:
        inventory = yaml.safe_load(f)
    control_host = list(inventory["all"]["children"]["control_nodes"]["hosts"].values())[0]["ansible_host"]
    worker_hosts = [
        v["ansible_host"]
        for v in inventory["all"]["children"]["worker_nodes"]["hosts"].values()
    ]
    return control_host, worker_hosts

async def main():
    import csv
    conductor = Conductor()
    agent = HumanAgent(conductor)
    conductor.register_agent(agent, name="human")

    agent.display_welcome_message()
    args= parse_args()
    print(f"Problem: {args.problem}, App: {args.app}, Method: {args.method}")
    original = (args.method == "original")
    print(f"Original deployment: {original}")
    # master = args.master
    # workers = [args.worker1, args.worker2]
    master, workers = load_inventory()
    print(f"Master: {master}, Workers: {workers}")
    csv_filename = f"{args.problem}-{args.app}-{args.method}.csv"
    fieldnames = ["test_run", "deploy_and_backup_time", "cleanup_or_restoration_time"]

    deploy_times = []
    cleanup_times = []
    with open(csv_filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        csvfile.flush() 
        for i in range(10):
            await agent.set_problem(problem_name=args.problem)
            results = await conductor.start_problem(original=original, master_node=master, worker_nodes=workers)
            deploy_time = results.get("deploy_and_backup_time", 0)
            cleanup_time = results.get("cleanup_or_restoration_time", 0)
            deploy_times.append(deploy_time)
            cleanup_times.append(cleanup_time)
            writer.writerow({
                "test_run": i + 1,
                "deploy_and_backup_time": deploy_time,
                "cleanup_or_restoration_time": cleanup_time
            })
            csvfile.flush() 
            print(results)

        avg_deploy = sum(deploy_times) / len(deploy_times)
        avg_cleanup = sum(cleanup_times) / len(cleanup_times)
        avg_deploy_9 = sum(deploy_times[1:]) / 9
        avg_cleanup_9 = sum(cleanup_times[1:]) / 9

        writer.writerow({
            "test_run": "Average",
            "deploy_and_backup_time": f"{avg_deploy:.4f}",
            "cleanup_or_restoration_time": f"{avg_cleanup:.4f}"
        })
        writer.writerow({
            "test_run": "Average for last 9 runs",
            "deploy_and_backup_time": f"{avg_deploy_9:.4f}",
            "cleanup_or_restoration_time": f"{avg_cleanup_9:.4f}"
        })
        csvfile.flush() 

if __name__ == "__main__":
    asyncio.run(main())
#usage: python3 cli.py --problem k8s_target_port-misconfig --app social_network --method original
# or python3 cli.py --problem k8s_target_port-misconfig --app social_network --method snapshot