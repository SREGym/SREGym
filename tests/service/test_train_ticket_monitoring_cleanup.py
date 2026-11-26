from unittest.mock import MagicMock, patch

from sregym.service.apps.train_ticket import TrainTicket

MANIFESTS = "train-ticket/deploy-job/deployment/kubernetes-manifests/prometheus"


def _app():
    app = TrainTicket.__new__(TrainTicket)
    app.namespace = "train-ticket"
    app.helm_configs = {}
    app.kubectl = MagicMock()
    return app


def test_cleanup_removes_bundled_kube_system_prometheus():
    app = _app()

    app.cleanup()

    cmd = app.kubectl.exec_command.call_args_list[0].args[0]
    assert cmd.startswith("kubectl delete -f ")
    assert cmd.split()[3].endswith(MANIFESTS)
    assert "--ignore-not-found" in cmd


def test_deploy_removes_bundled_prometheus_after_the_deploy_job_finishes():
    app = _app()
    with (
        patch.object(TrainTicket, "_is_train_ticket_deployed", return_value=False),
        patch.object(TrainTicket, "_deploy_flagd_infrastructure"),
        patch.object(TrainTicket, "_deploy_load_generator"),
        patch("sregym.service.apps.train_ticket.Helm.install"),
    ):
        app.deploy()

    names = [c[0] for c in app.kubectl.method_calls]
    wait = names.index("wait_for_job_completion")
    delete = next(
        i for i, c in enumerate(app.kubectl.method_calls) if c[0] == "exec_command" and MANIFESTS in c.args[0]
    )
    assert wait < delete
