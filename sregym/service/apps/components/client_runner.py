"""Execute application-owned Python workflows inside the client pod."""

import json
from collections.abc import Callable, Iterable
from pathlib import Path


class PythonClientRunner:
    def __init__(self, command: Callable, pod: str = "application-client"):
        self.command = command
        self.pod = pod

    def run(self, sources: Iterable[Path], call: str, *, timeout: int = 180):
        source = "\n".join(path.read_text() for path in sources) + "\n" + call + "\n"
        output = self.command("exec", "-i", self.pod, "--", "python", "-", input_text=source, timeout=timeout)
        return json.loads(output)
