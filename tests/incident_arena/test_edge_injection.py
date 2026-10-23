from sregym.conductor.problems.apps.frappe.configuration import EdgeRequestFilterIA
from sregym.conductor.problems.edge_request_filter_cpu_saturation import EdgeRequestFilterCPUSaturation


def test_frappe_and_original_use_the_same_checked_launcher(offline_port_cluster):
    frappe = EdgeRequestFilterIA()
    assert type(frappe)._start_crafted_traffic is EdgeRequestFilterCPUSaturation._start_crafted_traffic
    assert frappe.crafted_payload == "a" * 5000 + "!"
    assert frappe.app.deploy_overrides["loadgen"]["target"] == "http://erp:8080"
    assert frappe.app.deploy_overrides["erpnext"]["nginx"]["environment"]["frappeSiteNameHeader"] == "svc-frappe-web"
