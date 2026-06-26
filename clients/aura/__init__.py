"""SREGym-side AURA adapter (vendored).

This package is copied verbatim by ``mezmo-bench sregym-run`` into
``<MEZMO_BENCH_SREGYM_ROOT>/clients/aura/``. SREGym's ``main.py`` imports
``clients.aura.driver`` as the per-problem entry point.

Keeping it inside ``benchmarks/sregym/sregym_aura_adapter/`` in this repo
(rather than vendoring the SREGym source here) preserves a clean
dependency boundary: SREGym lives at ``MEZMO_BENCH_SREGYM_ROOT``, our
adapter source lives here, and ``mezmo-bench sregym-run`` copies it into
place before invoking SREGym.
"""

from __future__ import annotations
