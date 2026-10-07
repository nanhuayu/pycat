"""Library organization metadata. Source content keeps its original owner."""
from __future__ import annotations

from dataclasses import dataclass

from pycat.models.contracts.content import ContentRef

ANNOTATION_COLOR = '#2684ff'


@dataclass(frozen=True)
class LibraryTopic:
    id: str
    title: str
    parent_id: str = ''
    has_children: bool = False


@dataclass(frozen=True)
class LibraryItem:
    id: str
    title: str
    ref: ContentRef
    topic_id: str = ''
    favorite: bool = False
    owned: bool = False
    updated_at: str = ''
    evidence: bool = False

    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'ref': self.ref.to_dict(),
                'topic_id': self.topic_id, 'favorite': self.favorite,
                'owned': self.owned, 'updated_at': self.updated_at, 'evidence': self.evidence}
