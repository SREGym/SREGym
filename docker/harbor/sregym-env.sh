# The image's ENV, for providers that start commands without it. E2B runs
# Harbor's commands as login shells but does not apply the Dockerfile's ENV,
# so without this the setup cannot find k3s and the agent's kubectl has no
# cluster. Values already set (Docker applies ENV) are kept.
: "${K3S_BIN:=/opt/k3s/bin}"
: "${KUBECONFIG:=/run/sregym/kubeconfig}"
: "${WAIT_FOR_POD_READY_TIMEOUT:=1800}"
case ":$PATH:" in
    *:/opt/sregym/.venv/bin:*) ;;
    *) PATH="/opt/sregym/.venv/bin:$PATH" ;;
esac
export K3S_BIN KUBECONFIG WAIT_FOR_POD_READY_TIMEOUT PATH
