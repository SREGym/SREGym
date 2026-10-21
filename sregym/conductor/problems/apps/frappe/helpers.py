"""Frappe: helpers."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

APP = "frappe"


GUNICORN = "erp-gunicorn"


WEB_SERVICE = "svc-frappe-web"


def selector_of(problem, deployment: str) -> str:
    """Label selector string built from a Deployment's ``spec.selector.matchLabels``."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def rollout_status(problem, deployment: str, timeout_s: int = 600, check: bool = True) -> str:
    command = f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s"
    if check:
        return problem.kubectl.exec_command_checked(command, timeout=timeout_s + 30)
    return problem.kubectl.exec_command(command)


def replace_pods_with_patch(problem, deployment: str, patch: dict, wait_ready: bool = False) -> None:
    """Apply a pod-template ``patch`` so that no pod of the old template keeps serving.

    A rolling update of a Deployment whose new pods never become Ready leaves
    the old pods serving (default surge), which hides the fault (H3). Scaling
    to zero first, patching, then scaling back makes the new template the only
    one running, as the originals' delete+apply injectors did.
    """
    ns = problem.namespace
    dep = problem.kubectl.get_deployment(deployment, ns)
    replicas = dep.spec.replicas if dep.spec.replicas is not None else 1
    selector = selector_of(problem, deployment)
    problem.kubectl.exec_command_checked(f"kubectl scale deployment/{deployment} -n {ns} --replicas=0")
    problem.kubectl.exec_command(f"kubectl wait pod -n {ns} -l {selector} --for=delete --timeout=180s")
    try:
        problem.kubectl.exec_command_checked(
            f"kubectl patch deployment {deployment} -n {ns} --type=strategic -p {shlex.quote(json.dumps(patch))}"
        )
    finally:
        problem.kubectl.exec_command_checked(f"kubectl scale deployment/{deployment} -n {ns} --replicas={replicas}")
    if wait_ready:
        rollout_status(problem, deployment)


SITE_DB_PROBE = """
frappe.db.sql("SELECT COUNT(*) FROM `tabDocType`")
frappe.db.sql("INSERT INTO `tabToDo` (name) SELECT name FROM `tabToDo` WHERE 1=0")
frappe.db.sql("UPDATE `tabToDo` SET modified = modified WHERE 1=0")
frappe.db.sql("DELETE FROM `tabToDo` WHERE 1=0")
frappe.db.rollback()
print("SITE_DB_OK")
"""


def site_db_probe(problem, oracle) -> dict | None:
    """Connect to MariaDB exactly as the site does (site_config credentials, host,
    transport) from the gunicorn pod and exercise SELECT/INSERT/UPDATE/DELETE
    (no-op statements, rolled back)."""
    try:
        out = problem.app.site_python(SITE_DB_PROBE, timeout=180)
    except Exception as exc:
        error = "\n".join(line for line in str(exc).splitlines() if "RuntimeWarning" not in line)[-600:]
        print(f"❌ The site cannot use its database: {error}")
        return oracle.fail("site_database_unusable", error=error)
    if "SITE_DB_OK" not in out:
        print(f"❌ Unexpected site probe output: {out[-300:]!r}")
        return oracle.fail("site_database_unusable", output=out[-300:])
    print("✅ The site connects to MariaDB with its own account and can read and write")
    return None


def site_config(problem) -> dict:
    path = f"/home/frappe/frappe-bench/sites/{problem.app.SITE_NAME}/site_config.json"
    return json.loads(problem.app.exec_in(f"deploy/{GUNICORN}", f"cat {path}"))


def delete_pods(problem, deployment: str) -> list[str]:
    selector = selector_of(problem, deployment)
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(problem.namespace, label_selector=selector).items
    for pod in pods:
        problem.kubectl.core_v1_api.delete_namespaced_pod(pod.metadata.name, problem.namespace)
    return [pod.metadata.name for pod in pods]


def apply_manifest(problem, manifest: dict) -> None:
    problem.kubectl.exec_command_checked("kubectl apply -f -", input_data=json.dumps(manifest), timeout=60)


def state_file(problem, name: str) -> Path:
    return Path(f"/tmp/sregym-{problem.namespace}-{name}.json")
