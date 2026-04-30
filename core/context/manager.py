"""统一上下文管理器 - 负责消息准备、压缩触发和历史总结注入。

这个模块是解决上下文管理问题的核心：
1. 在每轮 LLM 调用前自动检查是否需要压缩
2. 触发压缩（单消息压缩 + 全局归档）
3. 构建包含历史总结的系统消息
4. 返回准备好的消息列表

参考 VSCode Copilot 的设计：
- 保留最后 2-3 条完整消息
- 使用 LLM 生成历史总结
- 环境信息仅在首次或变化时注入
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from models.conversation import Conversation, Message
from models.provider import Provider
from core.context.condenser import ContextCondenser, CondensePolicy
from core.llm.token_utils import estimate_conversation_tokens
from core.prompts.system_builder import build_system_prompt
from core.prompts.context_assembler import build_context_messages
from core.prompts.history import count_user_turn_blocks

logger = logging.getLogger(__name__)


class ContextManager:
    """统一上下文管理器 - 自动压缩和消息准备。"""

    def __init__(
        self,
        condenser: ContextCondenser,
        policy: Optional[CondensePolicy] = None
    ):
        """初始化上下文管理器。

        Args:
            condenser: 上下文压缩器实例
            policy: 压缩策略配置
        """
        self.condenser = condenser
        self.policy = policy or CondensePolicy()

    async def prepare_messages(
        self,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        tools: List[Any],
        app_config: Any,
        default_work_dir: str = ".",
        *,
        compress: bool = True,
    ) -> List[Message]:
        """准备发送给 LLM 的消息列表。

        这是核心方法，执行以下步骤：
        1. 检查是否需要压缩（消息数或 token 数超过阈值）
        2. 如果需要，触发自动压缩
        3. 获取有效的历史消息（过滤已压缩消息）
        4. 构建系统消息（稳定规则 / 工具 / 状态）
        5. 返回完整的消息列表

        Args:
            conversation: 当前对话
            provider: LLM 提供商配置
            context_window_limit: 上下文窗口限制（token 数）
            tools: 可用工具列表
            app_config: 应用配置
            default_work_dir: 默认工作目录

        Returns:
            准备好的消息列表（系统消息 + 环境/summary/最近历史）
        """
        # 1. 检查是否需要压缩
        should_compress = self._should_compress(
            conversation,
            context_window_limit
        )

        if compress and should_compress:
            logger.info(
                f"触发上下文压缩: 消息数={len(conversation.messages)}, "
                f"阈值={self.policy.max_active_messages}"
            )
            # 2. 执行自动压缩
            await self.condenser.auto_condense(
                conversation=conversation,
                provider=provider,
                context_window_limit=context_window_limit,
                app_config=app_config,
                policy=self.policy,
            )

        # 3. 按三段式装配环境信息 + summary + 最近完整历史
        effective_messages = build_context_messages(
            conversation,
            app_config=app_config,
            keep_last_turns=self.policy.keep_last_n,
            default_work_dir=default_work_dir,
        )

        # 4. 构建系统消息（只保留稳定规则 / 工具 / 状态）
        system_content = build_system_prompt(
            conversation=conversation,
            tools=tools,
            provider=provider,
            app_config=app_config,
            default_work_dir=default_work_dir,
        )

        system_message = Message(
            role="system",
            content=system_content,
        )

        # 5. 返回完整消息列表
        return [system_message] + effective_messages

    def _should_compress(
        self,
        conversation: Conversation,
        context_window_limit: int
    ) -> bool:
        """判断是否需要触发压缩。

        压缩触发条件（满足任一即触发）：
        1. 消息数量超过阈值（默认 20 条）
        2. Token 数量超过上下文窗口的 70%

        Args:
            conversation: 当前对话
            context_window_limit: 上下文窗口限制

        Returns:
            是否需要压缩
        """
        # 策略 0：真实 user-led turn blocks 超过保留窗口
        turn_block_count = count_user_turn_blocks(conversation.messages)
        if turn_block_count > self.policy.keep_last_n:
            logger.debug(
                "用户轮次触发压缩: %s > %s",
                turn_block_count,
                self.policy.keep_last_n,
            )
            return True

        # 策略 1：消息数量超过阈值
        message_count = len(conversation.messages)
        if message_count > self.policy.max_active_messages:
            logger.debug(
                f"消息数量触发压缩: {message_count} > {self.policy.max_active_messages}"
            )
            return True

        # 策略 2：Token 数量超过阈值
        estimated_tokens = estimate_conversation_tokens(conversation)
        if context_window_limit <= 0:
            return False

        token_threshold = int(context_window_limit * self.policy.token_threshold_ratio)

        if estimated_tokens > token_threshold:
            logger.debug(
                f"Token 数量触发压缩: {estimated_tokens} > {token_threshold} "
                f"({self.policy.token_threshold_ratio * 100}% of {context_window_limit})"
            )
            return True

        return False

    def reset(self):
        """重置上下文管理器状态。

        在创建新对话时调用。
        """
        pass  # 当前无需重置状态

