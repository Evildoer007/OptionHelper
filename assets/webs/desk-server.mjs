import { createReadStream, promises as fs } from 'node:fs';
import { createServer } from 'node:http';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { currentActivation, importLicense } from './core/license.mjs';
import { deleteApiKey, readApiKey, saveApiKey } from './core/keychain.mjs';
import { clearConnection, getConnection, getConnections, getModePreference, getWorkspaceTaskPreference, saveConnection, setActiveConnection, setModePreference, setWorkspaceTaskPreference } from './core/preferences.mjs';
import { createReport, deleteReport, listReports, readReport } from './core/report-store.mjs';
import { appendMessage as appendTaskMessage, contextForTask, createTask, deleteTask, listMessages, listTasks, messagesForSummary, readTask, saveContextSummary, saveModule, titleFromFirstPrompt, updateTask } from './core/task-store.mjs';
import { contractFor } from './parameter-contracts.js';

const repoRoot = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const webRoot = resolve(process.env.OPTIONHELPER_WEB_ROOT || resolve(repoRoot, 'assets', 'webs'));
const payoffOutputRoot = resolve(process.env.OPTIONHELPER_PAYOFF_ROOT || resolve(repoRoot, 'modules', 'payoffer', 'figures'));
const logoPath = resolve(process.env.OPTIONHELPER_LOGO_PATH || (process.env.OPTIONHELPER_WEB_ROOT
  ? resolve(webRoot, '..', 'icons', 'optionhelper-logo.svg')
  : resolve(repoRoot, 'assets', 'icons', 'optionhelper-logo.svg')));
const markPath = resolve(process.env.OPTIONHELPER_MARK_PATH || (process.env.OPTIONHELPER_WEB_ROOT
  ? resolve(webRoot, '..', 'icons', 'optionhelper-mark.svg')
  : resolve(repoRoot, 'assets', 'icons', 'optionhelper-mark.svg')));
