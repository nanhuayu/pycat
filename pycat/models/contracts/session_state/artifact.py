from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class SessionArtifact:
    """A model-managed session artifact such as a plan, report, note, or reference.

    Artifacts are not memory and are not project instructions. Prompt assembly
    injects only their index/abstract by default; tools can read full content
    when needed.
    """
    name: str
    content: str = ""
    abstract: str = ""
    kind: str = ""
    status: str = "draft"
    references: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    content_path: str = ""
    content_digest: str = ""
    content_chars: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'content': self.content,
            'abstract': self.abstract,
            'kind': self.kind,
            'status': self.status,
            'references': list(self.references),
            'related': list(self.related),
            'frontmatter': dict(self.frontmatter),
            'content_path': self.content_path,
            'content_digest': self.content_digest,
            'content_chars': self.content_chars,
            'updated_seq': self.updated_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionArtifact':
        return cls(
            name=data.get('name', ''),
            content=data.get('content', ''),
            abstract=data.get('abstract', ''),
            kind=data.get('kind', ''),
            status=data.get('status', 'draft'),
            references=[str(item) for item in (data.get('references', []) or []) if str(item).strip()],
            related=[str(item) for item in (data.get('related', []) or []) if str(item).strip()],
            frontmatter=dict(data.get('frontmatter', {}) or {}) if isinstance(data.get('frontmatter', {}), dict) else {},
            content_path=data.get('content_path', ''),
            content_digest=data.get('content_digest', ''),
            content_chars=int(data.get('content_chars', 0) or 0),
            updated_seq=data.get('updated_seq', 0),
        )


