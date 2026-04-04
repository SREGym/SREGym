"""
generate_trajectories.py 

Walks a results directory and converts any agent output files
(claude-code.txt, codex.txt) that do not yet have a corresponding
*_trajectory.jsonl into the stratus JSONL format the visualizer reads.

Usage:
    python3 visualizer/generate_trajectories.py results/
    python3 visualizer/generate_trajectories.py results/ --dry-run
"""

import argparse
import importlib.util
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RUN_LABEL_RE = re.compile(r"^run_\d+$")


def _load_converter(name: str):
    """Load a converter module by filename from the converters/ directory."""
    path = _HERE / "converters" / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"Converter not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extract_problem_id(run_dir: Path) -> str:
    """
    Derive problem_id from the directory structure:
      results/{ts}/{agent}/{problem_id}/run_{N}/
    The problem_id is the directory directly above run_N.
    """
    for part in reversed(run_dir.parts):
        if _RUN_LABEL_RE.match(part):
            idx = list(run_dir.parts).index(part)
            if idx > 0:
                return run_dir.parts[idx - 1]
    # Fallback: use the run_dir's parent directory name
    return run_dir.parent.name


def _already_has_trajectory(run_dir: Path) -> bool:
    return any(run_dir.rglob("*_trajectory.jsonl"))


def process_results(root: Path, dry_run: bool = False) -> None:
    claudecode_mod = _load_converter("claudecode_to_trajectory")
    codex_mod = _load_converter("codex_to_trajectory")

    tasks = []
    for output_file in sorted(root.rglob("claude-code.txt")):
        run_dir = output_file.parent
        if not _already_has_trajectory(run_dir):
            tasks.append(("claudecode", output_file, run_dir, claudecode_mod))

    for output_file in sorted(root.rglob("codex.txt")):
        run_dir = output_file.parent
        if not _already_has_trajectory(run_dir):
            tasks.append(("codex", output_file, run_dir, codex_mod))

    if not tasks:
        print("All runs already have trajectory files — nothing to do.")
        return

    for agent, output_file, run_dir, mod in tasks:
        problem_id = _extract_problem_id(run_dir)
        traj_dir = run_dir / "trajectory"
        # Derive a timestamp from the results timestamp directory if present
        ts = ""
        for part in run_dir.parts:
            if re.match(r"^\d{4}_\d{4}$", part):
                ts = part
                break
        run_label = next((p for p in run_dir.parts if _RUN_LABEL_RE.match(p)), "run_1")
        out_name = f"{ts}_{problem_id}_{agent}_agent_trajectory.jsonl" if ts else f"{problem_id}_{agent}_agent_trajectory.jsonl"
        out_path = traj_dir / out_name

        print(f"{'[dry-run] ' if dry_run else ''}Converting {output_file.relative_to(root)} → {out_path.relative_to(root)}")

        if not dry_run:
            try:
                mod.convert(
                    input_path=output_file,
                    output_path=out_path,
                    problem_id=problem_id,
                )
            except Exception as exc:
                print(f"  ERROR: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate trajectory JSONL files from agent output in a results directory.")
    ap.add_argument("root", help="Root results directory to walk (e.g. results/)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be converted without writing files")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    process_results(root, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
