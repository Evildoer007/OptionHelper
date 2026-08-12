export const api = async (path, options = {}) => {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '请求未完成。');
  return payload;
};

export const escapeText = (value) => String(value || '');

const connectionLabel = (connection) => connection ? `${connection.providerLabel || connection.provider} / ${connection.model}` : '配置模型';

const connectionSetupUrl = () => {
  const source = new URL(location.href);
  const target = new URL('./setup.html', source);
  const mode = document.querySelector('[data-workspace-shell]')?.dataset.mode === 'desk' ? 'desk' : 'chat';
  target.searchParams.set('returnTo', mode);
  ['task', 'tab'].forEach((key) => {
    const value = source.searchParams.get(key);
    if (value) target.searchParams.set(key, value);
  });
  return `${target.pathname}${target.search}`;
};

const updateConnectionLinkTargets = () => {
  document.querySelectorAll('[data-connection-link]').forEach((connectionLink) => {
    connectionLink.href = connectionSetupUrl();
  });
};

const renderConnectionLinks = (connection) => {
  updateConnectionLinkTargets();
  document.querySelectorAll('[data-connection-link]').forEach((connectionLink) => {
    const ready = Boolean(connection?.keyConfigured);
    connectionLink.dataset.connected = String(ready);
    if (connectionLink.hasAttribute('data-connection-static')) return;
    const value = connectionLink.querySelector('[data-connection-value]');
    if (value) value.textContent = ready ? connectionLabel(connection) : '配置模型';
    else connectionLink.textContent = ready ? connectionLabel(connection) : '配置模型';
  });
};

export const currentTaskId = () => new URLSearchParams(location.search).get('task') || '';

const modePage = (mode) => mode === 'desk' ? 'desk.html' : 'chat.html';
const shellForWorkspace = () => document.querySelector('[data-workspace-shell]');

const clampWorkspaceSize = (value, min, max) => Math.min(max, Math.max(min, value));
const readWorkspaceSize = (key, fallback, min, max) => {
  try {
    const saved = Number(localStorage.getItem(`optionhelper.workspace.${key}`));
    return Number.isFinite(saved) ? clampWorkspaceSize(saved, min, max) : fallback;
  } catch { return fallback; }
};
const saveWorkspaceSize = (key, value) => {
  try { localStorage.setItem(`optionhelper.workspace.${key}`, String(Math.round(value))); } catch { /* Local layout still works without storage. */ }
};

