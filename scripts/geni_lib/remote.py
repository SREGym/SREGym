import os
import time
from pathlib import Path

import paramiko
from paramiko.ssh_exception import PasswordRequiredException, SSHException


class RemoteExecutor:
    """Thin SSH helper around paramiko suitable for non-interactive commands."""

    def __init__(self, host: str, user: str, key_path: str | None = None):
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        keyfile: str | None = None
        if key_path:
            keyfile = os.path.expanduser(os.path.expandvars(key_path))
            if not Path(keyfile).is_file():
                print(f"Warning: Key file '{keyfile}' not found")
                keyfile = None
            else:
                print(f"Using SSH key: {keyfile}")

        # Try multiple times to connect
        max_retries = 5
        retry_delay = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                self.client.connect(
                    hostname=host,
                    username=user,
                    key_filename=keyfile,
                    look_for_keys=(keyfile is None),
                    allow_agent=(keyfile is None),
                    timeout=30,
                )
                return  # Successfully connected
            except PasswordRequiredException:
                try:
                    self.client.connect(
                        hostname=host,
                        username=user,
                        look_for_keys=True,
                        allow_agent=True,
                        timeout=30,
                    )
                    return  # Successfully connected
                except Exception as e:
                    last_error = e
            except (SSHException, Exception) as e:
                last_error = e
                if attempt < max_retries - 1:
                    print(f"Connection attempt {attempt + 1} failed, retrying in 5 seconds...")
                    time.sleep(retry_delay)
                continue

        # If we get here, all retries failed
        print(f"SSH connection error to {host}: {last_error}")
        raise last_error

    def exec(self, cmd: str, timeout: int | None = None) -> tuple[int, str, str]:
        """Execute a command with optional timeout"""
        try:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
            rc = stdout.channel.recv_exit_status()
            return rc, stdout.read().decode(), stderr.read().decode()
        except Exception as e:
            print(f"Error executing command on {self.host}: {e}")
            return 1, "", str(e)

    def close(self) -> None:
        self.client.close()
