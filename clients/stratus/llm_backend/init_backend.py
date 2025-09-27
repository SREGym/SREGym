"""Adopted from previous project"""

import os

from dotenv import load_dotenv

from clients.stratus.llm_backend.get_llm_backend import LiteLLMBackend

load_dotenv()

global PROVIDER_TOOLS, MODEL_TOOLS, URL_TOOLS, API_VERSION_TOOLS, API_KEY_TOOLS, REASONING_EFFORT_TOOLS, SEED_TOOLS, TOP_P_TOOLS, TEMPERATURE_TOOLS, THINKING_TOOLS, THINKING_BUDGET_TOOLS, MAX_TOKENS_TOOLS

try:
    PROVIDER = os.environ["PROVIDER"]
except KeyError:
    PROVIDER = "openai"
    print("Unable to find environment variable - PROVIDER, setting to openai...")
    raise

try:
    PROVIDER_TOOLS = os.environ["PROVIDER_TOOLS"]
except KeyError:
    PROVIDER_TOOLS = ""
    print("Unable to find environment variable - PROVIDER_TOOLS.")
    raise

try:
    MODEL_TOOLS = os.environ["MODEL_TOOLS"]
except KeyError:
    MODEL_TOOLS = ""
    print("Unable to find environment variable - MODEL_TOOLS.")
    raise

try:
    URL_TOOLS = os.environ["URL_TOOLS"].rstrip("/")
except KeyError:
    URL_TOOLS = ""
    print("Unable to find environment variable - URL_TOOLS.")
    raise

try:
    API_KEY_TOOLS = os.environ["API_KEY_TOOLS"]
    # os.environ["OPENAI_API_KEY"] = API_KEY_TOOLS # should not use this fallback
except KeyError:
    API_KEY_TOOLS = ""
    print("Unable to find environment variable, leave it empty - API_KEY_TOOLS.")

try:
    SEED_TOOLS = int(os.environ["SEED_TOOLS"])
except KeyError:
    SEED_TOOLS = 10
    print(f"Unable to find environment variable - SEED_TOOLS. Defaulting to {SEED_TOOLS}.")

try:
    TOP_P_TOOLS = float(os.environ["TOP_P_TOOLS"])
except KeyError:
    TOP_P_TOOLS = 0.95
    print(f"Unable to find environment variable - TOP_P_TOOLS. Defaulting to {TOP_P_TOOLS}.")

try:
    TEMPERATURE_TOOLS = float(os.environ["TEMPERATURE_TOOLS"])
except KeyError:
    TEMPERATURE_TOOLS = 0.0
    print(f"Unable to find environment variable - TEMPERATURE_TOOLS. Defaulting to {TEMPERATURE_TOOLS}.")
except ValueError as e:
    print("Incorrect TEMPERATURE_TOOLS value:", e)
    raise

try:
    REASONING_EFFORT_TOOLS = str(os.environ["REASONING_EFFORT_TOOLS"]).lower()
except KeyError:
    REASONING_EFFORT_TOOLS = ""
    print(f"Unable to find environment variable - REASONING_EFFORT_TOOLS. Setting to {REASONING_EFFORT_TOOLS}.")

try:
    API_VERSION_TOOLS = os.environ["API_VERSION_TOOLS"]
except KeyError:
    API_VERSION_TOOLS = ""
    print(f"Unable to find environment variable - API_VERSION_TOOLS. Setting to {API_VERSION_TOOLS}.")

try:
    THINKING_TOOLS = os.environ["THINKING_TOOLS"]
except KeyError:
    THINKING_TOOLS = ""
    print(f"Unable to find environment variable - THINKING_TOOLS. Setting to {THINKING_TOOLS}.")

try:
    WX_PROJECT_ID = os.environ["WX_PROJECT_ID"]
except KeyError:
    WX_PROJECT_ID = ""
    print(f"Unable to find environment variable - WX_PROJECT_ID. Setting to {WX_PROJECT_ID}.")

