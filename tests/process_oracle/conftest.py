"""
inject_app lives on main but not on this branch yet.
Mock it so process_oracle tests can import through sregym.conductor.
"""

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("sregym.generators.fault.inject_app", MagicMock())
