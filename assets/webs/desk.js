import { api, appendMessage, configureModelPicker, currentTaskId, currentWorkspaceMode, enableComposerAutoResize, ensureTask, initializeChrome, initializeWorkspaceLayoutControls, rememberWorkspaceTask, renderMessages, renderReports, renderTasks, setTaskUrl } from './workspace.js?desk-task-scope-1';
import { backtestExtrasFor, contractFor, parameterFields, pricingExtrasFor, productContracts } from './parameter-contracts.js';

window.__optionhelperPageAbort?.abort();
const pageAbort = new AbortController();
window.__optionhelperPageAbort = pageAbort;

const stream = document.querySelector('#message-stream');
const form = document.querySelector('#desk-form');
const input = document.querySelector('#desk-input');
const sendButton = document.querySelector('#send-button');
const modelPicker = document.querySelector('#desk-model-picker');
const notice = document.querySelector('#desk-notice');
const railTaskList = document.querySelector('#rail-task-list');
const taskTitle = document.querySelector('#task-title');
const reportList = document.querySelector('#desk-report-list');
const chatReportList = document.querySelector('#chat-report-list');
const reportContextKicker = document.querySelector('#report-context-kicker');
const reportContextTitle = document.querySelector('#report-context-title');
const state = document.querySelector('#task-state');
const parameterSummary = document.querySelector('#parameter-summary');
const parameterForm = document.querySelector('#parameter-form');
const parameterUnderlying = document.querySelector('#parameter-underlying');
const parameterProduct = document.querySelector('#parameter-product');
const parameterFieldsTarget = document.querySelector('#payoff-parameter-fields');
const parameterStatus = document.querySelector('#parameter-contract-status');
const pricingForm = document.querySelector('#pricing-form');
const pricingFieldsTarget = document.querySelector('#pricing-parameter-fields');
const backtestForm = document.querySelector('#backtest-form');
const backtestFieldsTarget = document.querySelector('#backtest-parameter-fields');
const payoffProductTitle = document.querySelector('#payoff-product-title');
const payoffLibraryCopy = document.querySelector('#payoff-library-copy');
const payoffBinding = document.querySelector('#payoff-binding');
const payoffTermCount = document.querySelector('#payoff-term-count');
const payoffTermList = document.querySelector('#payoff-term-list');
const payoffCanvasState = document.querySelector('#payoff-canvas-state');
const payoffStateDot = document.querySelector('#payoff-state-dot');
const payoffEmptyCanvas = document.querySelector('#payoff-empty-canvas');
const payoffSvgStage = document.querySelector('#payoff-svg-stage');
const payoffSvg = document.querySelector('#payoff-svg');
const payoffZoomValue = document.querySelector('#payoff-zoom-value');
const payoffInspectorTitle = document.querySelector('#payoff-inspector-title');
const payoffInspectorSource = document.querySelector('#payoff-inspector-source');
const payoffBoundFields = document.querySelector('#payoff-bound-fields');
const deskShell = document.querySelector('.workspace-shell--desk');
const resizeComposer = enableComposerAutoResize(input);
const validTabs = ['parameters', 'payoff', 'pricing', 'backtest', 'report'];
const tabButtons = [...document.querySelectorAll('[data-tab]')];
const allowedContracts = productContracts.filter((contract) => contract.kind !== 'multi');
const moduleCopyTargets = [...document.querySelectorAll('[data-module-copy]')];

let task;
let moduleState = {};
let requestController = null;
let payoffZoom = 1;
const parameterDrafts = new Map();

const initialMode = location.pathname.endsWith('/chat.html') ? 'chat' : 'desk';
const taskPath = (taskId, suffix = '') => `/api/tasks/${encodeURIComponent(taskId)}${suffix}?mode=${currentWorkspaceMode()}`;

const renderReportContextTitle = () => {
  const desk = currentWorkspaceMode() === 'desk';
  if (reportContextKicker) reportContextKicker.textContent = desk ? '当前任务' : '本机报告';
  if (reportContextTitle) reportContextTitle.textContent = desk ? '研究报告' : '报告库';
};

const moduleCopyStyle = (source) => source
  .replace(/:root/g, ':host')
  .replace(/\bbody\b/g, '.module-copy-page')
  .replace(/"Microsoft YaHei",?/g, '')
  .replace(/Georgia/g, '"Avenir Next"');

const copyControls = (root) => [...root.querySelectorAll('input, select, textarea, button')];

const resetCopySelect = (root, selector, label) => {
  const select = root.querySelector(selector);
  if (!select) return;
  const option = document.createElement('option');
  option.value = '';
  option.textContent = label;
  select.replaceChildren(option);
  select.value = '';
};

