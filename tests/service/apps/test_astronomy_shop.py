from unittest.mock import Mock

from sregym.service.apps import astronomy_shop as module
from sregym.service.apps.astronomy_shop import AstronomyShop


def test_deploy_can_disable_the_bundled_load_generator(monkeypatch):
    app = AstronomyShop.__new__(AstronomyShop)
    app.load_generator_enabled = False
    app.namespace = "astronomy-shop"
    app.kubectl = Mock()
    app.helm_configs = {"namespace": "astronomy-shop"}
    app.logger = Mock()
    monkeypatch.setattr(module, "is_svelte", lambda: False)
    monkeypatch.setattr(module.Helm, "install", Mock())
    monkeypatch.setattr(module.Helm, "assert_if_deployed", Mock())

    app.deploy()

    extra_args = app.helm_configs["extra_args"]
    assert "components.load-generator.enabled=false" in extra_args
