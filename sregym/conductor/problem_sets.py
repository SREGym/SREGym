"""Named sets of registered SREGym problems."""

SREGYM_LITE_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "mutating_webhook_resource_limits_social_network",
    "internal_traffic_policy_local_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
)

# SREGym-Lite's faults re-targeted at the larger Incident Arena apps (Frappe,
# Saleor, Slack Spine), in Lite order; see problems/environment_ports.py.
SREGYM_LITE_IA_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_frappe",
    "mutating_webhook_resource_limits_slack_spine",
    "internal_traffic_policy_local_slack_spine",
    "unschedulable_incorrect_port_assignment_frappe",
)

PROBLEM_SETS = {
    "sregym-lite": SREGYM_LITE_PROBLEMS,
    "sregym-lite-ia": SREGYM_LITE_IA_PROBLEMS,
}
