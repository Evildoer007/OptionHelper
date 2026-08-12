import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { spawn } from 'node:child_process';
import test from 'node:test';

const temporaryHome = `/private/tmp/optionhelper-test-${process.pid}-${Date.now()}`;
const { privateKey, publicKey } = generateKeyPairSync('ed25519');
process.env.OPTIONHELPER_HOME = temporaryHome;
process.env.OPTIONHELPER_LICENSE_PUBLIC_KEY = publicKey.export({ type: 'spki', format: 'pem' });

const licenseCore = await import('../core/license.mjs');
const reportStore = await import('../core/report-store.mjs');
const preferenceCore = await import('../core/preferences.mjs');
const taskStore = await import('../core/task-store.mjs');
const parameterContracts = await import('../parameter-contracts.js');

const issue = (tier = 'optchat') => licenseCore.issueSignedLicense({ holder: '测试用户', tier, privateKey, productVersion: '1.0.0' });

test('许可证签名、层级和本机持久化', async () => {
  const license = issue('optdesk');
  const verified = await licenseCore.verifyLicense(license);
  assert.equal(verified.tier, 'optdesk');
  const imported = await licenseCore.importLicense(license);
  assert.deepEqual(imported.profile.allowedModes, ['chat', 'desk']);
  assert.deepEqual(licenseCore.tierProfile('admin').allowedModes, ['chat', 'desk']);
  assert.equal((await licenseCore.currentActivation()).license.licenseId, license.licenseId);
  await assert.rejects(() => licenseCore.verifyLicense({ ...license, holder: '被篡改' }), /签名校验/);
});

test('Report库拒绝OptChat读取完整版', async () => {
  const report = await reportStore.createReport({ taskId: 'task-test', format: 'full', title: '测试Report', sections: { conclusion: '结论', suitability: '场景', keyTerms: ['条款'], risks: ['风险'], disclaimer: '提示', taskBackground: '背景', moduleSources: ['Pricing'], parameters: ['参数'], valuationAssumptions: ['假设'], backtestStatus: '未接入计算', runRecord: '原型状态', pendingItems: ['确认'] }, provider: '测试服务', model: 'test-model' });
  const chatProfile = licenseCore.tierProfile('optchat');
  const deskProfile = licenseCore.tierProfile('optdesk');
  assert.equal((await reportStore.listReports(chatProfile)).length, 0);
  assert.equal((await reportStore.readReport(report.id, deskProfile)).title, '测试Report');
  await assert.rejects(() => reportStore.readReport(report.id, chatProfile), /无权读取/);
});

test('多个模型连接可独立保存并切换默认连接', async () => {
  const first = await preferenceCore.saveConnection({ provider: 'deepseek', model: 'deepseek-chat', baseUrl: 'https://api.deepseek.com', thinking: 'enabled' });
  const second = await preferenceCore.saveConnection({ provider: 'openai', model: 'gpt-5', baseUrl: 'https://api.openai.com/v1', thinking: 'disabled' });
  assert.equal((await preferenceCore.getConnections()).length, 2);
  assert.equal((await preferenceCore.getConnection()).id, second.id);
  await preferenceCore.setActiveConnection(first.id);
  assert.equal((await preferenceCore.getConnection()).id, first.id);
  assert.equal((await preferenceCore.getConnection(second.id)).model, 'gpt-5');
});

test('任务库持久化消息、模块参数和关联上下文', async () => {
  const task = await taskStore.createTask();
  assert.equal(task.title, '新建研究任务');
  assert.equal(task.scope, 'chat');
  const pinned = await taskStore.updateTask(task.id, { pinned: true });
  assert.equal(pinned.pinned, true);
  assert.equal((await taskStore.readTask(task.id)).task.pinned, true);
  await taskStore.appendMessage(task.id, { role: 'user', content: '研究中证500ETF的经典型雪球。' });
  await taskStore.appendMessage(task.id, { role: 'assistant', content: '请先确认期限与敲入敲出条款。', provider: '测试服务', model: 'test-model' });
  await taskStore.titleFromFirstPrompt(task.id, '研究中证500ETF的经典型雪球。');
  await taskStore.saveModule(task.id, 'parameters', { underlying: '中证500ETF', productId: '9.1', payoff: { U: '中证500ETF' } });
  const stored = await taskStore.readTask(task.id);
  assert.equal(stored.task.title, '研究中证500ETF的经典型雪球。');
  assert.equal(stored.modules.parameters.underlying, '中证500ETF');
  assert.equal((await taskStore.listMessages(task.id)).length, 2);
  assert.equal((await taskStore.contextForTask(task.id)).messages.length, 2);

  for (let index = 0; index < 23; index += 1) {
    await taskStore.appendMessage(task.id, { role: index % 2 ? 'assistant' : 'user', content: `补充对话${index + 1}` });
  }
  const summaryCandidate = await taskStore.messagesForSummary(task.id);
  assert.equal(summaryCandidate.messages.length, 5);
  await taskStore.saveContextSummary(task.id, '已压缩较早对话。', summaryCandidate.throughSequence);
  const compactedContext = await taskStore.contextForTask(task.id);
  assert.equal(compactedContext.summary, '已压缩较早对话。');
  assert.equal(compactedContext.messages.length, 20);
  await taskStore.deleteTask(task.id);
  await assert.rejects(() => taskStore.readTask(task.id), /未找到/);
});

