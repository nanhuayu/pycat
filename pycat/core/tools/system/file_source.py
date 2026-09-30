"""Resolve file tool inputs through their existing permission and content owners."""
from copy import copy
from pathlib import Path

from pycat.core.content.mime import guess_mime
from pycat.core.content.resolver import ResolvedContent, SessionContentResolver
from pycat.core.tools.base import ToolContext
from pycat.models.contracts.content import ContentRef


def resolve_file_source(path_text: str, context: ToolContext, *, archive_images: bool = False,
                        max_bytes: int | None = None) -> ResolvedContent:
    if path_text.startswith('input:') or (archive_images and path_text.startswith('archive:')):
        if context.conversation is None or context.content_service is None:
            raise ValueError('Content references require an active conversation and its content service.')
        if path_text.startswith('archive:') and '/images/' not in path_text:
            raise ValueError('Use archive__read for archived text, or an archive:<id>/images/<number> image reference.')
        resolver = SessionContentResolver(context.content_service)
        try:
            return resolver.resolve_content(context.conversation, path_text)
        except FileNotFoundError:
            parent_id = str(context.conversation.settings.get('parent_session_id') or '')
            if not path_text.startswith('archive:') or not parent_id:
                raise
            parent = copy(context.conversation)
            parent.id = parent_id
            return resolver.resolve_content(parent, path_text)

    path = context.resolve_read_path(path_text.removeprefix('workspace:'))
    if context.files:
        options = {'max_bytes': max_bytes} if max_bytes is not None else {}
        local_path = context.workspace_service.materialize(
            context.conversation, str(path), files=context.files, **options,
        )
        ref = ContentRef(id=str(path), name=path.name, mime=guess_mime(path.name),
            size=local_path.stat().st_size, digest='', ref=f'workspace:{path}', kind='workspace', workspace=context.work_dir)
        return ResolvedContent(local_path, ref)
    if not path.is_file():
        raise FileNotFoundError(f'Not a file: {path_text}')
    relative = ''
    if context.work_dir:
        try:
            relative = path.relative_to(Path(context.work_dir).expanduser().resolve()).as_posix()
        except ValueError:
            pass
    return ResolvedContent(path, ContentRef(
        id=relative or str(path), name=path.name, mime=guess_mime(path.name),
        size=path.stat().st_size, digest='',
        ref=f'workspace:{relative}' if relative else f'file:{path.as_posix()}',
        kind='workspace' if relative else 'file', source='workspace' if relative else 'local',
    ))
