"""Otel demo imageSlowLoad feature flag fault."""

import time

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.image_slow_load import ImageSlowLoadOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.generators.workload.image_client import wait_for_images
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ImageSlowLoad(Problem):
    def __init__(self):
        super().__init__(app=AstronomyShop())
        # Only clients that fetch product images with the delay header trigger the flag.
        self.app.image_client_enabled = True
        self.kubectl = KubeCtl()
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "frontend"
        self.feature_flag = "imageSlowLoad"
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment is serving image responses with abnormally high "
                "latency, causing slow page renders and degraded user experience. Image-dependent pages show "
                "elevated latency and partial rendering while requests wait for delayed asset responses. Users "
                "observe sluggish page loads and visibly delayed product imagery. "
                f"Mechanism: the `flagd-config` ConfigMap in the `{self.namespace}` namespace has the "
                f'`{self.feature_flag}` feature flag\'s `defaultVariant` set to `"10sec"`, which triggers the '
                "OpenTelemetry demo's Envoy proxy fault-injection path that adds a 10-second delay to product-image "
                "responses served through the frontend."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ImageSlowLoadOracle(problem=self)

    def _frontend_latency_alert_is_firing(self):
        return any(
            alert.get("labels", {}).get("alertname") == "HighRequestLatency"
            and alert.get("labels", {}).get("service_name") == "frontend-proxy"
            for alert in AlertOracle(self)._query_firing_alerts(self.namespace)
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault(self.feature_flag)
        # The alert's lookback and firing delay outlast the feature-flag rollout.
        evidence = wait_for_images(
            self.kubectl,
            after=time.time(),
            timeout=420,
            delayed=True,
            alert_is_firing=self._frontend_latency_alert_is_firing,
        )
        print(f"Verified slow product images and firing frontend-proxy HighRequestLatency: {evidence}", flush=True)
        print(f"Fault: imageSlowLoad | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault(self.feature_flag)
