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
        self.console.print(Panel(env_input, title="Environment", style="white on blue"))
        self.console.print()

    async def set_problem(self):
        user_input = await self.get_user_input(completer=self.completer)

        if user_input.startswith("start"):
            try:
                _, problem_id = user_input.split(maxsplit=1)
            except ValueError:
                self.console.print("Invalid command. Please use `start <problem_id>`")
                return

            self.conductor.problem_id = problem_id.strip()

        else:
            self.console.print("Invalid command. Please use `start <problem_id>`")

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
                atexit.register(exit_cleanup_fault, conductor=self.conductor)
                raise SystemExit from None

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

async def main():
    conductor = Conductor()
    agent = HumanAgent(conductor)
    conductor.register_agent(agent, name="human")

    agent.display_welcome_message()
    await agent.set_problem()

    results = await conductor.start_problem()
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
