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
from srearena.service.apps.registry import AppRegistry
from srearena.service.kubectl import KubeCtl
from srearena.service.shell import Shell
from srearena.service.telemetry.prometheus import Prometheus
from srearena.utils.sigint_aware_section import SigintAwareSection

WELCOME = """
# SREArena
- Type your commands or actions below.
"""

OPTIONS = """
- Use `start <problem_id>` to begin a new problem.
- Use `deploy <app_name>` to deploy an available app.
- Use `undeploy <app_name>` to undeploy a running app.
- Use `list` to list the applications currently deployed on SREArena.
- Use `options` to list available commands.
- Use `exit` to quit the application.
"""

WARNING = "[bold yellow][WARNING][/bold yellow] Starting a new problem that uses a running app will restart the app. Please make sure you have concluded your work on a deployed app before starting any problem."

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
        self.apps = AppRegistry()
        self.session_purpose = None  # "problem", "app_deploy", "app_undeploy"

        self.instantiate_completer_options()
        self.completer = WordCompleter(self.available_options, ignore_case=True, match_middle=True, sentence=True)

        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()

    def instantiate_completer_options(self):
        pids = self.conductor.problems.get_problem_ids()
        app_names = self.apps.get_app_names()

        self.available_options = ["list", "options", "exit"]

        for pid in pids:
            self.available_options.append(f"start {pid}")

        for app_name in app_names:
            self.available_options.append(f"deploy {app_name}")
            self.available_options.append(f"undeploy {app_name}")

    def display_welcome_message(self):
        self.console.print(Markdown(WELCOME), justify="center")
        self.display_options_message()
        self.console.print()

    def display_options_message(self):
        self.console.print(Markdown(OPTIONS), justify="center")
        self.console.print()
        self.console.print(WARNING)

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

    def print_running_apps(self):
        deployed_apps = self.conductor.get_deployed_apps()
        self.console.print(
            Panel(
                "\n".join(deployed_apps) if len(deployed_apps) > 0 else "No app currently deployed",
                title="Deployed Apps",
                style="white on blue",
            )
        )
        self.console.print()

    async def process_user_command(self):
        user_input = await self.get_user_input(completer=self.completer)
        if user_input.startswith("start"):
            try:
                _, problem_id = user_input.split(maxsplit=1)
            except ValueError:
                self.console.print("Invalid command. Please use `start <problem_id>`")

            self.conductor.problem_id = problem_id.strip()
            self.completer = None
            self.session = PromptSession()
            self.session_purpose = "problem"
        elif user_input.startswith("deploy"):
            try:
                _, app_name = user_input.split(maxsplit=1)
            except ValueError:
                self.console.print("Invalid command. Please use `deploy <app_name>`")

            self.app_name = app_name.strip()
            self.session_purpose = "app_deploy"
        elif user_input.startswith("undeploy"):
            try:
                _, app_name = user_input.split(maxsplit=1)
            except ValueError:
                self.console.print("Invalid command. Please use `undeploy <app_name>`")

            self.app_name = app_name.strip()
            self.session_purpose = "app_undeploy"
        elif user_input.strip() == "list":
            self.print_running_apps()
        elif user_input.strip() == "options":
            self.display_options_message()
        else:
            self.console.print(
                "Invalid command. Please use the available options. Type `options` to see availble commands."
            )

    async def get_action(self, env_input):
        self.display_env_message(env_input)
        user_input = await self.get_user_input()

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
                if self.session_purpose and self.conductor.submission_stage not in ["detection", "localization", "mitigation"]:
                    atexit.register(exit_cleanup_fault, conductor=self.conductor)
                raise SystemExit from None

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}


async def main():
    conductor = Conductor()
    agent = HumanAgent(conductor)
    conductor.register_agent(agent, name="human")

    agent.display_welcome_message()

    while not agent.session_purpose:
        await agent.process_user_command()

    match agent.session_purpose:
        case "problem":
            results = await conductor.start_problem()
            print(results)
        case "app_deploy":
            conductor.app = conductor.apps.get_app_instance(agent.app_name)
            conductor.deploy_app()
        case "app_undeploy":
            conductor.app = conductor.apps.get_app_instance(agent.app_name)
            conductor.undeploy_app()
        case _:
            pass


if __name__ == "__main__":
    asyncio.run(main())
