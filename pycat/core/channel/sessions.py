from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelConversationSummary:
    conversation_id: str
    title: str
    updated_at: float = 0.0
    preview: str = ""
    participant_label: str = ""
    is_manual_test_session: bool = False
    is_primary_session: bool = False
    bound_channel_id: str = ""
    bound_channel_name: str = ""
    is_bindable: bool = True
    is_bound_to_other_channel: bool = False


__all__ = ["ChannelConversationSummary"]