const port = Number(process.env.OPTIONHELPER_DESK_PORT || 4180);
// The launcher checks this endpoint before it reuses a listening process. Bump it
// whenever a client-facing API required by the current web shell changes.
const serviceVersion = '2026.07.30.6';
const maxBodyBytes = 256 * 1024;
const localOrigins = new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`]);

const mimeTypes = new Map([
  ['.css', 'text/css; charset=utf-8'], ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'], ['.svg', 'image/svg+xml'],
]);

const providerCatalog = {
  deepseek: { id: 'deepseek', label: 'DeepSeek', protocol: 'openai', baseUrl: 'https://api.deepseek.com', defaultModel: 'deepseek-chat', supportsThinking: true },
  openai: { id: 'openai', label: 'OpenAI', protocol: 'openai', baseUrl: 'https://api.openai.com/v1', defaultModel: '', supportsThinking: false },
  anthropic: { id: 'anthropic', label: 'Anthropic', protocol: 'anthropic', baseUrl: 'https://api.anthropic.com/v1/messages', defaultModel: '', supportsThinking: false },
  gemini: { id: 'gemini', label: 'Google Gemini', protocol: 'gemini', baseUrl: 'https://generativelanguage.googleapis.com', defaultModel: '', supportsThinking: false },
  compatible: { id: 'compatible', label: 'OpenAI兼容接口', protocol: 'openai', baseUrl: '', defaultModel: '', supportsThinking: false },
};

const sendJson = (response, status, body, headers = {}) => {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers });
  response.end(JSON.stringify(body));
};

const sendRedirect = (response, location) => {
  response.writeHead(302, { Location: location, 'Cache-Control': 'no-store' });
  response.end();
};

const readJson = async (request) => {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) throw new Error('请求内容过大。');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); } catch { throw new Error('请求格式无效。'); }
};

const isSameOrigin = (request) => !request.headers.origin || localOrigins.has(request.headers.origin);

const requireSameOrigin = (request, response) => {
  if (isSameOrigin(request)) return true;
  sendJson(response, 403, { error: '来源校验未通过。' });
  return false;
};

const profileFor = async (response) => {
  const activation = await currentActivation();
  if (!activation) {
    sendJson(response, 401, { activated: false, error: '尚未导入有效许可证。' });
    return null;
  }
  return activation;
};

const publicConnection = async (connection) => {
  let keyConfigured = false;
  try { await readApiKey(connection.provider); keyConfigured = true; } catch { /* metadata may outlive a deleted Keychain item */ }
  return { ...connection, providerLabel: providerCatalog[connection.provider].label, keyConfigured };
};

const publicProfile = async (activation) => {
  const connections = await Promise.all((await getConnections()).map(publicConnection));
  const activeConnectionId = (await getConnection())?.id || null;
  const connection = connections.find((item) => item.id === activeConnectionId) || null;
  const preferredMode = await getModePreference();
  const allowedModes = activation.profile.allowedModes;
  return {
    activated: true,
    licenseId: activation.license.licenseId,
    holder: activation.license.holder,
    tier: activation.profile.tier,
    allowedModes,
    preferredMode: allowedModes.includes(preferredMode) ? preferredMode : 'chat',
    connection,
    connections,
    activeConnectionId,
  };
};

const requireDesk = (activation, response) => {
  if (activation.profile.allowedModes.includes('desk')) return true;
  sendJson(response, 403, { error: '当前许可证不包含OptDesk能力。' });
  return false;
};

const taskModeFrom = (url) => url.searchParams.get('mode') === 'desk' ? 'desk' : 'chat';

const requireTaskMode = (activation, mode) => {
  if (mode === 'desk' && !activation.profile.allowedModes.includes('desk')) {
    const error = new Error('当前许可证不包含OptDesk能力。');
    error.statusCode = 403;
    throw error;
  }
};

const readTaskForMode = async (taskId, mode, activation) => {
  requireTaskMode(activation, mode);
  const loaded = await readTask(taskId);
  if (mode === 'chat' && loaded.task.scope !== 'chat') {
    const error = new Error('Chat不能读取独立Desk任务。');
    error.statusCode = 403;
    throw error;
  }
  return loaded;
};

const assertReportMode = (report, mode) => {
  if (!report) {
    const error = new Error('未找到Report。');
    error.statusCode = 404;
    throw error;
  }
  if (mode === 'chat' && report.format !== 'simple') {
    const error = new Error('Chat仅显示简版Report。');
    error.statusCode = 403;
    throw error;
  }
  return report;
};

const normalizeBaseUrl = (value, provider) => {
  const raw = (typeof value === 'string' && value.trim()) ? value.trim() : provider.baseUrl;
  if (!raw) throw new Error('请填写接口地址。');
  let parsed;
  try { parsed = new URL(raw); } catch { throw new Error('接口地址格式无效。'); }
  const localHttp = parsed.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(parsed.hostname);
  if (parsed.protocol !== 'https:' && !localHttp) throw new Error('接口地址必须使用HTTPS，本机服务除外。');
  return raw.replace(/\/+$/, '');
};

const connectionFromRequest = (input) => {
  const provider = providerCatalog[input?.provider];
  if (!provider) throw new Error('请选择支持的模型服务。');
  const model = typeof input?.model === 'string' ? input.model.trim() : '';
  if (!model) throw new Error('请填写模型名称。');
  return {
    provider: provider.id,
    model,
    baseUrl: normalizeBaseUrl(input.baseUrl, provider),
    thinking: input.thinking === 'enabled' && provider.supportsThinking ? 'enabled' : 'disabled',
  };
};

const safeProviderError = (status, detail) => {
  if (status === 401 || status === 403) return '模型服务拒绝了凭据。请检查API Key和账户权限。';
  if (status === 402 || status === 429) return '模型服务额度不足或请求过于频繁。请检查账户余额和限额。';
  if (status >= 500) return '模型服务暂时不可用，请稍后重试。';
  return detail || '模型服务请求失败。';
};

const requestJson = async (url, options) => {
  let response;
  try {
    response = await fetch(url, { ...options, signal: AbortSignal.timeout(60_000) });
  } catch (error) {
    if (error.name === 'TimeoutError') throw new Error('模型服务响应超时，请稍后重试。');
    throw new Error('无法连接模型服务。请检查网络和接口地址。');
  }
  let body = null;
  try { body = await response.json(); } catch { /* response detail intentionally ignored */ }
  if (!response.ok) throw new Error(safeProviderError(response.status, typeof body?.error?.message === 'string' ? body.error.message : ''));
  return body;
};

const systemPrompt = (mode) => {
  if (mode === 'summary') return '你负责压缩OptionHelper本机任务的较早对话。保留研究标的、产品条款、关键判断、假设、风险、待确认事项和用户约束。用不超过1200字的专业中文纯文本，不要输出HTML、报告区块或新的结论。';
  return `你是OptionHelper中的结构化产品研究助手。若需要自称，只能使用“OptionHelper”，不得使用“OptChat”或其他产品名称。用专业、简洁的中文回答，避免编造市场数据、价格、回测结果或已执行的计算。\n\n${mode === 'desk'
    ? '当前为Desk工作模式。每个任务仅研究一只标的。不要把真正多标的结构拆成多个单标的结构，也不要推荐需相关性矩阵或最差表现规则的多标的产品。只有在用户问题已经形成可交付研究结论时，在回答正文之后附上一个且仅一个<opt-report>JSON</opt-report>区块。JSON格式为{"format":"full","title":"","conclusion":"","suitability":"","keyTerms":[""],"risks":[""],"taskBackground":"","moduleSources":["Payoff"],"parameters":[""],"valuationAssumptions":[""],"backtestStatus":"未接入计算","runRecord":"原型状态","pendingItems":[""],"disclaimer":""}。若无法形成报告，不要输出该区块。'
    : '当前为对话模式。只有在用户问题已经形成可交付结论时，在回答正文之后附上一个且仅一个<opt-report>JSON</opt-report>区块。JSON格式为{"format":"simple","title":"","conclusion":"","suitability":"","keyTerms":[""],"risks":[""],"disclaimer":""}。若无法形成报告，不要输出该区块。'}\n报告区块以外不要使用HTML。`;
};

const callModel = async (connection, apiKey, messages, mode) => {
  const provider = providerCatalog[connection.provider];
  const baseUrl = connection.baseUrl || provider.baseUrl;
  const modelMessages = [{ role: 'system', content: systemPrompt(mode) }, ...messages];
  if (provider.protocol === 'openai') {
    const body = { model: connection.model, messages: modelMessages, temperature: 0.2 };
    if (connection.provider === 'deepseek') body.thinking = { type: connection.thinking };
    const json = await requestJson(`${baseUrl.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST', headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const content = json?.choices?.[0]?.message?.content;
    if (typeof content !== 'string' || !content.trim()) throw new Error('模型服务没有返回可用内容。');
    return content.trim();
  }
  if (provider.protocol === 'anthropic') {
    const system = modelMessages.shift().content;
    const json = await requestJson(baseUrl, {
      method: 'POST', headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: connection.model, max_tokens: 2048, temperature: 0.2, system, messages: modelMessages }),
    });
    const content = json?.content?.filter((item) => item.type === 'text').map((item) => item.text).join('\n');
    if (!content?.trim()) throw new Error('模型服务没有返回可用内容。');
    return content.trim();
  }
  const geminiMessages = modelMessages.map((message) => ({ role: message.role === 'assistant' ? 'model' : 'user', parts: [{ text: message.content }] }));
  const json = await requestJson(`${baseUrl.replace(/\/$/, '')}/v1beta/models/${encodeURIComponent(connection.model)}:generateContent`, {
    method: 'POST', headers: { 'x-goog-api-key': apiKey, 'Content-Type': 'application/json' },
    body: JSON.stringify({ contents: geminiMessages, generationConfig: { temperature: 0.2 } }),
  });
  const content = json?.candidates?.[0]?.content?.parts?.map((item) => item.text || '').join('\n');
  if (!content?.trim()) throw new Error('模型服务没有返回可用内容。');
  return content.trim();
};

