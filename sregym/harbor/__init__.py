"""Run SREGym problems as Harbor tasks.

Each Harbor task runs two Compose services:

- ``main``: the agent's unprivileged workstation with ``kubectl``.
- ``sregym``: the privileged Docker-in-Docker backend (``docker/dind``). It owns
  the KIND cluster, deploys one problem, injects its fault, and keeps the
  problem object alive so its mitigation oracle can grade the final state.

``adapter`` generates the task directories and ``backend`` is the sidecar
process. ``protocol`` holds the names both sides must agree on.
"""
