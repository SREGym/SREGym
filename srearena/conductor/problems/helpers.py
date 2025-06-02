from srearena.service.apps.base import Application
from srearena.service.kubectl import KubeCtl


def get_frontend_url(app: Application):
    kubectl = KubeCtl()
    endpoint = kubectl.get_cluster_ip(app.frontend_service, app.namespace)
    return f"http://{endpoint}:{app.frontend_port}"
