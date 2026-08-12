const setupReturnUrl = () => {
  const query = new URLSearchParams(location.search);
  const mode = query.get('returnTo') === 'desk' ? 'desk' : 'chat';
  const target = new URL(`./${mode}.html`, location.href);
  ['task', 'tab'].forEach((key) => {
    const value = query.get(key);
    if (value) target.searchParams.set(key, value);
  });
  return `${target.pathname}${target.search}`;
};

document.querySelectorAll('[data-setup-return]').forEach((link) => {
  link.href = setupReturnUrl();
});

const form = document.querySelector('#connection-form');
const provider = form.elements.provider;
const model = form.elements.model;
const baseUrl = form.elements.baseUrl;
const thinkingField = document.querySelector('#thinking-field');
const result = document.querySelector('#form-result');
const connectionList = document.querySelector('#connection-list');
const newConnectionButton = document.querySelector('#new-connection');
const editorTitle = document.querySelector('#connection-editor-title');
const editorCopy = document.querySelector('#connection-editor-copy');
let providerCatalog = {};
let connections = [];

const selectedProvider = () => providerCatalog[provider.value] || {};
const connectionLabel = (connection) => `${connection.providerLabel || providerCatalog[connection.provider]?.label || connection.provider} / ${connection.model}`;

const setDefaults = () => {
  const selected = selectedProvider();
  if (model.dataset.auto !== 'false') {
    model.value = selected.defaultModel || '';
    model.dataset.auto = 'true';
  }
  if (baseUrl.dataset.auto !== 'false') {
    baseUrl.value = selected.baseUrl || '';
    baseUrl.dataset.auto = 'true';
  }
  thinkingField.hidden = !selected.supportsThinking;
  if (thinkingField.hidden) form.elements.thinking.checked = false;
};

const renderProviders = () => {
  const previous = provider.value;
  provider.replaceChildren();
  Object.values(providerCatalog).forEach((item) => {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = item.label;
    provider.append(option);
  });
  provider.value = providerCatalog[previous] ? previous : 'deepseek';
};

const renderConnections = (selectedId = form.elements.id.value) => {
  connectionList.replaceChildren();
  if (!connections.length) {
    const empty = document.createElement('p');
    empty.className = 'connection-list__empty';
    empty.textContent = '尚未配置模型。';
    connectionList.append(empty);
    return;
  }
  connections.forEach((connection) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'connection-item';
    item.dataset.connectionId = connection.id;
    item.setAttribute('aria-pressed', String(connection.id === selectedId));
    const title = document.createElement('strong');
    const meta = document.createElement('small');
    title.textContent = connectionLabel(connection);
    meta.textContent = connection.keyConfigured ? '连接已保存' : '缺少API Key';
    item.append(title, meta);
    item.addEventListener('click', () => selectConnection(connection.id));
    connectionList.append(item);
  });
};

const resetEditor = () => {
  form.reset();
  form.elements.id.value = '';
  provider.value = providerCatalog.deepseek ? 'deepseek' : Object.keys(providerCatalog)[0] || '';
  model.dataset.auto = 'true';
  baseUrl.dataset.auto = 'true';
  editorTitle.textContent = '新增模型连接';
  editorCopy.textContent = '保存后会测试连接。配置完成后可在输入框中直接切换。';
  result.textContent = '';
  result.className = 'form-result';
  setDefaults();
  renderConnections();
  model.focus();
};

const selectConnection = (connectionId) => {
  const connection = connections.find((item) => item.id === connectionId);
  if (!connection) return;
  form.elements.id.value = connection.id;
  provider.value = connection.provider;
  model.value = connection.model;
  baseUrl.value = connection.baseUrl;
  form.elements.thinking.checked = connection.thinking === 'enabled';
  form.elements.apiKey.value = '';
  model.dataset.auto = 'false';
  baseUrl.dataset.auto = 'false';
  editorTitle.textContent = '编辑模型连接';
  editorCopy.textContent = connection.keyConfigured ? '留空API Key会继续使用当前钥匙串中的凭据。' : '当前连接缺少API Key，请填写后重新测试。';
  result.textContent = '';
  result.className = 'form-result';
  setDefaults();
  renderConnections(connection.id);
};

model.addEventListener('input', () => { model.dataset.auto = 'false'; });
baseUrl.addEventListener('input', () => { baseUrl.dataset.auto = 'false'; });
provider.addEventListener('change', () => {
  model.dataset.auto = 'true';
  baseUrl.dataset.auto = 'true';
  setDefaults();
});
newConnectionButton.addEventListener('click', resetEditor);

const fetchConnections = async (selectedId = null) => {
  const response = await fetch('/api/model-connection');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '模型配置无法读取。');
  providerCatalog = data.providers || {};
  connections = data.connections || [];
  renderProviders();
  const nextId = selectedId || data.activeConnectionId || connections[0]?.id || null;
  if (nextId) selectConnection(nextId);
  else resetEditor();
};

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const data = Object.fromEntries(new FormData(form));
  data.thinking = form.elements.thinking.checked ? 'enabled' : 'disabled';
  data.test = true;
  button.disabled = true;
  result.textContent = '正在测试连接。';
  result.className = 'form-result';
  try {
    const response = await fetch('/api/model-connection', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '模型连接失败。');
    form.elements.apiKey.value = '';
    await fetchConnections(payload.connection.id);
    result.textContent = '连接成功，已保存到当前设备。';
  } catch (error) {
    result.textContent = error.message || '模型连接失败。';
    result.className = 'form-result error';
  } finally {
    button.disabled = false;
  }
});

fetchConnections().catch((error) => {
  result.textContent = error.message || '模型配置无法读取。';
  result.className = 'form-result error';
});
