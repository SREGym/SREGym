from sregym.conductor.problems import frontend_geo_port_rollout as problem_module


class DummyHotelReservation:
    def __init__(self):
        self.namespace = "hotel-reservation"

    def create_workload(self):
        return None


class DummyKubeCtl:
    def __init__(self):
        self.get_deployment_called = False

    def get_deployment(self, name, namespace):
        self.get_deployment_called = True
        raise AssertionError("problem construction should not query live deployments")


def test_frontend_geo_port_rollout_init_does_not_query_cluster(monkeypatch):
    monkeypatch.setattr(problem_module, "HotelReservation", DummyHotelReservation)
    monkeypatch.setattr(problem_module, "KubeCtl", DummyKubeCtl)

    problem = problem_module.FrontendGeoPortRollout()

    assert problem.namespace == "hotel-reservation"
    assert problem.configmap_name == "frontend-runtime-config"
