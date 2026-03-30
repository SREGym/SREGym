import logging

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_text(text, max_length=10000):
    """
    Parse and truncate text if it's too long.

    Args:
        text (str): The text to parse

    Returns:
        str: The parsed text
    """
    # Truncate if needed to avoid token limits
    if len(text) > max_length:
        return text[:max_length] + "... [truncated]"
    return text


def cleanup_kubernetes_yaml(cluster_state: str) -> str:
    object = None

    try:
        object = list(yaml.safe_load_all(cluster_state))
    except Exception as e:
        logger.error(f"Yaml cleaner: Failed to parse YAML: {e}")
        return ""

    def recursive_remove(obj):
        if isinstance(obj, dict):
            obj.get("metadata", {}).pop("resourceVersion", None)
            obj.get("metadata", {}).get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
            obj.pop("uid", None)
            for k, v in obj.items():
                if k == "ownerReferences":
                    continue
                # Should not modify the last-applied-configuration string
                if isinstance(v, (dict, list)):
                    recursive_remove(v)
        elif isinstance(obj, list):
            for item in obj:
                recursive_remove(item)

    recursive_remove(object)

    return yaml.dump_all(object)
