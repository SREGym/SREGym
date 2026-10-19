"""jev_diag v2's diagnoser: build competing explanations, let the decision model pick, look closer when it is unsure.

It returns the team's `Diagnosis` so jev_diag's driver runs it unchanged (`--mode v2`). Budgets: at most
`max_requests` decision requests and `wall_budget_s` seconds of investigation; when they run out, the leading
explanation is submitted and marked uncertain. A winning "none of these" is never swapped for a rejected chain.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from clients.jev_diag.classifier import ChoiceResult, Diagnosis, JevDiagnoser, _sum_usage
from clients.jev_diag.collector import ClusterSnapshot, fit_state_to_budget
from clients.jev_diag.tree import detail_evidence
from clients.jev_diag.v2.decide import NONE, TOP_K, Characterization, characterize, pick, shortlist
from clients.jev_diag.v2.explain import PARTIAL, Explanation
from clients.jev_diag.v2.patterns import build_explanations
from clients.jev_diag.v2.report import build_report

logger = logging.getLogger("all.jev_diag v2.agent")

STATE_BUDGET = 20000  # tokens of focused evidence per decision request
MAX_SIGNALS = 8
CHARACTERIZE_REQUESTS = 2  # beyond max_requests: v1's characterization of the root-cause component
PRE_READ = 6  # components read before the first question: those the initial top candidates are about


class V2Diagnoser(JevDiagnoser):
    def __init__(
        self,
        client=None,
        *,
        fetch_detail: Callable[[ClusterSnapshot, str], dict] | None = None,
        max_requests: int = 8,
        wall_budget_s: float = 120.0,
        seed: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, **kwargs)
        self.fetch_detail = fetch_detail
        self.max_requests = max_requests
        self.wall_budget_s = wall_budget_s
        self.rng = random.Random(seed)
        self.details: dict[str, dict] = {}

    # ------------------------------------------------------------------ evidence

    def _component_view(self, snapshot: ClusterSnapshot, cid: str) -> dict:
        comp = snapshot.components.get(cid) or {}
        view = {
            "kind": comp.get("kind"),
            "name": comp.get("name"),
            "namespace": comp.get("namespace"),
            "healthy": comp.get("healthy"),
            "signals": (comp.get("signals") or [])[:MAX_SIGNALS],
            "template_changes": comp.get("template_changes"),
            "errors_point_to": comp.get("errors_point_to"),
            "referenced_by_errors_from": sorted(comp.get("referenced_by_errors_from") or {}),
        }
        if cid in self.details:
            view["detail"] = self.details[cid]
        return {k: v for k, v in view.items() if v not in (None, [], {})}

    def _state(self, snapshot: ClusterSnapshot, candidates: list[Explanation]) -> dict:
        """Only the evidence the candidates are about: their origins and observed impact, plus cluster context."""
        involved: list[str] = []
        by_object = {
            f"{c.get('kind', '').lower()}/{c.get('namespace')}/{c.get('name')}": cid
            for cid, c in snapshot.components.items()
        }
        for e in candidates:
            for obj in [e.origin, *e.contributing, *e.impact]:
                cid = by_object.get(obj)
                if cid and cid not in involved:
                    involved.append(cid)
        state = {
            "application": snapshot.app,
            "evidence": {cid: self._component_view(snapshot, cid) for cid in involved},
            "cluster": {
                "symptom_onset": snapshot.cluster.get("symptom_onset"),
                "recent_changes": snapshot.cluster.get("recent_changes"),
                "findings": snapshot.cluster.get("findings"),
            },
        }
        fitted, _ = fit_state_to_budget(state, STATE_BUDGET)
        return fitted

    def _look_closer(self, snapshot: ClusterSnapshot, candidates: list[Explanation], route: str) -> bool:
        """Fetch component detail: the top two origins when unsure, the two most affected unexamined components
        (signals plus error logs) when no explanation fits. Returns whether anything new was read."""
        if self.fetch_detail is None:
            return False
        by_object = {
            f"{c.get('kind', '').lower()}/{c.get('namespace')}/{c.get('name')}": cid
            for cid, c in snapshot.components.items()
        }
        if route == "check":  # every component the top two chains are about: origin, contributing, impact
            targets = [by_object.get(obj) for e in candidates[:2] for obj in [e.origin, *e.contributing, *e.impact]]
        else:
            comps = snapshot.components
            ranked = sorted(
                (cid for cid, c in comps.items() if not c.get("healthy", True) and cid not in self.details),
                key=lambda cid: -(len(comps[cid].get("signals") or []) + bool(comps[cid].get("log_error_lines"))),
            )
            targets = ranked[:2] or [by_object.get(e.origin) for e in candidates[:1]]
        added = False
        for cid in targets:
            if cid and cid not in self.details:
                try:
                    self.details[cid] = self.fetch_detail(snapshot, cid)
                except Exception as exc:  # noqa: BLE001 - a failed read is recorded, never fatal
                    self.details[cid] = {"error": f"{type(exc).__name__}: {exc}"}
                self._trace("collect", "detail", component=cid, route=route)
                added = True
        return added

    # ------------------------------------------------------------------ loop

    def diagnose(self, snapshot: ClusterSnapshot) -> Diagnosis:
        explanations = build_explanations(snapshot)
        if not explanations:
            raise ValueError("No component shows symptoms and no change or finding was recorded; nothing to explain.")
        started = time.monotonic()
        self._read(snapshot, self._involved(snapshot, shortlist(explanations, TOP_K))[:PRE_READ], "pre-read")
        rounds: list[dict] = []
        result = None
        candidates: list[Explanation] = []
        route = "check"
        while len(self.requests) + 2 <= self.max_requests and time.monotonic() - started < self.wall_budget_s:
            explanations = build_explanations(snapshot, self.details)  # new detail can add or fill in chains
            candidates = shortlist(explanations, TOP_K)
            result = pick(self._ask, self._state(snapshot, candidates), candidates, rng=self.rng)
            route = result.route()
            rounds.append(
                {
                    "candidates": [e.key for e in candidates],
                    "probabilities": result.probabilities,
                    "picks": result.picks,
                    "route": route,
                }
            )
            self._trace("decide", "round", **rounds[-1])
            if route == "accept" or not self._look_closer(snapshot, candidates, route):
                break
        winner = self._winner(result, candidates, explanations)
        target = self._target(snapshot, winner)
        char = self._characterize(snapshot, target) if target else Characterization()
        if winner.pattern == PARTIAL and char.fact:  # no pattern explains it: the picked fact is the mechanism
            winner = Explanation(
                key=f"key-fact:{target}",
                pattern="key-fact",
                origin=winner.origin,
                mechanism=f"{char.fact}.",
                values=[char.fact],
                contributing=winner.contributing,
                impact=winner.impact,
            )
        fault_object = self._fault_object(snapshot, winner, explanations, target)
        uncertain = route != "accept"
        text = build_report(
            [winner],
            observed=self._observed(snapshot, winner),
            uncertain=uncertain,
            key_fact=char.fact,
            fault_type=char.category,
            fault_object=fault_object,
        )
        probs = result.probabilities if result else {}
        return Diagnosis(
            component=self._component_of(snapshot, winner),
            component_result=ChoiceResult(
                choice=winner.key,
                confidence=round(probs.get(winner.key, 0.0), 4),
                probabilities={k: round(v, 4) for k, v in probs.items()},
            ),
            fault_visible=None,
            category_result=None,
            evidence_result=None,
            evidence=[],
            model=str(self.requests[-1].get("model") or "") if self.requests else "",
            input_tokens=_sum_usage("input_tokens", self.requests),
            output_tokens=_sum_usage("output_tokens", self.requests),
            investigation={
                "rounds": rounds,
                "explanations": len(explanations),
                "winner": winner.key,
                "pattern": winner.pattern,
                "key_fact": char.fact,
                "key_fact_probability": char.fact_probability,
                "fault_type": char.category,
                "fault_object": fault_object.origin if fault_object else None,
                "uncertain": uncertain,
            },
            text=text,
        )

    def _read(self, snapshot: ClusterSnapshot, cids: list[str], route: str) -> bool:
        """Fetch detail for components not read yet; returns whether anything new was read."""
        added = False
        for cid in cids:
            if self.fetch_detail is None or not cid or cid in self.details:
                continue
            try:
                self.details[cid] = self.fetch_detail(snapshot, cid)
            except Exception as exc:  # noqa: BLE001 - a failed read is recorded, never fatal
                self.details[cid] = {"error": f"{type(exc).__name__}: {exc}"}
            self._trace("collect", "detail", component=cid, route=route)
            added = True
        return added

    @staticmethod
    def _involved(snapshot: ClusterSnapshot, candidates: list[Explanation]) -> list[str]:
        """Components the candidates are about (origin, contributing, impact), in candidate order."""
        by_object = {
            f"{c.get('kind', '').lower()}/{c.get('namespace')}/{c.get('name')}": cid
            for cid, c in snapshot.components.items()
        }
        out: list[str] = []
        for e in candidates:
            for obj in [e.origin, *e.contributing, *e.impact]:
                cid = by_object.get(obj)
                if cid and cid not in out:
                    out.append(cid)
        return out

    def _target(self, snapshot: ClusterSnapshot, winner: Explanation) -> str | None:
        """The component to characterize: the origin when it is a component, else the first one it impacts."""
        for cid in self._involved(snapshot, [winner]):
            return cid
        return None

    def _characterize(self, snapshot: ClusterSnapshot, cid: str) -> Characterization:
        """v1's characterization step: read the component, list its facts with the team's detail_evidence, ask for
        the fact that reveals the mechanism and the fault category (CHARACTERIZE_REQUESTS extra requests)."""
        comp = snapshot.components.get(cid)
        if comp is None or len(self.requests) + 2 > self.max_requests + CHARACTERIZE_REQUESTS:
            return Characterization()
        self._read(snapshot, [cid], "characterize")
        facts = [item["text"] for item in detail_evidence(comp, self.details.get(cid) or {})]
        if not facts:
            return Characterization()
        state = {"application": snapshot.app, "component": self._component_view(snapshot, cid)}
        char = characterize(self._ask, state, facts, rng=self.rng)
        self._trace("decide", "characterize", component=cid, fact=char.fact, category=char.category)
        return char

    @staticmethod
    def _fault_object(
        snapshot: ClusterSnapshot, winner: Explanation, explanations: list[Explanation], cid: str | None
    ) -> Explanation | None:
        """An outside object (change or check finding) linked to the root-cause component, when the winner is that
        component itself: v1's "Fault object" line."""
        if not cid or winner.pattern not in (PARTIAL, "key-fact", "own-setting", "change", "dependency", "arrival"):
            return None
        comp = snapshot.components.get(cid) or {}
        me = f"{comp.get('kind', '').lower()}/{comp.get('namespace')}/{comp.get('name')}"
        if winner.origin != me:
            return None
        linked = [
            e
            for e in shortlist(explanations, len(explanations))
            if e.pattern == "acts-on" and e.origin != me and me in e.impact and not e.contradicted()
        ]
        return linked[0] if linked else None

    @staticmethod
    def _winner(result, candidates: list[Explanation], explanations: list[Explanation]) -> Explanation:
        """The averaged leader; when "none of these" leads, the best-supported partial finding, never a chain
        the model just rejected."""
        by_key = {e.key: e for e in explanations}
        if result is not None and result.leader != NONE:
            return by_key[result.leader]
        partials = [e for e in shortlist(explanations, len(explanations)) if e.pattern == PARTIAL]
        return partials[0] if partials else candidates[0]

    @staticmethod
    def _component_of(snapshot: ClusterSnapshot, e: Explanation) -> str:
        for cid, c in snapshot.components.items():
            if f"{c.get('kind', '').lower()}/{c.get('namespace')}/{c.get('name')}" == e.origin:
                return cid
        return e.origin

    def _observed(self, snapshot: ClusterSnapshot, e: Explanation) -> list[str]:
        """A few observed facts behind the winner: its values, then the origin's and affected components' signals."""
        lines = list(e.values)
        cid = self._component_of(snapshot, e)
        lines += [s for s in (snapshot.components.get(cid) or {}).get("signals") or []][:3]
        for obj in e.impact:
            other = self._component_of(snapshot, Explanation(key="", pattern="", origin=obj, mechanism=""))
            sig = ((snapshot.components.get(other) or {}).get("signals") or [])[:1]
            lines += [f"{other}: {s}" for s in sig]
        return list(dict.fromkeys(lines))[:10]
