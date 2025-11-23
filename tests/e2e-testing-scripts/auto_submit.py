import subprocess
import threading
from time import sleep


def automatic_submit():
    ctr = 0
    while ctr < 10000:
        subprocess.run(
            [
                "bash",
                "-c",
                'curl -v http://localhost:8000/submit -H "Content-Type: application/json" -d \'{"solution":"yes"}\'',
            ],
            stdin=subprocess.DEVNULL,
        )
        sleep(30)
        ctr += 1


if __name__ == "__main__":
    thread = threading.Thread(target=automatic_submit)
    thread.start()
