"""Convert a problem spec dict into a Python CassandraBugProblem subclass file.

Usage:
    from sregym.conductor.problems.generators.spec_to_code import generate_problem_file
    code = generate_problem_file(spec)
    Path("cassandra_20108.py").write_text(code)
"""

from __future__ import annotations

import textwrap


def generate_problem_file(spec: dict) -> str:
    """Return Python source for a CassandraBugProblem subclass derived from spec."""
    system = spec.get("system", "cassandra")
    if system == "cassandra":
        return _generate_cassandra(spec)
    raise NotImplementedError(f"Code generation for system '{system}' not yet implemented")


def _generate_cassandra(spec: dict) -> str:
    class_name = spec["python_class_name"]
    version = spec["version"]
    git_ref = spec["git_ref"]
    root_cause_file = spec["root_cause_file"]
    root_cause_description = spec["root_cause_description"]
    expected_exception = spec.get("expected_exception", "Exception")
    trigger_cql = spec["trigger_cql"]
    needs_background_loop = spec.get("needs_background_loop", False)
    background_select = spec.get("background_select")
    docstring = spec.get("docstring", "")
    source_url = spec.get("source_url", "")
    jira_id = spec.get("jira_id", "")

    # Normalise trigger CQL indentation for embedding in a Python string
    trigger_cql_indented = textwrap.indent(trigger_cql.strip(), "        ")

    module_header = f'''\
"""{docstring}

JIRA: {source_url}
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)
'''

    trigger_block = f'''\
_TRIGGER_CQL = """
{trigger_cql_indented}
"""
'''

    if needs_background_loop and background_select:
        failing_select_block = f'_FAILING_SELECT = "{background_select}"\n'
    else:
        failing_select_block = ""

    class_body = f'''\
class {class_name}(CassandraBugProblem):
    cassandra_version = "{version}"
    source_git_ref = "{git_ref}"

    root_cause_file = "{root_cause_file}"
    root_cause_description = (
        "{root_cause_description}"
    )

    trigger_cql = _TRIGGER_CQL
'''

    if needs_background_loop and background_select:
        class_body += _inject_fault_with_loop(class_name, expected_exception)
        class_body += _background_workload_method(class_name)
        class_body += _recover_fault_method(class_name)
    else:
        class_body += _inject_fault_simple(class_name, expected_exception)
        class_body += _recover_fault_simple(class_name)

    return "\n".join([module_header, trigger_block, failing_select_block, class_body])


# ---------------------------------------------------------------------------
# Method templates
# ---------------------------------------------------------------------------

def _inject_fault_with_loop(class_name: str, expected_exception: str) -> str:
    return f'''
    @mark_fault_injected
    def inject_fault(self):
        """Set up the data state then start a background loop that keeps firing
        the failing query so {expected_exception} appears continuously in logs.
        """
        logger.info("[{class_name}] Running setup CQL")
        try:
            self.app.run_cql(self.trigger_cql)
        except Exception as e:
            logger.info(f"[{class_name}] Setup CQL error (may be expected): {{e}}")

        logger.info("[{class_name}] Firing initial failing query")
        try:
            self.app.run_cql(_FAILING_SELECT)
        except Exception as e:
            logger.info(f"[{class_name}] Expected {expected_exception}: {{e}}")

        logger.info("[{class_name}] Starting background query loop")
        self._start_background_workload()
'''


def _background_workload_method(class_name: str) -> str:
    return f'''
    def _start_background_workload(self):
        """Fire the failing SELECT every 15 s so {class_name} keeps appearing in logs."""
        pod = subprocess.run(
            f"kubectl get pods -n {{self.namespace}} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{{{.items[0].metadata.name}}}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            logger.warning("[{class_name}] No Cassandra pod found — skipping background workload")
            return

        username, password = self.app._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        cmd = (
            f"kubectl exec -n {{self.namespace}} {{pod}} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {{u_b64}} | base64 -d); P=$(echo {{p_b64}} | base64 -d); "
            f"while true; do "
            f"cqlsh -u \\"$U\\" -p \\"$P\\" -e \\"{{_FAILING_SELECT}}\\" 2>&1; "
            f"sleep 15; "
            f"done'"
        )
        self._workload_proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[{class_name}] Background workload started on pod {{pod}}")
'''


def _recover_fault_method(class_name: str) -> str:
    return f'''
    @mark_fault_injected
    def recover_fault(self):
        """Stop the background query loop."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[{class_name}] Background workload stopped")
'''


def _inject_fault_simple(class_name: str, expected_exception: str) -> str:
    return f'''
    @mark_fault_injected
    def inject_fault(self):
        """Trigger the bug via CQL — {expected_exception} will appear in Cassandra logs."""
        logger.info("[{class_name}] Running trigger CQL")
        try:
            result = self.app.run_cql(self.trigger_cql)
            logger.info(f"[{class_name}] Trigger CQL completed: {{result!r}}")
        except Exception as e:
            logger.info(f"[{class_name}] Expected {expected_exception}: {{e}}")
'''


def _recover_fault_simple(class_name: str) -> str:
    return f'''
    @mark_fault_injected
    def recover_fault(self):
        """No runtime state to clean up — fault is in source code."""
        logger.info("[{class_name}] No fault recovery needed (source-code bug)")
'''