const summarizeTaskIfNeeded = async (taskId, connection, apiKey) => {
  const candidate = await messagesForSummary(taskId);
  if (!candidate.messages.length) return;
  const source = [candidate.summary ? `已有摘要：\n${candidate.summary}` : '', ...candidate.messages.map((message) => `${message.role === 'user' ? '用户' : '助手'}：${message.content}`)]
    .filter(Boolean).join('\n\n');
  try {
    const summary = await callModel(connection, apiKey, [{ role: 'user', content: `请压缩以下较早对话：\n${source}` }], 'summary');
    await saveContextSummary(taskId, summary, candidate.throughSequence);
  } catch {
    // 摘要失败不阻断当前对话，下一次满足条件时再尝试。
  }
};

const modelMessagesForTask = async (taskId) => {
  const context = await contextForTask(taskId);
  const messages = context.messages.map(({ role, content }) => ({ role, content }));
  if (context.summary) messages.unshift({ role: 'user', content: `以下是当前任务较早对话的本机摘要，仅用于保持上下文：\n${context.summary}` });
  return messages;
};

const stringValue = (value, maximum = 2000) => typeof value === 'string' && value.trim() && value.trim().length <= maximum ? value.trim() : null;
const stringList = (value, maximumItems = 12) => Array.isArray(value) && value.length <= maximumItems
  ? value.map((item) => stringValue(item, 1000)).filter(Boolean) : null;

