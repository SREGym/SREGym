"""Checked Kubernetes command transport for manifest-based applications."""

import subprocess

import yaml


class KubernetesCommands:
    namespace: str

    def command(self, *args, input_text=None, timeout=120):
        return subprocess.run(
            ["kubectl", "-n", self.namespace, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def apply(self, documents):
        self.command("apply", "-f", "-", input_text=yaml.safe_dump_all(documents, sort_keys=False))
