"""ConfigMap subPath helpers for overlaying a single file in a running pod.

The injector writes replacement source into a ConfigMap, mounts that key over
one path with subPath (so the rest of the image is unchanged), and rolls the
Deployment. Recovery removes the volume, mount, and ConfigMap so the image
file returns.
"""

from kubernetes import client


def override_configmap_name(deployment_name: str, configmap_name: str | None = None) -> str:
    return configmap_name or f"{deployment_name}-src-override"


def override_volume_name(configmap_name: str) -> str:
    return f"{configmap_name}-vol"


def overlay_basename(source_path: str) -> str:
    return source_path.rstrip("/").rsplit("/", 1)[-1]


def select_container(pod_spec, container_name: str | None):
    containers = list(pod_spec.containers or [])
    if not containers:
        raise RuntimeError("deployment has no containers")
    if container_name is None:
        return containers[0]
    for container in containers:
        if container.name == container_name:
            return container
    names = ", ".join(container.name for container in containers)
    raise RuntimeError(f"container {container_name!r} not found (have: {names})")


def apply_source_file_overlay(
    pod_spec,
    *,
    volume_name: str,
    configmap_name: str,
    source_path: str,
    basename: str,
    container_name: str | None = None,
) -> None:
    """Add a ConfigMap volume and a read-only subPath mount. Idempotent."""
    existing_volumes = list(pod_spec.volumes or [])
    if not any(volume.name == volume_name for volume in existing_volumes):
        existing_volumes.append(
            client.V1Volume(
                name=volume_name,
                config_map=client.V1ConfigMapVolumeSource(name=configmap_name),
            )
        )

    container = select_container(pod_spec, container_name)
    existing_mounts = list(container.volume_mounts or [])
    if not any(mount.name == volume_name for mount in existing_mounts):
        existing_mounts.append(
            client.V1VolumeMount(
                name=volume_name,
                mount_path=source_path,
                sub_path=basename,
                read_only=True,
            )
        )

    pod_spec.volumes = existing_volumes
    container.volume_mounts = existing_mounts


def remove_source_file_overlay(pod_spec, *, volume_name: str, container_name: str | None = None) -> None:
    """Drop the overlay volume from the pod spec and the named container."""
    pod_spec.volumes = [volume for volume in (pod_spec.volumes or []) if volume.name != volume_name]
    container = select_container(pod_spec, container_name)
    container.volume_mounts = [mount for mount in (container.volume_mounts or []) if mount.name != volume_name]
