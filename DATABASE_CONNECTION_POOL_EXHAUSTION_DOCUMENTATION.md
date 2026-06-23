# Database Connection Pool Exhaustion - SREGym Benchmark Problem

## Real-World Failure Story

### Background
Database connection pooling is a critical optimization in modern applications. Connection pooling maintains a set of pre-established database connections that can be reused by applications, avoiding the overhead of creating new connections for every query. This is essential for performance in microservices architectures.

### The Failure
In real-world production environments, database connection pool exhaustion occurs when:

1. **Misconfiguration**: A service is deployed with an incorrectly low connection pool size (e.g., 5 connections instead of 50)
2. **Resource Constraints**: A shared database instance has limited connections available due to capacity planning issues
3. **Sudden Load Increase**: An unexpected spike in traffic causes all pooled connections to be in use simultaneously
4. **Connection Leaks**: Application bugs cause connections to not be properly returned to the pool, gradually starving new requests

### Impact Chain
- **Symptom 1**: Initial spike causes new requests to queue waiting for an available connection
- **Symptom 2**: As queue builds up, request timeouts increase (typically 30-60 seconds)
- **Symptom 3**: Clients receive `ConnectionTimeoutException` or HTTP 503 Service Unavailable errors
- **Symptom 4**: If connection acquisition timeout is very short (1-5s), immediate failures
- **Cascading Effect**: Dependent services calling this service also start failing
- **Result**: Complete service unavailability despite no actual database crash

### Real-World Examples
This type of failure has occurred at major companies:
- **Uber**: Connection pool exhaustion in Cassandra drivers during surge pricing events (2015-2017 era)
- **Stripe**: Database connection limits hit during high-frequency trading integration tests
- **Airbnb**: MongoDB connection pool exhaustion during flash sales
- **Common Pattern**: Most companies running microservices have experienced this at least once

### Why It's Hard to Debug
- The database itself appears healthy (CPU, memory, disk I/O all normal)
- `kubectl get nodes` shows all nodes in Ready state
- `kubectl get pods` shows all pods Running
- Logs might only show "connection timeout" without revealing the pool is exhausted
- SREs must understand connection pooling semantics, not just Kubernetes

---

## Simulation on SREGym

### Problem: `database_connection_pool_exhaustion_hotel_reservation`

#### Application Target
The Hotel Reservation application's `reservation` service, which handles booking operations and connects to MongoDB for data persistence.

#### Fault Mechanism
The problem injects connection pool constraints via environment variables:

```python
MONGODB_POOL_SIZE = 3              # Limit to only 3 concurrent connections
MONGODB_POOL_TIMEOUT_MS = 5000     # Wait 5 seconds max for a connection
MONGODB_WAIT_QUEUE_TIMEOUT_MS = 1000  # Queue wait timeout
```

#### Simulation Process

1. **Before Injection**: 
   - Reservation service operates normally with default MongoDB connection pool (typically 10-20 connections)
   - All requests complete quickly

2. **After Injection**:
   - Environment variables are added to the reservation deployment
   - Pod restarts with new constraints
   - New MongoDB connections are limited to 3 total
   - Any request exceeding pool capacity must wait up to 1 second, then times out

3. **Observable Symptoms**:
   - Pod CPU and memory appear normal
   - Pod restarts count increases (application times out and crashes)
   - Load generator reports connection timeouts
   - Reservations can no longer be made

#### Why This Works
- The Hotel Reservation application uses a standard MongoDB driver (likely Go driver or similar)
- Most drivers respect connection pool configuration via environment variables
- No modification to application code needed
- Purely a configuration/environment issue, just like production

---

## Expected Behavior During Run

### Diagnosis Phase
When the fault is injected and an AI agent investigates, it should observe:

```bash
$ kubectl get pods -n hotel-reservation
NAME                            READY   STATUS    RESTARTS   AGE
reservation-5f7d8c4a8b-abc12    1/1     Running   0          2m
mongodb-rate-0                  1/1     Running   0          10m

$ kubectl describe pod reservation-5f7d8c4a8b-abc12
# Events show normal startup, no obvious errors

$ kubectl logs reservation-5f7d8c4a8b-abc12
# Contains sporadic "mongodb: connection pool exhausted" errors

$ kubectl get deployment reservation -o yaml | grep -A5 env:
# Shows MONGODB_POOL_SIZE=3, MONGODB_POOL_TIMEOUT_MS=5000
```

The key insight is that **the pod itself is healthy** - the issue is in how it's configured to interact with its dependency.

### Root Cause Identification
A skilled SRE or AI agent should identify:

1. **Component**: deployment/reservation
2. **Problem**: Environment variable configuration limiting connection pool
3. **Effect**: Database connection timeouts causing request failures
4. **Remedy**: Remove or increase the connection pool limits

### Mitigation Phase
To fix the problem, an agent should:

```bash
$ kubectl set env deployment/reservation MONGODB_POOL_SIZE-
$ kubectl set env deployment/reservation MONGODB_POOL_TIMEOUT_MS-
$ kubectl set env deployment/reservation MONGODB_WAIT_QUEUE_TIMEOUT_MS-
# Or: kubectl rollout restart deployment/reservation
```

---

## Oracle Behavior

### Diagnosis Oracle
- **Expected Root Cause**: Environment variables on reservation service limiting pool to 3 connections with 5s timeout
- **Oracle Type**: LLM-as-a-Judge
- **Evaluation**: Checks if the diagnosis correctly identifies:
  - Component affected (reservation service)
  - Type of issue (connection pool limit)
  - The timeout mechanism
  - Symptoms align with root cause

