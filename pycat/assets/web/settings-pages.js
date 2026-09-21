import {node, button, modal, reader, closeModal, toast, markdown} from './ui.js';
import {fieldset, fieldControl, choices, categories, option, getPath, setPath} from './fields.js';
import {renderModels, renderModes, renderMcp, renderCapabilities} from './settings-resources.js';
import {renderChannels} from './channels.js';
import {renderSkills} from './skills.js';
import {renderResourceDiscovery} from './discovery.js';
import {closeSettings} from './config.js';

export const groups = [
  {title: '通用', description: '调整界面与日常操作。', pages: [['general', '外观'], ['shortcuts', '快捷键']]},
  {title: '模型与服务', description: '选择默认模型，管理连接与模型档案。', pages: [['models', '模型与服务']]},
  {title: '运行与权限', description: '设置工作方式、操作边界与上下文策略。', pages: [['modes', '模式'], ['permissions', '权限'], ['strategy', '策略'], ['instructions', '指令与来源']]},
  {title: '工具与能力', description: '设置模型能力与工具运行方式。', pages: [['skills', '技能 Skills'], ['mcp', 'MCP'], ['capabilities', '能力'], ['search', '搜索'], ['automation', '电脑与浏览器'], ['ocr', 'OCR']]},
  {title: '记忆与资料', description: '维护可复用的记忆与项目知识。', pages: [['memory', '记忆与资料']]},
  {title: '消息通道', description: '连接外部消息平台与会话。', pages: [['channels', '消息通道']]},
  {title: '高级与数据', description: '网络、诊断、终端与应用信息。', pages: [['network', '网络与诊断'], ['terminal', '终端'], ['about', '关于']]}
];
const f = (key, label, type = 'str', extra = {}) => ({key, label, type, ...extra});
const options = values => values.map(([value, label]) => option(value, label));
export async function renderSettingsPage(w, page, body) {
  const app = w.draft.app_settings, change = () => w.changed();
  const section = (title, description, fields, object = app) => body.append(fieldset(title, description, object, fields, change));
  const openLibrary = () => closeSettings(() => w.context.library?.());
  if (page === 'models') return renderModels(w, body);
  if (page === 'modes') return renderModes(w, body);
  if (page === 'mcp') return renderResourceDiscovery(w, body, 'mcp', renderMcp);
  if (page === 'skills') return renderResourceDiscovery(w, body, 'skill', renderSkills);
  if (page === 'capabilities') return renderCapabilities(w, body);
  if (page === 'channels') return renderChannels(w, body);
  if (page === 'general') {
    section('界面', '保存后应用于工作台；会话中的独立设置优先。', [
      f('theme', '主题', 'str', {options: options([['light', '浅色'], ['dark', '深色'], ['auto', '跟随系统']])}),
      f('accent', '强调色', 'str', {options: options([['lavender', '薰衣草紫'], ['blue', '蓝色'], ['forest', '森林绿']])}),
      f('show_thinking', '显示思考过程', 'bool'), f('show_stats', '显示辅助栏', 'bool'),
      f('show_sidebar', '显示会话侧栏', 'bool')
    ]);
    section('桌面应用', '以下设置用于 Qt 桌面窗口。', [f('close_to_tray', '关闭窗口后留在托盘', 'bool')]);
  } else if (page === 'shortcuts') {
    section('键盘操作', '输入法组合输入期间不会触发发送快捷键。', []);
    const rows = [['Ctrl / ⌘ K', '搜索命令或设置'], ['Ctrl / ⌘ N', '新建当前工作区会话'], ['Ctrl / ⌘ J', '运行检查'],
      ['Ctrl / ⌘ Shift I', '右侧辅助栏'], ['Alt ↑ / ↓', '上一条 / 下一条消息'], ['Alt Home / End', '首条 / 最新消息'],
      ['Enter 或 Ctrl Enter', '发送消息'], ['Shift Enter', '换行'], ['Ctrl / ⌘ S', '保存设置草稿'], ['Esc', '关闭窗口或停止运行']];
    body.append(node('div', {class: 'shortcut-list'}, rows.map(([key, label]) => node('div', {class: 'setting-row'}, node('span', {}, label), node('kbd', {}, key)))));
    if (Object.keys(app.shortcuts || {}).length) section('桌面快捷键', '修改已保存的桌面绑定。', Object.keys(app.shortcuts).map(key => f(key, key)), app.shortcuts);
  } else if (page === 'permissions') {
    section('工具规则', '三种决策：允许、询问、拒绝。会话可选预设，运行中的批准仍会单独请求。', categories.map(([key, label]) =>
      f('permissions.category_defaults.' + key + '.action', label, 'str', {options: options([['allow', '允许'], ['ask', '询问'], ['deny', '拒绝']])})));
    const overrides = app.permissions.tools || (app.permissions.tools = {});
    body.append(node('h3', {}, '单个工具覆盖'), node('p', {class: 'muted'}, '未单独指定时使用所属类别的规则。'));
    const tools = await w.api.operation('tools.list', {session: w.session?.id || null});
    const select = node('select', {'aria-label': '选择工具'}, tools.map(item => node('option', {value: item.function?.name || item.name}, item.function?.name || item.name)));
    body.append(node('div', {class: 'toolbar'}, select, button('添加规则', () => { overrides[select.value] = {action: 'ask'}; change(); w.navigate(page); })));
    for (const [name, value] of Object.entries(overrides)) {
      const row = fieldControl({label: name, options: options([['allow', '允许'], ['ask', '询问'], ['deny', '拒绝']])}, value.action, action => { value.action = action; change(); });
      row.append(button('恢复类别规则', () => { delete overrides[name]; change(); w.navigate(page); }, 'text-button')); body.append(row);
    }
  } else if (page === 'strategy') {
    section('运行', '模型执行与失败重试使用同一套策略。', [f('agent.max_turns', '最多运行轮数', 'int', {min: 1, max: 1000}),
      f('retry.max_retries', '失败重试次数', 'int', {min: 0}), f('retry.base_delay', '基础重试间隔（秒）', 'float', {min: 0}),
      f('retry.backoff_factor', '退避倍数', 'float', {min: 1})]);
    section('两层压缩', '工具内容压缩应早于上下文压缩；关闭开关会保留阈值。', [
      f('context.compression_policy.tight_replay_enabled', '压缩已读工具内容', 'bool'),
      f('context.compression_policy.tight_replay_threshold_ratio', '工具压缩阈值（0–1）', 'float', {min: .1, max: .94}),
      f('context.agent_auto_compress_enabled', '自动压缩上下文', 'bool'),
      f('context.compression_policy.token_threshold_ratio', '上下文压缩阈值（0–1）', 'float', {min: .11, max: .95})]);
    const rows = [...body.querySelectorAll('.setting-field')];
    const toggle = () => {
      rows[5].querySelector('input').disabled = !app.context.compression_policy.tight_replay_enabled;
      rows[7].querySelector('input').disabled = !app.context.agent_auto_compress_enabled;
    };
    body.addEventListener('change', toggle); toggle();
    section('记忆容量', '降低上限保留已有记忆，并允许精简内容。', [
      f('memory_char_limit', '项目记忆字符数', 'int', {options: [4000, 8000]}),
      f('user_memory_char_limit', '用户偏好字符数', 'int', {options: [4000, 8000]})]);
  } else if (page === 'instructions') {
    section('全局追加指令', '应用于新请求；项目规则、会话指令和记忆保留独立来源。', [
      f('prompts.global_instructions', '追加指令', 'text'), f('prompts.include_environment', '包含环境信息', 'bool'),
      f('prompts.file_tree_max_depth', '项目树深度', 'int', {min: 1, max: 10})]);
    body.append(node('div', {class: 'source-links'},
      button('项目 AGENTS.md', () => w.action(async () => { if (!w.session?.work_dir) throw Error('请先选择项目工作区'); reader('AGENTS.md', (await w.api.operation('materials.read', {session: w.session.id, ref: 'workspace:AGENTS.md'})).text); })),
      button('会话指令', () => closeSettings(() => w.context.sessionSettings?.())), button('记忆与资料', openLibrary)));
  } else if (page === 'search') {
    const providers = await w.api.operation('search.providers');
    const value = w.draft.search_config;
    section('搜索服务', '选择搜索来源并检查连接。', [f('enabled', '启用网络搜索', 'bool'),
      f('provider', '搜索引擎', 'str', {options: providers.map(item => option(item.id, item.name))}),
      f('api_key', 'API Key', 'str', {secret: true}), f('api_base', 'API 地址')], value);
    const update = () => {
      const selected = providers.find(item => item.id === value.provider), rows = body.querySelectorAll('.setting-field');
      rows[2].hidden = !selected?.requires_api_key; rows[3].hidden = !selected?.requires_api_base;
    };
    body.addEventListener('change', update); update();
    section('搜索结果', '', [f('max_results', '结果数量', 'int', {min: 1, max: 20}), f('include_date', '包含日期', 'bool')], value);
    body.append(button('检查连接', event => w.action(async () => {
      const result = await w.api.operation('search.check', {configuration: value}); toast(result[1] || '检查完成');
    }, event.currentTarget), 'secondary'));
  } else if (page === 'automation') {
    section('电脑与浏览器', '浏览器和桌面能力复用 MCP。在 MCP 的“发现”中查看来源并按需安装。', []);
    body.append(button('查找浏览器与桌面 MCP', () => { w.discoveryTarget = 'agent-browser'; w.navigate('mcp'); }, 'primary'));
    body.append(node('p', {class: 'muted'}, '浏览器内截图可通过粘贴或上传加入当前输入。桌面截图快捷键由 Qt 应用提供。'));
  } else if (page === 'ocr') {
    section('文字识别', '在资料阅读器中选择图片或 PDF，识别结果可阅读或复制。', [
      f('ocr.enabled', '启用 OCR', 'bool'), f('ocr.pdf_batch_pages', '每批 PDF 页数', 'int', {min: 1, max: 50})]);
    const status = await w.api.operation('doctor');
    body.append(node('p', {class: 'notice'}, status.ocr.message || status.ocr.reason || JSON.stringify(status.ocr)), button('打开资料', openLibrary));
  } else if (page === 'memory') {
    section('记忆与知识', '记忆保存短事实和偏好；项目知识保留正文与来源；成果属于当前会话。', []);
    body.append(button('打开资料与记忆', openLibrary, 'primary'), node('p', {class: 'muted'}, '会话记忆开关位于会话设置。容量和自动整理策略位于“运行与权限 → 策略”。'),
      button('调整记忆容量', () => w.navigate('strategy')));
  } else if (page === 'network') {
    section('网络与诊断', '', [f('proxy_url', '代理地址', 'str', {placeholder: 'http://127.0.0.1:7890'}),
      f('llm_timeout_seconds', '模型超时（秒）', 'float', {min: 1}), f('log_stream', '记录流式诊断', 'bool')]);
    body.append(button('检查应用状态', () => w.action(async () => reader('应用状态', await w.api.operation('doctor')))));
    body.append(node('h3', {}, '会话数据'), node('div', {class: 'source-links'},
      button('导入会话', () => closeSettings(() => document.querySelector('#import-input').click())),
      button('导出当前会话', () => closeSettings(() => w.context.exportSession?.()))));
  } else if (page === 'terminal') {
    section('终端', '命令在应用宿主执行，仍经过当前权限规则。', [
      f('shell.backend', '默认终端', 'str', {options: ['auto', 'cmd', 'powershell', 'wsl', 'bash', 'zsh', 'sh', 'fish', 'custom']}),
      f('shell.executable', 'Shell 程序'),
      f('shell.wsl_distro', 'WSL 发行版'), f('shell.output_encoding', '输出编码', 'str', {options: ['auto', 'utf-8', 'system', 'gb18030']}),
      f('shell.inherit_env', '继承环境变量', 'bool'), f('shell.bang_command_behavior', '! 命令行为', 'str', {options: options([['shell', '执行终端命令'], ['agent', '交给 Agent']])}),
      f('shell.wait_seconds', '前台等待（秒）', 'int', {min: 5, max: 600})]);
  } else if (page === 'about') {
    const bootstrap = await w.api.json('/bootstrap');
    body.append(node('div', {class: 'about-mark'}, node('img', {src: '/brand.svg', alt: '', width: 44}), node('h2', {}, 'PyCat'), node('p', {class: 'muted'}, bootstrap.version)),
      node('p', {}, '在会话、终端和浏览器中继续同一项工作。'),
      button('检查更新', event => w.action(async () => {
        const result = await w.api.operation('updates.check');
        if (result.status === 'error') throw Error(result.error);
        if (result.status === 'up_to_date') { toast('已是当前稳定版本'); return; }
        modal('可用更新 · ' + result.release.version, [markdown(result.release.body),
          node('a', {href: result.release.html_url, target: '_blank', rel: 'noopener noreferrer'}, '打开发行页面')], [button('关闭', closeModal)]);
      }, event.currentTarget)),
      node('div', {class: 'shortcut-list'}, node('p', {}, 'CLI：pycat exec · pycat resume · pycat model · pycat config'),
        node('p', {}, 'WebUI：pycat serve'), node('p', {}, '附加终端：pycat --endpoint <服务地址>')));
  }
}