const syncModuleCopy = (kind, parameters = normalizeParameters(moduleState.parameters)) => {
  const target = moduleCopyTargets.find((item) => item.dataset.moduleCopy === kind);
  const root = target?.shadowRoot;
  if (!root) return;
  const selected = deskContractFor(parameters.productId);
  const status = root.querySelector('#serviceStatus');
  const product = root.querySelector('#productId');
  const underlying = root.querySelector('#underlyings');
  const terms = root.querySelector('#parameters');
  const message = root.querySelector('#message');
  if (product) {
    product.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '请先在参数配置选择产品';
    product.append(placeholder);
    allowedContracts.forEach((contract) => {
      const option = document.createElement('option');
      option.value = contract.id;
      option.textContent = `${contract.id} ${contract.name}`;
      product.append(option);
    });
    product.value = selected?.id || '';
  }
  if (underlying) underlying.value = parameters.underlying || parameters.payoff.U || '';
  if (terms) terms.value = selected ? JSON.stringify(parameters.payoff, null, 2) : '';
  ['#tenor', '#rate', '#dividend', '#valuationDate', '#start', '#end'].forEach((selector) => {
    const control = root.querySelector(selector);
    if (control) control.value = '';
  });
  ['#schedules'].forEach((selector) => {
    const control = root.querySelector(selector);
    if (control) control.value = '';
  });
  if (kind === 'pricing') resetCopySelect(root, '#hv', '未接入市场数据');
  if (kind === 'backtest') {
    resetCopySelect(root, '#frequency', '未接入入场规则');
    resetCopySelect(root, '#denominator', '未接入收益率分母');
  }
  if (status) status.textContent = selected ? '当前Desk任务已载入，计算未接入' : '请先在参数配置保存产品条款';
  if (message) message.textContent = selected ? '条款已同步。市场、模型与运行接口尚未接入。' : '当前模块等待参数配置中的产品条款。';
  const resultTitle = root.querySelector('#resultTitle');
  if (resultTitle) resultTitle.textContent = '等待参数与计算接入';
  const method = root.querySelector('#method, #rule');
  if (method) method.textContent = '未接入';
  const rightCopy = root.querySelector('.right .copy');
  if (rightCopy) rightCopy.textContent = kind === 'pricing' ? '市场数据与模型假设将在后续接入。当前仅展示定价模块的工作台结构。' : '历史数据与执行规则将在后续接入。当前仅展示回测模块的工作台结构。';
  const boundary = root.querySelector('.right .notice');
  if (boundary) boundary.textContent = '当前副本不读取数据、不调用计算内核，也不生成估值、收益、胜率或风险指标。';
  const hint = root.querySelector('#fieldHint');
  if (hint) hint.textContent = selected ? '完整条款来自当前Desk任务。' : '请先在参数配置保存产品条款。';
};

const mountModuleCopy = async (target) => {
  const sourcePath = target.dataset.copySource;
  try {
    const response = await fetch(sourcePath, { cache: 'no-store' });
    if (!response.ok) throw new Error('副本文件不可用。');
    const source = await response.text();
    const documentCopy = new DOMParser().parseFromString(source, 'text/html');
    const root = target.attachShadow({ mode: 'open' });
    const styles = [...documentCopy.querySelectorAll('style')].map((node) => node.textContent).join('\n');
    const style = document.createElement('style');
    style.textContent = `${moduleCopyStyle(styles)}
      :host { display: block; min-width: 0; font-family: "Avenir Next", "PingFang SC", sans-serif; }
      .module-copy-page { min-width: 0; }
      .module-copy-page .bar { min-height: 54px; height: auto; padding: 0 18px; }
      .module-copy-page .bar small { color: #8a706f; letter-spacing: .06em; }
      .module-copy-page .status { color: #776e69; }
      .module-copy-page .shell { min-height: 632px; }
      .module-copy-page button:disabled,
      .module-copy-page input:disabled,
      .module-copy-page select:disabled,
      .module-copy-page textarea:disabled { opacity: 1; cursor: default; }
      .module-copy-page button:disabled { background: #9e1027; }
      @media (min-width: 860px) {
        .module-copy-page .shell { grid-template-columns: 270px minmax(0, 1fr) 248px; }
        .module-copy-page .right { grid-column: auto; border-top: 0; border-left: 1px solid var(--line); }
        .module-copy-page .right .stack { display: block; }
      }`;
    const page = document.createElement('div');
    page.className = 'module-copy-page';
    [...documentCopy.body.children].filter((node) => node.tagName !== 'SCRIPT').forEach((node) => page.append(node.cloneNode(true)));
    const header = page.querySelector('.bar');
    const marker = header?.querySelector('.mark');
    const title = header?.querySelector('strong');
    const status = header?.querySelector('#serviceStatus');
    if (marker) marker.textContent = target.dataset.moduleCopy === 'pricing' ? 'P' : 'B';
    if (title) title.textContent = target.dataset.moduleCopy === 'pricing' ? '估值定价' : '历史回测';
    if (status) status.textContent = 'Desk副本已接入，计算未接入';
    const run = page.querySelector('#run');
    if (run) run.textContent = '计算尚未接入';
    copyControls(page).forEach((control) => { control.disabled = true; });
    root.append(style, page);
    target.dataset.loaded = 'true';
    target.setAttribute('aria-busy', 'false');
    syncModuleCopy(target.dataset.moduleCopy);
  } catch (error) {
    target.replaceChildren();
    const message = document.createElement('p');
    message.className = 'module-copy-stage__error';
    message.textContent = `${target.dataset.moduleCopy === 'pricing' ? 'Pricing' : 'Backtest'}副本未能载入。参数配置功能不受影响。`;
    target.append(message);
    target.setAttribute('aria-busy', 'false');
  }
};

const mountModuleCopies = () => Promise.all(moduleCopyTargets.map(mountModuleCopy));

