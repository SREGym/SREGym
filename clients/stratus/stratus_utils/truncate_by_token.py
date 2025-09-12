import tiktoken


def truncate_to_tokens(text: str, max_tokens: int = 6000, model: str = "gpt-4o-mini"):
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        # Fallback that works for most modern OpenAI chat models
        enc = tiktoken.get_encoding("cl100k_base")

    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text, len(tokens)

    # Truncate and decode back to string
    truncated_text = enc.decode(tokens[:max_tokens])

    # Optional safety pass to ensure token count is <= max_tokens after decoding
    retokens = enc.encode(truncated_text)
    if len(retokens) > max_tokens:
        truncated_text = enc.decode(retokens[:max_tokens])

    return truncated_text
