import paramiko

class RemoteExecutor:
    def __init__(self, host, user, key_path):
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(hostname=host, username=user, key_filename=key_path)

    def exec(self, cmd):
        stdin, stdout, stderr = self.client.exec_command(cmd)
        code = stdout.channel.recv_exit_status()
        return code, stdout.read().decode(), stderr.read().decode()

    def close(self):
        self.client.close()
