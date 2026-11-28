import logging
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class LlmContextWindowMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for llm_context_window_astronomy_shop.

    The fault leaves every pod Running and Ready, so on top of the inherited
    namespace health checks a mitigation passes only if:

      1. `llm` is still the llama.cpp server running the original model, so the
         assistant is not "fixed" by swapping in a mock or another model.
      2. The AI assistant answers through the frontend for each probe product.

    The oracle does not check which setting was changed. Lowering the slot count,
    raising the context size, or enabling the unified KV cache all pass.
    """

    FAILURE_CLASSES = {
        "llm_replaced": FailureClass.AGENT_ERROR,
        "ai_assistant_failing": FailureClass.AGENT_ERROR,
    }
    ATTEMPTS = 3
    RETRY_DELAY_S = 5

    def _evaluate_current_state(self) -> dict:
        base = super()._evaluate_current_state()
        if not base.get("success"):
            return base

        p = self.problem
        props = p.llm_server_props()
        model_path = str((props or {}).get("model_path", ""))
        if not model_path.endswith(p.MODEL_FILE):
            print(f"❌ llm is not the llama.cpp server running {p.MODEL_FILE} (model_path={model_path!r})")
            return self.fail("llm_replaced", model_path=model_path)

        for product_id in p.PROBE_PRODUCT_IDS:
            status, body = self._ask(product_id)
            if not p.assistant_answered(status, body):
                print(f"❌ AI assistant did not answer for {product_id} (status={status}): {body[:200]}")
                return self.fail("ai_assistant_failing", product_id=product_id, status=status, body=body[:200])

        print("✅ The AI assistant answers through the llama.cpp server")
        return {"success": True}

    def _ask(self, product_id: str) -> tuple[int | None, str]:
        # Under the fault every attempt is rejected (the first prompt alone is larger than a
        # slot), so retrying only absorbs a one-off error from a healthy server.
        p = self.problem
        for attempt in range(1, self.ATTEMPTS + 1):
            status, body = p.ask_ai_assistant(product_id)
            if p.assistant_answered(status, body) or attempt == self.ATTEMPTS:
                return status, body
            logger.info("AI assistant attempt %d for %s failed (status=%s), retrying", attempt, product_id, status)
            time.sleep(self.RETRY_DELAY_S)
