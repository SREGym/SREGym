"""OpenSRE agent client for SREGym.

Runs the SREGym benchmark with OpenSRE as the diagnosis "planner" and any
existing SREGym coding agent as the mitigation "executor" (planner -> executor).

OpenSRE is consumed only as the installed `opensre` CLI on PATH — this package
never imports from or edits the OpenSRE codebase.
"""
