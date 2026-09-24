"""Interface to K8S controller service."""

import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import time
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
    def exec_command(command: str, input_data=None, timeout: float | None = None):
        """Execute an arbitrary kubectl command with timeout protection."""
        if input_data is not None:
            input_data = input_data.encode("utf-8")
        timeout = timeout if timeout is not None else _kubectl_timeout_seconds()
        started = time.monotonic()
        try:
            logger.info("kubectl exec start timeout=%ss command=%r", timeout, command)
            out = _run_in_process_group(
                command,
                check=True,
                capture_output=True,
                input=input_data,
                timeout=timeout,
            )
            out.stdout = _decode_output(out.stdout)
            out.stderr = _decode_output(out.stderr)
            logger.info("kubectl exec done elapsed=%.2fs command=%r", time.monotonic() - started, command)
            return out
        except subprocess.CalledProcessError as e:
            e.stdout = _decode_output(e.stdout)
            e.stderr = _decode_output(e.stderr)
            logger.warning(
                "kubectl exec failed returncode=%s elapsed=%.2fs command=%r stderr=%s",
                e.returncode,
                time.monotonic() - started,
                command,
                e.stderr,
            )
            return e
        except subprocess.TimeoutExpired as e:
            stdout = _decode_output(e.stdout)
            stderr = _decode_output(e.stderr)
            message = f"kubectl command timed out after {timeout:.0f}s: {command}"
            logger.warning("%s elapsed=%.2fs", message, time.monotonic() - started)
            return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=(stderr + "\n" + message).strip())

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
    def extract_namespace_from_command(command: str) -> str | None:
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
    def insert_flags(command: str, flags: str | list[str]) -> str:
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
        timeout = _kubectl_dry_run_timeout_seconds()
        try:
            dry_run_result = _run_in_process_group(
                dry_run_command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return DryRunResult(
                status=DryRunStatus.ERROR,
                description=f"Dry-run timed out after {timeout:.0f}s.",
                result=[],
            )

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


def _kubectl_timeout_seconds() -> float:
    return float(os.getenv("SREGYM_KUBECTL_CMD_TIMEOUT_SECONDS", "300"))


def _kubectl_dry_run_timeout_seconds() -> float:
    return float(os.getenv("SREGYM_KUBECTL_DRY_RUN_TIMEOUT_SECONDS", "30"))


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_in_process_group(command, *, timeout, input=None, check=False, **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command in a new session so timeout kills the whole tree, not just /bin/sh.

    Mirrors ``subprocess.run``: raises ``TimeoutExpired`` on timeout and ``CalledProcessError``
    when ``check`` is set and the command fails.
    """
    if input is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    with subprocess.Popen(
        command,
        shell=True,
        start_new_session=True,
        **kwargs,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            stdout, stderr = _reap_after_kill(proc)
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            _kill_process_group(proc)
            _reap_after_kill(proc)
            raise
        retcode = proc.wait()
        if check and retcode:
            raise subprocess.CalledProcessError(retcode, command, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, retcode, stdout, stderr)


def _reap_after_kill(proc: subprocess.Popen, timeout: float = 10.0) -> tuple:
    """Bounded drain/reap so a process that escaped the group can't re-hang the cleanup."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("kubectl reap timed out after %.0fs; abandoning pipe drain", timeout)
        with contextlib.suppress(Exception):
            proc.wait(timeout=timeout)
        return None, None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group led by ``proc``."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Can't signal the group; fall back to the child alone.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
