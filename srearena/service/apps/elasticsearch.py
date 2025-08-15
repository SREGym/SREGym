from srearena.paths import ELASTIC_SEARCH_METADATA
from srearena.service.apps.base import Application
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl

class Elasticsearch(Application):
    def __init__(self, config_file: str = ELASTIC_SEARCH_METADATA):
        super().__init__(config_file)
        self.kubectl = KubeCtl()
        self.load_app_json()
        try:
            self.kubectl.create_namespace_if_not_exist(self.namespace)
        except AttributeError:
            self.create_namespace()

    def deploy(self):
        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])

    def delete(self):
        Helm.uninstall(**self.helm_configs)

    def cleanup(self):
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.namespace)
