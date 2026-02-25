import os
import subprocess
from time import sleep

api_hostname = os.getenv("API_HOSTNAME", "localhost")
api_port = os.getenv("API_PORT", "8000")
server_url = f"http://{api_hostname}:{api_port}"


def automatic_submit():
    ctr = 0
    while ctr < 10000:
        subprocess.run(
            [
                "curl",
                "-X",
                "POST",
                f"{server_url}/submit",
                "-H",
                "Content-Type: application/json",
                "-d",
                '{"solution":"yes"}',
            ],
            capture_output=True,
            text=True,
        )
        sleep(60)
        ctr += 1


if __name__ == "__main__":
    automatic_submit()
