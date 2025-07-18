"""Adopted from previous project"""

import json
import logging
from typing import Dict, Optional

import litellm
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain_litellm import ChatLiteLLM 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()


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
            # logger.info(f"NL input as list received: {messages}")
            prompt_messages = messages
            if isinstance(messages[0], HumanMessage):
                logger.info("No system message provided.")
                system_message = SystemMessage(content="You are a helpful assistant.")
                if system_prompt is None:
                    logger.warning("No system prompt provided. Using default system prompt.")
                else:
                    logger.info("Using system prompt provided.")
                    system_message.content = system_prompt
                logger.info(f"inserting [{system_message}] at the beginning of messages")
                prompt_messages.insert(0, system_message)
        else:
            raise ValueError(f"messages must be either a string or a list of dicts, but got {type(messages)}")
        # logger.info(f"prompting llm with messages: {prompt_messages}")

        if self.provider == "openai":
            llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
            )
        elif self.provider == "watsonx":
            llm = ChatLiteLLM(
                model = self.model_name,
                api_key=self.api_key,
                temperature=self.temperature,
                custom_llm_provider = self.provider,   
            )
        
       

        if tools:
            logger.info(f"binding tools to llm: {tools}")
            llm = llm.bind_tools(tools, tool_choice="auto")

        # FIXME: when using openai models, finish_reason would be the function name
        #   if the model decides to do function calling
        # TODO: check how does function call looks like in langchain

        completion = llm.invoke(input=prompt_messages)
        logger.info(f"llm response: {completion}")
        return completion
