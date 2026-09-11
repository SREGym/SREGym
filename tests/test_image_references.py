import copy
import json
import shlex
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.apps.flight_ticket import FlightTicket
from sregym.service.apps.hotel_reservation import HOTEL_RESERVATION_APPLICATION_IMAGE
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.apps.tidb_cluster_operator import TiDBClusterDeployer
from sregym.service.apps.train_ticket import TrainTicket
from sregym.service.container_runner import DEFAULT_AGENT_IMAGE
from sregym.service.helm import Helm

ROOT = Path(__file__).resolve().parents[1]
IMAGES = json.loads((ROOT / "docker/images.lock.json").read_text())


def test_hotel_manifests_and_recovery_use_the_published_image():
    assert IMAGES["hotel-reservation"] == HOTEL_RESERVATION_APPLICATION_IMAGE
    references = []
    for path in (ROOT / "SREGym-applications/hotelReservation/kubernetes").rglob("*.yaml"):
        document = yaml.safe_load(path.read_text())
        if document.get("kind") != "Deployment":
            continue
        for container in document["spec"]["template"]["spec"]["containers"]:
            if "hotel-reservation" in container["image"]:
                references.append(container["image"])
    assert references == [IMAGES["hotel-reservation"]] * 8


def test_social_network_chart_uses_published_images():
    chart = ROOT / "SREGym-applications/socialNetwork/helm-chart/socialnetwork"
    defaults = yaml.safe_load((chart / "values.yaml").read_text())["global"]
    services = 0
    for path in (chart / "charts").glob("*/values.yaml"):
        values = yaml.safe_load(path.read_text())
        container = values.get("container", {})
        name = values.get("name", "")
        if name.endswith("-service") or name in {"nginx-thrift", "media-frontend"}:
            version = container.get("imageVersion", defaults["defaultImageVersion"])
            image = f"{container.get('dockerRegistry', defaults['dockerRegistry'])}/{container['image']}:{version}"
            target = {"nginx-thrift": "openresty-thrift", "media-frontend": "media-frontend"}.get(
                name, "social-network"
            )
            assert image == IMAGES[target], name
            services += 1
    assert services == 13


def test_social_deploy_does_not_switch_images_or_accumulate_overrides():
    app = SocialNetwork.__new__(SocialNetwork)
    app.create_namespace = Mock()
    app.create_tls_secret = Mock()
    app.kubectl = Mock()
    app.helm_configs = {"namespace": "social-network", "extra_args": ["--wait"]}
    with patch.object(Helm, "install") as install, patch.object(Helm, "assert_if_deployed"):
        app.deploy()
        app.deploy()
    assert app.helm_configs["extra_args"] == ["--wait"]
    assert install.call_count == 2
    app.kubectl.get_node_architectures.assert_not_called()


def test_locust_image_override_preserves_the_complete_upstream_sidecar():
    upstream = yaml.safe_load(
        (ROOT / "SREGym-applications/astronomy-shop/charts/opentelemetry-demo/values.yaml").read_text()
    )
    fixes = yaml.safe_load((ROOT / "sregym/service/apps/values/astronomy-shop-fixes.yaml").read_text())
    expected = copy.deepcopy(upstream["components"]["load-generator"]["sidecarContainers"])
    actual = fixes["components"]["load-generator"]["sidecarContainers"]
    assert len(actual) == len(expected) == 1
    image = actual[0]["imageOverride"]
    assert f"{image['repository']}:{image['tag']}" == IMAGES["locust-exporter"]
    expected[0]["imageOverride"] = image
    assert actual == expected


def test_workload_node_and_agent_use_the_recorded_releases():
    workload = yaml.safe_load((ROOT / "sregym/generators/workload/wrk-job-template.yaml").read_text())
    assert workload["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGES["wrk2"]
    assert IMAGES["agent-base"] == DEFAULT_AGENT_IMAGE
    for arch in ("arm", "x86"):
        config = yaml.safe_load((ROOT / f"kind/kind-config-{arch}.yaml").read_text())
        assert [node["image"] for node in config["nodes"]] == [IMAGES["kind-node"]] * 4


def test_fleetcast_loads_the_published_backend_override():
    with patch("sregym.service.apps.fleet_cast.KubeCtl"), patch.object(FleetCast, "create_namespace"):
        app = FleetCast()
    values = yaml.safe_load(Path(app.helm_configs["values_file"]).read_text())
    image = values["backend"]["image"]
    assert f"{image['repository']}:{image['tag']}" == IMAGES["fleetcast-backend"]


def test_helm_install_passes_the_image_override_file_to_helm():
    process = Mock(returncode=0)
    process.communicate.return_value = (b"installed", b"")
    values_file = "/project with spaces/values/images.yaml"
    with patch("sregym.service.helm.subprocess.Popen", return_value=process) as popen:
        Helm.install(
            release_name="test",
            chart_path="example/chart",
            namespace="test",
            remote_chart=True,
            values_file=values_file,
            extra_args=["--wait"],
        )
    command = shlex.split(popen.call_args.args[0])
    assert command[command.index("-f") + 1] == values_file
    assert command[-1] == "--wait"


def test_tidb_uses_matching_vendored_chart_without_a_repository_lookup():
    deployer = TiDBClusterDeployer(ROOT / "sregym/service/metadata/tidb_metadata.json")
    assert Path(deployer.operator_chart) == ROOT / "SREGym-applications/FleetCast/tidb-operator"
    with patch.object(deployer, "run_cmd") as run, patch.object(Helm, "add_repo") as add_repo:
        deployer.install_operator_with_values()
    add_repo.assert_not_called()
    assert deployer.operator_chart in run.call_args.args[0]


def test_tidb_does_not_substitute_a_different_operator_version():
    deployer = TiDBClusterDeployer(ROOT / "sregym/service/metadata/tidb-with-operator.json")
    assert deployer.operator_version == "v1.6.0"
    assert deployer.operator_chart == "pingcap/tidb-operator"


def test_flight_ticket_loads_all_three_published_job_images():
    with patch("sregym.service.apps.flight_ticket.KubeCtl"), patch.object(FlightTicket, "create_namespace"):
        app = FlightTicket()
    values = yaml.safe_load(Path(app.helm_configs["values_file"]).read_text())
    assert values["jobs"] == {
        "deployActions": {"image": IMAGES["flight-ticket-action-deployer"]},
        "populateRedis": {"image": IMAGES["flight-ticket-populate-redis"]},
        "loadGenerator": {"image": IMAGES["flight-ticket-load-generator"]},
    }


def test_flight_ticket_action_packaging_and_execution_use_the_published_runtime():
    dockerfile = (ROOT / "docker/flight-ticket/action-deployer.Dockerfile").read_text()
    assert f"ARG PYTHON_RUNTIME_IMAGE={IMAGES['flight-ticket-python-runtime']}" in dockerfile
    assert "${PYTHON_RUNTIME_IMAGE}" in dockerfile


def test_train_ticket_uses_the_same_multiarch_locust_exporter():
    documents = yaml.safe_load_all((ROOT / "sregym/resources/trainticket/locust-deployment.yaml").read_text())
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    exporter = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "locust-exporter")
    assert exporter["image"] == IMAGES["locust-exporter"]


def test_train_ticket_loads_the_published_installer():
    with patch("sregym.service.apps.train_ticket.KubeCtl"):
        app = TrainTicket()
    values = yaml.safe_load(Path(app.helm_configs["values_file"]).read_text())
    assert values == {"job": {"image": IMAGES["train-ticket-deploy"]}}
