import logging
import os

from dotenv import load_dotenv

from provisioner.utils.logger import logger
from provisioner.utils.ssh import SSHManager, SSHUtilError

load_dotenv(override=True)

# Put actual hostname here
TEST_HOSTNAME = "YOUR_TEST_HOSTNAME_OR_IP"

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger.info("SSH Utils direct execution test started.")

    test_hostname = TEST_HOSTNAME
    test_username = "sregym"
    test_private_key_path = os.getenv("PROVISIONER_SSH_PRIVATE_KEY_PATH")

    if test_hostname == "YOUR_TEST_HOSTNAME_OR_IP":
        logger.warning("Skipping SSH tests: Please update test_hostname and credentials in ssh_utils.py")
    else:
        try:
            # Test execute_ssh_command
            logger.info("\n--- Testing execute_ssh_command ---")
            ssh_manager = SSHManager(test_hostname, test_username, test_private_key_path)
            stdout, stderr, exit_code = ssh_manager.execute_ssh_command(
                "echo 'Hello from Paramiko!' && ls -la / && date"
            )
            logger.info(f"Execute Command Result:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}\nEXIT_CODE: {exit_code}")
            assert exit_code == 0

            # Test file upload
            logger.info("\n--- Testing upload_file_scp ---")
            local_test_file = "test_upload.txt"
            remote_test_file = f"/tmp/{local_test_file}"
            with open(local_test_file, "w") as f:
                f.write("This is a test file for SCP upload.\n")

            ssh_manager.upload_file_scp(local_test_file, remote_test_file)
            # Verify upload by trying to cat it
            stdout_cat, _, exit_code_cat = ssh_manager.execute_ssh_command(f"cat {remote_test_file}")
            assert exit_code_cat == 0
            assert "This is a test file for SCP upload." in stdout_cat
            logger.info(f"Verified uploaded file content: {stdout_cat.strip()}")

            # Test file download
            logger.info("\n--- Testing download_file_scp ---")
            local_downloaded_file = "test_downloaded.txt"
            if os.path.exists(local_downloaded_file):
                os.remove(local_downloaded_file)

            ssh_manager.download_file_scp(remote_test_file, local_downloaded_file)
            assert os.path.exists(local_downloaded_file)
            with open(local_downloaded_file, "r") as f:
                content = f.read()
                assert "This is a test file for SCP upload." in content
            logger.info(f"Verified downloaded file content: {content.strip()}")

            # Clean up remote test file
            ssh_manager.execute_ssh_command(f"rm {remote_test_file}")
            # Clean up local test files
            if os.path.exists(local_test_file):
                os.remove(local_test_file)
            if os.path.exists(local_downloaded_file):
                os.remove(local_downloaded_file)

            logger.info("\nSSH Utils tests completed successfully.")

        except SSHUtilError as e:
            logger.error(f"SSH Util Test FAILED: {e}", exc_info=True)
        except FileNotFoundError as e:
            logger.error(f"SSH Util Test FAILED (FileNotFound): {e}", exc_info=True)
        except AssertionError as e:
            logger.error(f"SSH Util Test FAILED (AssertionError): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"An unexpected error occurred during SSH Util tests: {e}", exc_info=True)
