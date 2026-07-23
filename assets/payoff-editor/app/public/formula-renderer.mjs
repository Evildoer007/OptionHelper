const COMMANDS = Object.freeze({
  le: '≤',
  ge: '≥',
  neq: '≠',
  ne: '≠',
  cdot: '·',
  times: '×',
  pm: '±',
  mp: '∓',
  infty: '∞',
  alpha: 'α',
  beta: 'β',
  gamma: 'γ',
  delta: 'δ',
  epsilon: 'ε',
  theta: 'θ',
  lambda: 'λ',
  mu: 'μ',
  pi: 'π',
  rho: 'ρ',
  sigma: 'σ',
  tau: 'τ',
  phi: 'φ',
  omega: 'ω',
  Gamma: 'Γ',
  Delta: 'Δ',
  Theta: 'Θ',
  Lambda: 'Λ',
  Pi: 'Π',
  Sigma: 'Σ',
  Phi: 'Φ',
  Omega: 'Ω',
});

function textNode(value) {
  return { type: 'text', value };
}

function appendText(nodes, value) {
  if (!value) return;
  const previous = nodes.at(-1);
  if (previous?.type === 'text') previous.value += value;
  else nodes.push(textNode(value));
}

function readGroup(source, start) {
  if (source[start] !== '{') return { value: source[start] || '', next: Math.min(start + 1, source.length) };
  let depth = 1;
  let cursor = start + 1;
  while (cursor < source.length && depth > 0) {
    if (source[cursor] === '{') depth += 1;
    else if (source[cursor] === '}') depth -= 1;
    cursor += 1;
  }
  return {
    value: source.slice(start + 1, depth === 0 ? cursor - 1 : source.length),
    next: cursor,
  };
}

function parseMath(source) {
  const nodes = [];
  for (let cursor = 0; cursor < source.length;) {
    const character = source[cursor];
    if (character === '\\') {
      const commandMatch = /^[A-Za-z]+/.exec(source.slice(cursor + 1));
      if (!commandMatch) {
        appendText(nodes, source[cursor + 1] || '');
        cursor += 2;
        continue;
      }
      const command = commandMatch[0];
      appendText(nodes, COMMANDS[command] || command);
      cursor += command.length + 1;
      continue;
    }
    if (character === '_' || character === '^') {
      const group = readGroup(source, cursor + 1);
      nodes.push({ type: character === '_' ? 'sub' : 'sup', children: parseMath(group.value) });
      cursor = group.next;
      continue;
    }
    if (character === '{') {
      const group = readGroup(source, cursor);
      nodes.push(...parseMath(group.value));
      cursor = group.next;
      continue;
    }
    if (character !== '}') appendText(nodes, character);
    cursor += 1;
  }
  return nodes;
}

export function parseInlineFormula(value) {
  const source = String(value ?? '');
  const pieces = [];
  let cursor = 0;
  while (cursor < source.length) {
    const opening = source.indexOf('$', cursor);
    if (opening < 0) {
      if (cursor < source.length) pieces.push({ type: 'text', value: source.slice(cursor) });
      break;
    }
    if (opening > cursor) pieces.push({ type: 'text', value: source.slice(cursor, opening) });
    const closing = source.indexOf('$', opening + 1);
    if (closing < 0) {
      pieces.push({ type: 'math', nodes: parseMath(source.slice(opening + 1)), malformed: true });
      break;
    }
    pieces.push({ type: 'math', nodes: parseMath(source.slice(opening + 1, closing)) });
    cursor = closing + 1;
  }
  return pieces;
}

function flattenNodes(nodes) {
  return nodes.map((node) => node.type === 'text' ? node.value : flattenNodes(node.children)).join('');
}

export function formulaPlainText(value) {
  return parseInlineFormula(value).map((piece) => piece.type === 'text' ? piece.value : flattenNodes(piece.nodes)).join('');
}

function appendMathNodes(target, nodes, documentRef) {
  for (const node of nodes) {
    if (node.type === 'text') {
      target.append(documentRef.createTextNode(node.value));
      continue;
    }
    const element = documentRef.createElement(node.type);
    element.className = node.type === 'sub' ? 'formula-sub' : 'formula-sup';
    appendMathNodes(element, node.children, documentRef);
    target.append(element);
  }
}

export function appendFormulaContent(target, value) {
  const documentRef = target.ownerDocument;
  for (const piece of parseInlineFormula(value)) {
    if (piece.type === 'text') {
      target.append(documentRef.createTextNode(piece.value));
      continue;
    }
    const formula = documentRef.createElement('span');
    formula.className = 'formula';
    appendMathNodes(formula, piece.nodes, documentRef);
    target.append(formula);
  }
}
