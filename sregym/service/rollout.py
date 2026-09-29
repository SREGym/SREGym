"""Deployment rollout checks shared by setup, recovery, and mitigation grading."""

from collections.abc import Mapping


def _field(resource, attribute: str, json_key: str | None = None):
    if isinstance(resource, Mapping):
        return resource.get(json_key or attribute)
    return getattr(resource, attribute, None)


def deployment_rollout_complete(deployment, *, allow_zero: bool = False) -> bool:
    """Check the current generation, not just surviving Ready pods.

    Accept a Kubernetes client Deployment or its API JSON representation.
    Every desired replica must be updated, Ready, and Available, with no extra
    old replicas. Missing status and resources pending deletion fail closed.
    Setup/recovery can explicitly allow a completed scale-down to zero; an
    application required by a mitigation oracle must have at least one replica.
    """
    metadata = _field(deployment, "metadata")
    spec = _field(deployment, "spec")
    status = _field(deployment, "status")
    if metadata is None or spec is None or status is None:
        return False
    if _field(metadata, "deletion_timestamp", "deletionTimestamp") is not None:
        return False

    desired = _field(spec, "replicas")
    desired = 1 if desired is None else desired
    if desired < (0 if allow_zero else 1):
        return False

    generation = _field(metadata, "generation")
    observed = _field(status, "observed_generation", "observedGeneration")
    if generation is None or observed is None or observed < generation:
        return False

    return (
        (_field(status, "replicas") or 0) == desired
        and (_field(status, "updated_replicas", "updatedReplicas") or 0) == desired
        and (_field(status, "ready_replicas", "readyReplicas") or 0) == desired
        and (_field(status, "available_replicas", "availableReplicas") or 0) == desired
        and (_field(status, "unavailable_replicas", "unavailableReplicas") or 0) == 0
    )
