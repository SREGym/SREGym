"""Ports that subclass a problem wrapping app.cleanup must not wrap it twice.

The base class stores the app's cleanup in ``_app_cleanup`` and installs its own
``_cleanup``. A subclass doing the same stored the wrapper itself, so cleanup
recursed until RecursionError and the problem failed setup in every run.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from sregym.harbor import adapter

PORTS = ["priority_preemption_cascade_saleor", "calico_route_reflector_label_drift_slack_spine"]

# Constructed in a child process: the kubernetes client reads KUBECONFIG at import.
CHILD = textwrap.dedent(
    """
    import sys
    from sregym.conductor.problems.registry import ProblemRegistry

    problem = ProblemRegistry().get_problem(sys.argv[1])()
    assert problem.app.cleanup == problem._cleanup
    # The saved cleanup must be the application's own, not the wrapper.
    assert problem._app_cleanup != problem._cleanup, "app.cleanup was wrapped twice"
    assert getattr(problem._app_cleanup, "__self__", None) is problem.app
    """
)


@pytest.mark.parametrize("problem_id", PORTS)
def test_port_wraps_app_cleanup_once(problem_id, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(adapter._OFFLINE_KUBECONFIG)
    result = subprocess.run(
        [sys.executable, "-c", CHILD, problem_id],
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
