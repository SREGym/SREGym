"""
### Pipe to a log file
PYTHONUNBUFFERED=1 uv run tests/alerts/check_alerts.py --include-pending 2>&1 | tee tmp.log
"""

import argparse
import subprocess
import sys
import time

import requests

PROMETHEUS_NODE_PORT = 32000


def detect_prometheus_url() -> str:
    """Auto-detect Prometheus"""
    try:
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"http://{result.stdout.strip()}:{PROMETHEUS_NODE_PORT}"
    except Exception:
        pass
    return f"http://localhost:{PROMETHEUS_NODE_PORT}"


def get_alerts(prometheus_url: str, include_pending: bool) -> list[dict]:
    states = {"firing", "pending"} if include_pending else {"firing"}
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/alerts", timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        return []
    alerts = resp.json().get("data", {}).get("alerts", [])
    return [a for a in alerts if a.get("state") in states]


def print_alerts(alerts: list[dict], turn: int) -> None:
    print(f"\n{'=' * 60}")
    print(f" SREGym Alerts | Turn: {turn}")
    print(f"{'=' * 60}")

    if not alerts:
        print("  No alerts")
    else:
        for a in alerts:
            labels = a.get("labels", {})
            annotations = a.get("annotations", {})
            state = a.get("state", "unknown")
            ns = labels.get("namespace", "?")
            print(f"\n  [{ns}] {labels.get('alertname', '?')} [{state}]")
            if labels.get("pod"):
                print(f"    Pod:       {labels['pod']}")
            if labels.get("container"):
                print(f"    Container: {labels['container']}")
            desc = annotations.get("description", annotations.get("summary", ""))
            if desc:
                print(f"    Detail:    {desc}")

    print(f"\n{'=' * 60}")
    print(f" Total: {len(alerts)} alert(s)")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(description="Watch SREGym Prometheus alerts")
    parser.add_argument(
        "--include-pending", action="store_true", help="Show pending alerts (still in 'for' waiting period)"
    )
    args = parser.parse_args()

    url = detect_prometheus_url()
    mode = "firing + pending" if args.include_pending else "firing only"
    print(f"Watching SREGym alerts via {url} ({mode})")

    turn = 0
    while True:
        turn += 1
        alerts = get_alerts(url, args.include_pending)
        print_alerts(alerts, turn)
        time.sleep(15)


if __name__ == "__main__":
    main()
