"""Interface to K8S controller service."""

import logging
import re
import shlex
import subprocess  # nosec B404
from enum import Enum

import bashlex
from kubernetes import config
from pydantic.dataclasses import dataclass

from mcp_server.kubectl_server_helper.utils import parse_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class DryRunStatus(Enum):
    SUCCESS = "SUCCESS"
    NOEFFECT = "NOEFFECT"
    ERROR = "ERROR"


@dataclass
class DryRunResult:
    status: DryRunStatus
    description: str
    result: list[str]


class KubeCtl:
    def __init__(self):
        """Initialize the KubeCtl object and load the Kubernetes configuration."""
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        # self.core_v1_api = client.CoreV1Api()
        # self.apps_v1_api = client.AppsV1Api()

    @staticmethod
    def exec_command(command: str, input_data=None):
        """Execute an arbitrary kubectl command."""
        if input_data is not None:
            input_data = input_data.encode("utf-8")
        try:
            out = subprocess.run(command, shell=True, check=True, capture_output=True, input=input_data)  # nosec B602
            out.stdout = out.stdout.decode("utf-8")
            out.stderr = out.stderr.decode("utf-8")
            return out
        except subprocess.CalledProcessError as e:
            e.stderr = e.stderr.decode("utf-8")
            return e

    @staticmethod
    def exec_command_result(command: str, input_data=None) -> str:
        result = KubeCtl.exec_command(command, input_data)
        if result.returncode == 0:
            logger.info(f"Command execution:\n{parse_text(result.stdout, 500)}")
            return result.stdout
        else:
            logger.error(f"Error executing kubectl command:\n{result.stderr}")
            return f"Error executing kubectl command:\n{result.stderr}"

    @staticmethod
    def extract_namespace_from_command(command: str) -> str:
        """
        Returns the namespace.
        """
        namespace = None
        command_parts = list(bashlex.split(command))
        for i, part in enumerate(command_parts):
            if part == "-n" or part == "--namespace":
                if i + 1 < len(command_parts):
                    namespace = command_parts[i + 1]
                    break
            elif part.startswith("--namespace="):
                namespace = part.split("=")[1]
                break
        return namespace

    @staticmethod
    def insert_flags(command: str, flags=str | list[str]) -> str:
        """
        Insert flags into a kubectl command.
        Args:
            command (str | list[str]): The kubectl command to modify.
            flags (str | list[str]): The flags to insert into the command.
        Returns:
            str | list[str]: The modified kubectl command with the flags inserted.
                             The type is the same as the input command.
        """
        flags_parsed = shlex.join(flags) if isinstance(flags, list) else flags

        position = None
        last_word = None

        def traverse_AST(node):
            if node.kind == "word":
                nonlocal position
                nonlocal last_word
                if position is None:
                    if node.word == "--":
                        position = node.pos
                    if node.word == "-" and last_word is not None and last_word.word == "-f":
                        position = last_word.pos
                last_word = node
            if hasattr(node, "parts"):
                for part in node.parts:
                    traverse_AST(part)

        for parts in bashlex.parse(command):
            traverse_AST(parts)

        if position is None:
            return command + " " + flags_parsed
        else:
            position = position[0]
            return command[:position] + " " + flags_parsed + " " + command[position:]

    @staticmethod
    def dry_run_json_output(command: str, keylist: list[str] | str | None = None) -> DryRunResult:
        """ """
        dry_run_arguments = ["--dry-run=server"]

        if isinstance(keylist, list) and len(keylist) != 0:
            keylist = list(map(lambda x: f"{{{x}}}", keylist))
            jsonpath = "$".join(keylist)
            dry_run_arguments.extend(["-o", f"jsonpath='[[[{jsonpath}]]]'"])
        elif isinstance(keylist, str):
            # This case is for kubectl delete, which only supports:
            #   kubectl delete <resource> <name> -o name
            dry_run_arguments.extend(["-o", keylist])

        dry_run_command = KubeCtl.insert_flags(command, dry_run_arguments)
        dry_run_result = subprocess.run(dry_run_command, shell=True, capture_output=True, text=True)  # nosec B602

        if dry_run_result.returncode == 0:
            if len(dry_run_result.stdout.strip()) == 0:
                return DryRunResult(
                    status=DryRunStatus.NOEFFECT,
                    description="The dry-run output is empty. Possibly this command won't affect any resources.",
                    result=[],
                )

            if isinstance(keylist, list) and len(keylist) != 0:
                resource = re.search(r"\[\[\[(.*?)\]\]\]", dry_run_result.stdout, re.DOTALL)
                if resource is None:
                    raise RuntimeError("Unhandled dry-run output format.")
                resource = resource.group(1).strip()
                if resource.count("$") + 1 != len(keylist):
                    raise RuntimeError(f"Invalid resource format in dry-run output. {resource}")
                resources = [r.strip() for r in resource.split("$")]
            elif isinstance(keylist, str):
                resources = [r.strip() for r in dry_run_result.stdout.split("/")]
                if len(resources) != 2:
                    raise RuntimeError(f"Invalid resource format in dry-run output. {dry_run_result.stdout}")
            else:
                resources = [dry_run_result.stdout]

            return DryRunResult(
                status=DryRunStatus.SUCCESS,
                description="Dry run executed successfully.",
                result=resources,
            )
        else:
            if "error: unknown flag: --dry-run" in dry_run_result.stderr:
                return DryRunResult(
                    status=DryRunStatus.NOEFFECT,
                    description="Dry-run not supported. Possibly it's a safe command.",
                    result=[],
                )
            elif "can't be used with attached containers options" in dry_run_result.stderr:
                return DryRunResult(
                    status=DryRunStatus.ERROR,
                    description="Interactive command is not supported.",
                    result=[],
                )
            else:
                return DryRunResult(
                    status=DryRunStatus.ERROR,
                    description=f"Dry-run failed. Potentially it's an invalid command. stderr: {parse_text(dry_run_result.stderr, 200)}",
                    result=[],
                )