const prepareWorkspaceLayout = () => {
  const workArea = document.querySelector('.work-area');
  const header = workArea.querySelector('.workbench-head');
  const tabs = workArea.querySelector('.desk-module-tabs');
  const status = workArea.querySelector('.desk-status-strip');
  const stage = workArea.querySelector('.work-stage');
  const assistant = workArea.querySelector('.desk-command-dock');
  const content = document.createElement('div');
  content.className = 'workspace-content';
  const chatSurface = document.createElement('section');
  chatSurface.className = 'chat-surface';
  chatSurface.setAttribute('aria-label', '任务对话');
  const chatHeader = document.createElement('header');
  chatHeader.className = 'conversation__header conversation__header--shared';
  const chatHeading = document.createElement('h2');
  chatHeading.textContent = '任务对话';
  const chatCopy = document.createElement('p');
  chatCopy.textContent = '对话、任务参数和关联Report会保留在当前设备。';
  chatHeader.append(chatHeading, chatCopy);
  const chatScrollStage = document.createElement('div');
  chatScrollStage.className = 'conversation-scroll-stage';
  stream.hidden = false;
  chatScrollStage.append(stream);
  chatSurface.append(chatHeader, chatScrollStage);
  const deskSurface = document.createElement('section');
  deskSurface.className = 'desk-surface';
  deskSurface.setAttribute('aria-label', '研究模块');
  deskSurface.append(tabs, status, stage);
  const assistantBody = assistant.querySelector('.desk-command-dock__body');
  const assistantPet = document.createElement('button');
  assistantPet.type = 'button';
  assistantPet.className = 'assistant-toggle';
  assistantPet.dataset.assistantToggle = '';
  assistantPet.setAttribute('aria-label', '打开任务对话');
  assistantPet.setAttribute('aria-expanded', 'false');
  const toggleIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  toggleIcon.setAttribute('viewBox', '0 0 24 24');
  toggleIcon.setAttribute('aria-hidden', 'true');
  const togglePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  togglePath.setAttribute('d', 'M5.5 6.75A3.25 3.25 0 0 1 8.75 3.5h6.5a3.25 3.25 0 0 1 3.25 3.25v4.5a3.25 3.25 0 0 1-3.25 3.25H11l-3.75 3v-3H8.75a3.25 3.25 0 0 1-3.25-3.25z');
  toggleIcon.append(togglePath);
  const toggleText = document.createElement('span');
  toggleText.className = 'sr-only';
  toggleText.textContent = '打开任务对话';
  assistantPet.append(toggleIcon, toggleText);
  const assistantPanel = document.createElement('section');
  assistantPanel.className = 'assistant-panel';
  assistantPanel.setAttribute('aria-label', '任务对话');
  const assistantHead = document.createElement('header');
  assistantHead.className = 'assistant-panel__head';
  const assistantTitle = document.createElement('div');
  const assistantName = document.createElement('strong');
  assistantName.textContent = '任务对话';
  const assistantCopy = document.createElement('span');
  assistantCopy.textContent = '当前任务的连续对话';
  assistantTitle.append(assistantName, assistantCopy);
  const assistantClose = document.createElement('button');
  assistantClose.type = 'button';
  assistantClose.className = 'assistant-panel__close';
  assistantClose.dataset.assistantClose = '';
  assistantClose.setAttribute('aria-label', '收起任务对话');
  assistantClose.textContent = '×';
  assistantHead.append(assistantTitle, assistantClose);
  const assistantTranscript = document.createElement('div');
  assistantTranscript.className = 'assistant-panel__transcript';
  assistantTranscript.setAttribute('aria-live', 'polite');
  assistant.className = 'assistant-region';
  assistant.setAttribute('aria-label', '任务助手');
  assistant.querySelector('.composer').classList.add('composer--shared');
  assistantPanel.append(assistantHead, assistantTranscript, assistantBody);
  assistant.replaceChildren(assistantPet, assistantPanel);
  content.append(chatSurface, deskSurface);
  workArea.replaceChildren(header, content, assistant);
  return { chatSurface, chatScrollStage, deskSurface, assistant, assistantPet, assistantPanel, assistantTranscript, assistantClose };
};

const workspaceLayout = prepareWorkspaceLayout();
initializeWorkspaceLayoutControls();

const setTaskAssistant = (open, { focus = false } = {}) => {
  const desk = currentWorkspaceMode() === 'desk';
  input.placeholder = desk ? '输入研究任务' : '输入任务要求，按Enter发送，Shift加Enter换行';
  if (!desk) {
    workspaceLayout.assistant.classList.remove('is-open');
    workspaceLayout.assistantPanel.setAttribute('aria-hidden', 'false');
    workspaceLayout.assistantPet.setAttribute('aria-expanded', 'false');
    if (stream.parentElement !== workspaceLayout.chatScrollStage) workspaceLayout.chatScrollStage.prepend(stream);
    return;
  }
  if (stream.parentElement !== workspaceLayout.assistantTranscript) workspaceLayout.assistantTranscript.append(stream);
  workspaceLayout.assistant.classList.toggle('is-open', open);
  workspaceLayout.assistantPanel.setAttribute('aria-hidden', String(!open));
  workspaceLayout.assistantPet.setAttribute('aria-expanded', String(open));
  if (open && focus) input.focus();
};

