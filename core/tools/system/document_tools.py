"""CRUD for long-form session documents stored inside SessionState."""

from typing import Dict, Any

from core.state.services.document_service import DocumentService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState


class ManageDocumentTool(BaseTool):
    """Create, read, update, or delete session-level documents."""

    @property
    def name(self) -> str:
        return "manage_document"

    @property
    def description(self) -> str:
        return (
            "Manage session-level persistent documents that survive context condensation. "
            "Use this to maintain a 'plan', 'memory', 'notes', or any named document "
            "across the conversation. Documents are always visible in the system prompt.\n\n"
            "Actions:\n"
            "- create/update: Set or replace a document's content\n"
            "- read: Read a document's full content\n"
            "- delete: Remove a document\n"
            "- list: List all documents\n"
            "- append: Append text to an existing document"
        )

    @property
    def group(self) -> str:
        return "modes"

    @property
    def category(self) -> str:
        return "misc"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "read", "update", "delete", "list", "append"],
                    "description": "The action to perform"
                },
                "name": {
                    "type": "string",
                    "description": "Document name (e.g. 'plan', 'memory', 'notes'). Required for all actions except 'list'."
                },
                "content": {
                    "type": "string",
                    "description": "Document content. Required for create/update/append."
                },
                "abstract": {
                    "type": "string",
                    "description": "Short abstract for indexing and prompt previews."
                },
                "kind": {
                    "type": "string",
                    "description": "Optional document kind, e.g. plan, note, report, reference."
                },
                "references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Important file paths, URLs, or code locations related to the document."
                },
            },
            "required": ["action"],
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments.get("action", "")
        name = DocumentService.normalize_name(arguments.get("name"))
        content = arguments.get("content", "")
        abstract = arguments.get("abstract") if "abstract" in arguments else None
        kind = arguments.get("kind") if "kind" in arguments else None
        references = arguments.get("references") if "references" in arguments else None

        state = SessionState.from_dict(dict(context.state or {}))
        seq = int((context.state or {}).get("_current_seq", 0))

        if action == "list":
            documents = DocumentService.list_documents(state)
            if not documents:
                return ToolResult("No documents in this session.")
            lines = []
            for doc_name, doc in documents:
                preview = (doc.content[:80] + "...") if len(doc.content) > 80 else doc.content
                lines.append(f"- **{doc_name}** ({len(doc.content)} chars): {preview}")
            return ToolResult("\n".join(lines))

        if not name:
            return ToolResult("'name' is required for this action.", is_error=True)

        if action in ("create", "update"):
            DocumentService.upsert_document(
                state,
                name=name,
                content=content,
                current_seq=seq,
                abstract=abstract,
                kind=kind,
                references=references,
            )
            state.last_updated_seq = seq
            DocumentService.sync_context_state(context.state, state)
            return ToolResult(f"Document '{name}' saved ({len(content)} chars).")

        elif action == "append":
            doc = DocumentService.append_document(
                state,
                name=name,
                content=content,
                current_seq=seq,
                abstract=abstract,
                kind=kind,
                references=references,
            )
            state.last_updated_seq = seq
            DocumentService.sync_context_state(context.state, state)
            total = len(state.documents[name].content)
            return ToolResult(f"Appended to '{name}' (total {total} chars).")

        elif action == "read":
            doc = state.documents.get(name)
            if doc is None:
                return ToolResult(f"Document '{name}' not found.", is_error=True)
            return ToolResult(doc.content)

        elif action == "delete":
            if not DocumentService.delete_document(state, name=name):
                return ToolResult(f"Document '{name}' not found.", is_error=True)
            state.last_updated_seq = seq
            DocumentService.sync_context_state(context.state, state)
            return ToolResult(f"Document '{name}' deleted.")

        return ToolResult(f"Unknown action: {action}", is_error=True)
