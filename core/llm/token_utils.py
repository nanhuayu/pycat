import re

def estimate_tokens(text: str) -> int:
    """
    Estimate the number of tokens in a text string.
    
    Heuristic:
    - Chinese characters: ~0.6 tokens/char (1.5 chars/token)
    - English/Code: ~0.25 tokens/char (4 chars/token)
    """
    if not text:
        return 0
        
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    
    # Chinese is roughly 1.5 chars per token? No, usually 1 token is 1-2 chars.
    # OpenAI: 1000 tokens ~ 750 words.
    # Chinese: 1 token ~ 0.6-0.8 chars. 
    # Let's use conservative estimate:
    # Chinese: 1 char = 1.5 tokens? No, 1 char = 1-2 tokens usually?
    # Actually, widely used estimate for GPT-4:
    # 1 Chinese char ~ 2 tokens (sometimes 1-3).
    # 1 English char ~ 0.25 tokens (1 token ~ 4 chars).
    
    # Previous implementation: int(chinese_chars / 1.5 + other_chars / 4)
    # which means 1 Chinese char = 0.66 tokens. This might be too low.
    # Let's adjust to be safer (prevent overflow):
    # 1 Chinese char = 1 token.
    # 1 English char = 0.25 token.
    
    return int(chinese_chars * 1.0 + other_chars * 0.25)

def estimate_message_tokens(message) -> int:
    """Estimate tokens for a Message object."""
    content = getattr(message, "content", "") or ""
    
    # Add tokens for role/overhead
    count = 4  # Per message overhead
    count += estimate_tokens(content)
    
    # Tool calls
    if getattr(message, "tool_calls", None):
        for tc in message.tool_calls:
            count += estimate_tokens(str(tc))
            
    # Thinking
    if getattr(message, "thinking", None):
        count += estimate_tokens(message.thinking)
        
    return count

def estimate_conversation_tokens(conversation) -> int:
    """Estimate total tokens in conversation."""
    messages = getattr(conversation, "messages", [])
    return sum(estimate_message_tokens(m) for m in messages)
