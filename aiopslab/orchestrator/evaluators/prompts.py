# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

############### LLMJudge: Scorer ################

SCORE_SYSTEM = """Please act as an impartial judge and evaluate the quality of the response provided by an AI assistant towards a Service Operations task displayed below. 
Your evaluation should consider factors such as the helpfulness, relevance, accuracy, depth, creativity, and level of detail of the response. 
Begin your evaluation by providing a short explanation. Be as objective as possible. 
After providing your explanation, you must rate the response on a scale of 1 to 10 by strictly following this format: "[[rating]]", for example: "Rating: [[5]]".

"""

SCORE_TASK = """<|The Start of Assistant A's Interaction with Service|>

{trace}

<|The End of Assistant A's Interaction with Service|>"
"""

SCORER_PROMPTS = {
    "system": SCORE_SYSTEM,
    "user": SCORE_TASK,
}


# FUTURE (TODO): Implement prompts to qualitatively evaluate deviations from task
# FUTURE (TODO): Implement prompts to write grading notes during eval
