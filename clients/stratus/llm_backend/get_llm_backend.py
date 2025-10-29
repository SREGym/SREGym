"""Adopted from previous project"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import litellm
import openai
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ibm import ChatWatsonx
from langchain_litellm import ChatLiteLLM
from langchain_openai import ChatOpenAI
from litellm.utils import trim_messages
from requests.exceptions import HTTPError

from clients.stratus.llm_backend.trim_util import trim_messages_conservative

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

LLM_QUERY_MAX_RETRIES = int(os.getenv("LLM_QUERY_MAX_RETRIES", "5"))  # Maximum number of retries for rate-limiting
LLM_QUERY_INIT_RETRY_DELAY = int(os.getenv("LLM_QUERY_INIT_RETRY_DELAY", "1"))  # Initial delay in seconds


class LiteLLMBackend:

    def __init__(
        self,
        provider: str,
        model_name: str,
        url: str,
        api_key: str,
        api_version: str,
        seed: int,
        top_p: float,
        temperature: float,
        reasoning_effort: str,
        thinking_tools: str,
        thinking_budget_tools: int,
        max_tokens: int,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.provider = provider
        self.model_name = model_name
        self.url = url
        self.api_key = api_key
        self.api_version = api_version
        self.temperature = temperature
        self.seed = seed
        self.top_p = top_p
        self.reasoning_effort = reasoning_effort
        self.thinking_tools = thinking_tools
        self.thinking_budget_tools = thinking_budget_tools
        self.max_tokens = max_tokens
        self.extra_headers = extra_headers
        litellm.drop_params = True
        litellm.modify_params = True  # for Anthropic

    def inference(
        self,
        messages: str | list[SystemMessage | HumanMessage | AIMessage],
        system_prompt: Optional[str] = None,
        tools: Optional[list[any]] = None,
    ):
        if isinstance(messages, str):
            # logger.info(f"NL input as str received: {messages}")
            # FIXME: This should be deprecated as it does not contain prior history of chat.
            #   We are building new agents on langgraph, which will change how messages are
            #   composed.
            if system_prompt is None:
                logger.info("No system prompt provided. Using default system prompt.")
                system_prompt = "You are a helpful assistant."
            prompt_messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=messages),
            ]
        elif isinstance(messages, list):
            prompt_messages = messages
            if len(messages) == 0:
                arena_logger = logging.getLogger("sregym-global")
                arena_logger.info("[ERROR] Canary died!")
            elif isinstance(messages[0], HumanMessage):
                # logger.info("No system message provided.")
                system_message = SystemMessage(content="You are a helpful assistant.")
                if system_prompt is None:
                    logger.warning("No system prompt provided. Using default system prompt.")
                else:
                    # logger.info("Using system prompt provided.")
                    system_message.content = system_prompt
                # logger.info(f"inserting [{system_message}] at the beginning of messages")
                prompt_messages.insert(0, system_message)
                arena_logger = logging.getLogger("sregym-global")
                arena_logger.info(f"[PROMPT] (inserted system prompt at the beginning) \n {system_message}")
        else:
            raise ValueError(f"messages must be either a string or a list of dicts, but got {type(messages)}")

        if self.provider == "openai":
            # Some models (o1, o3, gpt-5) don't support top_p and temperature
            model_config = {
                "model": self.model_name,
            }
            # Only add temperature and top_p for models that support them
            # Reasoning models (o1, o3) and newer models (gpt-5) don't support these params
            if not any(prefix in self.model_name.lower() for prefix in ["o1", "o3", "gpt-5"]):
                model_config["temperature"] = self.temperature
                model_config["top_p"] = self.top_p
            llm = ChatOpenAI(**model_config)
        elif self.provider == "watsonx":
            llm = ChatWatsonx(
                model_id=self.model_name,
                url=self.url,
                project_id=os.environ["WX_PROJECT_ID"],
                apikey=self.api_key,
                temperature=self.temperature,
            )
        # FIXME: openai client is broken, but we can just use LiteLLM to use openai
        elif self.provider == "openai":
            llm = ChatLiteLLM(
                model=self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
                api_key=self.api_key,
            )
        elif self.provider == "litellm":
            llm = ChatLiteLLM(
                model=self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
            )
        elif self.provider == "compatible":
            llm = ChatLiteLLM(
                model=self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
                api_key=self.api_key,
                api_base=self.url,
            )
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

        if tools:
            # logger.info(f"binding tools to llm: {tools}")
            llm = llm.bind_tools(tools, tool_choice="auto")

        # FIXME: when using openai models, finish_reason would be the function name
        #   if the model decides to do function calling
        # TODO: check how does function call looks like in langchain

        # Retry logic for rate-limiting
        retry_delay = LLM_QUERY_INIT_RETRY_DELAY
        trim_message = False

        for attempt in range(LLM_QUERY_MAX_RETRIES):
            try:
                # trim the first ten message who are AI messages and user messages
                if trim_message:
                    arena_logger = logging.getLogger("sregym-global")
                    new_prompt_messages, trim_sum = trim_messages_conservative(prompt_messages)
                    arena_logger.info(f"[WARNING] Trimming the {trim_sum}/{len(prompt_messages)} messages")
                    prompt_messages = new_prompt_messages
                completion = llm.invoke(input=prompt_messages)
                # logger.info(f">>> llm response: {completion}")
                return completion
            except openai.BadRequestError as e:
                # BadRequestError indicates malformed request (e.g., missing tool responses)
                # Don't retry as the request itself is invalid
                logger.error(f"Bad request error - request is malformed: {e}")
                logger.error(f"Error details: {e.response.json() if hasattr(e, 'response') else 'No response details'}")
                logger.error("This often happens when tool_calls don't have matching tool response messages.")
                logger.error(
                    f"Last few messages: {prompt_messages[-3:] if len(prompt_messages) >= 3 else prompt_messages}"
                )
                raise
            except (openai.RateLimitError, HTTPError) as e:
                # Rate-limiting errors - retry with exponential backoff
                logger.warning(
                    f"Rate-limited. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )

                arena_logger = logging.getLogger("sregym-global")
                arena_logger.info(
                    f"[WARNING] HTTP error occurred: {e}. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            except openai.APIError as e:
                # Other OpenAI API errors
                logger.error(f"OpenAI API error occurred: {e}")
                raise
                # else:
                #     logger.error(f"HTTP error occurred: {e}")
                #     raise

            except litellm.RateLimitError as e:
                provider_delay = _extract_retry_delay_seconds_from_exception(e)
                if provider_delay is not None and provider_delay > 0:
                    arena_logger = logging.getLogger("sregym-global")
                    arena_logger.info(
                        f"[WARNING] Rate-limited by provider. Retrying in {provider_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                    )
                    time.sleep(provider_delay)
                else:  # actually this fallback should not happen
                    arena_logger = logging.getLogger("sregym-global")
                    arena_logger.info(
                        f"Rate-limited. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff

                trim_message = True  # reduce overhead
            except litellm.ServiceUnavailableError as e:  # 503
                arena_logger = logging.getLogger("sregym-global")
                arena_logger.info(
                    f"[WARNING] Service unavailable (mostly 503). Retrying in 60 seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(60)
                trim_message = True  # reduce overhead
            except IndexError as e:
                arena_logger = logging.getLogger("sregym-global")
                arena_logger.info(
                    f"[ERROR] IndexError occurred on Gemini Server Side: {e}, keep calm for a while... {attempt + 1}/{LLM_QUERY_MAX_RETRIES}"
                )
                trim_message = True
                time.sleep(30)
                if attempt == LLM_QUERY_MAX_RETRIES - 1:
                    arena_logger = logging.getLogger("sregym-global")
                    arena_logger.info(
                        f"[WARNING] Max retries exceeded due to index error. Unable to complete the request."
                    )
                    # return an error
                    return AIMessage(content="Server side error")
            except Exception as e:
                logger.error(f"An unexpected error occurred: {e}")
                raise

        raise RuntimeError("Max retries exceeded. Unable to complete the request.")


def _parse_duration_to_seconds(duration: Any) -> Optional[float]:
    """Convert duration to seconds.

    Supports:
    - string like "56s" or "56.374s"
    - dict with {"seconds": int, "nanos": int}
    - numeric seconds
    """
    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        return float(duration)
    if isinstance(duration, str):
        val = duration.strip().lower()
        if val.endswith("s"):
            try:
                return float(val[:-1])
            except ValueError:
                return None
        return None
    if isinstance(duration, dict):
        seconds = duration.get("seconds")
        nanos = duration.get("nanos", 0)
        if isinstance(seconds, (int, float)):
            return float(seconds) + (float(nanos) / 1_000_000_000.0)
    return None


def _extract_retry_delay_seconds_from_exception(exc: BaseException) -> Optional[float]:
    """Extract retry delay seconds from JSON details RetryInfo only.

    Returns 60.0 if no RetryInfo found in error details.
    """
    candidates: list[Any] = []

    print(f"exc: {exc}")

    # response.json() or response.text
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            if hasattr(response, "json"):
                candidates.append(response.json())
        except Exception:
            pass
        try:
            text = getattr(response, "text", None)
            if isinstance(text, (str, bytes)):
                candidates.append(json.loads(text))
        except Exception:
            pass

    # message/body/content attributes
    for attr in ("body", "message", "content"):
        try:
            val = getattr(exc, attr, None)
            if isinstance(val, (dict, list)):
                candidates.append(val)
            elif isinstance(val, (str, bytes)):
                candidates.append(json.loads(val))
        except Exception:
            pass

    # args may contain dict/JSON strings
    try:
        for arg in getattr(exc, "args", []) or []:
            if isinstance(arg, (dict, list)):
                candidates.append(arg)
            elif isinstance(arg, (str, bytes)):
                try:
                    candidates.append(json.loads(arg))
                except Exception:
                    pass
    except Exception:
        pass

    def find_retry_delay(data: Any) -> Optional[float]:
        if data is None:
            return None
        if isinstance(data, dict):
            # Error envelope {"error": {...}}
            if "error" in data:
                found = find_retry_delay(data.get("error"))
                if found is not None:
                    return found
            # Google RPC details list
            details = data.get("details")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict):
                        type_url = item.get("@type") or item.get("type")
                        if type_url and "google.rpc.RetryInfo" in type_url:
                            parsed = _parse_duration_to_seconds(item.get("retryDelay"))
                            if parsed is not None:
                                return parsed
        elif isinstance(data, list):
            for v in data:
                found = find_retry_delay(v)
                if found is not None:
                    return found
        return None

    for cand in candidates:
        delay = find_retry_delay(cand)
        if delay is not None:
            return delay

    # Default to 60 seconds if no RetryInfo found
    return 60.0