const parseOptReport = (content, expectedFormat) => {
  const match = /<opt-report>\s*([\s\S]*?)\s*<\/opt-report>/i.exec(content);
  const cleanContent = content.replace(/\s*<opt-report>[\s\S]*?<\/opt-report>\s*/gi, '\n').trim();
  if (!match) return { content: cleanContent, report: null };
  try {
    const payload = JSON.parse(match[1]);
    if (payload.format !== expectedFormat) return { content: cleanContent, report: null };
    const sections = {
      conclusion: stringValue(payload.conclusion), suitability: stringValue(payload.suitability), keyTerms: stringList(payload.keyTerms),
      risks: stringList(payload.risks), disclaimer: stringValue(payload.disclaimer),
    };
    if (!stringValue(payload.title, 160) || !sections.conclusion || !sections.suitability || !sections.keyTerms?.length || !sections.risks?.length || !sections.disclaimer) {
      return { content: cleanContent, report: null };
    }
    if (expectedFormat === 'full') {
      sections.taskBackground = stringValue(payload.taskBackground);
      sections.moduleSources = stringList(payload.moduleSources);
      sections.parameters = stringList(payload.parameters);
      sections.valuationAssumptions = stringList(payload.valuationAssumptions);
      sections.backtestStatus = stringValue(payload.backtestStatus);
      sections.runRecord = stringValue(payload.runRecord);
      sections.pendingItems = stringList(payload.pendingItems);
      if (!sections.taskBackground || !sections.moduleSources?.length || !sections.parameters?.length || !sections.valuationAssumptions?.length
        || !sections.backtestStatus || !sections.runRecord || !sections.pendingItems?.length) return { content: cleanContent, report: null };
    }
    return { content: cleanContent, report: { title: payload.title.trim(), sections } };
  } catch {
    return { content: cleanContent, report: null };
  }
};

