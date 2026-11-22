import json
from functools import lru_cache

from sregym.paths import BASE_PARENT_DIR
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl


@lru_cache(maxsize=1)
def _image_catalog() -> dict[str, str]:
    return json.loads((BASE_PARENT_DIR / "docker" / "images.lock.json").read_text())


def image_reference(image_id: str) -> str:
    """Resolve a local build's deployed image without a generation step."""
    try:
        reference = _image_catalog()[image_id]
    except KeyError:
        raise ValueError(f"Image {image_id!r} is not recorded in docker/images.lock.json") from None
    if "@sha256:" not in reference:
        raise ValueError(f"Image {image_id!r} must use a pinned SHA256 digest")
    return reference


def get_frontend_url(app: Application):
    kubectl = KubeCtl()
    endpoint = kubectl.get_cluster_ip(app.frontend_service, app.namespace)
    return f"http://{endpoint}:{app.frontend_port}"
