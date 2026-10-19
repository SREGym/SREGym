"""v1's unit tests, run against v2's copies of the same modules (the copies must keep v1's behaviour)."""

import pathlib
import re

_V1 = pathlib.Path(__file__).with_name("test_jev_diag.py")
_MODULES = "classifier|collector|derive|investigate|tree|checks|changes|probes|trend"
_SRC = re.sub(rf"clients\.jev_diag(\.(?:{_MODULES})\b| import )", r"clients.jev_diag.v2\1", _V1.read_text())
exec(compile(_SRC, "test_jev_diag[v2]", "exec"))  # noqa: S102 - defines the same test functions in this module
