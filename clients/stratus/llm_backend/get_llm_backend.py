"""Adopted from previous project"""

import logging
import os
import time
from typing import Dict, Optional

import litellm
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_ibm import ChatWatsonx
from langchain_litellm import ChatLiteLLM
from langchain_openai import ChatOpenAI
from requests.exceptions import HTTPError

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
            if isinstance(messages[0], HumanMessage):
                # logger.info("No system message provided.")
                system_message = SystemMessage(content="You are a helpful assistant.")
                if system_prompt is None:
                    logger.warning("No system prompt provided. Using default system prompt.")
                else:
                    # logger.info("Using system prompt provided.")
                    system_message.content = system_prompt
                # logger.info(f"inserting [{system_message}] at the beginning of messages")
                prompt_messages.insert(0, system_message)
        else:
            raise ValueError(f"messages must be either a string or a list of dicts, but got {type(messages)}")

        if self.provider == "openai":
            llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
            )
        elif self.provider == "watsonx":
            llm = ChatWatsonx(
                model_id=self.model_name,
                url=self.url,
                project_id=os.environ["WX_PROJECT_ID"],
                apikey=self.api_key,
                temperature=self.temperature,
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
        for attempt in range(LLM_QUERY_MAX_RETRIES):
            try:
                completion = llm.invoke(input=prompt_messages)
                # logger.info(f"llm response: {completion}")
                return completion
            except HTTPError as e:
                # if e.response.status_code == 429 or e.response.status_code == 502 or e.response.status_code == 400:  # Rate-limiting error
                logger.warning(
                    f"Rate-limited. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{LLM_QUERY_MAX_RETRIES})"
                )
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                # else:
                #     logger.error(f"HTTP error occurred: {e}")
                #     raise
            except Exception as e:
                logger.error(f"An unexpected error occurred: {e}")
                raise

        raise RuntimeError("Max retries exceeded. Unable to complete the request.")
