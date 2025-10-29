"""Interface to run shell commands in the service cluster."""

import subprocess


class Shell:
    """Interface to run shell commands. Currently used for development/debugging with cli.py"""

    @staticmethod
    def exec(command: str, input_data=None, cwd=None):
        """Execute a shell command on localhost."""
        if input_data is not None:
            input_data = input_data.encode("utf-8")

        try:
            out = subprocess.run(
                command,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                cwd=cwd,
            )

            if out.stderr or out.returncode != 0:
                error_message = out.stderr.decode("utf-8")
                print(f"[ERROR] Command execution failed: {error_message}")
                return error_message
            else:
                output_message = out.stdout.decode("utf-8")
                return output_message

        except Exception as e:
            raise RuntimeError(f"Failed to execute command: {command}\nError: {str(e)}")
