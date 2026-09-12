/* Help extends the existing neutral App shell: a reading panel with searchable
   chapters, a stable left contents list, and a close action returning to work.
   Authenticated server content selects the edition; no client role switch. */
import { request } from './app.js';

export function initializeUserGuide(shell) {
  const trigger = shell.querySelector('[data-user-guide]');
  if (!trigger || trigger.dataset.ready) return;
  trigger.dataset.ready = 'true';
  const dialog = document.createElement('dialog');
  dialog.className = 'user-guide';
  dialog.setAttribute('aria-labelledby', 'user-guide-title');
  dialog.innerHTML = `<header class="user-guide__header"><div><h1 id="user-guide-title">使用说明</h1><p data-guide-edition></p></div><button type="button" data-guide-close aria-label="关闭使用说明">×</button></header><div class="user-guide__layout"><aside class="user-guide__sidebar"><label for="user-guide-search">搜索说明</label><input id="user-guide-search" type="search" placeholder="例如：回测、报告、停止" autocomplete="off"><nav aria-label="使用说明目录" data-guide-nav></nav></aside><main class="user-guide__body" tabindex="0"><p role="status" data-guide-status>正在读取说明…</p><div data-guide-content></div></main></div>`;
  document.body.append(dialog);
  const status = dialog.querySelector('[data-guide-status]');
  const content = dialog.querySelector('[data-guide-content]');
  const nav = dialog.querySelector('[data-guide-nav]');
  const search = dialog.querySelector('input');
  let loaded = false;
  let loading = false;
  search.disabled = true;
  let chapters = [];
  const close = () => dialog.close();
  dialog.querySelector('[data-guide-close]').addEventListener('click', close);
  dialog.addEventListener('close', () => trigger.focus());
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
  search.addEventListener('input', () => {
    if (!loaded) return;
    const query = search.value.trim().toLocaleLowerCase();
    let found = 0;
    for (const { article, link } of chapters) {
      const visible = !query || article.textContent.toLocaleLowerCase().includes(query);
      article.hidden = !visible;
      link.hidden = !visible;
      if (visible) found += 1;
    }
    status.hidden = !query;
    status.textContent = found ? `找到${found}个相关章节。` : '没有找到相关章节，请换一个关键词。';
    dialog.querySelector('.user-guide__body').scrollTop = 0;
  });
  trigger.addEventListener('click', async () => {
    if (dialog.open) return;
    dialog.showModal();
    dialog.querySelector('[data-guide-close]').focus();
    if (loaded || loading) return;
    loading = true;
    status.hidden = false;
    status.textContent = '正在读取说明…';
    try {
      const guide = await request('/api/user-guide');
      dialog.querySelector('[data-guide-edition]').textContent = guide.edition;
      content.replaceChildren();
      nav.replaceChildren();
      chapters = guide.sections.map(section => {
        const article = document.createElement('article');
        article.id = `guide-${section.id}`;
        article.tabIndex = -1;
        const heading = document.createElement('h2');
        heading.textContent = section.title;
        article.append(heading);
        const body = document.createElement('div');
        // Only static App-authored HTML from the authenticated guide endpoint.
        body.innerHTML = section.body;
        body.querySelectorAll('[data-example]').forEach(example => {
          const label = document.createElement('span');
          label.className = 'user-guide__example-label';
          label.textContent = '可以这样提问';
          example.prepend(label);
        });
        article.append(body);
        content.append(article);
        const link = document.createElement('a');
        link.href = `#${article.id}`;
        link.textContent = section.title;
        link.addEventListener('click', event => {
          event.preventDefault();
          article.scrollIntoView({ block: 'start', behavior: 'instant' });
          article.focus({ preventScroll: true });
          nav.querySelectorAll('a').forEach(item => item.removeAttribute('aria-current'));
          link.setAttribute('aria-current', 'location');
        });
        nav.append(link);
        return { article, link };
      });
      loaded = true;
      search.disabled = false;
      status.hidden = true;
      search.dispatchEvent(new Event('input'));
    } catch (error) {
      status.hidden = false;
      status.textContent = `说明暂时无法读取：${error.message}。请关闭后重试。`;
    } finally {
      loading = false;
    }
  });
}
