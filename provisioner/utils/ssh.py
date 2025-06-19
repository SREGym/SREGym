import os
import time
from typing import Optional, Tuple

import paramiko

from provisioner.config.settings import DefaultSettings
from provisioner.utils.logger import logger


class SSHUtilError(Exception):
    """Custom exception for SSH utility errors."""

    pass


class SSHManager:
    def __init__(
        self,
        hostname: str,
        username: str,
        private_key_path: Optional[str] = None,
        port: int = 22,
        timeout: int = DefaultSettings.DEFAULT_SSH_TIME_OUT_SECONDS,
        max_retries: int = 10,
        retry_delay: int = 2,  # seconds
    ):
        self.hostname = hostname
        self.username = username
        self.private_key_path = private_key_path
        self.port = port
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _create_ssh_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        for attempt in range(1, self.max_retries + 1):
            try:
                if self.private_key_path:
                    self.private_key_path = os.path.expanduser(self.private_key_path)
                    if not os.path.exists(self.private_key_path):
                        raise SSHUtilError(f"Private key file not found: {self.private_key_path}")
                    try:
                        # Try ED25519 first
                        key = paramiko.Ed25519Key.from_private_key_file(self.private_key_path)
                    except paramiko.SSHException:
                        try:
                            # Fall back to RSA
                            key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
                        except paramiko.SSHException as e:
                            raise SSHUtilError(f"Failed to load private key: {e}")
                    logger.debug(
                        f"Attempting SSH connection to {self.username}@{self.hostname}:{self.port} using private key {self.private_key_path} (attempt {attempt}/{self.max_retries})"
                    )
                    client.connect(
                        self.hostname, port=self.port, username=self.username, pkey=key, timeout=self.timeout
                    )
                else:
                    raise SSHUtilError("SSH connection requires either a private key.")
                logger.info(f"Successfully connected to {self.username}@{self.hostname}:{self.port}")
                return client
            except (paramiko.AuthenticationException, paramiko.SSHException, Exception) as e:
                if attempt < self.max_retries:
                    logger.warning(
                        f"SSH connection attempt {attempt} failed: {e}. Retrying in {self.retry_delay} seconds..."
                    )
                    time.sleep(self.retry_delay)
                else:
                    msg = f"SSH connection failed after {self.max_retries} attempts for {self.username}@{self.hostname}: {e}"
                    logger.error(msg, exc_info=True)
                    if isinstance(e, paramiko.AuthenticationException):
                        raise SSHUtilError(msg) from e
                    elif isinstance(e, paramiko.SSHException):
                        raise SSHUtilError(msg) from e
                    else:
                        raise SSHUtilError(msg) from e

    def execute_ssh_command(
        self,
        command: str,
    ) -> Tuple[str, str, int]:
        client = None
        try:
            client = self._create_ssh_client()
            logger.info(f"Executing command on {self.hostname}: {command}")
            stdin, stdout, stderr = client.exec_command(command, timeout=self.timeout)

            # It's important to read stdout and stderr before checking exit_status
            stdout_output = stdout.read().decode("utf-8", errors="replace").strip()
            stderr_output = stderr.read().decode("utf-8", errors="replace").strip()

            # exit_status_ready() can be used to check if status is available without blocking
            # recv_exit_status() will block until the command finishes.
            exit_code = stdout.channel.recv_exit_status()

            if stdout_output:
                logger.debug(f"Command stdout on {self.hostname}: {stdout_output}")
            if stderr_output:
                logger.warning(f"Command stderr on {self.hostname}: {stderr_output}")
            logger.info(f"Command on {self.hostname} finished with exit code: {exit_code}")

            return stdout_output, stderr_output, exit_code
        except SSHUtilError:
            logger.error(f"Error creating SSH client for {self.hostname}:{self.username}:{self.port}")
            raise
        except paramiko.SSHException as e:
            msg = f"Error executing command '{command}' on {self.hostname}: {e}"
            logger.error(msg)
            raise SSHUtilError(msg) from e
        except Exception as e:
            msg = f"An unexpected error occurred while executing command on {self.hostname}: {e}"
            logger.error(msg, exc_info=True)
            raise SSHUtilError(msg) from e
        finally:
            if client:
                client.close()
                logger.debug(f"SSH connection to {self.hostname} closed.")

    def upload_file_scp(
        self,
        local_path: str,
        remote_path: str,
    ):
        local_path = os.path.expanduser(local_path)
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        client = None
        sftp = None
        try:
            client = self._create_ssh_client()
            sftp = client.open_sftp()
            logger.info(f"Uploading {local_path} to {self.username}@{self.hostname}:{remote_path}")
            sftp.put(local_path, remote_path)
            logger.info(f"Successfully uploaded {local_path} to {remote_path} on {self.hostname}")
        except SSHUtilError:
            msg = f"Error creating SSH client for {self.hostname}:{self.username}:{self.port}"
            logger.error(msg)
            raise SSHUtilError(msg)
        except Exception as e:
            msg = f"Error uploading file {local_path} to {self.hostname}:{remote_path}: {e}"
            logger.error(msg, exc_info=True)
            raise SSHUtilError(msg) from e
        finally:
            if sftp:
                sftp.close()
            if client:
                client.close()
                logger.debug(f"SSH connection to {self.hostname} closed (after upload).")

    def download_file_scp(
        self,
        remote_path: str,
        local_path: str,
    ):
        local_path = os.path.expanduser(local_path)
        client = None
        sftp = None
        try:
            client = self._create_ssh_client()
            sftp = client.open_sftp()
            logger.info(f"Downloading {self.username}@{self.hostname}:{remote_path} to {local_path}")
            sftp.get(remote_path, local_path)
            logger.info(f"Successfully downloaded {remote_path} from {self.hostname} to {local_path}")
        except SSHUtilError:
            msg = f"Error creating SSH client for {self.hostname}:{self.username}:{self.port}"
            logger.error(msg)
            raise SSHUtilError(msg)
        except Exception as e:
            msg = f"Error downloading file {remote_path} from {self.hostname} to {local_path}: {e}"
            logger.error(msg, exc_info=True)
            raise SSHUtilError(msg) from e
        finally:
            if sftp:
                sftp.close()
            if client:
                client.close()
                logger.debug(f"SSH connection to {self.hostname} closed (after download).")
