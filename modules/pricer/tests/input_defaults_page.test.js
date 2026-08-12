import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const root = path.resolve(import.meta.dirname, '..');
const page = fs.readFileSync(path.join(root, 'page/pricer.html'), 'utf8');

test('Pricer page defaults new contracts from the local device date', () => {
  assert.match(page, /const localTodayIso = \(\) =>/);
  assert.match(page, /function setNewIssueDefaults\(\).*valuationDate.*contractStartDate/s);
  assert.match(page, /setNewIssueDefaults\(\);if\(location\.protocol/);
  assert.match(page, /placeholder="yyyy\/mm\/dd"/);
});

test('S0Raw stays data-bound and only the explicit MC10 demo owns a spot', () => {
  assert.match(page, /placeholder="绑定行情后自动填充" readonly/);
  assert.match(page, /MC10演示结果，不可报价/);
  assert.match(page, /demo_mode:true,spot:csi500Demo\.referenceSpot/);
  assert.doesNotMatch(page, /valuationDate:'2026-07-28'/);
  assert.match(page, /function fillResolvedReference\(data\)/);
});
