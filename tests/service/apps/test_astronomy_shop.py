from unittest.mock import Mock, call, patch

import yaml

from sregym.service.apps import astronomy_shop
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.helm import Helm


def _app(architectures: set[str]) -> AstronomyShop:
    app = AstronomyShop.__new__(AstronomyShop)
    app.namespace = "astronomy-shop"
    app.logger = Mock()
    app.kubectl = Mock()
    app.kubectl.get_node_architectures.return_value = architectures
    app.helm_configs = {
        "release_name": "astronomy-shop",
        "chart_path": "/tmp/opentelemetry-demo",
        "namespace": "astronomy-shop",
    }
    return app


def test_deploy_registers_chart_repositories_and_applies_arm64_values():
    app = _app({"arm64"})

    with (
        patch.object(astronomy_shop, "is_svelte", return_value=False),
        patch.object(Helm, "add_repo") as add_repo,
        patch.object(Helm, "install") as install,
        patch.object(Helm, "assert_if_deployed"),
    ):
        app.deploy()

    add_repo.assert_has_calls([call(name, url) for name, url in AstronomyShop._HELM_REPOSITORIES.items()])
    extra_args = install.call_args.kwargs["extra_args"]
    assert str(AstronomyShop._VALUES_DIR / "astronomy-shop-arm64.yaml") in extra_args


def test_deploy_does_not_apply_arm64_values_to_x86_cluster():
    app = _app({"amd64"})

    with (
        patch.object(astronomy_shop, "is_svelte", return_value=False),
        patch.object(Helm, "add_repo"),
        patch.object(Helm, "install") as install,
        patch.object(Helm, "assert_if_deployed"),
    ):
        app.deploy()

    extra_args = install.call_args.kwargs["extra_args"]
    assert str(AstronomyShop._VALUES_DIR / "astronomy-shop-arm64.yaml") not in extra_args


def test_svelte_values_override_full_profile_sidecar_settings():
    app = _app({"arm64"})

    with (
        patch.object(astronomy_shop, "is_svelte", return_value=True),
        patch.object(Helm, "add_repo"),
        patch.object(Helm, "install") as install,
        patch.object(Helm, "assert_if_deployed"),
    ):
        app.deploy()

    extra_args = install.call_args.kwargs["extra_args"]
    fixes_values = str(AstronomyShop._VALUES_DIR / "astronomy-shop-fixes.yaml")
    arm64_values = str(AstronomyShop._VALUES_DIR / "astronomy-shop-arm64.yaml")
    svelte_values = str(AstronomyShop._VALUES_DIR / "astronomy-shop-svelte.yaml")
    assert extra_args.index(arm64_values) < extra_args.index(svelte_values)
    assert extra_args.index(fixes_values) < extra_args.index(svelte_values)


def test_full_profile_caps_ui_descriptors_without_increasing_memory():
    values = yaml.safe_load((AstronomyShop._VALUES_DIR / "astronomy-shop-fixes.yaml").read_text())
    ui = values["components"]["flagd"]["sidecarContainers"][0]
    assert ui["name"] == "flagd-ui"
    assert ui["resources"]["limits"]["memory"] == "250Mi"
    assert "ulimit -n 65536" in ui["command"][2]
    assert '"$current" -gt 65536' in ui["command"][2]
    assert "exec /app/bin/server" in ui["command"][2]


def test_arm_go_services_have_memory_headroom():
    values = yaml.safe_load((AstronomyShop._VALUES_DIR / "astronomy-shop-arm64.yaml").read_text())
    for name in ("product-catalog", "checkout"):
        service = values["components"][name]
        assert service["resources"]["limits"]["memory"] == "64Mi"
        assert service["envOverrides"] == [{"name": "GOMEMLIMIT", "value": "48MiB"}]
