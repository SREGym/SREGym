"""
SREGym CLI client. Use this for debugging and platform development work—
otherwise use main.py.

This version talks directly to the in-process Conductor for both environment
setup and grading, but still gives you the PromptToolkit+Rich UI.
"""

import asyncio
import json
import logging
import sys
from multiprocessing import Process, set_start_method
from threading import Thread

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from dashboard.dashboard_app import SREGymDashboardServer
from dashboard.proxy import LogProxy
from logger import init_logger
from sregym.conductor.conductor import Conductor
from sregym.conductor.constants import StartProblemResult
from sregym.service.shell import Shell

WELCOME = """
# SREGym
- This CLI is used by benchmark developers to test new problems.
"""

OPTIONS = """
- Use `start <problem_id>` to run your selected problem.
- Tools like `get_logs`, `get_metrics`, and `get_traces` are available in this CLI.
- Use the `submit()` function in the console to test an agent submission.
"""

WARNING = (
    "[bold yellow][WARNING][/bold yellow] Starting a new problem will "
    "restart any running app. Make sure you finish working before you start."
)

# (If you still want TASK_MESSAGE for problem context, you can re-enable it here.)


class HumanAgent:
    def __init__(self, conductor: Conductor):
        self.session = PromptSession()
        self.console = Console(force_terminal=True, color_system="auto")
        self.conductor = conductor
        self.pids = self.conductor.problems.get_problem_ids()
        self.completer = WordCompleter(
            ["list", "options", "exit"] + [f"start {pid}" for pid in self.pids],
            ignore_case=True,
            match_middle=True,
            sentence=True,
        )
        self.session_purpose = None  # "problem", "exit", etc.

    def display_welcome(self):
        self.console.print(Markdown(WELCOME), justify="center")
        self.console.print(Markdown(OPTIONS), justify="center")
        self.console.print(WARNING)
        self.console.print()

    async def select_mode(self):
        """Prompt until we get 'start <problem_id>' or 'exit'."""
        while True:
            inp = await self._prompt()
            cmd = inp.strip().split(maxsplit=1)
            if cmd[0].lower() == "exit":
                sys.exit(0)
            if cmd[0].lower() == "options":
                self.console.print(Markdown(OPTIONS), justify="center")
                continue
            if cmd[0].lower() == "start" and len(cmd) == 2:
                pid = cmd[1]
                if pid not in self.pids:
                    self.console.print(f"[red]Unknown problem id: {pid}")
                    continue
                self.conductor.problem_id = pid
                self.session_purpose = "problem"
                return
            self.console.print("[red]Invalid command. Type `options` to see choices.")

    async def interactive_loop(self):
        """Once problem is started, repeatedly shell or submit until done."""
        env = ""
        while self.conductor.submission_stage != "done":
            # display last environment or grading response
            if env:
                print(env)

            inp = await self._prompt()
            text = inp.strip()

            # shell command
            if not text.startswith("submit("):
                try:
                    out = Shell.exec(text)
                except Exception as e:
                    out = f"[❌] Shell error: {e}"
                env = out
                continue

            wrapped = f"```\n{text}\n```"
            try:
                resp = await self.conductor.submit(wrapped)
            except Exception as e:
                env = f"[❌] Grading error: {e}"
            else:
                env = resp

        # final results panel
        final = json.dumps(self.conductor.results, indent=2)
        self.console.print(Panel(final, title="Final Results", style="bold green"))

    async def _prompt(self) -> str:
        loop = asyncio.get_running_loop()
        style = Style.from_dict({"prompt": "ansigreen bold"})
        prompt_txt = [("class:prompt", "SREGym> ")]
        with patch_stdout():
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: self.session.prompt(prompt_txt, style=style, completer=self.completer),
                )
            except (KeyboardInterrupt, EOFError):
                sys.exit(0)


def run_dashboard_server():
    """Entry point for multiprocessing child: construct Dash in child process."""
    # Silence child process stdout/stderr and noisy loggers
    import logging
    import os
    import sys

    try:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    server = SREGymDashboardServer(host="127.0.0.1", port=11451, debug=False)
    server.run(threaded=False)


async def main():
    # set up the logger
    """
    logging.getLogger("sregym-global").setLevel(logging.INFO)
    logging.getLogger("sregym-global").addHandler(LogProxy())

    try:
        set_start_method("spawn")
    except RuntimeError:
        pass

    # Start dashboard in a separate process; construct server inside the child
    p = Process(target=run_dashboard_server, daemon=True)
    p.start()
    """

    init_logger()

    """
    import os, subprocess
    
    dash_path = os.path.join(os.path.dirname(__file__), "dashboard", "dashboard_app.py")
    dash_cmd = ["python3", dash_path]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"} 

    proc = subprocess.Popen(
        dash_cmd,
        stdout=subprocess.DEVNULL,  #
        stderr=subprocess.DEVNULL,
        env=env,
    )

    proc.terminate()  
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    """

    conductor = Conductor()
    agent = HumanAgent(conductor)
    conductor.register_agent()  # no-op but for symmetry

    # 1) Intro & pick a problem
    agent.display_welcome()
    await agent.select_mode()

    # 2) Deploy environment & launch HTTP server
    result = await conductor.start_problem()
    while result != StartProblemResult.SUCCESS:
        agent.console.print(
            "[yellow]⏭️  This problem requires Khaos but cannot run on emulated clusters. "
            "Please select another problem.[/yellow]"
        )
        await agent.select_mode()
        result = await conductor.start_problem()

    # 3) Interactive shell / submit loop
    await agent.interactive_loop()


if __name__ == "__main__":
    asyncio.run(main())
