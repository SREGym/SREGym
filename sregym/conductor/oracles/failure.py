"""The vocabulary for saying *why* an oracle failed, and whose fault it was.

A failed mitigation used to read identically whether the model did the wrong
thing or the cluster was broken underneath it. That is a benchmark-validity
problem rather than a reporting nicety: flaky infrastructure depresses scores
in a way indistinguishable from model incapability, and ``sregym/results/report.py``
folds those runs into its saturation decision as if they were difficulty.

Oracles supply a stable ``reason`` code; this module maps reasons to one of four
classes. The mapping is data rather than logic because it is a judgement the
oracle author is best placed to make, and judgements should be reviewable in one
place.
"""


class FailureClass:
    """Who or what a failure is attributable to.

    Deliberately four values and no more. ``AMBIGUOUS`` is a first-class answer,
    not a placeholder: when a connectivity probe fails, the honest verdict often
    *is* "cannot tell", and forcing that into either bucket is worse than
    admitting it. A problem whose failures are mostly ambiguous is telling you
    its oracle needs a better precondition -- which is useful signal, and is how
    the environment-health precondition gets prioritised.
    """

    #: The agent did the wrong thing, or did not do the right thing. The only
    #: class that should count against a model's score.
    AGENT_ERROR = "agent_error"

    #: The cluster was not fit to host the mitigation. Not the model's fault;
    #: these attempts should be excluded from scoring, not counted as failures.
    ENVIRONMENT_ERROR = "environment_error"

    #: SREGym's own fault -- an oracle raised, a helper had a bug. Never
    #: attributable to the model, and distinct from ENVIRONMENT_ERROR because it
    #: implicates our code rather than the infrastructure.
    HARNESS_ERROR = "harness_error"

    #: Genuinely undecidable from the oracle's vantage point.
    AMBIGUOUS = "ambiguous"


#: Every class, for validation and for tests that pin the closed set.
ALL_CLASSES = frozenset(
    {
        FailureClass.AGENT_ERROR,
        FailureClass.ENVIRONMENT_ERROR,
        FailureClass.HARNESS_ERROR,
        FailureClass.AMBIGUOUS,
    }
)


#: Precedence when several failures must collapse into one verdict, most
#: tainting first. Read it as "how much does this failure undermine our ability
#: to judge the model at all":
#:
#: - ENVIRONMENT_ERROR wins outright. If the cluster could not host the
#:   mitigation then the fault still being present tells us nothing about the
#:   model, so an environmental failure dominates even a decisive agent one.
#: - HARNESS_ERROR next, for the same reason one step closer to home: if our
#:   own code failed we have no verdict to report.
#: - AGENT_ERROR beats AMBIGUOUS, because a check that positively confirms the
#:   injected fault is still present is decisive evidence, and should not be
#:   diluted by a sibling check that merely could not tell.
_PRECEDENCE = (
    FailureClass.ENVIRONMENT_ERROR,
    FailureClass.HARNESS_ERROR,
    FailureClass.AGENT_ERROR,
    FailureClass.AMBIGUOUS,
)


#: Reason codes that recur across oracles, mapped once here so twenty-eight
#: files do not each invent a spelling for "the Deployment never came back".
#: Oracle-specific reasons belong in that oracle's own ``FAILURE_CLASSES``.
SHARED_FAILURE_CLASSES = {
    # -- The environment could not host the work ------------------------------
    # A workload the problem depends on but did not deliberately break. If it is
    # missing, scaled away or never rolled out, the mitigation had no chance.
    "required_deployment_missing": FailureClass.ENVIRONMENT_ERROR,
    "required_deployment_scaled_to_zero": FailureClass.ENVIRONMENT_ERROR,
    "required_deployment_not_rolled_out": FailureClass.ENVIRONMENT_ERROR,
    "namespace_missing": FailureClass.ENVIRONMENT_ERROR,
    "no_deployments_found": FailureClass.ENVIRONMENT_ERROR,
    # Observability we grade *with* rather than grade. If Prometheus is
    # unreachable the oracle has no evidence, and that is the cluster's problem,
    # not the model's. This is the single most common cause of a spurious
    # mitigation failure, because AlertOracle backs 23 problems.
    "prometheus_unreachable": FailureClass.ENVIRONMENT_ERROR,
    # -- The harness itself failed -------------------------------------------
    "oracle_raised": FailureClass.HARNESS_ERROR,
    # -- The agent left the fault in place -----------------------------------
    # Reserved for checks that positively confirm the injected fault, as opposed
    # to observing a symptom that has other possible causes.
    "fault_still_present": FailureClass.AGENT_ERROR,
    # -- Undecidable ----------------------------------------------------------
    # Symptoms with more than one plausible cause. A Service with no ready
    # endpoints may be the model's doing or a node that lost its network
    # sandbox; the oracle cannot tell from here.
    "no_ready_endpoints": FailureClass.AMBIGUOUS,
    "no_active_replicaset": FailureClass.AMBIGUOUS,
    "connectivity_probe_failed": FailureClass.AMBIGUOUS,
    "pods_not_ready": FailureClass.AMBIGUOUS,
}


def classify(reason: str, overrides: dict | None = None) -> str:
    """Return the failure class for *reason*.

    *overrides* is an oracle's own mapping, consulted before the shared one so a
    single oracle can take a different view of a shared reason code without
    editing the shared table.

    An unmapped reason classifies as ``AMBIGUOUS`` rather than raising. Adding a
    new reason code must never be able to fail a run -- the classification is
    metadata, and metadata should not be load-bearing for whether a run
    completes.
    """
    if overrides and reason in overrides:
        return overrides[reason]
    return SHARED_FAILURE_CLASSES.get(reason, FailureClass.AMBIGUOUS)


def worst(classes) -> str:
    """Collapse several failure classes into the one that dominates.

    Used where a verdict aggregates children -- see ``CompoundedOracle``. Empty
    input yields ``AMBIGUOUS``: something failed (or we would not be here) but
    nothing said why.
    """
    present = set(classes)
    for candidate in _PRECEDENCE:
        if candidate in present:
            return candidate
    return FailureClass.AMBIGUOUS
