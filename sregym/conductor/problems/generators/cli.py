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
    from sregym.conductor.problems.generators.spec_to_code import generate_problem_file

    print(f"Fetching issue: {args.url}", file=sys.stderr)
    spec = parse_issue(args.url, system=args.system)

    if args.show_spec:
        print("--- spec ---")
        print(yaml.dump(spec, default_flow_style=False, allow_unicode=True))
        print("--- end spec ---\n")

    print(f"Generating problem class: {spec['python_class_name']}", file=sys.stderr)
    code = generate_problem_file(spec)

    if args.write:
        out_path = PROBLEMS_DIR / f"{spec['module_filename']}.py"
        if out_path.exists():
            print(
                f"WARNING: {out_path} already exists — skipping write. "
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


if __name__ == "__main__":
    main()
