import asyncio
import logging
import traceback

import requests
from fastmcp import FastMCP
from kubernetes import client, config

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.tools.localization import get_resource_uid

logger = get_logger()
logger.info("Starting Submission MCP Server")

langgraph_tool_config = LanggraphToolConfig()

mcp = FastMCP("Submission MCP Server")


@mcp.tool(name="submit")
def submit(ans: str) -> dict[str, str]:
    """Submit task result to benchmark

    Args:
        ans (str): task result that the agent submits

    Returns:
        dict[str]: http response code and response text of benchmark submission server
    """

    logger.info("[submit_mcp] submit mcp called")
    # FIXME: reference url from config file, remove hard coding
    url = langgraph_tool_config.benchmark_submit_url
    headers = {"Content-Type": "application/json"}
    # Match curl behavior: send "\"yes\"" when ans is "yes"
    payload = {"solution": f"{ans}"}

    try:
        response = requests.post(url, json=payload, headers=headers)
        logger.info(f"[submit_mcp] Response status: {response.status_code}, text: {response.text}")
        return {"status": str(response.status_code), "text": str(response.text)}

    except Exception as e:
        logger.error(f"[submit_mcp] HTTP submission failed: {e}")
        return {"status": "N/A", "text": f"[submit_mcp] HTTP submission failed: {e}"}


@mcp.tool(name="localization")
async def localization(
    resource_type: str,
    resource_name: str,
    namespace: str,
) -> dict[str, str]:
    """Retrieve the UID of a specified Kubernetes resource."""
    config.load_kube_config()
    try:
        cmd = [
            "kubectl",
            "get",
            resource_type,
            resource_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ]
        logger.info(f"[localization_mcp] Running command: {' '.join(cmd)}")
        if resource_type.lower() == "pod":
            api = client.CoreV1Api()
            obj = api.read_namespaced_pod(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "service":
            api = client.CoreV1Api()
            obj = api.read_namespaced_service(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "deployment":
            api = client.AppsV1Api()
            obj = api.read_namespaced_deployment(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "statefulset":
            api = client.AppsV1Api()
            obj = api.read_namespaced_stateful_set(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "persistentvolumeclaim":
            api = client.CoreV1Api()
            obj = api.read_namespaced_persistent_volume_claim(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "persistentvolume":
            api = client.CoreV1Api()
            obj = api.read_persistent_volume(name=resource_name)
        elif resource_type.lower() == "configmap":
            api = client.CoreV1Api()
            obj = api.read_namespaced_config_map(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "replicaset":
            api = client.AppsV1Api()
            obj = api.read_namespaced_replica_set(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "memoryquota":
            api = client.CoreV1Api()
            obj = api.read_namespaced_resource_quota(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "ingress":
            api = client.NetworkingV1Api()
            obj = api.read_namespaced_ingress(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "networkpolicy":
            api = client.NetworkingV1Api()
            obj = api.read_namespaced_network_policy(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "tidbcluster":
            api = client.CustomObjectsApi()
            obj = api.read_namespaced_custom_object(
                group="pingcap.com", version="v1alpha1", namespace=namespace, plural="tidbclusters", name=resource_name
            )
        elif resource_type.lower() == "job":
            api = client.BatchV1Api()
            obj = api.read_namespaced_job(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "daemonset":
            api = client.AppsV1Api()
            obj = api.read_namespaced_daemon_set(name=resource_name, namespace=namespace)
        elif resource_type.lower() == "clusterrole":
            api = client.RbacAuthorizationV1Api()
            obj = api.read_cluster_role(name=resource_name)
        elif resource_type.lower() == "clusterrolebinding":
            api = client.RbacAuthorizationV1Api()
            obj = api.read_cluster_role_binding(name=resource_name)
        else:
            err_msg = f"Unsupported resource type: {resource_type}"
            logger.error(f"[localization_mcp] {err_msg}")
            return {"uid": f"Error: {err_msg}"}
        uid = obj.metadata.uid
        logger.info(f"[localization_mcp] Retrieved UID using Kubernetes client: {uid}")
        return {"uid": uid}
    except Exception as e:
        logger.error(f"[localization_mcp] Exception occurred: {e}")
        logger.error(traceback.format_exc())
        return {"uid": f"Exception: {e}"}