export const initializeWorkspaceLayoutControls = () => {
  const shell = shellForWorkspace();
  if (!shell || shell.dataset.layoutControlsReady === 'true') return;
  shell.dataset.layoutControlsReady = 'true';

  const railSplitter = shell.querySelector('[data-workspace-splitter="rail"]');
  const contextSplitter = shell.querySelector('[data-workspace-splitter="context"]');
  const railCollapseToggle = shell.querySelector('[data-rail-collapse-toggle]');
  const reportToggles = [...document.querySelectorAll('[data-report-toggle]')];
  const reportClose = shell.querySelector('[data-report-close]');
  const limits = {
    rail: { min: 248, max: 384, fallback: 248 },
    context: { min: 280, max: 480, fallback: 332 },
  };
  const minimumWorkArea = 560;

  const sizeFor = (kind) => {
    const limitsForKind = limits[kind];
    const cssVariable = kind === 'rail' ? '--rail-width' : '--report-width';
    const raw = Number.parseFloat(getComputedStyle(shell).getPropertyValue(cssVariable));
    return Number.isFinite(raw) && raw > 0 ? raw : limitsForKind.fallback;
  };
  const maximumFor = (kind) => {
    const limitsForKind = limits[kind];
    const reportWidth = shell.dataset.reportOpen === 'true' ? sizeFor('context') : 0;
    const splitterWidth = shell.dataset.reportOpen === 'true' ? 16 : 8;
    const otherWidth = kind === 'rail' ? reportWidth : sizeFor('rail');
    const available = Math.floor(shell.getBoundingClientRect().width - minimumWorkArea - splitterWidth - otherWidth);
    return Math.max(limitsForKind.min, Math.min(limitsForKind.max, available));
  };
  const applySize = (kind, value, persist = false) => {
    const limitsForKind = limits[kind];
    const maximum = maximumFor(kind);
    const next = clampWorkspaceSize(value, limitsForKind.min, maximum);
    shell.style.setProperty(kind === 'rail' ? '--rail-width' : '--report-width', `${next}px`);
    const splitter = kind === 'rail' ? railSplitter : contextSplitter;
    splitter?.setAttribute('aria-valuenow', String(Math.round(next)));
    splitter?.setAttribute('aria-valuemax', String(Math.round(maximum)));
    if (persist) saveWorkspaceSize(kind, next);
  };
  const constrainLayout = () => {
    applySize('rail', sizeFor('rail'));
    applySize('context', sizeFor('context'));
  };
  const setReportOpen = (open) => {
    shell.dataset.reportOpen = String(open);
    reportToggles.forEach((button) => {
      button.setAttribute('aria-expanded', String(open));
      button.classList.toggle('is-active', open);
    });
    if (contextSplitter) {
      contextSplitter.tabIndex = open ? 0 : -1;
      contextSplitter.setAttribute('aria-hidden', String(!open));
    }
    if (open) constrainLayout();
  };

  const setRailCollapsed = (collapsed, persist = false) => {
    shell.dataset.railCollapsed = String(collapsed);
    railCollapseToggle?.setAttribute('aria-expanded', String(!collapsed));
    railCollapseToggle?.setAttribute('aria-label', collapsed ? '展开任务栏' : '收起任务栏');
    railCollapseToggle?.setAttribute('title', collapsed ? '展开任务栏' : '收起任务栏');
    if (railSplitter) {
      railSplitter.tabIndex = collapsed ? -1 : 0;
      railSplitter.setAttribute('aria-hidden', String(collapsed));
    }
    if (!collapsed) constrainLayout();
    if (persist) {
      try { localStorage.setItem('optionhelper.workspace.railCollapsed', String(collapsed)); } catch { /* Layout still works without storage. */ }
    }
  };

  applySize('rail', readWorkspaceSize('rail', limits.rail.fallback, limits.rail.min, limits.rail.max));
  applySize('context', readWorkspaceSize('context', limits.context.fallback, limits.context.min, limits.context.max));
  setReportOpen(false);
  try { setRailCollapsed(localStorage.getItem('optionhelper.workspace.railCollapsed') === 'true'); } catch { setRailCollapsed(false); }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    shell.dataset.layoutReady = 'true';
  }));

  const bindSplitter = (splitter, kind) => {
    if (!splitter) return;
    let drag = null;
    const finish = () => {
      if (!drag) return;
      saveWorkspaceSize(kind, sizeFor(kind));
      drag = null;
      shell.dataset.resizing = 'false';
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', finish);
      document.removeEventListener('pointercancel', finish);
    };
    const move = (event) => {
      if (!drag) return;
      const direction = kind === 'rail' ? 1 : -1;
      applySize(kind, drag.width + ((event.clientX - drag.startX) * direction));
    };
    splitter.addEventListener('pointerdown', (event) => {
      if (kind === 'context' && shell.dataset.reportOpen !== 'true') return;
      drag = { startX: event.clientX, width: sizeFor(kind) };
      shell.dataset.resizing = 'true';
      splitter.setPointerCapture?.(event.pointerId);
      event.preventDefault();
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', finish);
      document.addEventListener('pointercancel', finish);
    });
    splitter.addEventListener('keydown', (event) => {
      if (kind === 'context' && shell.dataset.reportOpen !== 'true') return;
      const step = event.shiftKey ? 40 : 16;
      if (event.key === 'ArrowLeft') applySize(kind, sizeFor(kind) + (kind === 'rail' ? -step : step), true);
      else if (event.key === 'ArrowRight') applySize(kind, sizeFor(kind) + (kind === 'rail' ? step : -step), true);
      else return;
      event.preventDefault();
    });
  };

  bindSplitter(railSplitter, 'rail');
  bindSplitter(contextSplitter, 'context');
  railCollapseToggle?.addEventListener('click', () => setRailCollapsed(shell.dataset.railCollapsed !== 'true', true));
  reportToggles.forEach((button) => button.addEventListener('click', () => setReportOpen(shell.dataset.reportOpen !== 'true')));
  reportClose?.addEventListener('click', () => setReportOpen(false));
  window.addEventListener('resize', constrainLayout);
};

