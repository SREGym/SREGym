"""Applications that keep their machinery but stop naming it in the description.

Screening showed that a clause in the application description is worth real
difficulty. `stripe_feature_config` was run twice, once with a nine-word clause
naming its operator workspace and once without: 3 of 3 at 154s median became
2 of 3 at 237s, every attempt slower, and the failing attempt stopped after
repairing the visible fault.

That makes naming a subsystem a lever, and the families solved 3 of 3 all name
theirs. These variants remove exactly one true sentence each. Nothing is hidden:
every deployment, service, volume and endpoint is still there and still visible
to `kubectl`. The task changes from "reconcile the mail" to "notice that there is
mail", which is the part a real responder has to get right.

A removal that silently matched nothing would be the worst outcome -- the problem
would look like a disclosure experiment while being identical to its parent, and
the screen would measure nothing. So each class declares the exact clause it
removes and raises if it is not there.
"""


class Unannounced:
    """Remove ``REMOVED_CLAUSE`` from the inherited description, or fail loudly."""

    #: The exact substring the parent appends and this variant withholds.
    REMOVED_CLAUSE = ""

    def get_app_json(self):
        result = super().get_app_json()
        clause = self.REMOVED_CLAUSE
        if not clause or clause not in result["Desc"]:
            raise RuntimeError(
                f"{type(self).__name__} no longer removes anything: the parent description "
                f"does not contain the declared clause"
            )
        result["Desc"] = result["Desc"].replace(clause, "")
        return result
