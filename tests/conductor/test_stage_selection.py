"""Stage selection: which stages a run attempts, and how that interacts with oracles.

Two separate questions meet in `_build_stage_sequence`. Which stages the run
*wants* comes from `ConductorConfig.stages`, the legacy `tasklist.yml`, or the
default. Which stages *can* run is decided by the oracles the problem attaches.
An explicitly requested stage with no oracle is a conflict; the same stage
arriving from a default is not.
"""

import pytest

from sregym.conductor.conductor import ALL_STAGES, ConductorConfig


class FakeProblem:
    """A problem with only the attributes stage selection looks at."""

    def __init__(self, *, diagnosis=True, mitigation=True):
        self.diagnosis_oracle = object() if diagnosis else None
        self.mitigation_oracle = object() if mitigation else None


@pytest.fixture
def conductor_factory(monkeypatch):
    """Build a Conductor without touching a cluster.

    __init__ constructs the whole service fleet (kubectl, Prometheus, Loki, the
    k8s proxy...), none of which stage selection needs.
    """
    from sregym.conductor import conductor as conductor_mod

    def factory(*, stages=None, problem=None, problem_id="fake_problem"):
        monkeypatch.setattr(conductor_mod.Conductor, "__init__", lambda self, config=None: None)
        c = conductor_mod.Conductor()
        c.config = ConductorConfig(stages=stages)
        c.logger = conductor_mod.logging.getLogger("test.stage_selection")
        c.problem = problem if problem is not None else FakeProblem()
        c.problem_id = problem_id
        c.tasklist = None
        # _build_stage_sequence needs these to exist; it resets them itself.
        c.stage_sequence = []
        c.current_stage_index = 0
        c._evaluate_diagnosis = lambda *a, **k: None
        c._evaluate_mitigation = lambda *a, **k: None
        return c

    return factory


def _stage_names(conductor):
    return [stage["name"] for stage in conductor.stage_sequence]


def test_unset_stages_runs_everything(conductor_factory):
    c = conductor_factory(stages=None)
    c.get_problem_stages()
    c._build_stage_sequence()
    assert c.tasklist == list(ALL_STAGES)
    assert _stage_names(c) == ["diagnosis", "mitigation"]


def test_diagnosis_only(conductor_factory):
    c = conductor_factory(stages=("diagnosis",))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == ["diagnosis"]


def test_mitigation_only(conductor_factory):
    c = conductor_factory(stages=("mitigation",))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == ["mitigation"]


def test_both_stages_explicitly(conductor_factory):
    c = conductor_factory(stages=("diagnosis", "mitigation"))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == ["diagnosis", "mitigation"]


def test_out_of_order_stages_are_rejected(conductor_factory):
    c = conductor_factory(stages=("mitigation", "diagnosis"))
    with pytest.raises(ValueError, match="in that order"):
        c.get_problem_stages()


def test_explicit_stage_without_an_oracle_is_an_error(conductor_factory):
    """The case that would otherwise report a successful, empty run."""
    c = conductor_factory(stages=("mitigation",), problem=FakeProblem(mitigation=False))
    c.get_problem_stages()
    with pytest.raises(ValueError) as excinfo:
        c._build_stage_sequence()
    # The message has to name both, or it is not actionable.
    assert "mitigation" in str(excinfo.value)
    assert "fake_problem" in str(excinfo.value)


def test_implicit_stage_without_an_oracle_is_skipped_quietly(conductor_factory):
    """Unchanged behaviour when the stage came from the default, not the CLI."""
    c = conductor_factory(stages=None, problem=FakeProblem(mitigation=False))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == ["diagnosis"]


@pytest.fixture
def tasklist_yml(tmp_path, monkeypatch):
    """Point get_problem_stages' tasklist.yml lookup at a temporary file.

    It resolves the path as Path(__file__).resolve().parent / "tasklist.yml",
    so redirecting Path is enough to relocate it.
    """
    from sregym.conductor import conductor as conductor_mod

    def write(body: str):
        (tmp_path / "tasklist.yml").write_text(body)
        monkeypatch.setattr(conductor_mod, "Path", lambda *_a, **_k: tmp_path / "conductor.py")

    return write


ONE_STAGE_TASKLIST = "all:\n  problems:\n    fake_problem:\n      - mitigation\n"


def test_tasklist_yml_is_still_honoured_when_stages_is_unset(conductor_factory, tasklist_yml):
    """The legacy path is kept deliberately, so it needs to keep working."""
    tasklist_yml(ONE_STAGE_TASKLIST)
    c = conductor_factory(stages=None)
    c.get_problem_stages()
    assert c.tasklist == ["mitigation"]


def test_config_stages_take_precedence_over_tasklist_yml(conductor_factory, tasklist_yml):
    """Same file as above, but --stages overrides it rather than merging."""
    tasklist_yml(ONE_STAGE_TASKLIST)
    c = conductor_factory(stages=("diagnosis",))
    c.get_problem_stages()
    assert c.tasklist == ["diagnosis"]


def test_tasklist_stages_are_not_treated_as_explicit(conductor_factory, tasklist_yml):
    """A file-specified stage with no oracle skips quietly rather than raising.

    Only stages named on the command line are explicit; promoting the file to
    explicit would turn a working tasklist.yml setup into a hard failure.
    """
    tasklist_yml(ONE_STAGE_TASKLIST)
    c = conductor_factory(stages=None, problem=FakeProblem(mitigation=False))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == []


def test_unknown_stage_in_tasklist_is_skipped(conductor_factory):
    """A stage name with no evaluator is dropped, not crashed on.

    Only reachable via tasklist.yml -- argparse `choices` rejects it on the CLI.
    """
    c = conductor_factory(stages=None)
    c.tasklist = ["diagnosis", "teleportation"]
    c._build_stage_sequence()
    assert _stage_names(c) == ["diagnosis"]


def test_no_available_stages_leaves_an_empty_sequence(conductor_factory):
    c = conductor_factory(stages=None, problem=FakeProblem(diagnosis=False, mitigation=False))
    c.get_problem_stages()
    c._build_stage_sequence()
    assert _stage_names(c) == []
