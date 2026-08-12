const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {parseAndFormatDate, buildFetchRequest, resultSummary, parseServiceResponse} = require(path.join(__dirname, '..', 'page', 'datafetcher.js'));

assert.deepEqual(parseAndFormatDate('2026/07/31'), {iso: '2026-07-31', display: '2026/07/31'});
assert.deepEqual(parseAndFormatDate('2026-07-31'), {iso: '2026-07-31', display: '2026/07/31'});
assert.deepEqual(parseAndFormatDate('2024/02/29'), {iso: '2024-02-29', display: '2024/02/29'});
assert.equal(parseAndFormatDate('2025/02/29'), null);
assert.equal(parseAndFormatDate('2026/13/01'), null);
assert.equal(parseAndFormatDate('2026/07-31'), null);

assert.deepEqual(buildFetchRequest({
  assetIds: '510300.SH\n510500.SH', startDate: '2026/07/28', endDate: '2026-07-31', fields: 'close, adj_close',
  frequency: '1d', adjustment: 'auto', providerPriority: 'ifind_http', cachePolicy: 'force_refresh', offline: false, localCsv: '',
}), {asset_ids: ['510300.SH', '510500.SH'], start_date: '2026-07-28', end_date: '2026-07-31', fields: ['close', 'adj_close'], frequency: '1d', adjustment: 'auto', source_priority: ['ifind_http'], cache_policy: 'force_refresh', offline: false});
assert.throws(() => buildFetchRequest({assetIds: '', startDate: '2026/07/28', endDate: '2026/07/31', fields: 'close', frequency: '1d', adjustment: 'none', providerPriority: 'local', cachePolicy: 'reuse'}), /资产标识/);
assert.deepEqual(resultSummary({cache_decision: 'cache_hit', data_asset_ref: {data_asset_id: 'data-1', row_count: 3}}), [
  {label: '缓存决策', value: 'cache_hit'}, {label: '数据行数', value: 3}, {label: '资产引用', value: 'data-1'},
]);

(async () => {
  const payload = {ok: false, data_fetch_run_id: 'run-failed', provider_calls: [{provider: 'ifind_http', outcome: 'unauthorized'}], quota_usage: {used: 0, limit: 20}, error: {code: 'unauthorized', message: '凭据未配置'}};
  await assert.rejects(
    parseServiceResponse({ok: false, json: async () => payload}),
    error => error.message === '凭据未配置' && error.payload === payload,
  );
})().catch(error => { console.error(error); process.exitCode = 1; });

const html = fs.readFileSync(path.join(__dirname, '..', 'page', 'datafetcher.html'), 'utf8');
assert.match(html, /src="\.\.\/\.\.\/icons\/optionhelper-logo\.svg"/);
assert.match(html, /onerror="this\.onerror=null;this\.src='\.\.\/\.\.\/\.\.\/assets\/icons\/optionhelper-logo\.svg'"/);
assert.match(html, /href="\.\.\/\.\.\/icons\/optionhelper-app-icon-tile-light\.svg"/);
assert.doesNotMatch(html, /value="1w"|value="1m"/);
assert.match(html, /value="auto" selected/);
assert.match(html, /value="ifind_http" required/);
assert.match(html, /value="force_refresh" selected/);
assert.match(html, /任务数据资产留存/);

const pageJs = fs.readFileSync(path.join(__dirname, '..', 'page', 'datafetcher.js'), 'utf8');
assert.match(pageJs, /ref\.media_type === 'application\/json' \? '下载JSON' : '下载CSV'/);
assert.match(pageJs, /\/api\/assets\/\$\{encodeURIComponent\(id\)\}\/download/);
assert.match(pageJs, /unverified/);
assert.match(pageJs, /renderResult\(error\.payload\)/);
assert.match(pageJs, /实时获取数据/);
assert.doesNotMatch(html, />双端复权</);
