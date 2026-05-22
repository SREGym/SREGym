## Description

<!-- What does this PR change, and why? Reference any related issues. -->

## Problem Validation

<!--
If this PR ADDS or MODIFIES a problem, set the registered problem ID below — the
dictionary key from `sregym/conductor/problems/registry.py` (e.g. `incorrect_image`).

CI will then deploy the app, inject the fault, and verify that the mitigation
oracle fails while the fault is live and passes again after recovery. See
`.github/workflows/problem-validation.yml`.

Rules:
  - Leave the value as `none` if this PR does not add or change a problem.
  - Use exactly one ID per PR (one problem per PR keeps validation fast).
  - You can edit this field after opening the PR — the check re-runs automatically.
-->

Problem ID: none

## How was this tested?

<!-- Describe manual testing, the cluster used, etc. Automated validation does not replace human review. -->

## Checklist

- [ ] Code follows the project style guidelines (prek hooks pass)
- [ ] For a new or modified problem: the **Problem ID** above is set and the **Problem Validation** check is green
- [ ] Documentation updated if needed
- [ ] PR description explains the changes
