import {tr} from './i18n.js';
import {node, button, modal, reader, closeModal, toast, markdown} from './ui.js';
import {fieldset, fieldControl, choices, categories, option, getPath, setPath} from './fields.js';
import {renderModels, renderModes, renderMcp, renderCapabilities} from './settings-resources.js';
import {renderChannels} from './channels.js';
import {renderSkills} from './skills.js';
import {renderResourceDiscovery} from './discovery.js';
import {closeSettings} from './config.js';

export const groups = [
  {title: tr("通用"), description: tr("调整界面与日常操作。"), pages: [['general', tr("外观")], ['shortcuts', tr("快捷键")]]},
  {title: tr("模型与服务"), description: tr("选择默认模型，管理连接与模型档案。"), pages: [['models', tr("模型与服务")]]},
  {title: tr("运行与权限"), description: tr("设置工作方式、操作边界与上下文策略。"), pages: [['modes', tr("模式")], ['permissions', tr("权限")], ['strategy', tr("策略")]]},
  {title: tr("工具与能力"), description: tr("设置模型能力与工具运行方式。"), pages: [['skills', tr("技能 Skills")], ['mcp', 'MCP'], ['capabilities', tr("能力")], ['search', tr("搜索")], ['automation', tr("电脑与浏览器")], ['ocr', 'OCR']]},
  {title: tr("记忆与资料"), description: tr("设置全局追加指令，查看项目规则、记忆和资料。"), keywords: 'global instructions prompt agents.md environment 全局指令 项目规则 环境上下文', pages: [['memory', tr("记忆与资料")]]},
  {title: tr("消息通道"), description: tr("连接外部消息平台与会话。"), pages: [['channels', tr("消息通道")]]},
  {title: tr("高级与数据"), description: tr("网络、诊断、终端与应用信息。"), pages: [['network', tr("网络与诊断")], ['terminal', tr("终端")], ['about', tr("关于")]]}
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
    section(tr('语言'), tr('所有界面共用此设置。网页刷新、终端和桌面重新打开后生效。'), [
      f('language', tr('界面语言'), 'str', {options: options([['zh_CN', '简体中文'], ['en', 'English']])})
    ]);
    section(tr("界面"), tr("保存后应用于工作台；会话中的独立设置优先。"), [
      f('theme', tr("主题"), 'str', {options: options([['light', tr("浅色")], ['dark', tr("深色")], ['auto', tr("跟随系统")]])}),
      f('accent', tr("强调色"), 'str', {options: options([['lavender', tr("薰衣草紫")], ['blue', tr("蓝色")], ['forest', tr("森林绿")]])}),
      f('show_thinking', tr("显示思考过程"), 'bool'), f('show_stats', tr("显示辅助栏"), 'bool'),
      f('show_sidebar', tr("显示会话侧栏"), 'bool')
    ]);
    section(tr("桌面应用"), tr("以下设置用于 Qt 桌面窗口。"), [f('close_to_tray', tr("关闭窗口后留在托盘"), 'bool')]);
  } else if (page === 'shortcuts') {
    section(tr("键盘操作"), tr("输入法组合输入期间不会触发发送快捷键。"), []);
    const rows = [['Ctrl / ⌘ K', tr("搜索命令或设置")], ['Ctrl / ⌘ N', tr("新建当前工作区会话")], ['Ctrl / ⌘ J', tr("运行检查")],
      ['Ctrl / ⌘ Shift I', tr("右侧辅助栏")], ['Alt ↑ / ↓', tr("上一条 / 下一条消息")], ['Alt Home / End', tr("首条 / 最新消息")],
      [tr("Enter 或 Ctrl Enter"), tr("发送消息")], ['Shift Enter', tr("换行")], ['Ctrl / ⌘ S', tr("保存设置草稿")], ['Esc', tr("关闭窗口或停止运行")]];
    body.append(node('div', {class: 'shortcut-list'}, rows.map(([key, label]) => node('div', {class: 'setting-row'}, node('span', {}, label), node('kbd', {}, key)))));
    if (Object.keys(app.shortcuts || {}).length) section(tr("桌面快捷键"), tr("修改已保存的桌面绑定。"), Object.keys(app.shortcuts).map(key => f(key, key)), app.shortcuts);
  } else if (page === 'permissions') {
    section(tr("工具规则"), tr("三种决策：允许、询问、拒绝。会话可选预设，运行中的批准仍会单独请求。"), categories.map(([key, label]) =>
      f('permissions.category_defaults.' + key + '.action', label, 'str', {options: options([['allow', tr("允许")], ['ask', tr("询问")], ['deny', tr("拒绝")]])})));
    const overrides = app.permissions.tools || (app.permissions.tools = {});
    body.append(node('h3', {}, tr("单个工具覆盖")), node('p', {class: 'muted'}, tr("未单独指定时使用所属类别的规则。")));
    const tools = await w.api.operation('tools.list', {session: w.session?.id || null});
    const select = node('select', {'aria-label': tr("选择工具")}, tools.map(item => node('option', {value: item.function?.name || item.name}, item.function?.name || item.name)));
    body.append(node('div', {class: 'toolbar'}, select, button(tr("添加规则"), () => { overrides[select.value] = {action: 'ask'}; change(); w.navigate(page); })));
    for (const [name, value] of Object.entries(overrides)) {
      const row = fieldControl({label: name, options: options([['allow', tr("允许")], ['ask', tr("询问")], ['deny', tr("拒绝")]])}, value.action, action => { value.action = action; change(); });
      row.append(button(tr("恢复类别规则"), () => { delete overrides[name]; change(); w.navigate(page); }, 'text-button')); body.append(row);
    }
  } else if (page === 'strategy') {
    section(tr("运行"), tr("模型执行与失败重试使用同一套策略。"), [f('agent.max_turns', tr("最多运行轮数"), 'int', {min: 1, max: 1000}),
      f('retry.max_retries', tr("失败重试次数"), 'int', {min: 0}), f('retry.base_delay', tr("基础重试间隔（秒）"), 'float', {min: 0}),
      f('retry.backoff_factor', tr("退避倍数"), 'float', {min: 1})]);
    section(tr("两层压缩"), tr("工具内容压缩应早于上下文压缩；关闭开关会保留阈值。"), [
      f('context.compression_policy.tight_replay_enabled', tr("压缩已读工具内容"), 'bool'),
      f('context.compression_policy.tight_replay_threshold_ratio', tr("工具压缩阈值（0–1）"), 'float', {min: .1, max: .94}),
      f('context.agent_auto_compress_enabled', tr("自动压缩上下文"), 'bool'),
      f('context.compression_policy.token_threshold_ratio', tr("上下文压缩阈值（0–1）"), 'float', {min: .11, max: .95})]);
    const rows = [...body.querySelectorAll('.setting-field')];
    const toggle = () => {
      rows[5].querySelector('input').disabled = !app.context.compression_policy.tight_replay_enabled;
      rows[7].querySelector('input').disabled = !app.context.agent_auto_compress_enabled;
    };
    body.addEventListener('change', toggle); toggle();
    section(tr("记忆容量"), tr("降低上限保留已有记忆，并允许精简内容。"), [
      f('memory_char_limit', tr("项目记忆字符数"), 'int', {options: [4000, 8000]}),
      f('user_memory_char_limit', tr("用户偏好字符数"), 'int', {options: [4000, 8000]})]);
  } else if (page === 'search') {
    const providers = await w.api.operation('search.providers');
    const value = w.draft.search_config;
    section(tr("搜索服务"), tr("选择搜索来源并检查连接。"), [f('enabled', tr("启用网络搜索"), 'bool'),
      f('provider', tr("搜索引擎"), 'str', {options: providers.map(item => option(item.id, item.name))}),
      f('api_key', 'API Key', 'str', {secret: true}), f('api_base', tr("API 地址"))], value);
    const update = () => {
      const selected = providers.find(item => item.id === value.provider), rows = body.querySelectorAll('.setting-field');
      rows[2].hidden = !selected?.requires_api_key; rows[3].hidden = !selected?.requires_api_base;
    };
    body.addEventListener('change', update); update();
    section(tr("搜索结果"), '', [f('max_results', tr("结果数量"), 'int', {min: 1, max: 20}), f('include_date', tr("包含日期"), 'bool')], value);
    body.append(button(tr("检查连接"), event => w.action(async () => {
      const result = await w.api.operation('search.check', {configuration: value}); toast(result[1] || tr("检查完成"));
    }, event.currentTarget), 'secondary'));
  } else if (page === 'automation') {
    section(tr("电脑与浏览器"), tr("浏览器和桌面能力复用 MCP。在 MCP 的“发现”中查看来源并按需安装。"), []);
    body.append(button(tr("查找浏览器与桌面 MCP"), () => { w.discoveryTarget = 'agent-browser'; w.navigate('mcp'); }, 'primary'));
    body.append(node('p', {class: 'muted'}, tr("浏览器内截图可通过粘贴或上传加入当前输入。桌面截图快捷键由 Qt 应用提供。")));
  } else if (page === 'ocr') {
    section(tr("文字识别"), tr("在资料阅读器中选择图片或 PDF，识别结果可阅读或复制。"), [
      f('ocr.enabled', tr("启用 OCR"), 'bool'), f('ocr.pdf_batch_pages', tr("每批 PDF 页数"), 'int', {min: 1, max: 50})]);
    const status = await w.api.operation('doctor');
    body.append(node('p', {class: 'notice'}, status.ocr.message || status.ocr.reason || JSON.stringify(status.ocr)), button(tr("打开资料"), openLibrary));
  } else if (page === 'memory') {
    section(tr("全局追加指令"), tr("应用于所有模式；留空使用默认行为。"), [
      f('prompts.global_instructions', tr("内容"), 'text')]);
    section(tr("环境上下文"), '', [f('prompts.include_environment', tr("包含环境信息"), 'bool'),
      f('prompts.file_tree_max_depth', tr("项目树深度"), 'int', {min: 1, max: Math.max(10, app.prompts.file_tree_max_depth)})]);
    const materials = fieldset(tr("项目与资料"), tr("项目规则、会话指令和记忆保留独立来源。"), app, [], change);
    const project = button(tr("查看项目 AGENTS.md"), () => w.action(async () => {
      if (!w.session?.work_dir) throw Error(tr("请先选择项目工作区"));
      reader('AGENTS.md', (await w.api.operation('materials.read', {session: w.session.id, ref: 'workspace:AGENTS.md'})).text);
    }));
    project.disabled = !w.session?.work_dir;
    project.title = w.session?.work_dir || tr("当前未选择项目");
    materials.append(node('div', {class: 'source-links'}, project, button(tr("打开记忆与资料"), openLibrary)));
    body.append(materials);
  } else if (page === 'network') {
    section(tr("网络与诊断"), '', [f('proxy_url', tr("代理地址"), 'str', {placeholder: 'http://127.0.0.1:7890'}),
      f('llm_timeout_seconds', tr("模型超时（秒）"), 'float', {min: 1}), f('log_stream', tr("记录流式诊断"), 'bool')]);
    body.append(button(tr("检查应用状态"), () => w.action(async () => reader(tr("应用状态"), await w.api.operation('doctor')))));
    body.append(node('h3', {}, tr("会话数据")), node('div', {class: 'source-links'},
      button(tr("导入会话"), () => closeSettings(() => document.querySelector('#import-input').click())),
      button(tr("导出当前会话"), () => closeSettings(() => w.context.exportSession?.()))));
  } else if (page === 'terminal') {
    section(tr("终端"), tr("命令在应用宿主执行，仍经过当前权限规则。"), [
      f('shell.backend', tr("默认终端"), 'str', {options: ['auto', 'cmd', 'powershell', 'wsl', 'bash', 'zsh', 'sh', 'fish', 'custom']}),
      f('shell.executable', tr("Shell 程序")),
      f('shell.wsl_distro', tr("WSL 发行版")), f('shell.output_encoding', tr("输出编码"), 'str', {options: ['auto', 'utf-8', 'system', 'gb18030']}),
      f('shell.inherit_env', tr("继承环境变量"), 'bool'), f('shell.bang_command_behavior', tr("! 命令行为"), 'str', {options: options([['shell', tr("执行终端命令")], ['agent', tr("交给 Agent")]])}),
      f('shell.wait_seconds', tr("前台等待（秒）"), 'int', {min: 5, max: 600})]);
  } else if (page === 'about') {
    const bootstrap = await w.api.json('/bootstrap');
    body.append(node('div', {class: 'about-mark'}, node('img', {src: '/brand.svg', alt: '', width: 44}), node('h2', {}, 'PyCat'), node('p', {class: 'muted'}, bootstrap.version)),
      node('p', {}, tr("在会话、终端和浏览器中继续同一项工作。")),
      button(tr("检查更新"), event => w.action(async () => {
        const result = await w.api.operation('updates.check');
        if (result.status === 'error') throw Error(result.error);
        if (result.status === 'up_to_date') { toast(tr("已是当前稳定版本")); return; }
        modal(tr("可用更新 · ") + result.release.version, [markdown(result.release.body),
          node('a', {href: result.release.html_url, target: '_blank', rel: 'noopener noreferrer'}, tr("打开发行页面"))], [button(tr("关闭"), closeModal)]);
      }, event.currentTarget)),
      node('div', {class: 'shortcut-list'}, node('p', {}, 'CLI：pycat exec · pycat resume · pycat model · pycat config'),
        node('p', {}, 'WebUI：pycat serve'), node('p', {}, tr("附加终端：pycat --endpoint <服务地址>"))));
  }
}
