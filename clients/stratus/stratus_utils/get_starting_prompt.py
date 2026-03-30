import yaml
from langchain_core.messages import HumanMessage, SystemMessage


def get_starting_prompts(prompt_path, max_step):
    with open(prompt_path) as prompt_file:
        prompts = yaml.safe_load(prompt_file)
        sys_prompt = prompts["system"]
        user_prompt = prompts["user"].format(max_step=max_step)
        prompts = []
        if sys_prompt:
            prompts.append(SystemMessage(sys_prompt))
        if user_prompt:
            prompts.append(HumanMessage(user_prompt))

        return prompts
