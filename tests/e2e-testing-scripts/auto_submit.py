import threading
from time import sleep

import requests

SERVER_URL = "http://localhost:8000"


def automatic_submit():
    ctr = 0
    while ctr < 10000:
        try:
            status = requests.get(f"{SERVER_URL}/status", timeout=10).json().get("stage")
            if status == "done":
                return
            if status in {"diagnosis", "mitigation"}:
                response = requests.post(
                    f"{SERVER_URL}/submit",
                    json={"stage": status, "solution": "yes" if status == "diagnosis" else ""},
                    timeout=310,
                )
                response.raise_for_status()
        except requests.RequestException:
            pass
        sleep(30)
        ctr += 1


if __name__ == "__main__":
    thread = threading.Thread(target=automatic_submit)
    thread.start()
