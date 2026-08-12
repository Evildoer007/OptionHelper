import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';

const root = path.resolve(import.meta.dirname, '..');
const source = fs.readFileSync(path.join(root, 'page/ui/date-control.js'), 'utf8');
const context = { window: {} };
vm.runInNewContext(source, context);
const date = context.window.OptionHelperDate;

test('display uses yyyy/mm/dd and protocol uses ISO', () => {
  assert.equal(date.toDisplay('2026-07-08'), '2026/07/08');
  assert.equal(date.toIso('2026/07/08', '估值日'), '2026-07-08');
  assert.equal(date.toIso('2026-07-08', '估值日'), '2026-07-08');
  assert.equal(date.toIso('', '估值日'), null);
});

test('invalid calendar dates are rejected', () => {
  assert.throws(() => date.toIso('2026/02/29', '估值日'), /有效日历日期/);
  assert.throws(() => date.toIso('2026/7/08', '估值日'), /yyyy\/mm\/dd/);
});

test('page loads the one date boundary and avoids native date input', () => {
  const page = fs.readFileSync(path.join(root, 'page/pricer.html'), 'utf8');
  assert.match(page, /ui\/date-control\.js/);
  assert.doesNotMatch(page, /type="date"/);
  assert.equal((page.match(/placeholder="yyyy\/mm\/dd"/g) || []).length, 2);
  assert.match(page, /function validateDateInputs\(\)\{OptionHelperDate\.toIso/);
});
