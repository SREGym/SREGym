"""Run SREGym problems as Harbor tasks.

Each Harbor task is one unprivileged container (``docker/harbor``): a k3s
cluster, the SREGym backend that deploys one problem, injects its fault and keeps
the problem object alive so its mitigation oracle can grade the final state,
and the agent, which runs as an unprivileged user with ``kubectl``.

``adapter`` generates the task directories and ``backend`` is the backend
process. ``protocol`` holds the names they and the task image must agree on.
"""
