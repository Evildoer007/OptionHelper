import { createServer } from 'node:http';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  ROOT,
  buildPublishReview,
  createTemplateDraft,
  inspectNativeSvg,
  listProducts,
  loadStoredConfig,
  migrateConfig,
  publish,
  reimportDraft,
  renderPayoffSvg,
  saveDraft,
  validateConfig,
  verifyWorkspace,
} from './core.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, 'public');
const PORT = Number(process.env.PORT || 4179);

const MIME = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function respond(response, status, body, headers = {}) {
  response.writeHead(status, { 'cache-control': 'no-store', ...headers });
  response.end(body);
}

function json(response, status, body) {
  respond(response, status, JSON.stringify(body), { 'content-type': 'application/json; charset=utf-8' });
}

async function readJson(request) {
  let raw = '';
  for await (const chunk of request) {
    raw += chunk;
    if (raw.length > 1_500_000) throw new Error('请求过大。');
  }
  return raw ? JSON.parse(raw) : {};
}

function validationResponse(validation, extra = {}) {
  return { ok: validation.errors.length === 0, validation, ...extra };
}

async function api(request, response, url) {
  if (request.method === 'GET' && url.pathname === '/api/products') {
    const products = await listProducts();
    return json(response, 200, { ok: true, products, optionLibPath: path.join(ROOT, 'references', 'optionlib.md') });
  }
  if (request.method === 'GET' && url.pathname === '/api/config') {
    const id = url.searchParams.get('id');
    const loaded = await loadStoredConfig(id);
    if (loaded.issue) return json(response, 422, { ok: false, ...loaded });
    if (!loaded.config) return json(response, 200, { ok: true, ...loaded, validation: null });
    const validation = await validateConfig(loaded.config);
    return json(response, validation.errors.length ? 422 : 200, validationResponse(validation, loaded));
  }
  if (request.method !== 'POST') return json(response, 405, { ok: false, message: '不支持的请求方式。' });
  const body = await readJson(request);
  if (url.pathname === '/api/drafts/new') {
    const draft = await createTemplateDraft(body.id);
    if (draft.errors?.length) return json(response, 422, { ok: false, ...draft });
    return json(response, 200, validationResponse(draft, { config: draft.config, source: draft.source, svg: renderPayoffSvg(draft.config) }));
  }
  if (url.pathname === '/api/drafts/reimport') {
    const draft = await reimportDraft(body.id);
    if (draft.errors?.length) return json(response, 422, { ok: false, ...draft });
    return json(response, 200, validationResponse(draft, { config: draft.config, source: draft.source, svg: renderPayoffSvg(draft.config) }));
  }
  if (url.pathname === '/api/render') {
    const config = migrateConfig(body.config);
    const validation = await validateConfig(config);
    if (validation.errors.length) return json(response, 422, validationResponse(validation));
    return json(response, 200, validationResponse(validation, { config, svg: renderPayoffSvg(config, { editing: Boolean(body.editing) }) }));
  }
  if (url.pathname === '/api/drafts/save') {
    const saved = await saveDraft(body.config);
    return json(response, saved.errors.length ? 422 : 200, validationResponse(saved, { saved: saved.saved, config: saved.config, svg: saved.errors.length ? null : renderPayoffSvg(saved.config) }));
  }
  if (url.pathname === '/api/publish') {
    const config = migrateConfig(body.config);
    if (!body.confirmed) {
      const review = await buildPublishReview(config);
      return json(response, 200, { ok: true, confirmationRequired: true, config, review, message: '请核对发布前审阅，再由人工确认正式发布。' });
    }
    const result = await publish(config);
    return json(response, result.published ? 200 : 422, {
      ok: result.published,
      validation: { errors: result.errors, warnings: result.warnings },
      published: result.published,
      config: result.config,
      svg: result.published ? renderPayoffSvg(result.config) : null,
    });
  }
  if (url.pathname === '/api/external/check') {
    const result = await inspectNativeSvg(body.svg || '');
    return json(response, 200, { ok: true, ...result });
  }
  return json(response, 404, { ok: false, message: '未找到接口。' });
}

async function serveStatic(response, pathname) {
  const file = pathname === '/' ? path.join(PUBLIC, 'index.html') : path.resolve(PUBLIC, `.${pathname}`);
  if (!file.startsWith(`${PUBLIC}${path.sep}`) && file !== path.join(PUBLIC, 'index.html')) return respond(response, 403, 'Forbidden');
  try {
    const body = await fs.readFile(file);
    respond(response, 200, body, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
  } catch {
    respond(response, 404, 'Not Found');
  }
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://${request.headers.host || '127.0.0.1'}`);
    if (url.pathname.startsWith('/api/')) await api(request, response, url);
    else await serveStatic(response, url.pathname);
  } catch (error) {
    json(response, 500, { ok: false, message: error instanceof Error ? error.message : '服务发生未知错误。' });
  }
});

async function start() {
  try {
    await verifyWorkspace();
    server.listen(PORT, '127.0.0.1', () => {
      console.log(`Payoff SVG Editor已启动：http://127.0.0.1:${PORT}`);
    });
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'Payoff SVG Editor无法启动。');
    process.exitCode = 1;
  }
}

start();
