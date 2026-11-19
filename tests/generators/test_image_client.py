"""astronomy_shop_ad_service_image_slow_load needs a client that sends the image delay header."""

import subprocess
from unittest.mock import Mock

from sregym.generators.workload import image_client
from sregym.generators.workload.image_client import image_client_deployment, image_client_script
from sregym.service.apps import astronomy_shop
from sregym.service.apps.astronomy_shop import AstronomyShop


def test_script_reads_the_flag_and_sends_it_as_the_envoy_delay_header():
    script = image_client_script()
    assert "http://flagd:8016/ofrep/v1/evaluate/flags/imageSlowLoad" in script
    assert "http://frontend-proxy:8080/api/products" in script
    assert "x-envoy-fault-delay-request: ${delay:-0}" in script
    assert "http://frontend-proxy:8080/images/products/$picture" in script
    subprocess.run(["sh", "-n", "-c", script], check=True)


def test_deployment_pins_the_curl_image():
    deployment = image_client_deployment("astronomy-shop")
    assert deployment.metadata.name == "frontend-image-client"
    container = deployment.spec.template.spec.containers[0]
    assert container.image == "curlimages/curl:8.10.1"
    assert "cpu" not in container.resources.limits


def _shop(monkeypatch, enabled):
    shop = AstronomyShop.__new__(AstronomyShop)
    shop.namespace = "astronomy-shop"
    shop.wrk = Mock()
    if enabled:
        shop.image_client_enabled = True
    calls = []
    monkeypatch.setattr(astronomy_shop, "start_image_client", lambda ns: calls.append(("start", ns)))
    monkeypatch.setattr(astronomy_shop, "stop_image_client", lambda ns: calls.append(("stop", ns)))
    return shop, calls


def test_workload_runs_the_client_only_when_enabled(monkeypatch):
    shop, calls = _shop(monkeypatch, enabled=False)
    shop.start_workload()
    shop.stop_workload()
    assert calls == []

    shop, calls = _shop(monkeypatch, enabled=True)
    shop.start_workload()
    shop.stop_workload()
    assert calls == [("start", "astronomy-shop"), ("stop", "astronomy-shop")]
    shop.wrk.start.assert_called_once()


def test_image_slow_load_enables_the_client(monkeypatch):
    from sregym.conductor.problems import image_slow_load

    def app():
        shop = AstronomyShop.__new__(AstronomyShop)
        shop.namespace = "astronomy-shop"
        return shop

    monkeypatch.setattr(image_slow_load, "AstronomyShop", app)
    monkeypatch.setattr(image_slow_load, "KubeCtl", lambda: None)
    monkeypatch.setattr(image_slow_load, "OtelFaultInjector", lambda namespace: None)
    monkeypatch.setattr(image_slow_load, "LLMAsAJudgeOracle", lambda **kwargs: None)
    monkeypatch.setattr(image_slow_load, "AlertOracle", lambda **kwargs: None)
    assert image_slow_load.ImageSlowLoad().app.image_client_enabled is True
    assert AstronomyShop.image_client_enabled is False


def test_stop_ignores_a_missing_client(monkeypatch):
    apps = Mock()
    apps.delete_namespaced_deployment.side_effect = image_client.client.exceptions.ApiException(status=404)
    monkeypatch.setattr(image_client.client, "AppsV1Api", lambda: apps)
    image_client.stop_image_client("astronomy-shop")