### Mitigation Oracle
- **Success Criteria**: All pods in hotel-reservation namespace in Running state with:
  - All deployments have desired replicas Ready
  - No pods in CrashLoopBackOff
  - No unavailable replicas
  - Pod restart count returns to baseline

---

## Testing Against AI Agents

When tested with Stratus or other AI agents, we expect:

1. **Good Agents** (High-confidence diagnosis):
   - Quickly identify environment variable configuration as root cause
   - Understand connection pool semantics
   - Remove the problematic env vars
   - Verify fix by checking pod logs decrease in timeout errors

2. **Baseline Agents** (Partial diagnosis):
   - Might initially check database connectivity (works fine)
   - Check CPU/Memory (normal)
   - Eventually discover env vars through `kubectl get deployment`
   - Successfully mitigate but with longer investigation time

3. **Weak Agents** (Incorrect diagnosis):
   - Might incorrectly scale up pods (doesn't fix the real problem)
   - Might blame MongoDB pod itself (irrelevant)
   - Might try to increase node resources (not the issue)

---

## Educational Value

This benchmark problem teaches:

1. **Microservices Troubleshooting**:
   - Not all outages are Kubernetes failures
   - Application-level resource limits are critical
   - Understanding your application's dependencies (MongoDB driver behavior)

2. **Connection Pooling Concepts**:
   - Pool size vs. timeout configuration
   - How connection exhaustion cascades
   - Difference between "no connections" and "all in use"

3. **Observability**:
   - Looking beyond pod state (Running doesn't mean healthy)
   - Understanding application logs vs. Kubernetes events
   - Connecting observed behavior to root cause

4. **SRE Skills**:
   - Systematic diagnosis (eliminate database, check config, find limits)
   - Understanding configuration sources (env vars, ConfigMaps, etc.)
   - Safe remediation (modifying environment safely)

---

## Technical Implementation Details

### Problem Class: `DatabaseConnectionPoolExhaustionHotelReservation`

**File**: `sregym/conductor/problems/database_connection_pool_exhaustion_hotel_reservation.py`

**Key Components**:
- Inherits from `Problem` base class
- Uses `ApplicationFaultInjector` for environment variable injection
- Targets `reservation` service in `hotel_reservation` app
- Uses standard `LLMAsAJudgeOracle` for diagnosis
- Uses standard `MitigationOracle` for fix verification

**Injection Method**:
```python
deployment = kubectl.get_deployment("reservation", namespace)
# Add environment variables to limit pool
deployment.spec.template.spec.containers[0].env.append(V1EnvVar(...))
kubectl.update_deployment("reservation", namespace, deployment)
```

**Recovery Method**:
```python
# Remove the limiting environment variables
deployment.env = [env for env in deployment.env if env.name not in LIMIT_VARS]
kubectl.update_deployment("reservation", namespace, deployment)
```

---

## References and Further Reading

- **MongoDB Connection Pooling**: https://www.mongodb.com/docs/drivers/go/current/fundamentals/connection/connection-pooling/
- **Database Connection Pool Exhaustion**: https://aws.amazon.com/blogs/database/working-with-rds-for-mysql-logical-read-replicas/
- **Related SREGym Problems**:
  - `kubelet_eviction_threshold_misconfig` - OS-level resource limits
  - `namespace_memory_limit` - Kubernetes memory quotas
  - `node_conntrack_exhaustion_hotel_reservation` - Network connection exhaustion
  - `pod_cidr_exhaustion_hotel_reservation` - IP allocation exhaustion

- **Industry Postmortems**:
  - [When connection pooling bites back](https://www.markbetz.net/2023/12/12/exhausting-conntrack-table-space-crippled-our-k8s-cluster/)
  - [Connection Pool Best Practices](https://github.com/r2dbc/r2dbc-pool)

---

## How to Run This Problem

```bash
# Set up environment
export JUDGE_MODEL_ID="gpt-5.1"
export OPENAI_API_KEY="sk-..."

# Start CLI
uv run python cli.py

# At SREGym prompt
SREGym> list | grep database
SREGym> start database_connection_pool_exhaustion_hotel_reservation

# Investigate
SREGym> kubectl get pods -A
SREGym> kubectl describe pod <reservation-pod> -n hotel-reservation
SREGym> kubectl get deployment reservation -n hotel-reservation -o yaml

# Submit diagnosis
SREGym> submit("The reservation service deployment has environment variables MONGODB_POOL_SIZE=3 and MONGODB_POOL_TIMEOUT_MS=5000 set, which limit the database connection pool to only 3 connections with a 5 second timeout. This causes new requests to fail with connection exhaustion errors when traffic exceeds the pool capacity.")

# Fix the issue
SREGym> kubectl set env deployment/reservation MONGODB_POOL_SIZE-
SREGym> kubectl set env deployment/reservation MONGODB_POOL_TIMEOUT_MS-
SREGym> kubectl set env deployment/reservation MONGODB_WAIT_QUEUE_TIMEOUT_MS-

# Verify fix
SREGym> kubectl rollout status deployment/reservation -n hotel-reservation

# Submit mitigation
SREGym> submit()
```

---

## Benchmark Statistics

- **Problem Name**: `database_connection_pool_exhaustion_hotel_reservation`
- **Fault Category**: Application Misconfiguration / Resource Exhaustion
- **Fault Level**: Application
- **Failure Level**: Application
- **Namespace**: `hotel-reservation`
- **Target Service**: `reservation`
- **Difficulty Level**: Medium (requires understanding connection pooling)
- **Expected Investigation Time**: 5-15 minutes (manual), 2-5 minutes (experienced agent)
- **Mitigation Time**: 2-3 minutes
- **Similar Problems**: `kubelet_eviction_threshold_misconfig`, `namespace_memory_limit`
