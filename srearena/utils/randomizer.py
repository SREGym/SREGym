import random
import json

from srearena.service.apps.registry import AppRegistry

class Randomizer:
    def __init__(self, kubectl):
        self.kubectl = kubectl
        self.namespace = None
        self.apps = AppRegistry()

    def select_app(self, app_names=None):
        # Randomly choose an app from service_paths. If service_paths not provided, choose from list of all available apps. Return reference to app.
        if not app_names:
            app_names = self.apps.get_app_names()
        app_name = random.choice(app_names)

        app_metadata = self.apps.get_app_metadata(app_name)
        self.namespace = app_metadata["Namespace"]

        app = self.apps.get_app_instance(app_name)
        return app

    def select_service(self):
        # Queue kubectl for all available services in app, return service name.
        service_list = [svc.metadata.name for svc in self.kubectl.list_services(namespace=self.namespace).items]
        service = random.choice(service_list)
        print(f"Random service chosen: {service}")
        return service


    
