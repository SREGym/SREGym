#!/usr/bin/env python3
"""Extract root_cause strings from all Problem subclasses and write to CSV.

Usage:
    python -m sregym.conductor.oracles.llm_as_a_judge.extract_root_causes

Output:
    sregym/conductor/oracles/llm_as_a_judge/root_causes.csv
"""

import ast
import csv
import os
from pathlib import Path

PROBLEMS_DIR = Path(__file__).resolve().parents[2] / "problems"
OUTPUT_CSV = Path(__file__).resolve().parent / "root_causes.csv"


def _node_to_str(node: ast.expr, source_lines: list[str]) -> str:
    """Convert an AST expression node to a string representation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # Reconstruct f-string parts
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                expr_text = ast.unparse(value.value)
                parts.append(f"{{{expr_text}}}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _node_to_str(node.left, source_lines)
        right = _node_to_str(node.right, source_lines)
        return left + right
    # For attribute access like cfg.description, return the source text
    return ast.unparse(node)


def extract_from_file(filepath: Path) -> list[dict]:
    """Parse a Python file and extract class names + root_cause assignments."""
    source = filepath.read_text()
    source_lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_name = node.name
        for child in ast.walk(node):
            # Look for self.root_cause = ...
            if not isinstance(child, ast.Assign):
                continue
            for target in child.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "root_cause"
                ):
                    root_cause_str = _node_to_str(child.value, source_lines)
                    results.append(
                        {
                            "file": str(filepath.relative_to(PROBLEMS_DIR)),
                            "class": class_name,
                            "root_cause": root_cause_str,
                        }
                    )
    return results


def main():
    all_results = []

    # Walk the problems directory for .py files
    for dirpath, _dirnames, filenames in os.walk(PROBLEMS_DIR):
        for fname in sorted(filenames):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            fpath = Path(dirpath) / fname
            entries = extract_from_file(fpath)
            all_results.append(entries)

    # Flatten
    flat = [entry for group in all_results for entry in group]

    # Filter out the base class (root_cause = None)
    flat = [e for e in flat if "None" not in e["root_cause"] or len(e["root_cause"]) > 10]

    # Sort by file name
    flat.sort(key=lambda e: e["file"])

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "class", "root_cause"])
        writer.writeheader()
        writer.writerows(flat)

    print(f"Wrote {len(flat)} entries to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