const syncTaskAssistantMode = (mode) => setTaskAssistant(mode === 'desk' && workspaceLayout.assistant.classList.contains('is-open'));

// The assistant is one persistent conversation. Desk merely folds it into a
// floating panel; Chat returns the same message stream to the full canvas.
syncTaskAssistantMode(initialMode);

const conversationEdgeRail = document.createElement('div');
conversationEdgeRail.className = 'conversation-edge-rail';
conversationEdgeRail.setAttribute('aria-hidden', 'true');
const conversationEdgeTrack = document.createElement('span');
conversationEdgeTrack.className = 'conversation-edge-rail__track';
const conversationEdgeMarker = document.createElement('span');
conversationEdgeMarker.className = 'conversation-edge-rail__marker';
conversationEdgeRail.append(conversationEdgeTrack, conversationEdgeMarker);
workspaceLayout.chatScrollStage.append(conversationEdgeRail);

let edgeRailFrame = 0;
const updateConversationEdgeRail = () => {
  edgeRailFrame = 0;
  const travel = Math.max(0, stream.scrollHeight - stream.clientHeight);
  const isScrollableTranscript = travel > 1 && stream.querySelector('.message') !== null;
  conversationEdgeRail.classList.toggle('is-scrollable', isScrollableTranscript);
  if (!isScrollableTranscript) {
    conversationEdgeMarker.style.transform = 'translate3d(0, 0, 0)';
    return;
  }
  const progress = Math.max(0, Math.min(1, stream.scrollTop / travel));
  const markerTravel = Math.max(0, conversationEdgeRail.clientHeight - conversationEdgeMarker.offsetHeight);
  const markerOffset = Math.round(markerTravel * progress);
  conversationEdgeMarker.style.transform = `translate3d(0, ${markerOffset}px, 0)`;
};
const scheduleConversationEdgeRail = () => {
  if (edgeRailFrame) return;
  edgeRailFrame = window.requestAnimationFrame(updateConversationEdgeRail);
};
stream.addEventListener('scroll', scheduleConversationEdgeRail, { passive: true, signal: pageAbort.signal });
const edgeRailResizeObserver = new ResizeObserver(scheduleConversationEdgeRail);
edgeRailResizeObserver.observe(stream);
edgeRailResizeObserver.observe(workspaceLayout.chatScrollStage);
pageAbort.signal.addEventListener('abort', () => edgeRailResizeObserver.disconnect(), { once: true });

const renderTaskMessages = (messages, emptyText) => {
  const isEmpty = messages.length === 0;
  workspaceLayout.chatSurface.classList.toggle('chat-surface--empty', isEmpty);
  renderMessages(stream, messages, isEmpty ? emptyText : '');
  scheduleConversationEdgeRail();
};

const leaveConversationStart = async () => {
  const start = stream.querySelector('.conversation-start');
  workspaceLayout.chatSurface.classList.remove('chat-surface--empty');
  if (!start) return;
  start.classList.add('is-leaving');
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  await new Promise((resolve) => window.setTimeout(resolve, reduceMotion ? 0 : 180));
  if (start.isConnected) stream.replaceChildren();
};

const moduleLabels = { parameters: '参数配置', pricing: '估值定价', backtest: '历史回测' };
const inputPrefix = { parameters: 'payoff', pricing: 'pricing', backtest: 'backtest' };
const statusTargets = {
  pricing: document.querySelector('#pricing-contract-status'),
  backtest: document.querySelector('#backtest-contract-status'),
};

const deskContractFor = (id) => {
  const contract = contractFor(id);
  return contract?.kind === 'multi' ? null : contract;
};

const setNotice = (message = '', error = false) => {
  notice.hidden = !message;
  notice.textContent = message;
  notice.className = error ? 'notice notice--error' : 'notice';
};

const updateState = (message) => { state.textContent = message; };

const setGenerating = (value) => {
  sendButton.classList.toggle('is-generating', value);
  sendButton.setAttribute('aria-label', value ? '停止生成' : '发送');
  form.dataset.generating = String(value);
};

const activeTab = () => new URLSearchParams(location.search).get('tab') || 'parameters';