export const currentWorkspaceMode = () => shellForWorkspace()?.dataset.mode === 'desk' ? 'desk' : 'chat';

const modeUrl = (mode) => {
  const next = new URL(location.href);
  next.pathname = next.pathname.replace(/[^/]+$/, modePage(mode));
  return next;
};

const applyWorkspaceMode = (mode, { replace = false, updateHistory = true } = {}) => {
  const shell = shellForWorkspace();
  if (!shell) return;
  const nextMode = mode === 'desk' ? 'desk' : 'chat';
  shell.dataset.mode = nextMode;
  document.querySelectorAll('button[data-mode]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.mode === nextMode)));
  updateConnectionLinkTargets();
  if (updateHistory) {
    const next = modeUrl(nextMode);
    history[replace ? 'replaceState' : 'pushState']({ mode: nextMode }, '', next);
  }
  window.dispatchEvent(new CustomEvent('optionhelper:modechange', { detail: { mode: nextMode } }));
};

export const switchWorkspaceMode = async (mode, currentMode = currentWorkspaceMode()) => {
  if (mode === currentMode) return null;
  const shell = shellForWorkspace();
  const currentTask = currentTaskId();
  const currentScope = shell?.dataset.taskScope || '';
  let nextTask = currentTask;
  if (mode === 'chat' && currentScope !== 'chat') {
    const preferences = await api('/api/preferences/workspace');
    nextTask = preferences.lastChatTaskId || '';
  }
  if (!nextTask) {
    const scope = mode === 'desk' ? 'desk' : 'chat';
    const created = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ scope }) });
    nextTask = created.task.id;
  }
  const next = modeUrl(mode);
  next.searchParams.set('task', nextTask);
  if (mode === 'desk') next.searchParams.set('tab', next.searchParams.get('tab') || 'parameters');
  else next.searchParams.delete('tab');
  history.pushState({ mode }, '', next);
  applyWorkspaceMode(mode, { updateHistory: false });
  updateConnectionLinkTargets();
  void rememberWorkspaceTask(nextTask, mode);
  window.dispatchEvent(new CustomEvent('optionhelper:taskchange', { detail: { taskId: nextTask } }));
  return api('/api/preferences/mode', { method: 'POST', body: JSON.stringify({ mode }) }).catch(() => null);
};

export const rememberWorkspaceTask = async (taskId, mode = currentWorkspaceMode()) => {
  if (!taskId) return null;
  return api('/api/preferences/workspace', { method: 'POST', body: JSON.stringify({ mode, taskId }) });
};

export const setTaskUrl = (taskId, { page = null, replace = false, tab = null } = {}) => {
  const url = new URL(location.href);
  if (page) url.pathname = url.pathname.replace(/[^/]+$/, page);
  url.searchParams.set('task', taskId);
  if (tab) url.searchParams.set('tab', tab);
  if (shellForWorkspace()) {
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
    updateConnectionLinkTargets();
    void rememberWorkspaceTask(taskId);
    window.dispatchEvent(new CustomEvent('optionhelper:taskchange', { detail: { taskId } }));
    return;
  }
  if (replace) history.replaceState({}, '', url);
  else location.href = url;
};

export const ensureTask = async () => {
  const mode = currentWorkspaceMode();
  const load = (taskId) => api(`/api/tasks/${encodeURIComponent(taskId)}?mode=${mode}`);
  const requested = currentTaskId();
  if (requested) {
    try {
      const loaded = await load(requested);
      void rememberWorkspaceTask(loaded.task.id, mode);
      return loaded;
    } catch (error) {
      if (!/(未找到该任务|Chat不能读取独立Desk任务)/.test(error.message)) throw error;
    }
  }
  const preferences = await api('/api/preferences/workspace');
  const remembered = mode === 'desk' ? preferences.lastDeskTaskId : preferences.lastChatTaskId;
  if (remembered) {
    try {
      const loaded = await load(remembered);
      const url = new URL(location.href);
      url.searchParams.set('task', loaded.task.id);
      history.replaceState({}, '', url);
      updateConnectionLinkTargets();
      return loaded;
    } catch (error) {
      if (!/(未找到该任务|Chat不能读取独立Desk任务)/.test(error.message)) throw error;
    }
  }
  const scope = mode === 'desk' ? 'desk' : 'chat';
  const created = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ scope }) });
  const url = new URL(location.href);
  url.searchParams.set('task', created.task.id);
  history.replaceState({}, '', url);
  updateConnectionLinkTargets();
  void rememberWorkspaceTask(created.task.id, mode);
  return { task: created.task, modules: {} };
};

