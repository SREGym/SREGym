"""CLI for generating problem files from JIRA/GitHub issue URLs.

Usage:
    # Print generated Python file to stdout
    python -m sregym.conductor.problems.generators.cli \\
        https://issues.apache.org/jira/browse/CASSANDRA-20108

    # Write the file directly into the problems directory
    python -m sregym.conductor.problems.generators.cli \\
        https://issues.apache.org/jira/browse/CASSANDRA-20108 --write

    # Also print the intermediate spec YAML
    python -m sregym.conductor.problems.generators.cli \\
        https://issues.apache.org/jira/browse/CASSANDRA-20108 --write --show-spec

    # Override system if not inferable from the URL
    python -m sregym.conductor.problems.generators.cli \\
        https://github.com/pingcap/tidb/issues/12345 --system tidb --write

Environment variables:
    ANTHROPIC_API_KEY   Required — Claude API key for spec extraction
    GITHUB_TOKEN        Optional — avoids GitHub rate limits for private/high-traffic repos
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

import yaml

PROBLEMS_DIR = Path(__file__).parent.parent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a SREGym problem file from a JIRA or GitHub issue URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="JIRA or GitHub issue URL")
    parser.add_argument(
        "--system",
        choices=["cassandra", "tidb"],
        default=None,
        help="Override system name (inferred from URL by default)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"Write generated file to {PROBLEMS_DIR}/<module_filename>.py",
    )
    parser.add_argument(
        "--show-spec",
        action="store_true",
        help="Print the intermediate YAML spec before the generated code",
    )
    args = parser.parse_args(argv)
    from sregym.conductor.problems.generators.issue_parser import parse_issue

    print(f"Fetching issue: {args.url}", file=sys.stderr)
    spec = parse_issue(args.url, system=args.system)

    if args.show_spec:
        print("--- spec ---")
        print(yaml.dump(spec, default_flow_style=False, allow_unicode=True))
        print("--- end spec ---\n")

    print(f"Generating problem class: {spec['python_class_name']}", file=sys.stderr)
    code = _generate_problem_file(spec)

    if args.write:
        out_path = PROBLEMS_DIR / f"{spec['module_filename']}.py"
        if out_path.exists():
            print(
                f"WARNING: {out_path} already exists - skipping write. "
                "Delete it first or rename module_filename in the spec.",
                file=sys.stderr,
            )
        else:
            out_path.write_text(code)
            print(f"Wrote: {out_path}", file=sys.stderr)
            _print_registry_snippet(spec)
    else:
        print(code)


def _print_registry_snippet(spec: dict) -> None:
    """Print the registry.py line to add manually."""
    class_name = spec["python_class_name"]
    module = spec["module_filename"]
    key = spec["registry_key"]
    print(
        f"\nAdd to registry.py:\n"
        f"  from sregym.conductor.problems.{module} import {class_name}\n"
        f'  "{key}": {class_name},',
        file=sys.stderr,
    )


def _generate_problem_file(spec: dict) -> str:
    """Return Python source for a CassandraBugProblem subclass derived from spec."""
    system = spec.get("system", "cassandra")
    if system != "cassandra":
        raise NotImplementedError(f"Code generation for system '{system}' not yet implemented")

    class_name = spec["python_class_name"]
    expected_exception = spec.get("expected_exception", "Exception")
    trigger_cql_indented = textwrap.indent(spec["trigger_cql"].strip(), "        ")
    background_select = spec.get("background_select")
    needs_background_loop = spec.get("needs_background_loop", False)

    failing_select_block = (
        f'_FAILING_SELECT = "{background_select}"\n' if needs_background_loop and background_select else ""
    )
    methods = (
        _inject_fault_with_loop(class_name, expected_exception)
        + _background_workload_method(class_name)
        + _recover_fault_method(class_name)
        if needs_background_loop and background_select
        else _inject_fault_simple(class_name, expected_exception) + _recover_fault_simple(class_name)
    )

    return "\n".join(
        [
            f'''\
"""{spec.get("docstring", "")}

JIRA: {spec.get("source_url", "")}
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.cassandra_bug import CassandraBugProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)
''',
            f'''\
_TRIGGER_CQL = """
{trigger_cql_indented}
"""
''',
            failing_select_block,
            f'''\
class {class_name}(CassandraBugProblem):
    cassandra_version = "{spec["version"]}"
    source_git_ref = "{spec["git_ref"]}"

    root_cause_file = "{spec["root_cause_file"]}"
    root_cause_description = (
        "{spec["root_cause_description"]}"
    )

    trigger_cql = _TRIGGER_CQL
{methods}''',
        ]
    )


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
        """Trigger the bug via CQL - {expected_exception} will appear in Cassandra logs."""
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
        """No runtime state to clean up - fault is in source code."""
        logger.info("[{class_name}] No fault recovery needed (source-code bug)")
'''


if __name__ == "__main__":
    main()
