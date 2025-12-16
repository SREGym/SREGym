# Mitigation Agent Prompt and Problem Example

## Original Mitigation Agent Prompt (Snippet)

The following is a snippet of the original mitigation agent prompt from `clients/stratus/configs/mitigation_agent_prompts.yaml`. This shows the core system prompt before any learned insights are added.

```yaml
system: "Mitigate the identified faults in an IT incident. Some or none of the microservices have faults. Get all the pods and deployments to figure out what kind of services are running in the cluster if you don't know what the services are. You should carefully identify the whether the faults are present and if they are, what is the root cause of the fault. You can stop mitigation once you've fixed all the faults. 

Go as deep as you can into what is causing the issue, and mitigate the fault.

Your instructions to the tools must be clear and concise. Your queries to tools need to be single turn.

Remember to check these, and remember this information:

## Workloads (Applications)
- **Pod**: The smallest deployable unit in Kubernetes, representing a single instance of a running application. Can contain one or more tightly coupled containers.
- **ReplicaSet**: Ensures that a specified number of pod replicas are running at all times. Often managed indirectly through Deployments.
- **Deployment**: Manages the deployment and lifecycle of applications. Provides declarative updates for Pods and ReplicaSets.
- **StatefulSet**: Manages stateful applications with unique pod identities and stable storage. Used for workloads like databases.
- **DaemonSet**: Ensures that a copy of a specific pod runs on every node in the cluster. Useful for node monitoring agents, log collectors, etc.
- **Job**: Manages batch processing tasks that are expected to complete successfully. Ensures pods run to completion.
- **CronJob**: Schedules jobs to run at specified times or intervals (similar to cron in Linux).

## Networking
- **Service**: Provides a stable network endpoint for accessing a group of pods. Types: ClusterIP, NodePort, LoadBalancer, and ExternalName.
- **Ingress**: Manages external HTTP(S) access to services in the cluster. Supports routing and load balancing for HTTP(S) traffic.
- **NetworkPolicy**: Defines rules for network communication between pods and other entities. Used for security and traffic control.

## Storage
- **PersistentVolume (PV)**: Represents a piece of storage in the cluster, provisioned by an administrator or dynamically.
- **PersistentVolumeClaim (PVC)**: Represents a request for storage by a user. Binds to a PersistentVolume.
- **StorageClass**: Defines different storage tiers or backends for dynamic provisioning of PersistentVolumes.
- **ConfigMap**: Stores configuration data as key-value pairs for applications.
- **Secret**: Stores sensitive data like passwords, tokens, or keys in an encrypted format.

## Configuration and Metadata
- **Namespace**: Logical partitioning of resources within the cluster for isolation and organization.
- **ConfigMap**: Provides non-sensitive configuration data in key-value format.
- **Secret**: Stores sensitive configuration data securely.
- **ResourceQuota**: Restricts resource usage (e.g., CPU, memory) within a namespace.
- **LimitRange**: Enforces minimum and maximum resource limits for containers in a namespace.

## Cluster Management
- **Node**: Represents a worker machine in the cluster (virtual or physical). Runs pods and is managed by the control plane.
- **ClusterRole and Role**: Define permissions for resources at the cluster or namespace level.
- **ClusterRoleBinding and RoleBinding**: Bind roles to users or groups for authorization.
- **ServiceAccount**: Associates processes in pods with permissions for accessing the Kubernetes API.

An example procedure to remediate the faults:
1) Formulate a remediation plan with a list of actionable steps.
2) Execute the plan, one step at a time.
3) Check if the plan execution worked as you desired in the IT environment.
4) If not, you can either call wait_tool to wait for it to take effect or take other actions.
5) Otherwise, continue the plan and execution process until you call submit_tool as you believe the application has become healthy.

The following is a detailed description of your tasks.

1) mitigation: Mitigate the identified faults in an IT incident with the provided tools. You can submit an empty dict \"ans\" with the submit_tool as this task is not graded over your answer but the final result of the mitigation; therefore, you have to make sure the application has become healthy before you call submit_tool."
```

**Note:** In the actual prompt file, this is followed by a "## Learned Insights" section that contains all the learned points (which are added during the learning process).

---

## Problem Summary: `misconfig_app_hotel_res`

### Problem Overview

