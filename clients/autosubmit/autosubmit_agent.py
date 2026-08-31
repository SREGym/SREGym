import os
from time import sleep

import requests

api_hostname = os.getenv("API_HOSTNAME", "localhost")
api_port = os.getenv("API_PORT", "8000")
server_url = f"http://{api_hostname}:{api_port}"


def automatic_submit():
    ctr = 0
    while ctr < 10000:
        try:
            stage_response = requests.get(f"{server_url}/status", timeout=10)
            stage_response.raise_for_status()
            stage = stage_response.json().get("stage")
            if stage == "done":
                return
            if stage in {"diagnosis", "mitigation"}:
                response = requests.post(
                    f"{server_url}/submit",
                    json={"stage": stage, "solution": "yes" if stage == "diagnosis" else ""},
                    timeout=310,
                )
                response.raise_for_status()
        except requests.RequestException:
            pass
        sleep(60)
        ctr += 1


if __name__ == "__main__":
    automatic_submit()
