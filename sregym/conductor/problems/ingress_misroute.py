from kubernetes import client

from sregym.conductor.oracles.ingress_localization_oracle import IngressLocalizationOracle
from sregym.conductor.oracles.ingress_misroute_oracle import IngressMisrouteMitigationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class IngressMisroute(Problem):
    def __init__(self, path="/api", correct_service="frontend-service", wrong_service="recommendation-service"):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.path = path
        self.correct_service = correct_service
        self.wrong_service = wrong_service
        self.ingress_name = "hotel-reservation-ingress"
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.networking_v1 = client.NetworkingV1Api()

        self.localization_oracle = IngressLocalizationOracle(
            problem=self, namespace=self.namespace, expected_ingress_name=self.ingress_name
        )
        self.mitigation_oracle = IngressMisrouteMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        """Misroute /api to wrong backend"""

        try:
            ingress = self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                ingress_manifest = {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": {"name": self.ingress_name, "namespace": self.namespace},
                    "spec": {
                        "rules": [
                            {
                                "http": {
                                    "paths": [
                                        {
                                            "path": self.path,
                                            "pathType": "Prefix",
                                            "backend": {
                                                "service": {"name": self.correct_service, "port": {"number": 80}}
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                }
                self.networking_v1.create_namespaced_ingress(namespace=self.namespace, body=ingress_manifest)
                ingress = self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
            else:
                raise

        # Modify the rule for /api to wrong_service
        for rule in ingress.spec.rules:
            for path in rule.http.paths:
                if path.path == self.path:
                    path.backend.service.name = self.wrong_service
        self.networking_v1.replace_namespaced_ingress(name=self.ingress_name, namespace=self.namespace, body=ingress)

    @mark_fault_injected
    def recover_fault(self):
        """Revert misroute to correct backend"""
        ingress = self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        for rule in ingress.spec.rules:
            for path in rule.http.paths:
                if path.path == self.path:
                    path.backend.service.name = self.correct_service
        self.networking_v1.replace_namespaced_ingress(name=self.ingress_name, namespace=self.namespace, body=ingress)