**Problem ID:** `misconfig_app_hotel_res`  
**Problem Name:** Misconfiguration - Hotel Reservation App Mitigation  
**Description:** Application misconfiguration in hotel reservation service requiring mitigation

### Technical Details

**Application:** Hotel Reservation (`HotelReservation`)  
**Faulty Service:** `geo` (geolocation service)  
**Namespace:** Hotel Reservation application namespace

### Root Cause

The `geo` deployment is configured to use a **buggy container image** `yinfangchen/geo:app3`. This causes the pod to:
- Keep restarting continuously
- Enter the 'Error' state
- Fail to provide the geolocation service functionality

### Fault Injection

The fault is injected using the `ApplicationFaultInjector` with:
- **Fault Type:** `misconfig_app`
- **Target Microservice:** `geo`
- **Method:** Changes the deployment to use the buggy container image

### Expected Mitigation

The mitigation agent should:
1. **Identify** that the `geo` pod is in an Error state and restarting
2. **Diagnose** the root cause by checking:
   - Pod status and events (`kubectl describe pod`)
   - Deployment configuration (`kubectl get deployment geo`)
   - Container image being used
3. **Fix** the issue by:
   - Updating the deployment to use the correct container image
   - Rolling out the fix (may require deleting the faulty pod or updating the deployment)
4. **Verify** that:
   - The pod is running successfully
   - The service is healthy
   - No more restart loops

### Problem Class Structure

```python
class MisconfigAppHotelRes(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "geo"
        self.root_cause = "The 'geo' deployment is configured to use a buggy container image 'yinfangchen/geo:app3', this causes the pod keep restarting and entering the 'Error' state."
        
    def inject_fault(self):
        # Injects misconfig_app fault into geo service
        
    def recover_fault(self):
        # Recovers the fault by restoring correct configuration
```

### Evaluation

- **Diagnosis Oracle:** Uses `LLMAsAJudgeOracle` to evaluate if the agent correctly identifies the root cause
- **Mitigation Oracle:** Uses `MitigationOracle` to evaluate if the agent successfully fixes the issue

### Key Challenges for the Agent

1. **Identifying the Problem:**
   - Must recognize pod restart loops as a symptom
   - Must check pod events to see the error pattern
   - Must examine deployment configuration to find the misconfiguration

2. **Root Cause Analysis:**
   - Must connect the pod failures to the container image
   - Must understand that `yinfangchen/geo:app3` is the problematic image
   - Must identify what the correct image should be

3. **Mitigation:**
   - Must update the deployment with the correct image
   - Must ensure the change takes effect (may need to wait or force rollout)
   - Must verify the fix worked

### Example Agent Workflow

```
1. Check pod status: kubectl get pods -n <namespace>
   → See geo pod in Error/CrashLoopBackOff state

2. Describe pod: kubectl describe pod geo-xxx -n <namespace>
   → See events showing container crashes
   → See image: yinfangchen/geo:app3

3. Check deployment: kubectl get deployment geo -n <namespace> -o yaml
   → Confirm image is yinfangchen/geo:app3

4. Update deployment: kubectl set image deployment/geo geo=<correct-image> -n <namespace>
   OR
   Edit deployment: kubectl edit deployment geo -n <namespace>
   → Change image to correct version

5. Verify: kubectl get pods -n <namespace>
   → See new pod starting with correct image
   → Wait for pod to be Running

6. Submit: submit_tool with confirmation that fault is resolved
```

---

## Problem Set Overview

The learning system runs 7 problems (the user mentioned "out of 5" but the code shows 7):

1. **social_net_hotel_res_astro_shop_concurrent_failures** - Concurrent failures across multiple services
2. **misconfig_app_hotel_res** - Application misconfiguration (described above)
3. **revoke_auth_mongodb-1** - MongoDB authentication permissions revoked
4. **astronomy_shop_ad_service_high_cpu** - High CPU usage in ad service
5. **valkey_memory_disruption** - Valkey memory disruption
6. **network_policy_block** - Network policy blocking communication
7. **duplicate_pvc_mounts_hotel_reservation** - Duplicate PVC mounts causing storage conflicts

Each problem tests different aspects of the agent's mitigation capabilities, from configuration issues to resource problems to network and storage issues.

