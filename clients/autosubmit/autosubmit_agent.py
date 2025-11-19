import subprocess
import sys
import threading
from time import sleep

server_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def automatic_submit():
    ctr = 0
    while ctr < 10000:
        subprocess.run(
            [
                "bash",
                "-c",
                f"curl -v {server_url}/submit",
                '-H "Content-Type: application/json" -d \'{"solution":"yes"}\'',
            ],
            stdin=subprocess.DEVNULL,
        )
        sleep(60)
        ctr += 1


if __name__ == "__main__":
    automatic_submit()
