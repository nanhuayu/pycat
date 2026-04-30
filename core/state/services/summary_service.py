from typing import List, Optional, Any
from models.state import SessionState

class SummaryService:
    @staticmethod
    def update_summary(state: SessionState, new_summary: str, current_seq: int) -> str:
        old_summary_len = len(state.summary)
        state.summary = new_summary
        state.last_updated_seq = current_seq
        return f"✅ Summary updated manually ({old_summary_len} → {len(state.summary)} chars)"

    @staticmethod
    async def archive_context(
        state: SessionState, 
        llm_client: Any, 
        conversation: Any, 
        provider: Any,
        current_seq: int,
        keep_last_n: int = 3
    ) -> List[str]:
        feedback = []
        
        if not llm_client or not conversation or not provider:
            feedback.append("⚠️ Cannot archive: Context missing (client/conversation/provider).")
            return feedback

        # Use deferred import to avoid circular dependencies if any (though here it should be clean)
        from core.context.condenser import ContextCondenser
        condenser = ContextCondenser(llm_client)
        
        feedback.append("⏳ Archiving context... (this uses LLM)")
        
        try:
            success = await condenser.condense_state(
                conversation=conversation,
                provider=provider,
                state=state,
                keep_last_n=keep_last_n
            )
            if success:
                state.last_updated_seq = current_seq
                feedback.append("📦 Context archived. Old summary saved.")
            else:
                feedback.append("⚠️ Archive skipped (nothing to condense or error).")
        except Exception as e:
            feedback.append(f"❌ Archive failed: {str(e)}")
            
        return feedback
