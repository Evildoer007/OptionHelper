import assert from 'node:assert/strict';
import test from 'node:test';
import { listProducts } from '../app/core.mjs';
import { appendFormulaContent, formulaPlainText, parseInlineFormula } from '../app/public/formula-renderer.mjs';

function fakeDocument() {
  const documentRef = {
    createTextNode(value) { return { type: 'text', value }; },
    createElement(tag) {
      return {
        type: tag,
        className: '',
        ownerDocument: documentRef,
        children: [],
        append(...children) { this.children.push(...children); },
      };
    },
  };
  return documentRef;
}

test('轻量公式渲染消除定界符、下标源码和常用LaTex命令', () => {
  assert.equal(formulaPlainText('$S_T\\le K$'), 'ST≤ K');
  assert.equal(formulaPlainText('$K_1<S_T<K_2$'), 'K1<ST<K2');
  assert.equal(formulaPlainText('$H_{out,t}$'), 'Hout,t');
  assert.equal(formulaPlainText('未敲出且$S_T\\ge K_2$，且$H_{out,t}$未触及'), '未敲出且ST≥ K2，且Hout,t未触及');
});

test('未知命令和未闭合定界符降级为可读文本', () => {
  assert.equal(formulaPlainText('$S_T\\unknown K$'), 'STunknown K');
  assert.equal(formulaPlainText('条件为$S_T\\le K'), '条件为ST≤ K');
  assert.equal(parseInlineFormula('无公式文本').length, 1);
});

test('公式以文本节点和语义上下标节点写入DOM，不拼接资料库HTML', () => {
  const documentRef = fakeDocument();
  const target = documentRef.createElement('span');
  appendFormulaContent(target, '未敲出且$S_T\\ge K_2$');
  assert.equal(target.children[0].value, '未敲出且');
  assert.equal(target.children[1].type, 'span');
  assert.equal(target.children[1].className, 'formula');
  assert.deepEqual(target.children[1].children.map((node) => node.type), ['text', 'sub', 'text', 'sub']);
  assert.equal(target.children[1].children[1].className, 'formula-sub');
});

test('当前资料库的全部判断条件均可无源码符号降级', async () => {
  const conditions = (await listProducts()).flatMap((product) => product.scenarios.map((scenario) => scenario.condition));
  assert.equal(conditions.length, 197);
  for (const condition of conditions) {
    const rendered = formulaPlainText(condition);
    assert(!/[\\$_{}]/.test(rendered), `仍含公式源码：${condition} -> ${rendered}`);
  }
});
