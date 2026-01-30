"""Adopted from previous project"""

import os

import yaml
from dotenv import load_dotenv

from llm_backend.get_llm_backend import LiteLLMBackend

load_dotenv()


def load_model_config():
    with open(os.path.join(os.path.dirname(__file__), "configs.yaml")) as f:
        configs = yaml.load(f, Loader=yaml.FullLoader)
    return configs


def set_param(params, config, field, default_value, required=False):
    value_to_set = None
    value_or_env = None

    if field in config:
        value_or_env = config[field]
    elif default_value is not None:
        value_or_env = default_value

    if value_or_env is not None and isinstance(value_or_env, str) and value_or_env.startswith("$"):
        key = value_or_env[1:]
        if key in os.environ:
            value_to_set = os.environ[key]
        # else do nothing
    else:
        value_to_set = value_or_env

    if value_to_set is not None:
        params[field] = value_to_set
    else:
        if required:
            error_msg = f"Unable to find value for required field - {field}"
            print(f"{error_msg}. Exiting...")
            raise ValueError(error_msg)
        # else do nothing


def get_llm_backend_for_tools():
    llm_config = load_model_config()

    MODEL_ID = os.environ.get("MODEL_ID", "gpt-4o")
    print("Found MODEL_ID: ", MODEL_ID)

    if MODEL_ID not in llm_config:
        error_msg = f"Unable to find model configuration - {MODEL_ID}. Available models: {[key for key in llm_config.keys()]}"
        print(error_msg)
        raise ValueError(error_msg)
    model_config = llm_config[MODEL_ID]

    if model_config["provider"] == "litellm":
        config_params = {
            "provider": "litellm",
        }
        set_param(config_params, model_config, "model_name", "openai/gpt-4o")
        set_param(config_params, model_config, "url", None)
        set_param(config_params, model_config, "api_key", None, required=False)
        set_param(config_params, model_config, "top_p", 0.95)
        set_param(config_params, model_config, "temperature", 0.0)
        set_param(config_params, model_config, "max_tokens", None)

        if "AZURE_API_VERSION" not in os.environ:
            if "azure_version" in model_config:
                # set env
                print(f"Setting Azure API version env from config - {model_config['azure_version']}")
                os.environ["AZURE_API_VERSION"] = model_config["azure_version"]

        return LiteLLMBackend(**config_params)

    elif model_config["provider"] == "openai":
        config_params = {
            "provider": "openai",
        }
        set_param(config_params, model_config, "model_name", "openai/gpt-4o")
        set_param(config_params, model_config, "api_key", None, required=True)
        set_param(config_params, model_config, "seed", None)
        set_param(config_params, model_config, "top_p", 0.95)
        set_param(config_params, model_config, "temperature", 0.0)
        set_param(config_params, model_config, "max_tokens", None)

        return LiteLLMBackend(**config_params)

    elif model_config["provider"] == "watsonx":
        config_params = {
            "provider": "watsonx",
        }
        set_param(config_params, model_config, "model_name", "meta-llama/llama-3-3-70b-instruct")
        set_param(config_params, model_config, "url", "https://us-south.ml.cloud.ibm.com")
        set_param(config_params, model_config, "api_key", None, required=True)
        set_param(config_params, model_config, "seed", None)
        set_param(config_params, model_config, "top_p", 0.95)
        set_param(config_params, model_config, "temperature", 0.0)
        set_param(config_params, model_config, "max_tokens", None)
        set_param(config_params, model_config, "wx_project_id", "$WX_PROJECT_ID", required=True)

        return LiteLLMBackend(**config_params)

    else:
        raise ValueError(f"Unsupported provider - {model_config['provider']}. Exiting...")
