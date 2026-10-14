"""Start the local viewer with ``python -m sregym.viewer [path]``."""

import argparse
from pathlib import Path

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse ATIF trajectories without changing the source files.")
    parser.add_argument(
        "path", nargs="?", type=Path, default=Path("results"), help="Result directory or ATIF JSON file"
    )
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    args = parser.parse_args()
    if not args.path.exists():
        parser.error(f"Path does not exist: {args.path}")
    app = create_app(args.path)
    print(f"Trace viewer: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