const setTab = (tab, replace = false) => {
  const selected = validTabs.includes(tab) ? tab : 'parameters';
  const nextPanel = document.querySelector(`[data-panel="${selected}"]`);
  const currentPanel = document.querySelector('[data-panel].is-active');
  const shouldAnimate = !replace && currentPanel && currentPanel !== nextPanel && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  tabButtons.forEach((button) => {
    const active = button.dataset.tab === selected;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-panel]').forEach((panel) => panel.classList.toggle('is-active', panel === nextPanel));
  deskShell.dataset.activeTab = selected;
  if (shouldAnimate) {
    nextPanel.classList.remove('is-entering');
    void nextPanel.offsetWidth;
    nextPanel.classList.add('is-entering');
    window.setTimeout(() => nextPanel.classList.remove('is-entering'), 360);
  }
  const url = new URL(location.href);
  url.searchParams.set('tab', selected);
  history[replace ? 'replaceState' : 'pushState']({}, '', url);
};

const normalizeParameters = (value = {}) => ({
  underlying: typeof value.underlying === 'string' ? value.underlying : '',
  productId: deskContractFor(value.productId)?.id || '',
  payoff: value.payoff && typeof value.payoff === 'object' ? value.payoff : {},
  note: typeof value.note === 'string' ? value.note : '',
});

const fieldId = (scope, key) => `${scope}-${key}`.replace(/[^a-zA-Z0-9_-]/g, '-');

const optionListFor = (key) => ({
  full: ['仅保留完整期限样本', '允许未到期样本'],
  muobs: ['日收盘近似', '盘中价格观察', '指定时点价格观察'],
  mumiss: ['剔除样本', '以前值填补', '延后观察'],
  Dret: ['名义本金', '保证金', '实际投入资金'],
}[key] || []);

const controlFor = (scope, key, value = '') => {
  const descriptor = parameterFields[key];
  const id = fieldId(scope, key);
  const wrapper = document.createElement('div');
  wrapper.className = descriptor.control === 'textarea' ? 'field field--wide' : 'field';
  const label = document.createElement('label');
  label.htmlFor = id;
  label.append(document.createTextNode(descriptor.label));
  const symbol = document.createElement('span');
  symbol.className = 'field-symbol';
  symbol.textContent = descriptor.symbol;
  label.append(symbol);
  let control;
  if (descriptor.control === 'textarea') {
    control = document.createElement('textarea');
    control.rows = 3;
  } else if (descriptor.control === 'select') {
    control = document.createElement('select');
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '请选择口径';
    control.append(placeholder);
    optionListFor(key).forEach((item) => {
      const option = document.createElement('option');
      option.value = item;
      option.textContent = item;
      control.append(option);
    });
  } else {
    control = document.createElement('input');
    control.type = descriptor.control === 'date' ? 'date' : 'text';
    control.autocomplete = 'off';
    if (descriptor.control !== 'date') control.placeholder = `填写${descriptor.label}`;
  }
  control.id = id;
  control.name = `${scope}:${key}`;
  control.value = typeof value === 'string' ? value : '';
  wrapper.append(label, control);
  return wrapper;
};

const renderGeneratedFields = (target, keys, scope, values, heading, copy) => {
  target.replaceChildren();
  if (!keys.length) {
    const empty = document.createElement('p');
    empty.className = 'contract-empty';
    empty.textContent = '请先在参数配置保存产品条款。';
    target.append(empty);
    return;
  }
  const title = document.createElement('div');
  title.className = 'form-section-title field--wide';
  const headingNode = document.createElement('h3');
  headingNode.textContent = heading;
  const copyNode = document.createElement('p');
  copyNode.textContent = copy;
  title.append(headingNode, copyNode);
  target.append(title);
  keys.forEach((key) => target.append(controlFor(scope, key, values[key])));
};

const parameterContractText = (contract) => contract ? `${contract.id} ${contract.name}，PayoffInput共${contract.payoff.length}项。` : '选择产品后显示完整条款参数。';

const renderParameterForm = (productId, values = {}) => {
  const selected = deskContractFor(productId);
  parameterStatus.textContent = parameterContractText(selected);
  document.querySelector('#parameter-save').disabled = !selected;
  renderGeneratedFields(parameterFieldsTarget, selected?.payoff || [], inputPrefix.parameters, values, '完整PayoffInput', '本页只维护产品条款，后续模块将直接读取这份内容。');
};

const renderParameterSummary = (parameters) => {
  parameterSummary.replaceChildren();
  const selected = deskContractFor(parameters.productId);
  const pairs = selected
    ? [['标的', parameters.underlying || parameters.payoff.U || '待录入'], ['产品', `${selected.id} ${selected.name}`]]
    : [['研究标的', parameters.underlying || '待录入'], ['产品条款', '待选择产品']];
  pairs.forEach(([label, value]) => {
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    parameterSummary.append(term, detail);
  });
};

const renderSharedParameterLists = (parameters) => {
  const selected = deskContractFor(parameters.productId);
  const message = selected ? `${selected.id} ${selected.name}的完整PayoffInput已载入。` : '请先在参数配置保存产品条款。';
  Object.values(statusTargets).filter(Boolean).forEach((target) => { target.textContent = message; });
  document.querySelectorAll('[data-shared-parameters]').forEach((list) => {
    list.replaceChildren();
    if (!selected) {
      const empty = document.createElement('div');
      empty.className = 'shared-terms__empty';
      empty.textContent = '尚未载入产品条款。';
      list.append(empty);
      return;
    }
    selected.payoff.forEach((key) => {
      const row = document.createElement('div');
      row.className = 'shared-terms__row';
      const term = document.createElement('dt');
      const detail = document.createElement('dd');
      term.textContent = `${parameterFields[key].label}${parameterFields[key].symbol}`;
      detail.textContent = parameters.payoff[key] || '待录入';
      row.append(term, detail);
      list.append(row);
    });
  });
};

const renderSupplementalForms = (parameters, saved = moduleState) => {
  const selected = deskContractFor(parameters.productId);
  document.querySelector('#pricing-save').disabled = !selected;
  document.querySelector('#backtest-save').disabled = !selected;
  renderGeneratedFields(pricingFieldsTarget, pricingExtrasFor(selected), inputPrefix.pricing, saved.pricing?.inputs || {}, '估值定价追加参数', '这些字段与完整PayoffInput共同构成PricingInput。');
  renderGeneratedFields(backtestFieldsTarget, backtestExtrasFor(selected), inputPrefix.backtest, saved.backtest?.inputs || {}, '历史回测追加参数', '这些字段与完整PayoffInput共同构成BacktestInput。');
};

const setPayoffZoom = (value) => {
  payoffZoom = Math.max(.65, Math.min(1.45, value));
  payoffSvgStage.style.setProperty('--payoff-zoom', String(payoffZoom));
  payoffZoomValue.value = `${Math.round(payoffZoom * 100)}%`;
};

const renderPayoffWorkbench = (parameters) => {
  const selected = deskContractFor(parameters.productId);
  payoffTermList.replaceChildren();
  payoffBoundFields.replaceChildren();
  if (!selected) {
    payoffProductTitle.textContent = '尚未选择产品';
    payoffLibraryCopy.textContent = '请先在参数配置中填写研究标的并保存产品条款。';
    payoffBinding.textContent = '未载入PayoffInput';
    payoffTermCount.textContent = '0';
    payoffCanvasState.textContent = '等待产品条款';
    payoffStateDot.className = 'payoff-state-dot';
    payoffInspectorTitle.textContent = '未选择产品';
    payoffInspectorSource.textContent = '选择并保存产品条款后，显示该产品正式收益图和条款映射。';
    payoffSvg.removeAttribute('src');
    payoffSvgStage.hidden = true;
    payoffEmptyCanvas.hidden = false;
    return;
  }

  const productLabel = `${selected.id} ${selected.name}`;
  payoffProductTitle.textContent = productLabel;
  payoffLibraryCopy.textContent = `研究标的：${parameters.underlying || parameters.payoff.U || '待录入'}`;
  payoffBinding.textContent = `完整PayoffInput，${selected.payoff.length}项条款`;
  payoffTermCount.textContent = String(selected.payoff.length);
  payoffCanvasState.textContent = '已载入正式收益图';
  payoffStateDot.className = 'payoff-state-dot is-ready';
  payoffInspectorTitle.textContent = selected.name;
  payoffInspectorSource.textContent = `产品编号${selected.id}。图形与条款映射均来自当前任务已保存的PayoffInput。`;

  selected.payoff.forEach((key) => {
    const descriptor = parameterFields[key];
    const item = document.createElement('li');
    const label = document.createElement('span');
    const value = document.createElement('small');
    label.textContent = descriptor.label;
    value.textContent = parameters.payoff[key] || '待录入';
    item.append(label, value);
    payoffTermList.append(item);

    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = `${descriptor.label}${descriptor.symbol}`;
    detail.textContent = parameters.payoff[key] || '待录入';
    payoffBoundFields.append(term, detail);
  });

  payoffSvgStage.classList.remove('is-loaded');
  payoffCanvasState.textContent = '正在载入正式收益图';
  payoffStateDot.className = 'payoff-state-dot is-loading';
  payoffSvg.alt = `${productLabel}正式收益图`;
  payoffSvg.src = `/api/payoff/reference?productId=${encodeURIComponent(selected.id)}`;
  payoffSvgStage.hidden = false;
  payoffEmptyCanvas.hidden = true;
};

const valuesFrom = (formElement, prefix) => Object.fromEntries([...formElement.querySelectorAll(`[name^="${prefix}:"]`)]
  .map((control) => [control.name.slice(prefix.length + 1), control.value]));

const saveModuleState = async (moduleName, values) => {
  const payload = await api(taskPath(task.id), { method: 'PATCH', body: JSON.stringify({ module: moduleName, value: values }) });
  moduleState[moduleName] = payload.value;
  if (moduleName === 'parameters') {
    renderParameterSummary(values);
    renderSharedParameterLists(values);
    renderSupplementalForms(values);
    renderPayoffWorkbench(values);
    syncModuleCopy('pricing', values);
    syncModuleCopy('backtest', values);
    updateState('参数配置已保存。收益结构、估值定价和历史回测正在读取同一份完整条款。计算仍未接入。');
  } else {
    updateState(`${moduleLabels[moduleName]}参数已保存在当前任务。计算仍未接入。`);
  }
};

const restoreModuleState = () => {
  const parameters = normalizeParameters(moduleState.parameters);
  parameterProduct.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '请选择产品';
  parameterProduct.append(placeholder);
  allowedContracts.forEach((contract) => {
    const option = document.createElement('option');
    option.value = contract.id;
    option.textContent = `${contract.id} ${contract.name}`;
    parameterProduct.append(option);
  });
  parameterUnderlying.value = parameters.underlying;
  parameterProduct.value = parameters.productId;
  parameterProduct.dataset.currentId = parameters.productId;
  parameterDrafts.clear();
  parameterDrafts.set(parameters.productId, parameters.payoff);
  parameterForm.elements.note.value = parameters.note;
  renderParameterForm(parameters.productId, parameters.payoff);
  renderParameterSummary(parameters);
  renderSharedParameterLists(parameters);
  renderSupplementalForms(parameters);
  renderPayoffWorkbench(parameters);
  syncModuleCopy('pricing', parameters);
  syncModuleCopy('backtest', parameters);
};

const refreshReports = async () => {
  const { reports } = await api(`/api/reports?taskId=${encodeURIComponent(task.id)}&mode=${currentWorkspaceMode()}`);
  renderReports(reportList, reports, '当前任务尚无已保存的Report。');
  renderReports(chatReportList, reports, '当前任务尚无已保存的Report。');
};

const refreshTasks = async () => {
  const desk = currentWorkspaceMode() === 'desk';
  const taskResponses = await Promise.all(desk
    ? [api('/api/tasks?scope=desk'), api('/api/tasks?scope=chat')]
    : [api('/api/tasks?scope=chat')]);
  const tasks = [...new Map(taskResponses.flatMap((payload) => payload.tasks).map((item) => [item.id, item])).values()]
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
  renderTasks(railTaskList, tasks, {
    activeTaskId: task.id,
    emptyText: '暂无任务',
    onSelect: (selected) => {
      setTaskUrl(selected.id, { tab: activeTab() });
    },
    onDelete: async (selected) => {
      if (!confirm(`删除“${selected.title}”的对话与模块草稿？已生成Report将保留。`)) return;
      await api(taskPath(selected.id), { method: 'DELETE' });
      if (selected.id === task.id) await createAndOpenTask();
      else await refreshTasks();
    },
    onRename: async (selected, title) => {
      const payload = await api(taskPath(selected.id), { method: 'PATCH', body: JSON.stringify({ title }) });
      if (selected.id === task.id) {
        task = payload.task;
        taskTitle.textContent = task.title;
      }
      await refreshTasks();
    },
    onTogglePin: async (selected) => {
      const payload = await api(taskPath(selected.id), { method: 'PATCH', body: JSON.stringify({ pinned: !selected.pinned }) });
      if (selected.id === task.id) task = payload.task;
      await refreshTasks();
    },
    onError: (message) => setNotice(message, true),
  });
};

const loadTask = async () => {
  const payload = await ensureTask();
  task = payload.task;
  deskShell.dataset.taskScope = task.scope;
  renderReportContextTitle();
  void rememberWorkspaceTask(task.id);
  moduleState = payload.modules || {};
  taskTitle.textContent = task.title;
  const { messages } = await api(taskPath(task.id, '/messages'));
  renderTaskMessages(messages, '可直接输入任务要求，或选择一个研究起点。');
  restoreModuleState();
  await Promise.all([refreshTasks(), refreshReports()]);
  setTab(activeTab(), true);
};

const createAndOpenTask = async () => {
  const scope = currentWorkspaceMode() === 'desk' ? 'desk' : 'chat';
  const { task: created } = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ scope }) });
  setTaskUrl(created.id, { tab: currentWorkspaceMode() === 'desk' ? 'parameters' : null });
};

