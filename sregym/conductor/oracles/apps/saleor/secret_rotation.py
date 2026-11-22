"""Saleor: secret rotation."""

from __future__ import annotations

from sregym.conductor.oracles.secret_rotation_stale_env_mitigation import SecretRotationStaleEnvMitigation


class SecretRotationStaleEnvMitigationIA(SecretRotationStaleEnvMitigation):
    """The original checks; user-facing health comes from ``LoadgenHealthOracle``."""

    run_product_probe = False
