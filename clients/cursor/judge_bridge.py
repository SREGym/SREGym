"""Compatibility entry point: python -m clients.cursor.judge_bridge --port PORT."""

from llm_backend.judge_bridge import main, make_handler

__all__ = ["main", "make_handler"]

if __name__ == "__main__":
    main()
