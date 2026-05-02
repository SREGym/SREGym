# Hardware-problem validation summary

_Run finished at_ `2026-05-02T00:13:46Z`

| Problem ID | Bit app? | Alert names | Pod buckets at peak | Notes |
|---|---|---|---|---|
| nic_packet_corruption | ✅ | KubePodNotReady, PendingPodsDetected, PodStatusError | Running(1/1):20 |  |
| storage_controller_read_failure | ✅ | KubePodNotReady, PendingPodsDetected, PodStatusError | Running(1/1):20, __restarts__:3 |  |
| storage_write_failure | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):17, __restarts__:14, Running(0/1):3 |  |
| dram_module_failure | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):18, __restarts__:19, Running(0/1):2 |  |
| cpu_clocksource_failure | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):19, __restarts__:21, Running(0/1):1 |  |
| mmu_page_protection_failure | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):17, __restarts__:22, Running(0/1):3 |  |
| network_interface_link_down | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):19, __restarts__:25, Running(0/1):1 |  |
| dns_resolver_hardware_failure | ✅ | DeploymentNotReady, KubePodNotReady, PendingPodsDetected, PodStatusError, ServiceEndpointDown | Running(1/1):18, __restarts__:26, Running(0/1):2 |  |
