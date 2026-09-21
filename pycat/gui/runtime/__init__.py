"""UI runtime bridge layer.

This package contains Qt-specific runtime helpers (threads/signals/state) that
connect the UI to core engines.

Core engines must NOT import from here.
"""

from .message_runtime import MessageRuntime
from .prompt_optimizer_runtime import PromptOptimizer
from .channel_gateway_bridge import ChannelGatewayBridge

__all__ = ["MessageRuntime", "PromptOptimizer", "ChannelGatewayBridge"]