test('Chat与Desk任务分别存储，并各自记住最后打开的任务', async () => {
  const chatTask = await taskStore.createTask({ title: 'Chat任务', scope: 'chat' });
  const deskTask = await taskStore.createTask({ title: 'Desk任务', scope: 'desk' });
  assert.deepEqual((await taskStore.listTasks('chat')).map((task) => task.id), [chatTask.id]);
  assert.deepEqual((await taskStore.listTasks('desk')).map((task) => task.id), [deskTask.id]);
  const profile = licenseCore.tierProfile('optdesk');
  await preferenceCore.setWorkspaceTaskPreference('chat', chatTask.id, profile);
  await preferenceCore.setWorkspaceTaskPreference('desk', chatTask.id, profile);
  assert.deepEqual(await preferenceCore.getWorkspaceTaskPreference(), { lastChatTaskId: chatTask.id, lastDeskTaskId: chatTask.id });
  await preferenceCore.setWorkspaceTaskPreference('desk', deskTask.id, profile);
  assert.deepEqual(await preferenceCore.getWorkspaceTaskPreference(), { lastChatTaskId: chatTask.id, lastDeskTaskId: deskTask.id });
  await Promise.all([
    preferenceCore.setModePreference('desk', profile),
    preferenceCore.setWorkspaceTaskPreference('chat', chatTask.id, profile),
    preferenceCore.setWorkspaceTaskPreference('desk', deskTask.id, profile),
  ]);
  assert.equal(await preferenceCore.getModePreference(), 'desk');
  assert.deepEqual(await preferenceCore.getWorkspaceTaskPreference(), { lastChatTaskId: chatTask.id, lastDeskTaskId: deskTask.id });
});

test('65个产品的PayoffInput均被PricingInput和BacktestInput完整继承', () => {
  assert.equal(parameterContracts.productContracts.length, 65);
  parameterContracts.productContracts.forEach((contract) => {
    const pricingInput = parameterContracts.fullPricingInputFor(contract);
    const backtestInput = parameterContracts.fullBacktestInputFor(contract);
    contract.payoff.forEach((key) => {
      assert.ok(parameterContracts.parameterFields[key], `${contract.id}存在未定义条款${key}`);
      assert.ok(pricingInput.includes(key), `${contract.id}的PricingInput遗漏${key}`);
      assert.ok(backtestInput.includes(key), `${contract.id}的BacktestInput遗漏${key}`);
    });
  });
});

test('不同产品只追加各自的估值和回测参数', () => {
  const snowball = parameterContracts.contractFor('9.1');
  const worstOf = parameterContracts.contractFor('9.18');
  const varianceSwap = parameterContracts.contractFor('10.4');
  assert.deepEqual(parameterContracts.pricingExtrasFor(snowball), ['tv', 'Stv', 'tau', 'sigma', 'r', 'q', 'M', 'xi', 'eps']);
  assert.deepEqual(parameterContracts.backtestExtrasFor(snowball), ['hist', 'ts', 'E', 'full', 'muobs', 'mumiss', 'chipi', 'Dret']);
  assert.ok(parameterContracts.pricingExtrasFor(worstOf).includes('rho'));
  assert.ok(parameterContracts.backtestExtrasFor(worstOf).includes('histMulti'));
  assert.ok(parameterContracts.pricingExtrasFor(varianceSwap).includes('Vhat'));
  assert.ok(parameterContracts.backtestExtrasFor(varianceSwap).includes('logret'));
});