const send = async () => {
  const content = input.value.trim();
  if (!content || requestController) return;
  const connectionId = modelPicker.value;
  if (!connectionId) {
    setNotice('请先配置至少一个模型连接。', true);
    return;
  }
  if (currentWorkspaceMode() === 'desk') setTaskAssistant(true);
  setNotice();
  await leaveConversationStart();
  appendMessage(stream, 'user', content);
  input.value = '';
  resizeComposer();
  requestController = new AbortController();
  setGenerating(true);
  const pending = appendMessage(stream, 'assistant', '正在组织研究回复。', '处理中');
  pending.classList.add('message--loading');
  scheduleConversationEdgeRail();
  try {
    const data = await api('/api/ai/chat', {
      method: 'POST',
      signal: requestController.signal,
      body: JSON.stringify({ mode: currentWorkspaceMode(), taskId: task.id, content, connectionId, language: 'zh-CN', style: 'professional' }),
    });
    pending.remove();
    appendMessage(stream, 'assistant', data.content, `${data.provider} / ${data.model}`);
    scheduleConversationEdgeRail();
    if (data.report) {
      setNotice(`已保存${data.report.format === 'full' ? '完整版' : '简版'}Report：${data.report.title}`);
      await refreshReports();
      if (data.report.format === 'full') updateState('已生成完整Report。请在研究报告中查看和导出。');
    }
    const latest = await api(taskPath(task.id));
    task = latest.task;
    taskTitle.textContent = task.title;
    await refreshTasks();
  } catch (error) {
    pending.remove();
    scheduleConversationEdgeRail();
    if (error.name === 'AbortError') setNotice('已停止生成。已保存的消息会在重新打开任务时恢复。');
    else setNotice(error.message || '对话未完成。', true);
  } finally {
    requestController = null;
    setGenerating(false);
    input.focus();
  }
};

