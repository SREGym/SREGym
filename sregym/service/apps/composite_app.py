"""A class representing a composite of mulitple applications"""

from concurrent.futures import ThreadPoolExecutor

from sregym.service.apps.base import Application


class CompositeApp:
    def __init__(self, apps: list[Application]):
        self.namespace = "Multiple namespaces"
        self.apps = {}
        for app in apps:
            if app.name in self.apps:
                print(f"[CompositeApp] same app name: {app.name}, continue.")
                continue
            self.apps[app.name] = app
        print(f"[CompositeApp] Apps: {self.apps}")
        self.name = "CompositeApp"
        self.app_name = "CompositeApp"
        self.description = f"Composite application containing {len(self.apps)} apps: {', '.join(self.apps.keys())}"

    def deploy(self):
        def deploy_app(app):
            print(f"[CompositeApp] Deploying {app.name}...")
            app.deploy()

        with ThreadPoolExecutor() as executor:
            executor.map(deploy_app, self.apps.values())

    def start_workload(self):
        def start_workload_app(app):
            print(f"[CompositeApp] Starting workload for {app.name}...")
            app.start_workload()

        with ThreadPoolExecutor() as executor:
            executor.map(start_workload_app, self.apps.values())

    def cleanup(self):
        def cleanup_app(app):
            print(f"[CompositeApp] Cleaning up {app.name}...")
            app.cleanup()

        with ThreadPoolExecutor() as executor:
            executor.map(cleanup_app, self.apps.values())