test('本机API将OptChat与Desk入口隔离', async (t) => {
  const port = 45300 + Math.floor(Math.random() * 800);
  const server = spawn(process.execPath, ['assets/webs/desk-server.mjs'], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      OPTIONHELPER_DESK_PORT: String(port),
      OPTIONHELPER_HOME: `${temporaryHome}-server`,
      OPTIONHELPER_WEB_ROOT: 'assets/webs',
      OPTIONHELPER_LOGO_PATH: 'assets/icons/optionhelper-logo.svg',
    },
    stdio: 'ignore',
  });
  const root = `http://127.0.0.1:${port}`;
  try {
    let reachable = false;
    let localPortBlocked = false;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      try { if ((await fetch(`${root}/api/access/profile`)).ok) { reachable = true; break; } } catch (error) { if (error.cause?.code === 'EPERM') localPortBlocked = true; }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (localPortBlocked && !reachable) { t.skip('当前受限执行环境禁止连接本机端口。'); return; }
    assert.equal(reachable, true, '本机服务未能启动。');
    assert.deepEqual(await (await fetch(`${root}/api/access/profile`)).json(), { activated: false });
    const activate = await fetch(`${root}/api/activation/import`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ license: issue('optchat') }) });
    assert.equal(activate.status, 201);
    const profile = await (await fetch(`${root}/api/access/profile`)).json();
    assert.deepEqual(profile.allowedModes, ['chat']);
    const desk = await fetch(`${root}/assets/web-design/desk.html`);
    assert.equal(desk.status, 403);
    const chat = await fetch(`${root}/assets/web-design/chat.html`);
    assert.equal(chat.status, 200);
    const logo = await fetch(`${root}/logo/optionhelper-logo.svg`);
    assert.equal(logo.status, 200);
    assert.match(logo.headers.get('content-type') || '', /image\/svg\+xml/);
    const deskActivation = await fetch(`${root}/api/activation/import`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ license: issue('optdesk') }) });
    assert.equal(deskActivation.status, 201);
    const createdTask = await fetch(`${root}/api/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: 'chat' }) });
    assert.equal(createdTask.status, 201);
    const { task } = await createdTask.json();
    assert.equal(task.scope, 'chat');
    const createdDeskTask = await fetch(`${root}/api/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: 'desk' }) });
    assert.equal(createdDeskTask.status, 201);
    const { task: deskTask } = await createdDeskTask.json();
    assert.equal(deskTask.scope, 'desk');
    const deskTaskList = await fetch(`${root}/api/tasks?scope=desk`);
    assert.deepEqual((await deskTaskList.json()).tasks.map((item) => item.id), [deskTask.id]);
    const linkedDeskPreference = await fetch(`${root}/api/preferences/workspace`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'desk', taskId: task.id }) });
    assert.equal(linkedDeskPreference.status, 200);
    assert.equal((await linkedDeskPreference.json()).lastDeskTaskId, task.id);
    const deskReadChatTask = await fetch(`${root}/api/tasks/${task.id}?mode=desk`);
    assert.equal(deskReadChatTask.status, 200);
    const payoffReference = await fetch(`${root}/api/payoff/reference?productId=9.1`);
    assert.equal(payoffReference.status, 200);
    assert.match(payoffReference.headers.get('content-type') || '', /image\/svg\+xml/);
    const deniedMultiPayoff = await fetch(`${root}/api/payoff/reference?productId=9.18`);
    assert.equal(deniedMultiPayoff.status, 400);
    const rejectedMulti = await fetch(`${root}/api/tasks/${task.id}?mode=desk`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ module: 'parameters', value: { underlying: '中证500ETF', productId: '9.18', payoff: {} } }) });
    assert.equal(rejectedMulti.status, 400);
    const savedSingle = await fetch(`${root}/api/tasks/${task.id}?mode=desk`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ module: 'parameters', value: { underlying: '中证500ETF', productId: '9.1', payoff: { U: '中证500ETF' } } }) });
    assert.equal(savedSingle.status, 200);
    const taskMessages = await fetch(`${root}/api/tasks/${task.id}/messages?mode=desk`);
    assert.equal(taskMessages.status, 200);
    const chatActivation = await fetch(`${root}/api/activation/import`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ license: issue('optchat') }) });
    assert.equal(chatActivation.status, 201);
    const chatTask = await fetch(`${root}/api/tasks/${task.id}`);
    assert.deepEqual((await chatTask.json()).modules, {});
    const hiddenDeskTask = await fetch(`${root}/api/tasks/${deskTask.id}`);
    assert.equal(hiddenDeskTask.status, 403);
    const hiddenDeskList = await fetch(`${root}/api/tasks?scope=desk`);
    assert.equal(hiddenDeskList.status, 403);
    const deniedModule = await fetch(`${root}/api/tasks/${task.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ module: 'parameters', value: {} }) });
    assert.equal(deniedModule.status, 403);
  } finally {
    server.kill('SIGTERM');
  }
});

test.after(async () => { await fs.rm(temporaryHome, { recursive: true, force: true }); await fs.rm(`${temporaryHome}-server`, { recursive: true, force: true }); });
