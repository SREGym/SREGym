import paramiko


class RemoteExecutor:
    """Thin SSH helper around paramiko suitable for non-interactive commands."""

    def __init__(self, host: str, user: str, key_path: str):
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host,
            username=user,
            key_filename=key_path,
            look_for_keys=False,
            allow_agent=False,
        )

    def exec(self, cmd: str) -> tuple[int, str, str]:
        """
        Execute *cmd* and return (exit_code, stdout, stderr).
        All outputs are decoded as UTF-8.
        """
        stdin, stdout, stderr = self.client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()

    def close(self) -> None:
        self.client.close()
