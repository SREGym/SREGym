import contextlib
import json
import logging
import os
import time
from typing import Any

import litellm
import openai
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM
from requests.exceptions import HTTPError

from llm_backend.trim_util import trim_messages_conservative

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

LLM_QUERY_MAX_RETRIES = int(os.getenv("LLM_QUERY_MAX_RETRIES", "5"))
LLM_QUERY_INIT_RETRY_DELAY = int(os.getenv("LLM_QUERY_INIT_RETRY_DELAY", "1"))


class LiteLLMBackend:
    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        top_p: float = 0.95,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        litellm.drop_params = True
        litellm.modify_params = True

    def inference(
        self,
        messages: str | list[SystemMessage | HumanMessage | AIMessage],
        system_prompt: str | None = None,
        tools: list[any] | None = None,
    ):
        if isinstance(messages, str):
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
                logger.error("Empty messages list.")
            elif isinstance(messages[0], HumanMessage):
                logger.info("No system message provided.")
                system_message = SystemMessage(content="You are a helpful assistant.")
                if system_prompt is None:
                    logger.warning("No system prompt provided. Using default system prompt.")
                else:
                    system_message.content = system_prompt
                prompt_messages.insert(0, system_message)
        else:
            raise ValueError(f"messages must be either a string or a list of dicts, but got {type(messages)}")

        model_config = {"model": self.model_name}
        if self.temperature is not None:
            model_config["temperature"] = self.temperature
        if self.top_p is not None:
            model_config["top_p"] = self.top_p
        if self.api_key is not None:
            model_config["api_key"] = self.api_key
        if self.api_base is not None:
            model_config["api_base"] = self.api_base
        if self.max_tokens is not None:
            model_config["max_tokens"] = self.max_tokens

        llm = ChatLiteLLM(**model_config)

        if tools:
            llm = llm.bind_tools(tools, tool_choice="auto")

        retry_delay = LLM_QUERY_INIT_RETRY_DELAY
        trim_message = False

        for attempt in range(LLM_QUERY_MAX_RETRIES):
            try:
                if trim_message:
                    new_prompt_messages, trim_sum = trim_messages_conservative(prompt_messages)
                    logger.info(f"Trimming the {trim_sum}/{len(prompt_messages)} messages")
                    prompt_messages = new_prompt_messages
                completion = llm.invoke(input=prompt_messages)
                return completion
            except openai.BadRequestError as e:
                logger.error(f"Bad request error - request is malformed: {e}")
                logger.error(f"Error details: {e.response.json() if hasattr(e, 'response') else 'No response details'}")
                logger.error("This often happens when tool_calls don't have matching tool response messages.")
                logger.error(
                    f"Last few messages: {prompt_messages[-3:] if len(prompt_messages) >= 3 else prompt_messages}"
                )
                raise
            except (openai.RateLimitError, HTTPError):
                logger.warning(
                    f"Rate-limited. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(retry_delay)
                retry_delay *= 2
            except openai.APIError as e:
                logger.warning(
                    f"OpenAI API error occurred: {e}. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(retry_delay)
                retry_delay *= 2

            except litellm.RateLimitError as e:
                provider_delay = _extract_retry_delay_seconds_from_exception(e)
                if provider_delay is not None and provider_delay > 0:
                    logger.warning(
                        f"Rate-limited by provider. Retrying in {provider_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                    )
                    time.sleep(provider_delay)
                else:
                    logger.warning(
                        f"Rate-limited. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2

                trim_message = True
            except litellm.ServiceUnavailableError:
                logger.warning(
                    f"Service unavailable (mostly 503). Retrying in 60 seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(60)
                trim_message = True
            except IndexError as e:
                logger.warning(
                    f"IndexError occurred on Gemini Server Side: {e}, keep calm for a while... {attempt + 1}/{LLM_QUERY_MAX_RETRIES}"
                )
                trim_message = True
                time.sleep(30)
                if attempt == LLM_QUERY_MAX_RETRIES - 1:
                    logger.error("Max retries exceeded due to index error. Unable to complete the request.")
                    return AIMessage(content="Server side error")
            except Exception as e:
                logger.error(f"An unexpected error occurred: {e}")
                raise

        raise RuntimeError("Max retries exceeded. Unable to complete the request.")


def _parse_duration_to_seconds(duration: Any) -> float | None:
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


def _extract_retry_delay_seconds_from_exception(exc: BaseException) -> float | None:
    candidates: list[Any] = []

    print(f"exc: {exc}")

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

    for attr in ("body", "message", "content"):
        try:
            val = getattr(exc, attr, None)
            if isinstance(val, (dict, list)):
                candidates.append(val)
            elif isinstance(val, (str, bytes)):
                candidates.append(json.loads(val))
        except Exception:
            pass

    try:
        for arg in getattr(exc, "args", []) or []:
            if isinstance(arg, (dict, list)):
                candidates.append(arg)
            elif isinstance(arg, (str, bytes)):
                with contextlib.suppress(Exception):
                    candidates.append(json.loads(arg))
    except Exception:
        pass

    def find_retry_delay(data: Any) -> float | None:
        if data is None:
            return None
        if isinstance(data, dict):
            if "error" in data:
                found = find_retry_delay(data.get("error"))
                if found is not None:
                    return found
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

    return 60.0
