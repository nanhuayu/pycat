from typing import Dict, List, Tuple

from models.state import SessionDocument, SessionState


class DocumentService:
    @staticmethod
    def normalize_name(name: object) -> str:
        return str(name or "").strip().lower()

    @staticmethod
    def list_documents(state: SessionState) -> List[Tuple[str, SessionDocument]]:
        return [(name, doc) for name, doc in state.documents.items() if str(name or "").strip()]

    @staticmethod
    def normalize_references(references: object) -> List[str]:
        if not isinstance(references, list):
            return []
        out: List[str] = []
        for item in references:
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def upsert_document(
        state: SessionState,
        *,
        name: str,
        content: str,
        current_seq: int,
        abstract: object = None,
        kind: object = None,
        references: object = None,
    ) -> SessionDocument:
        normalized = DocumentService.normalize_name(name)
        doc = state.ensure_document(normalized)
        doc.content = str(content or "")
        if abstract is not None:
            doc.abstract = str(abstract or "").strip()
        if kind is not None:
            doc.kind = str(kind or "").strip().lower()
        if references is not None:
            doc.references = DocumentService.normalize_references(references)
        doc.updated_seq = int(current_seq)
        return doc

    @staticmethod
    def append_document(
        state: SessionState,
        *,
        name: str,
        content: str,
        current_seq: int,
        abstract: object = None,
        kind: object = None,
        references: object = None,
    ) -> SessionDocument:
        normalized = DocumentService.normalize_name(name)
        doc = state.ensure_document(normalized)
        addition = str(content or "")
        if doc.content and addition:
            doc.content += "\n" + addition
        elif addition:
            doc.content = addition
        if abstract is not None:
            doc.abstract = str(abstract or "").strip()
        if kind is not None:
            doc.kind = str(kind or "").strip().lower()
        if references is not None:
            doc.references = DocumentService.normalize_references(references)
        doc.updated_seq = int(current_seq)
        return doc

    @staticmethod
    def delete_document(state: SessionState, *, name: str) -> bool:
        normalized = DocumentService.normalize_name(name)
        if normalized not in state.documents:
            return False
        del state.documents[normalized]
        return True

    @staticmethod
    def sync_context_state(context_state: Dict[str, object], state: SessionState) -> None:
        context_state.clear()
        context_state.update(state.to_dict())