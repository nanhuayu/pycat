from __future__ import annotations

from core.prompts.providers.base import ProviderContext, synthetic_context_message
from core.prompts.user_context import build_environment_info, build_workspace_info


class EnvironmentProvider:
    name = "environment"
    priority = 10

    def build(self, context: ProviderContext):
        prompt_cfg = getattr(context.app_config, "prompts", None)
        max_depth = max(1, int(getattr(prompt_cfg, "file_tree_max_depth", 2) or 2))
        blocks: list[str] = []
        if bool(getattr(prompt_cfg, "include_environment", True)):
            blocks.append(build_environment_info(cwd=context.work_dir))
        workspace_block = build_workspace_info(context.work_dir, max_depth=max_depth)
        if workspace_block:
            blocks.append(workspace_block)
        content = "\n\n".join(block for block in blocks if block.strip())
        return [synthetic_context_message(content, kind=self.name)] if content else []
