"""v1's unit tests, run against v2's copies of the same modules (the copies must keep v1's behaviour)."""

import pathlib
import re

_V1 = pathlib.Path(__file__).with_name("test_jev_diag.py")
_MODULES = "classifier|collector|derive|investigate|tree|checks|changes|probes|trend"
_SRC = re.sub(rf"clients\.jev_diag(\.(?:{_MODULES})\b| import )", r"clients.jev_diag.v2\1", _V1.read_text())
# v2 has no one-shot evidence builders and no fault-visible question (see _INTENDED_DIFFERENCES); v1's import list
# names them
_SRC = re.sub(r"\n    (?:build_characterization_questions|build_evidence|FAULT_VISIBLE_QUESTION),", "", _SRC)
exec(compile(_SRC, "test_jev_diag[v2]", "exec"))  # noqa: S102 - defines the same test functions in this module

# v1 tests whose behaviour v2 changes on purpose; each is replaced by a v2 test named beside it.
_INTENDED_DIFFERENCES = {
    # an investigation's evidence option shows a bounded excerpt of its item, so the key evidence is chosen on
    # content (test_jev_diag_v2.py: test_evidence_options_show_a_bounded_excerpt_and_other_options_reference_state)
    "test_question_options_reference_state_instead_of_copying_evidence",
    # v2 has no one-shot path (one evidence request, then text from typed answers): its diagnosis is the iterative
    # examination in tree.py, tested in test_jev_diag_v2.py
    "test_evidence_and_characterisation_questions",
    "test_diagnose_runs_two_requests_and_builds_text_from_typed_answers",
    "test_low_confidence_and_other_paths_are_reported",
    "test_diagnose_rejects_empty_snapshot",
    # an unlevelled line saying a consumer paused is not a failure outcome in v2; a stuck consumer is measured instead
    # (test_jev_diag_v2_fixes.py: test_explicit_failures_count_at_info_level_and_normal_logs_do_not)
    "test_log_filter_keeps_explicit_failures_even_at_info_level_and_rejects_normal_logs",
    # triage asks no fault-visible question: no decision used its answer
    # (test_jev_diag_v2_decision.py: test_triage_asks_for_the_component_and_the_fault_object_kind_only)
    "test_component_question_offers_every_component_plus_other_and_serialises",
}
for _name in _INTENDED_DIFFERENCES:
    del globals()[_name]