tabButtons.forEach((button, index) => {
  button.addEventListener('click', () => setTab(button.dataset.tab));
  button.addEventListener('keydown', (event) => {
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? tabButtons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabButtons.length) % tabButtons.length;
    tabButtons[nextIndex].focus();
    setTab(tabButtons[nextIndex].dataset.tab);
  });
});

document.querySelectorAll('[data-tab-link]').forEach((link) => link.addEventListener('click', () => setTab(link.dataset.tabLink)));
document.querySelectorAll('[data-new-task]').forEach((button) => button.addEventListener('click', createAndOpenTask));
workspaceLayout.assistantPet.addEventListener('click', () => setTaskAssistant(!workspaceLayout.assistant.classList.contains('is-open'), { focus: true }));
workspaceLayout.assistantClose.addEventListener('click', () => setTaskAssistant(false));
window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && currentWorkspaceMode() === 'desk' && workspaceLayout.assistant.classList.contains('is-open')) setTaskAssistant(false);
}, { signal: pageAbort.signal });
stream.addEventListener('click', (event) => {
  const starter = event.target.closest('[data-starter-prompt]');
  if (!starter) return;
  input.value = starter.dataset.starterPrompt;
  resizeComposer();
  input.focus();
}, { signal: pageAbort.signal });