try:
    WATSONX_API_BASE = os.environ["WATSONX_API_BASE"]
except KeyError:
    WATSONX_API_BASE = "https://us-south.ml.cloud.ibm.com"
    print(f"Unable to find environment variable - WATSONX_API_BASE. Setting to {WATSONX_API_BASE}.")


try:
    THINKING_BUDGET_TOOLS = int(os.environ["THINKING_BUDGET_TOOLS"])
except KeyError:
    THINKING_BUDGET_TOOLS = 16000
    print(f"Unable to find environment variable - THINKING_BUDGET_TOOLS. Setting to {THINKING_BUDGET_TOOLS}.")

try:
    MAX_TOKENS_TOOLS = int(os.environ["MAX_TOKENS_TOOLS"])
except KeyError:
    MAX_TOKENS_TOOLS = 16000
    print(f"Unable to find environment variable - MAX_TOKENS_TOOLS. Setting to {MAX_TOKENS_TOOLS}.")


def get_llm_backend_for_tools():
    if PROVIDER == "watsonx":
        try:
            WATSONX_API_KEY = os.environ["WATSONX_API_KEY"]
        except KeyError:
            print(f"Unable to find environment variable - WATSONX_API_KEY. Exiting...")
            exit(1)
        return LiteLLMBackend(
            provider=PROVIDER,
            model_name=MODEL_TOOLS,
            url=URL_TOOLS,
            api_key=WATSONX_API_KEY,
            api_version=API_VERSION_TOOLS,
            seed=SEED_TOOLS,
            top_p=TOP_P_TOOLS,
            temperature=TEMPERATURE_TOOLS,
            reasoning_effort=REASONING_EFFORT_TOOLS,
            max_tokens=MAX_TOKENS_TOOLS,
            thinking_tools=THINKING_TOOLS,
            thinking_budget_tools=THINKING_BUDGET_TOOLS,
        )
    elif PROVIDER == "openai":
        return LiteLLMBackend(
            provider=PROVIDER,
            model_name=MODEL_TOOLS,
            url=URL_TOOLS,
            api_key=API_KEY_TOOLS,
            api_version=API_VERSION_TOOLS,
            seed=SEED_TOOLS,
            top_p=TOP_P_TOOLS,
            temperature=TEMPERATURE_TOOLS,
            reasoning_effort=REASONING_EFFORT_TOOLS,
            max_tokens=MAX_TOKENS_TOOLS,
            thinking_tools=THINKING_TOOLS,
            thinking_budget_tools=THINKING_BUDGET_TOOLS,
        )
    elif PROVIDER == "litellm":
        return LiteLLMBackend(
            provider=PROVIDER,
            model_name=MODEL_TOOLS,
            url=URL_TOOLS, # not used
            api_key=API_KEY_TOOLS,
            api_version=API_VERSION_TOOLS,
            seed=SEED_TOOLS,
            top_p=TOP_P_TOOLS,
            temperature=TEMPERATURE_TOOLS,
            reasoning_effort=REASONING_EFFORT_TOOLS,
            thinking_tools=THINKING_TOOLS,
            thinking_budget_tools=THINKING_BUDGET_TOOLS,
            max_tokens=MAX_TOKENS_TOOLS,
        )
    elif PROVIDER == "compatible":
        return LiteLLMBackend(
            provider=PROVIDER,
            model_name=MODEL_TOOLS,
            url=URL_TOOLS,
            api_key=API_KEY_TOOLS,
            api_version=API_VERSION_TOOLS,
            seed=SEED_TOOLS,
            top_p=TOP_P_TOOLS,
            temperature=TEMPERATURE_TOOLS,
            reasoning_effort=REASONING_EFFORT_TOOLS,
            thinking_tools=THINKING_TOOLS,
            thinking_budget_tools=THINKING_BUDGET_TOOLS,
            max_tokens=MAX_TOKENS_TOOLS,
        )
