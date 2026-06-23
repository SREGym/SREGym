"""
Database Connection Pool Exhaustion Problem

Real-world failure: When a backend service (reservation-service) has a limited 
database connection pool and experiences high load, all connections get exhausted.
New requests fail with connection timeouts, causing cascading failures.

This simulates a misconfiguration or resource exhaustion scenario where:
1. Reservation service is deployed with a limited connection pool (e.g., 5 connections)
2. Load increases or connections leak
3. All connections are exhausted
4. New requests queue or fail immediately
5. Fix: Increase connection pool size or optimize connection usage
"""

import time

from kubernetes.client.models import V1EnvVar

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class DatabaseConnectionPoolExhaustionHotelReservation(Problem):
    """Simulates database connection pool exhaustion on Hotel Reservation service.
    
    The reservation-service connects to MongoDB to store and retrieve reservations.
    A misconfigured or resource-limited connection pool causes new database requests
    to timeout or queue up, making the service unable to process requests.
    
    Symptoms:
    - Reservation endpoints return 500 errors or timeouts
    - Logs show "connection timeout" or "pool exhausted" errors
    - Response times increase dramatically
    - Service becomes unavailable under load
    """

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "reservation"):
        self.faulty_service = faulty_service
        self.app_name = app_name
        
        # Connection pool configuration: normally would be 20-50 connections
        # We set it to a very low number to simulate exhaustion
        self.pool_size = 3
        self.pool_timeout_ms = 5000  # 5 second timeout
        
        if app_name != "hotel_reservation":
            raise ValueError(f"Unsupported app name for this problem: {app_name}")

        super().__init__(app=HotelReservation())
        
        self.kubectl = KubeCtl()
        
        # Define the root cause in structured format
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} service has a misconfigured database connection pool "
                f"with only {self.pool_size} connections and a {self.pool_timeout_ms}ms timeout. "
                "Under load, all connections become exhausted, causing new requests to timeout. "
                "Applications receive 'connection pool exhausted' errors and fail to process requests. "
                "Symptoms include slow response times, 500 errors, and cascading failures to dependent services. "
                "Fix: Increase the connection pool size or optimize connection usage."
            ),
        )
        
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # Create the application workload
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        """Inject connection pool exhaustion by setting environment variables."""
        print("== Fault Injection: Database Connection Pool Exhaustion ==")
        
        # Get the deployment and modify environment variables
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        if not deployment:
            print(f"❌ Deployment {self.faulty_service} not found in namespace {self.namespace}")
            return
        
        # Add environment variables to limit connection pool
        # These variables control MongoDB connection pooling behavior
        env_vars_to_add = [
            {"name": "MONGODB_POOL_SIZE", "value": str(self.pool_size)},
            {"name": "MONGODB_POOL_TIMEOUT_MS", "value": str(self.pool_timeout_ms)},
            {"name": "MONGODB_WAIT_QUEUE_TIMEOUT_MS", "value": "1000"},  # 1 second wait before timeout
        ]
        
        for container in deployment.spec.template.spec.containers:
            if not container.env:
                container.env = []
            
            # Add/update the environment variables
            existing_names = {env.name for env in container.env}
            for new_env in env_vars_to_add:
                if new_env["name"] not in existing_names:
                    container.env.append(V1EnvVar(name=new_env["name"], value=new_env["value"]))
                else:
                    # Update existing
                    for env in container.env:
                        if env.name == new_env["name"]:
                            env.value = new_env["value"]
        
        # Apply the modified deployment
        self.kubectl.update_deployment(self.faulty_service, self.namespace, deployment)
        
        # Wait for rollout to complete
        time.sleep(15)
        
        print(f"✅ Connection pool limited to {self.pool_size} connections")
        print(f"   Service: {self.faulty_service} | Namespace: {self.namespace}")
        print(f"   Timeout: {self.pool_timeout_ms}ms")
        print(f"   Expected symptoms: Request timeouts, 'pool exhausted' errors")

    @mark_fault_injected
    def recover_fault(self):
        """Recover by removing or increasing the connection pool limits."""
        print("== Fault Recovery: Restore Connection Pool ==")
        
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        if not deployment:
            print(f"❌ Deployment {self.faulty_service} not found")
            return
        
        # Remove the connection pool limiting environment variables
        env_vars_to_remove = {
            "MONGODB_POOL_SIZE",
            "MONGODB_POOL_TIMEOUT_MS",
            "MONGODB_WAIT_QUEUE_TIMEOUT_MS",
        }
        
        for container in deployment.spec.template.spec.containers:
            if container.env:
                # Filter out the variables we injected
                container.env = [env for env in container.env if env.name not in env_vars_to_remove]
        
        # Apply the recovered deployment
        self.kubectl.update_deployment(self.faulty_service, self.namespace, deployment)
        
        # Wait for rollout to complete
        time.sleep(15)
        
        print(f"✅ Connection pool limits removed")
        print(f"   Service: {self.faulty_service} | Namespace: {self.namespace}")
        print(f"   Connection pooling restored to defaults")
