from unittest.mock import MagicMock, patch

from sregym.service.apps.train_ticket import TrainTicket


def test_delete_removes_bundled_kube_system_prometheus():
    app = TrainTicket.__new__(TrainTicket)
    app.namespace = "train-ticket"
    app.kubectl = MagicMock()
    app.delete()
    cmd = app.kubectl.exec_command.call_args_list[0].args[0]
    assert cmd.startswith("kubectl delete -f ")
    assert cmd.split()[3].endswith("train-ticket/deployment/kubernetes-manifests/prometheus")
    assert "--ignore-not-found" in cmd


def test_deploy_removes_bundled_prometheus_after_deploy_job():
    app = TrainTicket.__new__(TrainTicket)
    app.namespace = "train-ticket"
    app.helm_configs = {}
    app.kubectl = MagicMock()
    with (
        patch.object(TrainTicket, "_is_train_ticket_deployed", return_value=False),
        patch.object(TrainTicket, "_deploy_flagd_infrastructure"),
        patch.object(TrainTicket, "_deploy_load_generator"),
        patch("sregym.service.apps.train_ticket.Helm.install"),
    ):
        app.deploy()
    assert any("kubernetes-manifests/prometheus" in c.args[0] for c in app.kubectl.exec_command.call_args_list)