const reportHtml = (report) => `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(report.title)}</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#2b2224;max-width:800px;margin:48px auto;padding:0 28px;line-height:1.75}h1{color:#9e1027}h2{font-size:16px;margin-top:28px}ul{padding-left:20px}.notice{color:#7b5d24;border:1px solid #d8c191;padding:10px 14px;border-radius:10px}</style></head><body><h1>${escapeHtml(report.title)}</h1><p class="notice">${escapeHtml(report.aiStatus)}。仅供研究参考，不构成投资建议。</p>${Object.entries(report.sections).map(([key, value]) => `<h2>${escapeHtml(sectionLabel(key))}</h2>${Array.isArray(value) ? `<ul>${value.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : `<p>${escapeHtml(value || '未提供')}</p>`}`).join('')}<p>模型来源：${escapeHtml(report.provider)} / ${escapeHtml(report.model)}<br>生成时间：${escapeHtml(report.createdAt)}</p></body></html>`;
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const sectionLabel = (key) => ({ conclusion: '核心结论', suitability: '适配场景', keyTerms: '关键条款', risks: '主要风险', disclaimer: '合规提示', taskBackground: '任务背景', moduleSources: '模块来源', parameters: '参数', valuationAssumptions: '估值假设', backtestStatus: '回测状态', runRecord: '运行记录', pendingItems: '待确认事项' }[key] || key);

const staticPath = (pathname) => {
  if (!pathname.startsWith('/assets/web-design/')) return null;
  const relative = pathname.slice('/assets/web-design/'.length);
  if (!relative || relative.includes('\0') || relative.split('/').includes('..')) return null;
  const target = resolve(webRoot, relative);
  return target.startsWith(`${webRoot}${sep}`) ? target : null;
};

const protectedStatic = new Set(['chat.html', 'desk.html', 'report.html', 'setup.html']);
const deskOnlyRedirects = new Map([
  ['payoff.html', 'payoff'], ['pricing.html', 'pricing'], ['backtest.html', 'backtest'],
]);

const serveStatic = async (request, response, pathname) => {
  const logoAsset = new Map([
    ['/logo/optionhelper-logo.svg', logoPath],
    ['/logo/optionhelper-mark.svg', markPath],
  ]).get(pathname) || null;
  if (logoAsset) {
    try {
      const stats = await fs.stat(logoAsset);
      if (!stats.isFile()) throw new Error('not-file');
      response.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      createReadStream(logoAsset).pipe(response);
    } catch { sendJson(response, 404, { error: '资源不存在。' }); }
    return;
  }
  if (pathname === '/assets/web-design/login.html') return sendRedirect(response, '/assets/web-design/activation.html');
  if (pathname === '/assets/web-design/sales-report.html') return sendRedirect(response, '/assets/web-design/report.html');
  const filename = pathname.slice('/assets/web-design/'.length);
  if (deskOnlyRedirects.has(filename)) {
    const activation = await currentActivation();
    if (!activation) return sendRedirect(response, '/assets/web-design/activation.html');
    if (!requireDesk(activation, response)) return;
    return sendRedirect(response, `/assets/web-design/desk.html?tab=${deskOnlyRedirects.get(filename)}`);
  }
  if (protectedStatic.has(filename)) {
    const activation = await currentActivation();
    if (!activation && filename !== 'setup.html') return sendRedirect(response, '/assets/web-design/activation.html');
    if (filename === 'setup.html' && !activation) return sendRedirect(response, '/assets/web-design/activation.html');
    if (filename === 'desk.html' && !requireDesk(activation, response)) return;
  }
  const isChatEntry = filename === 'chat.html';
  const path = isChatEntry ? resolve(webRoot, 'desk.html') : staticPath(pathname);
  if (!path || /(^|\/)(core|data|result|distribution)(\/|$)/.test(pathname) || extname(path) === '.mjs') {
    sendJson(response, 404, { error: '资源不存在。' });
    return;
  }
  try {
    const stats = await fs.stat(path);
    if (!stats.isFile()) throw new Error('not-file');
    const headers = { 'Content-Type': mimeTypes.get(extname(path).toLowerCase()) || 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };
    if (isChatEntry) {
      const source = await fs.readFile(path, 'utf8');
      const chatMarkup = source
        .replace('data-workspace-shell data-mode="desk"', 'data-workspace-shell data-mode="chat"')
        .replace('data-mode="chat" aria-pressed="false"', 'data-mode="chat" aria-pressed="true"')
        .replace('data-mode="desk" aria-pressed="true"', 'data-mode="desk" aria-pressed="false"');
      response.writeHead(200, headers);
      response.end(chatMarkup);
      return;
    }
    response.writeHead(200, headers);
    createReadStream(path).pipe(response);
  } catch {
    sendJson(response, 404, { error: '资源不存在。' });
  }
};

const handleApi = async (request, response, url) => {
  const { pathname } = url;
  if (pathname === '/api/health' && request.method === 'GET') {
    return sendJson(response, 200, { service: 'optionhelper-web', version: serviceVersion });
  }
  if (pathname === '/api/access/profile' && request.method === 'GET') {
    const activation = await currentActivation();
    if (!activation) return sendJson(response, 200, { activated: false });
    return sendJson(response, 200, await publicProfile(activation));
  }
  if (pathname === '/api/activation/import' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const body = await readJson(request);
    const imported = await importLicense(body.license);
    return sendJson(response, 201, await publicProfile({ license: imported.license, profile: imported.profile }));
  }
  const activation = await profileFor(response);
  if (!activation) return;
  if (pathname === '/api/payoff/reference' && request.method === 'GET') {
    if (!requireDesk(activation, response)) return;
    const productId = url.searchParams.get('productId') || '';
    const contract = contractFor(productId);
    if (!contract || contract.kind === 'multi' || !/^\d+\.\d+$/.test(productId)) throw new Error('收益图产品无效。');
    const source = resolve(payoffOutputRoot, 'svg', `${contract.name}.svg`);
    if (!source.startsWith(`${payoffOutputRoot}${sep}`)) throw new Error('收益图路径无效。');
    let stats;
    try { stats = await fs.stat(source); } catch { throw new Error('该产品尚无已发布收益图。'); }
    if (!stats.isFile()) throw new Error('收益图资源无效。');
    response.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    createReadStream(source).pipe(response);
    return;
  }
  if (pathname === '/api/preferences/mode' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const { mode } = await readJson(request);
    const saved = await setModePreference(mode, activation.profile);
    return sendJson(response, 200, { mode: saved });
  }
  if (pathname === '/api/preferences/workspace' && request.method === 'GET') {
    return sendJson(response, 200, await getWorkspaceTaskPreference());
  }
  if (pathname === '/api/preferences/workspace' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const { mode, taskId } = await readJson(request);
    const taskMode = mode === 'desk' ? 'desk' : mode === 'chat' ? 'chat' : null;
    if (!taskMode) throw new Error('任务模式无效。');
    const loaded = await readTaskForMode(taskId, taskMode, activation);
    if (taskMode === 'chat' && loaded.task.scope !== 'chat') throw new Error('Chat只能记录Chat任务。');
    const saved = await setWorkspaceTaskPreference(taskMode, taskId, activation.profile);
    return sendJson(response, 200, { lastChatTaskId: saved.lastChatTaskId, lastDeskTaskId: saved.lastDeskTaskId });
  }
  if (pathname === '/api/model-connection' && request.method === 'GET') {
    const profile = await publicProfile(activation);
    return sendJson(response, 200, { providers: providerCatalog, connection: profile.connection, connections: profile.connections, activeConnectionId: profile.activeConnectionId });
  }
  if (pathname === '/api/model-connection' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const body = await readJson(request);
    const existing = typeof body.id === 'string' ? await getConnection(body.id) : null;
    const connection = { ...connectionFromRequest(body), id: existing?.id || undefined };
    let apiKey = typeof body.apiKey === 'string' ? body.apiKey.trim() : '';
    if (!apiKey && existing && existing.provider !== connection.provider) throw new Error('更换模型服务时请填写新的API Key。');
    if (!apiKey) {
      try { apiKey = await readApiKey(existing?.provider || connection.provider); } catch {
        throw new Error('该模型服务尚未配置API Key，请填写后重试。');
      }
    }
    if (body.test === true) await callModel(connection, apiKey, [{ role: 'user', content: '请只回复“连接成功”。' }], 'chat');
    if (typeof body.apiKey === 'string' && body.apiKey.trim()) await saveApiKey(connection.provider, apiKey);
    const saved = await saveConnection(connection);
    return sendJson(response, 200, { connection: await publicConnection(saved), tested: body.test === true });
  }
  if (pathname === '/api/model-connection/active' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const { id } = await readJson(request);
    const saved = await setActiveConnection(id);
    return sendJson(response, 200, { connection: await publicConnection(saved) });
  }
  if (pathname === '/api/model-connection' && request.method === 'DELETE') {
    if (!requireSameOrigin(request, response)) return;
    const connectionId = url.searchParams.get('id');
    if (!connectionId) {
      const providers = new Set((await getConnections()).map((connection) => connection.provider));
      await clearConnection();
      await Promise.all([...providers].map((provider) => deleteApiKey(provider)));
      return sendJson(response, 200, { cleared: true });
    }
    const removed = await clearConnection(connectionId);
    if (!removed) throw new Error('未找到指定模型连接。');
    const remaining = await getConnections();
    if (!remaining.some((connection) => connection.provider === removed.provider)) await deleteApiKey(removed.provider);
    return sendJson(response, 200, { cleared: true, id: removed.id });
  }
  if (pathname === '/api/tasks' && request.method === 'GET') {
    const scope = url.searchParams.get('scope') || 'chat';
    if (!['chat', 'desk'].includes(scope)) throw new Error('任务归属无效。');
    if (scope === 'desk') requireTaskMode(activation, 'desk');
    return sendJson(response, 200, { tasks: await listTasks(scope) });
  }
  if (pathname === '/api/tasks' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const body = await readJson(request);
    const scope = body.scope === 'desk' ? 'desk' : body.scope === undefined || body.scope === 'chat' ? 'chat' : null;
    if (!scope) throw new Error('任务归属无效。');
    if (scope === 'desk') requireTaskMode(activation, 'desk');
    const task = await createTask({ ...body, scope });
    return sendJson(response, 201, { task });
  }
  const taskMatch = /^\/api\/tasks\/([0-9a-f-]{36})(?:\/(messages))?$/.exec(pathname);
  if (taskMatch) {
    const [, taskId, action] = taskMatch;
    const taskMode = taskModeFrom(url);
    if (action === 'messages' && request.method === 'GET') {
      await readTaskForMode(taskId, taskMode, activation);
      return sendJson(response, 200, { messages: await listMessages(taskId) });
    }
    if (!action && request.method === 'GET') {
      const loaded = await readTaskForMode(taskId, taskMode, activation);
      return sendJson(response, 200, taskMode === 'desk' ? loaded : { task: loaded.task, modules: {} });
    }
    if (!action && request.method === 'PATCH') {
      if (!requireSameOrigin(request, response)) return;
      const body = await readJson(request);
      await readTaskForMode(taskId, taskMode, activation);
      if (body.module) {
        requireTaskMode(activation, 'desk');
        if (taskMode !== 'desk') {
          const error = new Error('模块参数只能在Desk中修改。');
          error.statusCode = 403;
          throw error;
        }
        if (body.module === 'parameters') {
          const contract = contractFor(body.value?.productId);
          if (contract?.kind === 'multi') throw new Error('OptDesk首版不支持多标的产品。');
          if (body.value?.underlying !== undefined && (typeof body.value.underlying !== 'string' || body.value.underlying.trim().length > 120)) throw new Error('研究标的格式无效。');
        }
        const value = await saveModule(taskId, body.module, body.value);
        return sendJson(response, 200, { module: body.module, value });
      }
      return sendJson(response, 200, { task: await updateTask(taskId, body) });
    }
    if (!action && request.method === 'DELETE') {
      if (!requireSameOrigin(request, response)) return;
      await readTaskForMode(taskId, taskMode, activation);
      return sendJson(response, 200, { deleted: await deleteTask(taskId) });
    }
  }
  if (pathname === '/api/ai/chat' && request.method === 'POST') {
    if (!requireSameOrigin(request, response)) return;
    const body = await readJson(request);
    const mode = body.mode === 'desk' ? 'desk' : body.mode === 'chat' ? 'chat' : null;
    if (!mode) throw new Error('对话模式无效。');
    requireTaskMode(activation, mode);
    const taskId = typeof body.taskId === 'string' ? body.taskId : '';
    const content = typeof body.content === 'string' ? body.content.trim() : '';
    if (!content || content.length > 10_000) throw new Error('对话内容无效。');
    await readTaskForMode(taskId, mode, activation);
    const connectionId = typeof body.connectionId === 'string' ? body.connectionId : null;
    const connection = await getConnection(connectionId);
    if (!connection) throw new Error('请先配置模型服务和API Key。');
    const apiKey = await readApiKey(connection.provider);
    await appendTaskMessage(taskId, { role: 'user', content });
    await titleFromFirstPrompt(taskId, content);
    await summarizeTaskIfNeeded(taskId, connection, apiKey);
    const rawContent = await callModel(connection, apiKey, await modelMessagesForTask(taskId), mode);
    const parsed = parseOptReport(rawContent, mode === 'desk' ? 'full' : 'simple');
    await appendTaskMessage(taskId, { role: 'assistant', content: parsed.content, provider: providerCatalog[connection.provider].label, model: connection.model });
    const report = parsed.report ? await createReport({ taskId, format: mode === 'desk' ? 'full' : 'simple', title: parsed.report.title, sections: parsed.report.sections, provider: providerCatalog[connection.provider].label, model: connection.model }) : null;
    return sendJson(response, 200, { content: parsed.content, provider: providerCatalog[connection.provider].label, model: connection.model, report: report && { id: report.id, format: report.format, title: report.title, sections: report.sections, createdAt: report.createdAt } });
  }
  if (pathname === '/api/reports' && request.method === 'GET') {
    const taskId = url.searchParams.get('taskId');
    const taskMode = taskModeFrom(url);
    requireTaskMode(activation, taskMode);
    if (taskId) await readTaskForMode(taskId, taskMode, activation);
    const reports = await listReports(activation.profile, taskId || null);
    return sendJson(response, 200, { reports: taskMode === 'chat' ? reports.filter((report) => report.format === 'simple') : reports });
  }
  const reportMatch = /^\/api\/reports\/([^/]+)(?:\/(export))?$/.exec(pathname);
  if (reportMatch) {
    const [, reportId, action] = reportMatch;
    if (!/^[0-9a-f-]{36}$/i.test(reportId)) throw new Error('Report ID无效。');
    const taskMode = taskModeFrom(url);
    requireTaskMode(activation, taskMode);
    if (action === 'export' && request.method === 'POST') {
      if (!requireSameOrigin(request, response)) return;
      const report = assertReportMode(await readReport(reportId, activation.profile), taskMode);
      await readTaskForMode(report.taskId, taskMode, activation);
      const requestedFormat = (await readJson(request)).format || 'html';
      if (requestedFormat !== 'html') throw new Error('PDF请在Report页面使用浏览器的打印为PDF功能。');
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Content-Disposition': `attachment; filename="OptionHelper-Report-${report.id}.html"`, 'Cache-Control': 'no-store' });
      response.end(reportHtml(report));
      return;
    }
    if (!action && request.method === 'GET') {
      const report = assertReportMode(await readReport(reportId, activation.profile), taskMode);
      await readTaskForMode(report.taskId, taskMode, activation);
      return sendJson(response, 200, { report });
    }
    if (!action && request.method === 'DELETE') {
      if (!requireSameOrigin(request, response)) return;
      const report = assertReportMode(await readReport(reportId, activation.profile), taskMode);
      await readTaskForMode(report.taskId, taskMode, activation);
      return sendJson(response, 200, { deleted: await deleteReport(reportId, activation.profile) });
    }
  }
  sendJson(response, 404, { error: '接口不存在。' });
};

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url || '/', `http://127.0.0.1:${port}`);
    if (url.pathname.startsWith('/api/')) return await handleApi(request, response, url);
    if (request.method !== 'GET' && request.method !== 'HEAD') return sendJson(response, 405, { error: '不支持的请求方法。' });
    if (url.pathname === '/') {
      const activation = await currentActivation();
      if (!activation) return sendRedirect(response, '/assets/web-design/activation.html');
      const preferredMode = await getModePreference();
      const destination = preferredMode === 'desk' && activation.profile.allowedModes.includes('desk') ? 'desk' : 'chat';
      return sendRedirect(response, `/assets/web-design/${destination}.html`);
    }
    return await serveStatic(request, response, url.pathname);
  } catch (error) {
    const status = error.statusCode || 400;
    sendJson(response, status, { error: error?.message || '本机服务发生错误。' });
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`OptionHelper本机服务已启动：http://127.0.0.1:${port}`);
});
