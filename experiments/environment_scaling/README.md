# Environment-scaling experiments

Application code is in `sregym/service/apps/`, grouped by application.
Fault definitions and oracles remain in the Conductor.

`evaluate.py` compares registered problems through the standard runner.
It rejects unregistered variants before building images or starting validation.
`prepare.py` builds the pinned Stripe reference image for KIND.

`evidence/` contains the original experiment reports, including superseded results.
Recorded paths and commands refer to the original checkout.
These reports do not define runtime registration policy.