document.querySelector('#payoff-zoom-out').addEventListener('click', () => setPayoffZoom(payoffZoom - .1));
document.querySelector('#payoff-zoom-in').addEventListener('click', () => setPayoffZoom(payoffZoom + .1));
document.querySelector('#payoff-zoom-fit').addEventListener('click', () => setPayoffZoom(1));
payoffSvg.addEventListener('load', () => {
  payoffSvgStage.classList.add('is-loaded');
  payoffCanvasState.textContent = '已载入正式收益图';
  payoffStateDot.className = 'payoff-state-dot is-ready';
});
payoffSvg.addEventListener('error', () => {
  payoffSvgStage.hidden = true;
  payoffEmptyCanvas.hidden = false;
  payoffCanvasState.textContent = '未找到已发布收益图';
  payoffStateDot.className = 'payoff-state-dot is-warning';
});

parameterProduct.addEventListener('change', () => {
  const previousId = parameterProduct.dataset.currentId;
  if (previousId) parameterDrafts.set(previousId, valuesFrom(parameterForm, inputPrefix.parameters));
  const selectedId = parameterProduct.value;
  parameterProduct.dataset.currentId = selectedId;
  const saved = normalizeParameters(moduleState.parameters);
  const values = parameterDrafts.get(selectedId) || (saved.productId === selectedId ? saved.payoff : {});
  renderParameterForm(selectedId, values);
  renderPayoffWorkbench({ ...saved, underlying: parameterUnderlying.value.trim(), productId: selectedId, payoff: values });
});

parameterForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const selected = deskContractFor(parameterProduct.value);
  const underlying = parameterUnderlying.value.trim();
  if (!selected || !underlying) {
    setNotice('请先填写一只研究标的并选择产品。', true);
    return;
  }
  const payoff = valuesFrom(parameterForm, inputPrefix.parameters);
  if (selected.payoff.includes('U') && !payoff.U) payoff.U = underlying;
  const values = { underlying, productId: selected.id, payoff, note: parameterForm.elements.note.value };
  parameterDrafts.set(selected.id, values.payoff);
  try {
    await saveModuleState('parameters', values);
    parameterForm.querySelector('.module-save-state').textContent = '已保存，并同步至收益结构、估值定价和历史回测。';
    setNotice();
  } catch (error) { setNotice(error.message || '参数保存失败。', true); }
});

pricingForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const parameters = normalizeParameters(moduleState.parameters);
  if (!deskContractFor(parameters.productId)) return;
  try {
    await saveModuleState('pricing', { productId: parameters.productId, inputs: valuesFrom(pricingForm, inputPrefix.pricing) });
    pricingForm.querySelector('.module-save-state').textContent = '已保存至当前任务。';
  } catch (error) { setNotice(error.message || '参数保存失败。', true); }
});

backtestForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const parameters = normalizeParameters(moduleState.parameters);
  if (!deskContractFor(parameters.productId)) return;
  try {
    await saveModuleState('backtest', { productId: parameters.productId, inputs: valuesFrom(backtestForm, inputPrefix.backtest) });
    backtestForm.querySelector('.module-save-state').textContent = '已保存至当前任务。';
  } catch (error) { setNotice(error.message || '参数保存失败。', true); }
});

form.addEventListener('submit', (event) => {
  event.preventDefault();
  if (requestController) requestController.abort();
  else send();
});

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    if (!requestController) send();
  }
});

window.addEventListener('popstate', async () => {
  if (currentTaskId() !== task?.id) await loadTask();
  else setTab(activeTab(), true);
}, { signal: pageAbort.signal });

window.addEventListener('optionhelper:taskchange', async () => {
  if (currentTaskId() !== task?.id) await loadTask();
}, { signal: pageAbort.signal });

window.addEventListener('optionhelper:modechange', (event) => {
  syncTaskAssistantMode(event.detail.mode);
  renderReportContextTitle();
  if (event.detail.mode === 'desk') setTab(activeTab(), true);
  deskShell.dataset.activeMode = event.detail.mode;
  resizeComposer();
  scheduleConversationEdgeRail();
  if (task && currentTaskId() === task.id) void loadTask().catch((error) => setNotice(error.message || '任务未能恢复。', true));
}, { signal: pageAbort.signal });

if (location.protocol === 'file:') {
  void mountModuleCopies();
  taskTitle.textContent = '静态预览';
  renderTaskMessages([], '请通过本机服务打开，以保存任务和使用模型。');
  setTab(activeTab(), true);
} else {
  const profile = await initializeChrome(initialMode);
  if (profile) {
    configureModelPicker(profile, modelPicker, (message) => setNotice(message, true));
    void mountModuleCopies();
    await loadTask();
  }
}
