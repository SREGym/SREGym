"""Fetches astronomy-shop product images the way the frontend's browser code does.

The demo applies imageSlowLoad only to requests carrying x-envoy-fault-delay-request,
which the frontend's browser JavaScript sets from the flag. Locust's HTTP users never
fetch images, and the demo 2.2.0 browser users crash, so nothing else sends it.

The client runs in a namespace hidden from the agent (agent_visibility_policy.HIDDEN_NAMESPACES).
Its command names the flag, and deleting it would clear the latency alert without a fix.
"""

from kubernetes import client

IMAGE_CLIENT_NAME = "frontend-image-client"
IMAGE_CLIENT_NAMESPACE = "khaos"
IMAGE_CLIENT_IMAGE = "curlimages/curl:8.10.1"
FLAG = "imageSlowLoad"
INTERVAL_SECONDS = 5


def image_client_script(app_namespace: str) -> str:
    frontend = f"http://frontend-proxy.{app_namespace}.svc.cluster.local:8080"
    flagd = f"http://flagd.{app_namespace}.svc.cluster.local:8016"
    return f"""while true; do
  delay=$(curl -s --max-time 3 -X POST -H 'Content-Type: application/json' -d '{{"context":{{}}}}' \\
    {flagd}/ofrep/v1/evaluate/flags/{FLAG} | sed -n 's/.*"value":\\([0-9]*\\).*/\\1/p')
  pictures=$(curl -s --max-time 5 {frontend}/api/products | grep -o '"picture":"[^"]*"' | cut -d'"' -f4)
  for picture in $pictures; do
    curl -sS -o /dev/null --max-time 30 -H "x-envoy-fault-delay-request: ${{delay:-0}}" -H 'Cache-Control: no-cache' \\
      -w "%{{http_code}} %{{time_total}}s /images/products/$picture\\n" {frontend}/images/products/$picture &
  done
  wait
  sleep {INTERVAL_SECONDS}
done"""


def image_client_deployment(app_namespace: str) -> client.V1Deployment:
    labels = {"app": IMAGE_CLIENT_NAME}
    container = client.V1Container(
        name="client",
        image=IMAGE_CLIENT_IMAGE,
        command=["sh", "-c", image_client_script(app_namespace)],
        # No CPU limit: a 100m limit throttled the parallel fetches.
        resources=client.V1ResourceRequirements(requests={"cpu": "10m", "memory": "16Mi"}, limits={"memory": "64Mi"}),
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=IMAGE_CLIENT_NAME, namespace=IMAGE_CLIENT_NAMESPACE, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                # sh runs as PID 1 and ignores SIGTERM, so a grace period only delays deletion.
                spec=client.V1PodSpec(containers=[container], termination_grace_period_seconds=0),
            ),
        ),
    )


def start_image_client(app_namespace: str) -> None:
    try:
        client.CoreV1Api().create_namespace(
            body=client.V1Namespace(metadata=client.V1ObjectMeta(name=IMAGE_CLIENT_NAMESPACE))
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
    apps = client.AppsV1Api()
    body = image_client_deployment(app_namespace)
    try:
        apps.create_namespaced_deployment(namespace=IMAGE_CLIENT_NAMESPACE, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        apps.replace_namespaced_deployment(name=IMAGE_CLIENT_NAME, namespace=IMAGE_CLIENT_NAMESPACE, body=body)


def stop_image_client() -> None:
    # Delete only the client: other helpers share the hidden namespace.
    try:
        client.AppsV1Api().delete_namespaced_deployment(name=IMAGE_CLIENT_NAME, namespace=IMAGE_CLIENT_NAMESPACE)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
