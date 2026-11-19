"""Fetches astronomy-shop product images the way the frontend's browser code does.

The demo applies imageSlowLoad only to requests carrying x-envoy-fault-delay-request,
which the frontend's browser JavaScript sets from the flag. Locust's HTTP users never
fetch images, and the demo 2.2.0 browser users crash, so nothing else sends it.
"""

from kubernetes import client

IMAGE_CLIENT_NAME = "frontend-image-client"
IMAGE_CLIENT_IMAGE = "curlimages/curl:8.10.1"
FLAG = "imageSlowLoad"
INTERVAL_SECONDS = 5


def image_client_script(frontend: str = "http://frontend-proxy:8080", flagd: str = "http://flagd:8016") -> str:
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


def image_client_deployment(namespace: str) -> client.V1Deployment:
    labels = {"app": IMAGE_CLIENT_NAME}
    container = client.V1Container(
        name="client",
        image=IMAGE_CLIENT_IMAGE,
        command=["sh", "-c", image_client_script()],
        # No CPU limit: CFS throttling would raise the namespace's ContainerCPUThrottling alert.
        resources=client.V1ResourceRequirements(requests={"cpu": "10m", "memory": "16Mi"}, limits={"memory": "64Mi"}),
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=IMAGE_CLIENT_NAME, namespace=namespace, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels), spec=client.V1PodSpec(containers=[container])
            ),
        ),
    )


def start_image_client(namespace: str) -> None:
    apps = client.AppsV1Api()
    body = image_client_deployment(namespace)
    try:
        apps.create_namespaced_deployment(namespace=namespace, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        apps.replace_namespaced_deployment(name=IMAGE_CLIENT_NAME, namespace=namespace, body=body)


def stop_image_client(namespace: str) -> None:
    try:
        client.AppsV1Api().delete_namespaced_deployment(name=IMAGE_CLIENT_NAME, namespace=namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
