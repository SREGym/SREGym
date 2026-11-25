import json
import logging
import os
import shlex
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.llm_context_window_mitigation import LlmContextWindowMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

MANIFEST_DIR = os.path.join(os.path.dirname(__file__), "manifests")


class LlmContextWindowAstronomyShop(Problem):
    """
    Fault:

    The shop's AI assistant (product-reviews) calls the `llm` service. In the demo that is a mock, so this
    problem runs a real llama.cpp server there instead (Qwen2.5-0.5B on CPU), configured through the
    llm-server-config ConfigMap (manifests/llm_server.yaml).

    The fault raises the server's parallel slots (LLAMA_ARG_N_PARALLEL) from 2 to 8 and leaves the context
    size at 2048. llama.cpp splits the KV cache evenly across slots, so each request now gets 256 tokens
    instead of 1024. The assistant's first prompt alone is ~320 tokens, so llama.cpp rejects every request
    with HTTP 400 exceed_context_size_error and the assistant returns 500s. All pods stay Running and Ready.

    This is documented llama.cpp behavior (the server docs: `-c 16384 -np 4` means 4096 per request) and
    people run into it when they raise --parallel (ggml-org/llama.cpp#11681).

    The fix is to give each slot enough context again: lower LLAMA_ARG_N_PARALLEL, raise LLAMA_ARG_CTX_SIZE,
    or turn on LLAMA_ARG_KV_UNIFIED, then restart the server. Restarting, scaling or rolling back the
    Deployment doesn't help because the setting lives in the ConfigMap.
    """

    LLM = "llm"
    CONFIGMAP = "llm-server-config"
    MODEL_FILE = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    FAULT_PARALLEL = 8
    PROBE_PRODUCT_IDS = ("OLJCESPC7Z", "66VCHSJNUP")

    def __init__(self, *, app: Application | None = None):
        super().__init__(app=app if app is not None else AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "llm_context_window_astronomy_shop"
        self.propagation_duration_s = 60
        self.manifest_path = os.path.join(MANIFEST_DIR, "llm_server.yaml")

        self.root_cause = self.build_structured_root_cause(
            component="configmap/llm-server-config (LLAMA_ARG_N_PARALLEL) used by deployment/llm",
            namespace=self.namespace,
            description=(
                "The llm service behind the AI assistant is a llama.cpp server configured through the "
                "llm-server-config ConfigMap. LLAMA_ARG_N_PARALLEL was raised from 2 to 8 while LLAMA_ARG_CTX_SIZE "
                "stayed 2048, and the server was restarted. llama.cpp splits the KV cache evenly across slots, "
                "so each request now gets 256 tokens of context instead of 1024. The assistant's prompts are "
                "longer than that, so llama.cpp rejects them with HTTP 400 exceed_context_size_error "
                "('request exceeds the available context size'), product-reviews raises openai.BadRequestError, "
                "and the AI assistant fails while the rest of the shop works. "
                "The fix is to lower LLAMA_ARG_N_PARALLEL, raise LLAMA_ARG_CTX_SIZE, or enable the unified KV "
                "cache, then restart the llm server."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = LlmContextWindowMitigationOracle(problem=self)

        # Swap the chart's mock llm for the real server at deploy time.
        self.app.helm_configs["values_file"] = os.path.join(MANIFEST_DIR, "llm_server_values.yaml")
        self._deploy_shop = self.app.deploy
        self.app.deploy = self.deploy_app
        self.app.create_workload()

    def deploy_app(self):
        self._deploy_shop()
        self.kubectl.apply_configs(self.namespace, self.manifest_path)
        # First start downloads the model (~470MB), so give it time.
        self._wait_for_llm(timeout=900)
        self._wait_for_assistant(answering=True, timeout=300)

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting llm context window fault...")

        patch = json.dumps({"data": {"LLAMA_ARG_N_PARALLEL": str(self.FAULT_PARALLEL)}})
        self.kubectl.exec_command_checked(
            f"kubectl patch configmap {self.CONFIGMAP} -n {self.namespace} --type=merge -p {shlex.quote(patch)}"
        )
        self._restart_llm()

        # Fail loudly if the assistant still answers
        self._wait_for_assistant(answering=False, timeout=180)

        logger.info("llm context window fault injected.")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from llm context window fault...")

        # Re-applying the manifest sets LLAMA_ARG_N_PARALLEL back to 2.
        self.kubectl.apply_configs(self.namespace, self.manifest_path)
        self._restart_llm()
        self._wait_for_assistant(answering=True, timeout=300)

        logger.info("llm context window fault recovered.")
        return True

    def _restart_llm(self):
        # llama.cpp only reads its env at startup, so config changes need a restart.
        self.kubectl.exec_command_checked(f"kubectl rollout restart deployment/{self.LLM} -n {self.namespace}")
        self._wait_for_llm()

    def _wait_for_llm(self, timeout: int = 600):
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.LLM} -n {self.namespace} --timeout={timeout}s",
            timeout=timeout + 30,
        )

    def _python(self, script: str) -> str:
        # load-generator has python and requests, and can reach every service.
        return self.kubectl.exec_command(
            f"kubectl exec -n {self.namespace} deploy/load-generator -c load-generator -- "
            f"python -c {shlex.quote(script)}"
        )

    def ask_ai_assistant(self, product_id: str) -> tuple[int | None, str]:
        """Ask the AI assistant about a product through the frontend, like a shopper would."""
        script = (
            "import requests; "
            f"r=requests.post('http://frontend:8080/api/product-ask-ai-assistant/{product_id}', "
            "json={'question': 'Can you summarize the product reviews?'}, timeout=120); "
            "print(r.status_code); print(r.text[:2000])"
        )
        out = self._python(script)
        status, _, body = out.partition("\n")
        return (int(status), body) if status.strip().isdigit() else (None, out)

    def llm_server_props(self) -> dict | None:
        """llama.cpp's /props, or None if whatever serves `llm` isn't llama.cpp."""
        try:
            return json.loads(
                self._python("import requests; print(requests.get('http://llm:8000/props', timeout=10).text)")
            )
        except ValueError:
            return None

    @staticmethod
    def assistant_answered(status: int | None, body: str) -> bool:
        # product-reviews answers with this text instead of an error on some failures
        return status == 200 and len(body.strip()) >= 20 and "unable to process" not in body

    def _wait_for_assistant(self, *, answering: bool, timeout: float):
        deadline = time.monotonic() + timeout
        status, body = None, ""
        while time.monotonic() < deadline:
            status, body = self.ask_ai_assistant(self.PROBE_PRODUCT_IDS[0])
            if self.assistant_answered(status, body) == answering:
                return
            time.sleep(5)
        raise RuntimeError(
            f"AI assistant did not {'answer' if answering else 'fail'} within {timeout}s (status={status}): {body[:200]}"
        )
