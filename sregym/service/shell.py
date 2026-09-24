"""Interface to run shell commands in the service cluster."""

import atexit
import os
import shlex
import subprocess
import tempfile


def _make_state_file() -> str:
    """Create a private, per-process state file and schedule its removal at exit."""
    fd, path = tempfile.mkstemp(prefix="_", suffix=".sh")
    os.close(fd)  # we only need the path; the wrapped command writes to it
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    return path


class Shell:
    """Interface to run shell commands. Currently used for development/debugging with cli.py"""

    # One private snapshot file per CLI process, reused across exec() calls.
    _state_file = _make_state_file()

    @classmethod
    def exec(cls, command: str, input_data=None, cwd=None):
        """Execute a shell command on localhost, preserving exported vars / functions / cwd."""
        if input_data is not None:
            input_data = input_data.encode("utf-8")

        state = shlex.quote(cls._state_file)
        # resolves cwd to absolute path instead of relative.
        dir_cmd = f"cd {shlex.quote(os.path.abspath(cwd))} 2>/dev/null; " if cwd else ""
        wrapped = (
            f"[ -f {state} ] && source {state} 2>/dev/null; "
            f"{dir_cmd}"
            f"{command}\n"
            f"__sregym_rc=$?; umask 077; "
            f'{{ declare -px; declare -pf; printf "cd %q\\n" "$PWD"; }} > {state} 2>/dev/null; '
            f"exit $__sregym_rc"
        )

        try:
            out = subprocess.run(
                wrapped,
                input=input_data,
                capture_output=True,
                shell=True,
                executable="/bin/bash",
            )

            stdout = out.stdout.decode("utf-8")
            stderr = out.stderr.decode("utf-8")
            combined = stdout + stderr

            if out.returncode != 0:
                # Surface failures (keyed on the real exit code, not merely the presence of
                # stderr — many tools write warnings to stderr while succeeding).
                print(f"[ERROR] Command execution failed (exit {out.returncode})")
                return combined if combined.strip() else f"[exit {out.returncode}]"
            return combined

        except Exception as e:
            raise RuntimeError(f"Failed to execute command: {command}\nError: {str(e)}") from e
