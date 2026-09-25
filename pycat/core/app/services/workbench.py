"""Deterministic client operations over existing application owners.

The catalog also describes forms and CLI arguments. It contains no storage,
agent loop, dynamic method invocation or transport-specific objects.
"""
from __future__ import annotations

import asyncio
import codecs
import inspect
import types
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.app.serialization import json_value, merge_draft, redact
from pycat.core.app.services.search import SearchService
from pycat.core.capabilities import default_capabilities_config
from pycat.core.commands.mentions import MentionCandidate, MentionKind, MentionResolver, utf16_to_python
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.llm.token_budget import build_token_usage_snapshot
from pycat.core.version import __version__
from pycat.models.contracts.agent import ConversationBusyError, InvalidRequestError
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.content import ContentRef
from pycat.models.contracts.mcp import McpServerConfig
from pycat.models.model_profile import MODEL_INPUT_MODALITIES, reasoning_codecs_for_provider
from pycat.models.model_ref import build_model_ref
from pycat.models.provider import SUPPORTED_API_TYPES, Provider, api_type_label
from pycat.models.search_config import SearchConfig


def operation(name, label, *, observed=False):
    def decorate(method):
        method.operation = (name, label)
        method.observed = observed
        return method
    return decorate


class WorkbenchService:
    def __init__(self, *, commands, settings, providers, provider_service, modes, tools, mcp,
                 skills, knowledge, workspace, channels, content, release, ocr, extensions=None):
        self.commands, self.settings = commands, settings
        self.providers, self.provider_service, self.modes = providers, provider_service, modes
        self.tools, self.mcp, self.skills, self.knowledge = tools, mcp, skills, knowledge
        self.workspace, self.channels, self.content = workspace, channels, content
        self.release, self.ocr = release, ocr
        self.extensions = extensions
        self.conversations, self.runs = commands.conversations, commands.runs
        self._operations = {method.operation[0]: method for _, method in inspect.getmembers(self, inspect.ismethod)
                            if hasattr(method, 'operation')}

    @classmethod
    def catalog(cls):
        result = {}
        for _, method in inspect.getmembers(cls, inspect.isfunction):
            if not hasattr(method, 'operation'):
                continue
            name = method.operation[0]
            parameters = []
            for key, arg in inspect.signature(method).parameters.items():
                if key == 'self' or key.startswith('_'):
                    continue
                required = arg.default is inspect.Parameter.empty
                parameters.append({'name': key, 'type': str(arg.annotation) if arg.annotation is not inspect.Parameter.empty else 'str',
                                   'required': required, 'default': None if required else arg.default})
            result[name] = {'label': method.operation[1], 'parameters': parameters, 'observed': method.observed}
        return result

    async def execute(self, name: str, arguments: dict, *, approval_callback=None):
        method = self._operations.get(name)
        if method is None or not isinstance(arguments, dict) or any(key.startswith('_') for key in arguments):
            raise InvalidRequestError(f'Unknown operation or invalid arguments: {name}')
        arguments = dict(arguments)
        signature = inspect.signature(method)
        if '_approval' in signature.parameters:
            arguments['_approval'] = approval_callback
        try:
            signature.bind(**arguments)
        except TypeError as exc:
            raise InvalidRequestError(str(exc)) from exc
        def matches(value, kind):
            if get_origin(kind) in {types.UnionType, Union}:
                return any(matches(value, member) for member in get_args(kind))
            return type(value) is kind if kind in {str, int, bool, float, dict, list, type(None)} else True
        for key, kind in get_type_hints(method).items():
            if key in arguments and not key.startswith('_') and not matches(arguments[key], kind):
                raise InvalidRequestError(f'Invalid type for {key}; expected {kind}.')
        result = await method(**arguments) if inspect.iscoroutinefunction(method) else await asyncio.to_thread(method, **arguments)
        if isinstance(result, tuple) and result and result[0] is False:
            raise InvalidRequestError(str(result[1] if len(result) > 1 else 'Operation failed.'))
        return json_value(result)

    def _session(self, session):
        return self.commands.require_session(session)

    def _provider(self, provider):
        found = next((item for item in self.providers.current() if item.id == provider or item.name == provider), None)
        if found is None:
            raise InvalidRequestError('Provider not found.')
        return found

    def mention_candidates(self, query, context):
        work_dir = context.get('work_dir') or ''
        prefix = query.prefix.casefold()
        result = MentionResolver(work_dir).search(query.prefix)
        if '/' not in prefix and '\\' not in prefix:
            result += [MentionCandidate(item['label'], item['id'], MentionKind(item['kind']),
                       insert_text='@' + ('"' + item['label'] + '"' if ' ' in item['label'] else item['label']) + ' ')
                       for item in self.runs.mention_catalog(work_dir)
                       if prefix in (item['label'] + ' ' + item['id']).casefold()]
        return result[:60]

    def argument_candidates(self, command, prefix, context):
        """Completion metadata shares model/mode/session owners with execution."""
        work_dir = context.get('work_dir') or ''
        if command == 'model':
            options = [(row['ref'], row['provider']) for row in self.models()]
        elif command == 'mode':
            options = [(mode.slug, mode.name) for mode in self.modes.list(work_dir)
                       if mode.is_primary_mode() and mode.slug != 'channel']
        elif command == 'resume':
            options = [(row['id'], row.get('title') or '未命名会话')
                       for row in self.commands.sessions(work_dir=work_dir)]
        else:
            return []
        query = prefix.casefold()
        return [MentionCandidate(f'{value} - {label}', f'{command}:{value}', MentionKind.COMMAND,
                    insert_text=f'/{command} {value}', submit_on_accept=True)
                for value, label in options if query in (value + ' ' + label).casefold()][:30]

    @operation('input.complete', '输入补全')
    def complete(self, text: str, cursor: int, session: str | None = None, work_dir: str = '', utf16: bool = False):
        if len(text) > 262144:
            raise InvalidRequestError('Input exceeds the completion limit.')
        position = utf16_to_python(text, cursor) if utf16 else cursor
        context = {'work_dir': self._session(session).work_dir if session else work_dir}
        result = self.commands.registry.get_command_candidates(text, position, context)
        if result is None:
            result = self.commands.registry.get_mention_candidates(text, position, context)
        if result is None:
            return {'query': None, 'candidates': []}
        query, candidates = result
        return {'query': json_value(query), 'candidates': [item.to_dict() for item in candidates]}

    @operation('sessions.list', '会话列表')
    def sessions(self, work_dir: str | None = None, archived: bool = False, query: str = '', offset: int = 0, limit: int = 100):
        rows = self.commands.sessions(work_dir=work_dir, archived=archived)
        rows = [row for row in rows if query.casefold() in ' '.join(str(row.get(key, ''))
                for key in ('title', 'id', 'work_dir')).casefold()]
        return rows[max(0, offset):max(0, offset) + min(200, max(1, limit))]

    @operation('sessions.create', '新建会话')
    def create(self, work_dir: str = '', title: str = '', model: str | None = None, mode: str = 'chat'):
        return self.read(self.commands.create(work_dir=work_dir, title=title, model=model, mode=mode).id)

    @operation('sessions.read', '会话内容')
    def read(self, session: str, offset: int = -1, limit: int = 100):
        conversation = self._session(session)
        data = conversation.to_dict()
        total = len(data['messages'])
        limit = min(200, max(1, limit))
        start = max(0, total - limit) if offset < 0 else min(total, offset)
        data['messages'] = data['messages'][start:start + limit]
        return {**data, 'revision': self.conversations.view_revision(conversation),
                'message_count': total, 'message_offset': start, 'active': self.conversations.active_operation(session)}

    @operation('sessions.resume', '继续会话')
    def resume(self, target: str = '', last: bool = False, work_dir: str | None = None):
        return self.read(self.commands.resume(target, last=last, work_dir=work_dir).id)

    @operation('sessions.rename', '重命名')
    def rename(self, session: str, title: str):
        self.conversations.update_navigation(session, title=title)
        return self.read(session)

    @operation('sessions.pin', '置顶')
    def pin(self, session: str, pinned: bool = True):
        self.conversations.update_navigation(session, pinned=pinned)
        return self.read(session)

    @operation('sessions.archive', '归档')
    def archive(self, session: str, archived: bool = True):
        self.conversations.update_navigation(session, archived=archived)
        return self.read(session)

    @operation('sessions.delete', '删除会话')
    def delete(self, session: str):
        return {'deleted': self.conversations.delete(session)}

    @operation('sessions.import', '导入会话')
    def import_session(self, path: str):
        conversation = self.conversations.import_from_file(path)
        if conversation is None:
            raise InvalidRequestError('Unsupported or invalid conversation file.')
        return self.read(conversation.id)

    @operation('sessions.export', '导出会话')
    def export_session(self, session: str, destination: str, format: str | None = None):
        return {'path': str(self.conversations.export(session, destination, format=format))}

    @operation('sessions.select', '模型与模式')
    def select(self, session: str, model: str | None = None, mode: str | None = None, expected_revision: str | None = None):
        self.commands.configure(session, model=model, mode=mode, expected_revision=expected_revision)
        return self.read(session)

    @operation('workspace.select', '切换工作区')
    def change_workspace(self, session: str, work_dir: str):
        return self.conversations.change_work_dir(self._session(session), self.workspace.validate(work_dir),
                                                 active_processes=self.tools.processes(session))

    @operation('sessions.settings', '会话设置')
    def session_settings(self, session: str, settings: dict | None = None, llm: dict | None = None,
                         expected_revision: str | None = None):
        if settings is not None or llm is not None:
            if not expected_revision:
                raise InvalidRequestError('Reload session settings before saving.')
            self.commands.configure(session, settings=settings, llm=llm, expected_revision=expected_revision)
        conversation = self._session(session)
        return {'settings': conversation.settings, 'llm': json_value(conversation.get_llm_config()),
                'revision': self.conversations.view_revision(conversation)}

    @operation('sessions.permissions', '工具与文件权限')
    def permissions(self, session: str, tool_approval: str | None = None, filesystem_mode: str | None = None,
                    expected_revision: str | None = None):
        return self.runs.update_access(session, tool_approval=tool_approval, filesystem_mode=filesystem_mode,
                                      expected_revision=expected_revision)

    @operation('sessions.remove', '删除此轮及后续消息')
    def remove_turn(self, session: str, message: str, expected_revision: str):
        token = self.conversations.begin_lifecycle(session, 'remove')
        if not token:
            raise ConversationBusyError('Session is busy.')
        try:
            conversation = self._session(session)
            if expected_revision != self.conversations.view_revision(conversation):
                raise InvalidRequestError('Session changed; reload before removing messages.')
            result = self.conversations.remove_turn(session, message, activity_token=token,
                expected_fingerprint=self.conversations.revision_fingerprint(conversation))
            if not result.ok:
                raise InvalidRequestError(result.error)
            return self.read(session)
        finally:
            self.conversations.end_lifecycle(session, token)

    @operation('sessions.trace', '运行过程')
    def trace(self, session: str, run_id: str = '', cursor: int = 0, limit: int = 100):
        return self.conversations.trace(session, run_id=run_id, cursor=cursor, limit=min(200, max(1, limit)))

    @operation('sessions.tasks', '更新任务')
    def tasks(self, session: str, operations: list, expected_revision: str):
        self.conversations.update_tasks(session, operations, expected_revision=expected_revision)
        return self.read(session)

    @operation('sessions.context', '上下文预算')
    def context(self, session: str):
        conversation = self._session(session)
        config = AppConfig.from_dict(self.settings.load())
        return build_token_usage_snapshot(conversation, providers=self.providers.current(),
            provider_id=conversation.provider_id, provider_name=conversation.provider_name, model_id=conversation.model,
            compact_threshold_ratio=config.context.compression_policy.token_threshold_ratio)

    @operation('sessions.trace-node', '过程详情')
    def trace_node(self, session: str, node: str, run_id: str):
        return self.conversations.trace_node(session, node, run_id=run_id)

    @operation('sessions.compact', '压缩上下文', observed=True)
    async def compact(self, session: str, expected_revision: str | None = None):
        await self.runs.compact(session, expected_revision=expected_revision)
        return await asyncio.to_thread(self.read, session)

    @operation('config.read', '应用配置')
    def config(self):
        return self.settings.view()

    @operation('config.schema', '设置选项')
    def configuration_schema(self):
        return {'api_types': [{'value': key, 'label': api_type_label(key)} for key in sorted(SUPPORTED_API_TYPES)],
                'reasoning_codecs': {key: reasoning_codecs_for_provider(key) for key in SUPPORTED_API_TYPES},
                'input_modalities': sorted(MODEL_INPUT_MODALITIES),
                'capabilities': json_value(default_capabilities_config())}

    @operation('config.update', '保存配置')
    def configure(self, patch: dict, expected_revision: str):
        return self.settings.update(patch, expected_revision=expected_revision)

    @operation('model.list', '可选模型')
    def models(self):
        return [{'ref': build_model_ref(provider.name, model), 'provider': provider.name, 'model': model}
                for provider in self.providers.current() if provider.enabled for model in provider.model_ids()]

    @operation('mode.list', '模式与 Agent 配置')
    def mode_list(self, work_dir: str = ''):
        return self.modes.list(work_dir)

    @operation('providers.check', '测试连接')
    async def provider_check(self, provider: str, configuration: dict | None = None):
        return await self.provider_service.test_connection(self._provider_draft(provider, configuration))

    @operation('providers.discover', '发现模型')
    async def discover(self, provider: str, configuration: dict | None = None):
        return await self.provider_service.fetch_models(self._provider_draft(provider, configuration))

    @operation('providers.model-template', '新模型默认档案')
    def model_template(self, provider: str, configuration: dict | None = None):
        return self._provider_draft(provider, configuration).model_profile_template()

    def _provider_draft(self, provider, configuration):
        if configuration is None:
            return self._provider(provider)
        current = next((item for item in self.providers.current() if item.id == provider), None)
        return Provider.from_dict(merge_draft(json_value(current) if current else {}, configuration))

    @operation('search.providers', '可用搜索引擎')
    def search_providers(self):
        return SearchService.list_providers()

    @operation('search.check', '检查搜索连接')
    async def search_check(self, configuration: dict | None = None):
        current = self.settings.load_snapshot().search_config
        config = SearchConfig.from_dict(merge_draft(json_value(current), configuration)) if configuration is not None else current
        return await SearchService(config).check()

    def _auth(self, provider):
        provider = self._provider(provider)
        auth = self.provider_service.account_auth(provider.auth_type)
        if auth is None:
            raise InvalidRequestError('This provider uses an API key, not account login.')
        return provider, auth

    @operation('providers.status', '账号状态')
    def account_status(self, provider: str):
        provider, auth = self._auth(provider)
        return auth.status(provider.id)

    @operation('providers.login', '开始账号登录')
    def account_login(self, provider: str):
        provider, auth = self._auth(provider)
        flow = auth.begin_login(provider.id)
        try:
            auth.prepare_login(flow)
            return {'provider': provider.id, 'url': flow.authorization_url}
        except BaseException:
            flow.cancel()
            raise

    @operation('providers.finish-login', '等待账号授权', observed=True)
    async def account_finish(self, provider: str):
        provider, auth = self._auth(provider)
        flow = auth.pending_login(provider.id)
        if flow is None:
            raise InvalidRequestError('Start login first.')
        try:
            return await asyncio.to_thread(auth.finish_login, flow)
        finally:
            flow.cancel()

    @operation('providers.logout', '退出账号')
    def account_logout(self, provider: str):
        provider, auth = self._auth(provider)
        auth.logout(provider.id)
        return auth.status(provider.id)

    @operation('tools.list', '可用工具')
    async def tool_list(self, session: str | None = None, work_dir: str | None = None):
        return await self.tools.list(conversation_id=session, work_dir=work_dir)

    @operation('tools.call', '调用工具', observed=True)
    async def tool_call(self, name: str, arguments: dict, session: str | None = None, work_dir: str | None = None, _approval=None):
        receipt = await self.tools.call(name, arguments, conversation_id=session, work_dir=work_dir, approval_callback=_approval)
        return {'id': receipt.id, 'content': receipt.content, 'is_error': receipt.is_error,
                'record': receipt.record, 'session': receipt.conversation.id}

    @operation('processes.list', '后台进程')
    def processes(self, session: str):
        return self.tools.processes(session)

    @operation('processes.stop', '停止进程')
    def stop_process(self, session: str, process: str):
        return self.tools.stop_process(process, conversation_id=session)

    @operation('mcp.list', 'MCP 服务')
    def mcp_list(self):
        return redact(json_value(self.mcp.list()))

    @operation('mcp.import', '解析 mcp.json 草稿')
    def mcp_import(self, configuration: dict, existing_names: list | None = None):
        entries = configuration.get('mcpServers')
        if not isinstance(entries, dict):
            raise InvalidRequestError('文件必须是 {"mcpServers": {...}} 格式。')
        servers, errors, names = [], [], set(existing_names or [])
        for name, raw in entries.items():
            try:
                server = McpServerConfig.from_mcp_json(name, raw)
                if server.name in names:
                    raise ValueError('MCP 服务名称重复')
                names.add(server.name)
                servers.append(server)
            except ValueError as exc:
                errors.append(f'{name}: {exc}')
        return {'servers': servers, 'errors': errors}

    @operation('mcp.validate', '校验 MCP 草稿')
    def mcp_validate(self, configuration: dict):
        return McpServerConfig.from_dict(configuration)

    @operation('mcp.export', '导出 mcp.json')
    def mcp_export(self, servers: list | None = None):
        current = json_value(self.settings.load_snapshot().mcp_servers)
        values = current if servers is None else merge_draft(current, servers)
        configs = [McpServerConfig.from_dict(item) for item in values]
        return {'mcpServers': {item.name: item.to_mcp_json() for item in configs if item.enabled}}

    @operation('mcp.probe', '测试 MCP')
    async def mcp_probe(self, name: str, configuration: dict | None = None):
        config = next((item for item in self.mcp.list() if item.name == name), None)
        if configuration is not None:
            config = McpServerConfig.from_dict(merge_draft(json_value(config) if config else {}, configuration))
        if config is None:
            raise InvalidRequestError('MCP server not found.')
        return await self.mcp.probe(config)

    @operation('skills.list', '技能')
    def skill_list(self, work_dir: str = '', include_disabled: bool = True):
        return self.skills.list_for_workdir(work_dir, include_disabled=include_disabled)

    @operation('skills.copy', '复制为可编辑技能')
    def skill_copy(self, name: str, work_dir: str = '', scope: str = 'global'):
        return self.skills.copy_to_managed(name, work_dir=work_dir, scope=scope)

    @operation('extensions.list', '扩展目录与安装状态')
    def extension_list(self, work_dir: str = '', servers: list | None = None):
        configs = self.mcp.list() if servers is None else [McpServerConfig.from_dict(item) for item in servers]
        return self.extensions.catalog(work_dir=work_dir, servers=configs)

    @operation('extensions.check', '检查扩展更新', observed=True)
    async def extension_check(self, id: str, work_dir: str = '', kind: str = 'mcp'):
        if id == 'agent-browser' and kind == 'mcp':
            return await self.extensions.check_browser()
        if kind == 'skill':
            return await self.extensions.check_skill(id, work_dir=work_dir)
        raise InvalidRequestError('此扩展由外部管理，请前往来源查看更新。')

    @operation('extensions.search', '搜索 MCP 或 Skills 市场', observed=True)
    async def extension_search(self, kind: str, query: str, cursor: str = '', work_dir: str = '', servers: list | None = None):
        configs = self.mcp.list() if servers is None else [McpServerConfig.from_dict(item) for item in servers]
        return await self.extensions.search_market(kind, query, cursor=cursor, work_dir=work_dir, servers=configs)

    @operation('extensions.market-skill', '准备市场技能安装', observed=True)
    async def extension_market_skill(self, repository: str, name: str):
        return await self.extensions.preview_market_skill(repository, name)

    @operation('extensions.skill-preview', '检查 GitHub 技能来源', observed=True)
    async def extension_skill_preview(self, repository: str, path: str, ref: str = 'HEAD'):
        return await self.extensions.preview_skill(repository, path, ref)

    @operation('extensions.install-skill', '安装或更新技能', observed=True)
    async def extension_install_skill(self, plan: dict, scope: str = 'global', work_dir: str = '', overwrite: bool = False):
        return await self.extensions.install_skill(plan, scope=scope, work_dir=work_dir, overwrite=overwrite)

    @operation('extensions.prepare-browser', '安装或更新浏览器驱动', observed=True)
    async def extension_prepare_browser(self, plan: dict, configuration: dict | None = None, browser_path: str = ''):
        existing = None
        if configuration is not None:
            saved = next((item for item in self.mcp.list() if item.name == configuration.get('name')), None)
            existing = McpServerConfig.from_dict(merge_draft(json_value(saved) if saved else {}, configuration))
        prepared = await self.extensions.prepare_browser(plan, existing=existing, browser_path=browser_path)
        return redact(prepared.to_dict())

    @operation('skills.read', '技能详情')
    def skill_read(self, name: str, work_dir: str = ''):
        skill = self.skills.get(name, work_dir=work_dir, include_disabled=True)
        if skill is None:
            raise InvalidRequestError('Skill not found.')
        return skill

    @operation('skills.create', '创建技能')
    def skill_create(self, name: str, description: str = '', work_dir: str = '', scope: str = 'global'):
        return self.skills.create_managed(name, description=description, work_dir=work_dir, scope=scope)

    @operation('skills.save', '保存技能')
    def skill_save(self, name: str, description: str, content: str, work_dir: str = '', scope: str = 'global'):
        return self.skills.upsert_skill_content(name, description=description, content=content, work_dir=work_dir, scope=scope)

    @operation('skills.import', '导入技能')
    def skill_import(self, path: str, work_dir: str = '', scope: str = 'global', overwrite: bool = False):
        return self.skills.import_managed(Path(path), work_dir=work_dir, scope=scope, overwrite=overwrite)

    @operation('skills.enable', '启用或停用技能')
    def skill_enable(self, name: str, enabled: bool, work_dir: str = ''):
        self.skills.set_managed_enabled(self.skill_read(name, work_dir), enabled, work_dir=work_dir)
        return self.skill_read(name, work_dir)

    @operation('skills.delete', '删除技能')
    def skill_delete(self, name: str, work_dir: str = ''):
        self.skills.delete_managed(self.skill_read(name, work_dir), work_dir=work_dir)
        return {'deleted': True}

    @operation('skills.resource', '保存技能资源')
    def skill_resource(self, name: str, relative_path: str, content: str, work_dir: str = '', scope: str = 'global', remove: bool = False):
        return self.skills.write_skill_resource(name, relative_path, content, work_dir=work_dir, scope=scope, remove=remove)

    @operation('skills.candidates', '待审核技能')
    def skill_candidates(self, work_dir: str = ''):
        return self.skills.list_candidates(work_dir)

    @operation('skills.candidate', '技能变更详情')
    def skill_candidate(self, proposal: str, scope: str, work_dir: str = ''):
        return self.skills.candidate_detail(proposal, scope=scope, work_dir=work_dir)

    @operation('skills.publish', '发布或回滚技能')
    def skill_publish(self, proposal: str, scope: str, work_dir: str = '', rollback: bool = False):
        return self.skills.publish_candidate(proposal, scope=scope, work_dir=work_dir, rollback=rollback)

    @operation('skills.evaluate', '评测技能候选', observed=True)
    async def skill_evaluate(self, session: str, proposal: str, scope: str, suite: list):
        conversation = self._session(session)
        provider = self._provider(conversation.provider_id)
        settings = self.settings.load()
        policy = RunPolicyBuilder.build(conversation=conversation, app_settings=settings, source='evaluation')
        return await self.skills.evaluate_candidate(proposal, work_dir=conversation.work_dir, scope=scope,
            suite=suite, runtime=self.runs.runtime, provider=provider, model=conversation.model,
            parent_policy=policy, app_settings=settings)

    @operation('memory.read', '记忆')
    def memory(self, work_dir: str = ''):
        return self.knowledge.memory_snapshot(work_dir)

    @operation('memory.edit', '编辑记忆')
    def memory_edit(self, session: str, target: str, expected_digest: str, text: str = '', entry_id: str = '', forget: bool = False):
        return self.knowledge.edit_memory(self._session(session), target, text=text, entry_id=entry_id,
                                          expected_digest=expected_digest, forget=forget)

    @operation('memory.retry', '重试记忆整理')
    def memory_retry(self, work_dir: str = ''):
        return {'queued': self.knowledge.retry_memory(work_dir)}

    @operation('memory.assign', '分配待归属记忆')
    def memory_assign(self, session: str):
        return self.knowledge.assign_legacy_memory(self._session(session))

    @operation('memory.source', '查看记忆来源')
    def memory_source(self, ref: dict):
        return self.knowledge.source_fragment(ContentRef.from_dict(ref))

    @operation('materials.list', '成果与资料')
    def materials(self, session: str, kind: str = 'all', query: str = '', offset: int = 0, limit: int = 50):
        return self.knowledge.materials(self._session(session), kind=kind, query=query, offset=offset, limit=limit)

    @operation('materials.read', '阅读资料')
    def material_read(self, session: str, ref: str, offset: int = 0, limit: int = 65536):
        resolved = SessionContentResolver(self.content).resolve_content(self._session(session), ref)
        size = resolved.path.stat().st_size
        with resolved.path.open('rb') as stream:
            stream.seek(max(0, offset))
            content = stream.read(min(262144, max(4, limit)))
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        text = decoder.decode(content, final=max(0, offset) + len(content) >= size)
        next_offset = max(0, offset) + len(content) - len(decoder.getstate()[0])
        return {'name': resolved.name, 'mime': resolved.mime, 'text': text,
                'size': size, 'offset': max(0, offset), 'next_offset': next_offset, 'has_more': next_offset < size}

    @operation('materials.promote', '整理为项目知识', observed=True)
    async def promote(self, session: str, ref: dict):
        conversation = self._session(session)
        return await self.knowledge.promote_artifact(conversation, ContentRef.from_dict(ref),
            provider=self._provider(conversation.provider_id))

    @operation('knowledge.read', '阅读项目知识')
    def wiki_read(self, work_dir: str, page: str):
        return self.knowledge.wiki.read(work_dir, page)

    @operation('materials.ocr', '识别附件文字', observed=True)
    async def recognize(self, session: str, ref: str, start_page: int = 1, page_count: int | None = None):
        resolved = SessionContentResolver(self.content).resolve_content(self._session(session), ref)
        return await self.ocr.recognize(resolved.path, source_ref=ref, source_name=resolved.name,
                                        source_mime=resolved.mime, source_digest=resolved.digest,
                                        start_page=start_page, page_count=page_count)

    @operation('knowledge.save', '保存项目知识')
    def wiki_save(self, work_dir: str, page: dict):
        return self.knowledge.save_knowledge(work_dir, page)

    @operation('knowledge.delete', '删除项目知识')
    def wiki_delete(self, work_dir: str, page: dict):
        self.knowledge.delete_knowledge(work_dir, page)
        return {'deleted': True}

    @operation('workspace.connect', '连接 SSH 工作区')
    def connect(self, host: str, port: int | None = None, password: str | None = None):
        return self.workspace.connect(host, port=port, password=password)

    @operation('workspace.directories', '浏览远程目录')
    def directories(self, host: str, path: str, port: int | None = None):
        return self.workspace.directories(host, path, port=port)

    @operation('channels.list', '频道与连接状态')
    def channel_list(self):
        channels = AppConfig.from_dict(self.settings.load()).channels
        return [{'channel': redact(json_value(item)), 'connection': json_value(self.channels.connection_snapshot(item))}
                for item in channels]

    @operation('channels.create', '创建频道草稿')
    def channel_create(self, type: str):
        return self.channels.create(type)

    @operation('channels.types', '可用频道类型')
    def channel_types(self):
        return self.channels.catalog.definitions()

    @staticmethod
    def _login_view(session):
        return {'id': session.id, 'channel': session.channel.id, 'qr_text': session.qr_text,
                'complete': session.is_complete, 'snapshot': json_value(session.snapshot)}

    @operation('channels.login', '微信扫码登录')
    def channel_login(self, channel: str):
        return self._login_view(self.channels.start_client_login(channel))

    @operation('channels.poll-login', '刷新扫码状态')
    def channel_poll_login(self, login: str, verification_code: str = ''):
        session = self.channels.advance_client_login(login, verification_code)
        if session.is_complete:
            snapshot = self.settings.view()
            channels = AppConfig.from_dict(self.settings.load()).channels
            if not any(item.id == session.channel.id for item in channels):
                raise InvalidRequestError('Channel was removed while logging in.')
            outcome = self.channel_save(json_value([session.channel if item.id == session.channel.id else item for item in channels]), snapshot['revision'])
            if not outcome['ok']:
                raise InvalidRequestError(str(outcome['domain_errors'] + outcome['stage_errors']))
        return self._login_view(session)

    @operation('channels.bind', '绑定频道会话')
    def channel_bind(self, channel: str, session: str | None = None):
        channels = AppConfig.from_dict(self.settings.load()).channels
        selected = next((item for item in channels if item.id == channel), None)
        if selected is None:
            raise InvalidRequestError('Channel not found.')
        if session:
            self._session(session)
        updated = self.channels.bind_session(selected, session)
        snapshot = self.settings.view()
        return self.channel_save(json_value([updated if item.id == channel else item for item in channels]), snapshot['revision'])

    @operation('channels.save', '保存频道')
    def channel_save(self, channels: list, expected_revision: str):
        current = self.settings.view()
        if current['revision'] != expected_revision:
            raise InvalidRequestError('Configuration changed; reload before saving.')
        existing = json_value(AppConfig.from_dict(self.settings.load()).channels)
        prepared = self.channels.prepare_for_save(merge_draft(existing, channels))
        return self.settings.update({'app_settings': {'channels': json_value(prepared.channels)}}, expected_revision=expected_revision)

    @operation('updates.check', '检查更新')
    def check_update(self):
        return self.release.check(current_version=__version__)

    @operation('doctor', '诊断')
    def doctor(self):
        return {'models': len(self.models()), 'ocr': json_value(self.ocr.status()),
                'sessions': len(self.conversations.list_all()), 'active_runs': self.runs.active_runs()}