export const enableComposerAutoResize = (input) => {
  const resize = () => {
    const compact = input.closest('[data-workspace-shell]')?.dataset.mode === 'desk';
    const maxHeight = compact ? 36 : 132;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, maxHeight)}px`;
    input.style.overflowY = input.scrollHeight > maxHeight ? 'auto' : 'hidden';
  };
  input.addEventListener('input', resize);
  resize();
  return resize;
};

export const initializeChrome = async (pageMode) => {
  const profile = await api('/api/access/profile');
  if (!profile.activated) { location.href = './activation.html'; return null; }
  if (pageMode === 'desk' && !profile.allowedModes.includes('desk')) { location.href = './chat.html'; return null; }
  document.querySelectorAll('button[data-mode]').forEach((button) => {
    const mode = button.dataset.mode;
    const available = profile.allowedModes.includes(mode);
    button.hidden = !available;
    button.setAttribute('aria-pressed', String(mode === pageMode));
    button.addEventListener('click', async () => {
      if (mode === currentWorkspaceMode() || !available) return;
      try {
        await switchWorkspaceMode(mode, currentWorkspaceMode());
      } catch { /* stay on the authorized page when the preference write fails */ }
    });
  });
  applyWorkspaceMode(pageMode, { updateHistory: false });
  renderConnectionLinks(profile.connection);
  return profile;
};

window.addEventListener('popstate', () => {
  const shell = shellForWorkspace();
  if (!shell) return;
  const mode = location.pathname.endsWith('/desk.html') ? 'desk' : 'chat';
  applyWorkspaceMode(mode, { updateHistory: false });
});

export const configureModelPicker = (profile, picker, onError = () => {}) => {
  if (!picker) return () => null;
  const shell = picker.closest('.model-picker');
  const connections = (profile.connections || []).filter((connection) => connection.keyConfigured);
  picker.replaceChildren();
  if (!connections.length) {
    const option = document.createElement('option');
    option.textContent = '未配置模型';
    option.value = '';
    picker.append(option);
    picker.disabled = true;
    if (shell) shell.hidden = false;
    return () => null;
  }
  connections.forEach((connection) => {
    const option = document.createElement('option');
    option.value = connection.id;
    option.textContent = connectionLabel(connection);
    picker.append(option);
  });
  picker.disabled = false;
  picker.value = connections.some((connection) => connection.id === profile.activeConnectionId) ? profile.activeConnectionId : connections[0].id;
  if (shell) shell.hidden = false;
  let previousId = picker.value;
  picker.addEventListener('change', async () => {
    const selectedId = picker.value;
    picker.disabled = true;
    try {
      const payload = await api('/api/model-connection/active', { method: 'POST', body: JSON.stringify({ id: selectedId }) });
      previousId = selectedId;
      renderConnectionLinks(payload.connection);
      picker.dispatchEvent(new CustomEvent('optionhelper:model-change', { bubbles: true, detail: payload.connection }));
    } catch (error) {
      picker.value = previousId;
      onError(error.message || '模型切换失败。');
    } finally {
      picker.disabled = false;
    }
  });
  return () => picker.value || null;
};

export const renderReports = (target, reports, emptyText = '尚无本机Report。') => {
  target.replaceChildren();
  if (!reports.length) {
    const empty = document.createElement('section');
    empty.className = 'report-empty';
    if (target.classList.contains('report-list--wide')) {
      empty.classList.add('report-empty--wide');
    }
    const title = document.createElement('strong');
    title.textContent = '尚无研究报告';
    const copy = document.createElement('p');
    copy.textContent = emptyText;
    empty.append(title, copy);
    target.append(empty);
    return;
  }
  reports.forEach((report) => {
    const link = document.createElement('a');
    link.className = 'report-item';
    link.href = `./report.html?id=${encodeURIComponent(report.id)}&mode=${currentWorkspaceMode()}`;
    const title = document.createElement('strong');
    title.textContent = report.title;
    const meta = document.createElement('small');
    meta.textContent = `${report.format === 'full' ? '完整版' : '简版'}Report ${new Date(report.createdAt).toLocaleString('zh-CN', { hour12: false })}`;
    link.append(title, meta);
    target.append(link);
  });
};

export const appendMessage = (stream, role, content, meta = '') => {
  const message = document.createElement('article');
  message.className = `message message--${role}`;
  const body = document.createElement('div');
  body.className = 'message__body';
  if (role === 'assistant') {
    const metaNode = document.createElement('div');
    metaNode.className = 'message__meta';
    const identity = document.createElement('span');
    identity.className = 'message__identity';
    identity.textContent = 'OptionHelper';
    metaNode.append(identity);
    if (meta) {
      const origin = document.createElement('span');
      origin.className = 'message__origin';
      origin.textContent = meta;
      metaNode.append(origin);
    }
    body.append(metaNode);
  }
  const text = document.createElement('div');
  text.textContent = escapeText(content);
  body.append(text);
  if (role === 'assistant') {
    const mark = document.createElement('div');
    mark.className = 'message__mark message__mark--assistant';
    const icon = document.createElement('img');
    icon.src = '../../logo/optionhelper-mark.svg';
    icon.alt = '';
    mark.append(icon);
    message.append(mark);
  }
  message.append(body);
  stream.append(message);
  stream.scrollTop = stream.scrollHeight;
  return message;
};

const conversationStarters = [
  {
    title: '筛选候选结构',
    copy: '根据标的、期限与风险约束缩小选择范围。',
    prompt: '请基于一只标的，为我筛选合适的单标的结构化产品。我的研究目标、期限和风险约束如下：',
  },
  {
    title: '设计产品条款',
    copy: '从执行价、障碍和票息整理合约条款。',
    prompt: '请帮我设计一份单标的结构化产品条款，包含标的、期限、执行价、障碍和票息：',
  },
  {
    title: '准备估值输入',
    copy: '梳理估值日、现价、波动率和利率假设。',
    prompt: '请为当前结构整理估值所需的市场数据和模型假设：',
  },
  {
    title: '建立回测方案',
    copy: '定义样本区间、入场规则和观察口径。',
    prompt: '请为当前结构建立历史回测方案，明确样本区间、入场规则和观察口径：',
  },
];

const renderConversationStart = (stream, emptyText) => {
  const start = document.createElement('section');
  start.className = 'conversation-start';
  start.setAttribute('aria-labelledby', 'conversation-start-title');

  const mark = document.createElement('div');
  mark.className = 'conversation-start__mark';
  const icon = document.createElement('img');
  icon.src = '../../logo/optionhelper-mark.svg';
  icon.alt = '';
  mark.append(icon);

  const heading = document.createElement('h2');
  heading.id = 'conversation-start-title';
  heading.textContent = '开始一项结构化产品研究';
  const copy = document.createElement('p');
  copy.className = 'conversation-start__copy';
  copy.textContent = '先描述研究目标，随后可进入条款、估值、回测和报告。';

  const starters = document.createElement('div');
  starters.className = 'conversation-starters';
  starters.setAttribute('aria-label', '研究任务建议');
  conversationStarters.forEach((starter) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'conversation-starter';
    button.dataset.starterPrompt = starter.prompt;
    const title = document.createElement('strong');
    title.textContent = starter.title;
    const detail = document.createElement('span');
    detail.textContent = starter.copy;
    button.append(title, detail);
    starters.append(button);
  });

  start.append(mark, heading, copy, starters);
  if (emptyText) {
    const note = document.createElement('p');
    note.className = 'conversation-start__note';
    note.textContent = emptyText;
    start.append(note);
  }
  stream.append(start);
};

export const renderMessages = (stream, messages, emptyText = '') => {
  stream.replaceChildren();
  if (!messages.length && emptyText) {
    renderConversationStart(stream, emptyText);
    return;
  }
  messages.forEach((message) => appendMessage(stream, message.role, message.content, message.provider && message.model ? `${message.provider} / ${message.model}` : ''));
};

export const renderTasks = (target, tasks, { activeTaskId, onSelect, onDelete, onRename, onTogglePin, onError, emptyText = '暂无任务' } = {}) => {
  target._taskMenuCleanup?.();
  target.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement('p');
    empty.className = 'task-item task-item--empty';
    empty.textContent = emptyText;
    target.append(empty);
    return;
  }
  let activeMenu = null;
  const closeMenus = () => {
    if (!activeMenu) return;
    activeMenu.hidden = true;
    activeMenu.closest('.task-list__row')?.querySelector('.task-menu-trigger')?.setAttribute('aria-expanded', 'false');
    activeMenu = null;
  };
  const runAction = async (action) => {
    try { await action(); } catch (error) { onError?.(error.message || '任务操作未完成。'); }
  };

  tasks.forEach((task) => {
    const row = document.createElement('div');
    row.className = `task-list__row${task.pinned ? ' is-pinned' : ''}`;
    row.dataset.taskId = task.id;
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'task-item';
    item.setAttribute('aria-current', String(task.id === activeTaskId));
    const title = document.createElement('span');
    title.className = 'task-item__title';
    title.textContent = task.title;
    const time = document.createElement('small');
    time.textContent = new Date(task.updatedAt).toLocaleString('zh-CN', { hour12: false });
    item.append(title, time);
    item.addEventListener('click', () => onSelect?.(task));
    row.append(item);

    const menuTrigger = document.createElement('button');
    menuTrigger.type = 'button';
    menuTrigger.className = 'task-menu-trigger';
    menuTrigger.setAttribute('aria-label', `任务操作${task.title}`);
    menuTrigger.setAttribute('aria-expanded', 'false');
    menuTrigger.textContent = '…';
    const menu = document.createElement('div');
    menu.className = 'task-menu';
    menu.hidden = true;

    const addMenuAction = (label, className, handler) => {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = className;
      action.textContent = label;
      action.addEventListener('click', (event) => {
        event.stopPropagation();
        closeMenus();
        runAction(handler);
      });
      menu.append(action);
    };
    addMenuAction(task.pinned ? '取消置顶' : '置顶', 'task-menu__action', () => onTogglePin?.(task));
    addMenuAction('重命名', 'task-menu__action', () => {
      if (!onRename) return;
      const editor = document.createElement('input');
      editor.className = 'task-rename-input';
      editor.value = task.title;
      editor.maxLength = 80;
      editor.setAttribute('aria-label', '任务名称');
      let settled = false;
      const restore = () => {
        if (editor.isConnected) editor.replaceWith(item);
      };
      const save = async () => {
        if (settled) return;
        settled = true;
        const nextTitle = editor.value.trim();
        if (!nextTitle || nextTitle === task.title) { restore(); return; }
        try { await onRename(task, nextTitle); } catch (error) { restore(); onError?.(error.message || '任务重命名未完成。'); }
      };
      editor.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); save(); }
        if (event.key === 'Escape') { settled = true; restore(); }
      });
      editor.addEventListener('blur', save, { once: true });
      item.replaceWith(editor);
      editor.focus();
      editor.select();
    });
    addMenuAction('删除', 'task-menu__action task-menu__action--danger', () => onDelete?.(task));
    menuTrigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      closeMenus();
      menu.hidden = !willOpen;
      menuTrigger.setAttribute('aria-expanded', String(willOpen));
      activeMenu = willOpen ? menu : null;
    });
    row.append(menuTrigger, menu);
    target.append(row);
  });
  const onDocumentClick = (event) => { if (!target.contains(event.target)) closeMenus(); };
  const onKeyDown = (event) => { if (event.key === 'Escape') closeMenus(); };
  document.addEventListener('click', onDocumentClick);
  target.addEventListener('keydown', onKeyDown);
  target._taskMenuCleanup = () => {
    document.removeEventListener('click', onDocumentClick);
    target.removeEventListener('keydown', onKeyDown);
  };
};
