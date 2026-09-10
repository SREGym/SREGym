"""Host-only tests: no project dependencies or Docker daemon required."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("dind_run", ROOT / "docker/dind/run.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_parallel_runs_have_separate_names_and_storage(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "REPO", Path(directory)):
            args = launcher.parser().parse_args(["run"])
            first = launcher.run_command(args)
            second = launcher.run_command(args)
            self.assertNotEqual(first[first.index("--name") + 1], second[second.index("--name") + 1])
            self.assertNotEqual(first[first.index("--mount") + 1], second[second.index("--mount") + 1])
            self.assertNotIn("--network", first)
            self.assertNotIn("-p", first)
            self.assertNotIn("docker.sock", " ".join(first))
            self.assertNotIn("/var/lib/docker", " ".join(first))

    def test_command_arguments_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = ["bash", "-c", "echo 'spaces; $literal'"]
            args = launcher.parser().parse_args(["run", "--output", directory, "--", *payload])
            command = launcher.run_command(args)
            self.assertEqual(command[-3:], payload)

    def test_optional_memory_backed_docker_data_keeps_results_on_host(self):
        with tempfile.TemporaryDirectory() as directory:
            args = launcher.parser().parse_args(
                ["run", "--output", directory, "--docker-tmpfs-size", "20g", "--memory", "28g"]
            )
            command = launcher.run_command(args)
            self.assertEqual(command[command.index("--tmpfs") + 1], "/run/sregym-docker-data:rw,size=20g")
            self.assertIn("SREGYM_DOCKER_TMPFS_SIZE=20g", command)
            self.assertEqual(command[command.index("--memory") + 1], "28g")
            self.assertIn("dst=/opt/sregym/results", command[command.index("--mount") + 1])

    def test_credentials_forwarded_by_name_only(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "UNRELATED_SECRET": "not-forwarded"}, clear=True),
        ):
            args = launcher.parser().parse_args(["run", "--output", directory])
            command = launcher.run_command(args)
            self.assertIn("OPENAI_API_KEY", command)
            self.assertNotIn("test-secret", " ".join(command))
            self.assertNotIn("UNRELATED_SECRET", command)

    def test_exit_code_is_propagated(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(sys, "argv", ["run.py", "run", "--output", directory, "--", "false"]),
            patch.object(launcher.subprocess, "call", return_value=17),
        ):
            self.assertEqual(launcher.main(), 17)

    def test_missing_docker_has_actionable_error(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(sys, "argv", ["run.py", "run", "--output", directory]),
            patch.object(launcher.subprocess, "call", side_effect=FileNotFoundError),
            self.assertRaisesRegex(SystemExit, "Docker is required"),
        ):
            launcher.main()

    def test_shell_syntax(self):
        for name in ("entrypoint.sh", "prepare-cgroups.sh", "smoke.sh"):
            subprocess.run(["bash", "-n", str(ROOT / "docker/dind" / name)], check=True)

    def test_entrypoint_rejects_remote_daemon_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_id = Path(directory) / "id"
            fake_id.write_text("#!/bin/sh\necho 0\n")
            fake_id.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "docker/dind/entrypoint.sh"), "true"],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "DOCKER_HOST": "tcp://example:2375"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("private local Docker daemon", result.stderr)

    def test_entrypoint_rejects_nonroot_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_id = Path(directory) / "id"
            fake_id.write_text("#!/bin/sh\necho 1000\n")
            fake_id.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "docker/dind/entrypoint.sh"), "true"],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("must run as root", result.stderr)


if __name__ == "__main__":
    unittest.main()
