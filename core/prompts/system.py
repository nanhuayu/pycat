from __future__ import annotations

from typing import List, Dict, Any

from models.conversation import Conversation
from models.provider import Provider

from core.config import AppConfig, load_app_config
from core.prompts.system_builder import build_system_prompt

class PromptManager:
    """
    Centralized manager for system prompts and context assembly.
    """
    
    def __init__(self, work_dir: str = "."):
        self.work_dir = work_dir

    def get_system_prompt(
        self,
        conversation: Conversation,
        tools: List[Dict[str, Any]],
        provider: Provider,
        *,
        app_config: AppConfig | None = None,
    ) -> str:
        cfg = app_config
        if cfg is None:
            try:
                cfg = load_app_config()
            except Exception:
                cfg = AppConfig()

        return build_system_prompt(
            conversation=conversation,
            tools=tools,
            provider=provider,
            app_config=cfg,
            default_work_dir=self.work_dir,
        )
