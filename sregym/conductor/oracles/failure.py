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


#: Reason codes that recur across oracles, mapped once here so thirty-odd files
#: do not each invent a spelling for "the Deployment never came back".
#: Oracle-specific reasons belong in that oracle's own ``FAILURE_CLASSES``.
#:
#: The rule used to place a reason here, arrived at by sweeping every oracle
#: rather than guessed up front, is **who could have produced this state**:
#:
#: - Only an *actor* can delete an object, set replicas to zero, or edit a spec.
#:   Kubernetes does not do these things spontaneously. If the problem's own
#:   fault injection did not do it, the agent did -- so these are AGENT_ERROR.
#: - A stalled rollout or absent endpoint can also result from an agent's edits.
#:   These symptoms alone do not establish an environmental cause.
#: - Where both are plausible, AMBIGUOUS. Separating agent destruction from an
#:   incomplete deploy needs a *before* observation, which is what the
#:   environment-health precondition would add and which no oracle has today.
SHARED_FAILURE_CLASSES = {
    # -- The agent left the fault in place, or broke something --------------
    # Reserved for checks that positively confirm the injected fault, as opposed
    # to observing a symptom that has other possible causes.
    "fault_still_present": FailureClass.AGENT_ERROR,
    # Nothing environmental scales a Deployment to zero. No controller in the
    # baseline does it either -- the HPAs in these apps have minReplicas >= 1.
    # So this is an action, and if the problem did not take it, the agent did.
    # It is also the classic way to make a health check pass by demolition.
    "required_deployment_scaled_to_zero": FailureClass.AGENT_ERROR,
    # Likewise: a replica count below one is a spec edit, not a symptom.
    "invalid_replica_count": FailureClass.AGENT_ERROR,
    # -- The environment could not host the work ---------------------------
    # Observability we grade *with* rather than grade. If Prometheus is
    # unreachable the oracle has no evidence, and that is the cluster's problem,
    # not the model's. This is the single most common cause of a spurious
    # mitigation failure, because AlertOracle backs 23 problems.
    "prometheus_unreachable": FailureClass.ENVIRONMENT_ERROR,
    # The API server refused or could not answer. Whatever the agent did, we
    # could not observe it.
    "kubernetes_api_error": FailureClass.ENVIRONMENT_ERROR,
    # -- The harness itself failed -----------------------------------------
    "oracle_raised": FailureClass.HARNESS_ERROR,
    # -- Undecidable -------------------------------------------------------
    # An object that is *gone* is the hard case, and the reason this table is
    # not simply split in two. A missing Deployment or namespace may be agent
    # destruction, or a deploy that never completed -- and at judgement time,
    # with no pre-mitigation observation to compare against, those look
    # identical. Calling them environmental would quietly forgive an agent that
    # deleted its way to a passing health check; calling them agent errors would
    # blame the model for a broken deploy. Neither is honest yet.
    "required_deployment_missing": FailureClass.AMBIGUOUS,
    "required_deployment_not_rolled_out": FailureClass.AMBIGUOUS,
    "kubernetes_resource_missing": FailureClass.AMBIGUOUS,
    "kubernetes_request_failed": FailureClass.AMBIGUOUS,
    "oracle_command_failed": FailureClass.AMBIGUOUS,
    "namespace_missing": FailureClass.AMBIGUOUS,
    "no_deployments_found": FailureClass.AMBIGUOUS,
    "no_pods_found": FailureClass.AMBIGUOUS,
    "no_matching_pods": FailureClass.AMBIGUOUS,
    # Symptoms with more than one plausible cause. A Service with no ready
    # endpoints may be the model's doing or a node that lost its network
    # sandbox; the oracle cannot tell from here.
    "no_ready_endpoints": FailureClass.AMBIGUOUS,
    "no_active_replicaset": FailureClass.AMBIGUOUS,
    "connectivity_probe_failed": FailureClass.AMBIGUOUS,
    "pods_not_ready": FailureClass.AMBIGUOUS,
    "deployment_replicas_unready": FailureClass.AMBIGUOUS,
    # A rollout that has not converged *and* we did not wait: distinct from
    # ``required_deployment_not_rolled_out``, which waited out a timeout.
    "rollout_not_settled": FailureClass.AMBIGUOUS,
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
