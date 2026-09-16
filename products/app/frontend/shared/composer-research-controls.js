/** Preset and depth are independent controls backed by the existing settings APIs. */
export function createResearchControls({ container, request, enhanceSelects, canChoosePreset, onError }) {
  const document = container.ownerDocument;
  const presetHost = document.createElement('div');
  presetHost.className = 'composer-preset';
  const depthHost = document.createElement('div');
  depthHost.className = 'composer-depth';
  depthHost.hidden = true;
  function createSelect(host, label, layout) {
    const select = document.createElement('select');
    select.dataset.choice = 'true';
    if (layout) select.dataset.choiceLayout = layout;
    select.setAttribute('aria-label', label);
    host.append(select);
    return select;
  }
  const presetSelect = createSelect(presetHost, '推荐预设');
  const depthSelect = createSelect(depthHost, '研究档位', 'research-depth');
  for (const [value, label] of [['quick','快速'],['standard','标准'],['deep','深入']]) {
    const option = document.createElement('option');
    option.value = value; option.textContent = label; depthSelect.append(option);
  }
  depthSelect.hidden = true;
  delete depthSelect.dataset.choice;
  const depths = ['quick', 'standard', 'deep'];
  const labels = ['快速', '标准', '深入'];
  const trigger = document.createElement('button');
  trigger.type = 'button'; trigger.className = 'composer-depth-trigger';
  trigger.setAttribute('aria-expanded', 'false');
  const panel = document.createElement('div');
  panel.className = 'research-depth-slider'; panel.hidden = true;
  panel.setAttribute('role', 'group'); panel.setAttribute('aria-label', '选择研究深度');
  const heading = document.createElement('div'); heading.className = 'research-depth-slider__heading';
  const title = document.createElement('span'); title.textContent = '研究深度';
  const current = document.createElement('strong');
  current.className = 'research-depth-slider__value';
  labels.forEach(label => {
    const item = document.createElement('span'); item.textContent = label;
    item.setAttribute('aria-hidden', 'true'); current.append(item);
  });
  heading.append(title, current);
  const range = document.createElement('input');
  range.type = 'range'; range.min = '0'; range.max = '2'; range.step = 'any';
  range.setAttribute('aria-label', '研究深度');
  const track = document.createElement('div'); track.className = 'research-depth-slider__track';
  const rail = document.createElement('div'); rail.className = 'research-depth-slider__rail';
  const fill = document.createElement('span'); fill.className = 'research-depth-slider__fill';
  const thumb = document.createElement('span'); thumb.className = 'research-depth-slider__thumb';
  rail.append(fill, thumb); track.append(rail, range);
  rail.setAttribute('aria-hidden', 'true');
  const ticks = document.createElement('div'); ticks.className = 'research-depth-slider__labels';
  labels.forEach((label, index) => {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
    button.addEventListener('click', () => {
      if (!canChoosePreset) return;
      range.value = String(index); updateSlider(); commitDepth();
    });
    ticks.append(button);
  });
  panel.append(heading, track, ticks); depthHost.append(trigger); document.body.append(panel);
  let panelAnimation = null;
  const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function closePanel(focus = false) {
    panelAnimation?.cancel();
    trigger.setAttribute('aria-expanded', 'false');
    if (!panel.hidden && !reducedMotion() && panel.animate) {
      panelAnimation = panel.animate([{opacity:1,transform:'translateY(0) scale(1)'},{opacity:0,transform:'translateY(5px) scale(.98)'}], {duration:110,easing:'ease-in',fill:'forwards'});
      const closing = panelAnimation;
      closing.finished.then(() => { if (panelAnimation === closing) { panel.hidden = true; closing.cancel(); panelAnimation = null; } }).catch(() => {});
    } else panel.hidden = true;
    if (focus) trigger.focus();
  }
  function updateSlider() {
    const position = Math.max(0, Math.min(2, Number(range.value)));
    const index = Math.round(position);
    [...current.children].forEach((item, i) => {
      item.dataset.selected = String(i === index);
      item.setAttribute('aria-hidden', String(i !== index));
    });
    range.setAttribute('aria-valuetext', labels[index]);
    panel.style.setProperty('--depth-progress', `${position * 50}%`);
    panel.style.setProperty('--depth-fraction', String(position / 2));
    [...ticks.children].forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
  }
  function positionPanel() {
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(236, window.innerWidth - 24);
    panel.style.width = `${width}px`;
    panel.style.left = `${Math.max(12, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 12))}px`;
    panel.style.top = `${Math.max(12, rect.top - panel.offsetHeight - 10)}px`;
  }
  trigger.addEventListener('click', () => {
    if (trigger.getAttribute('aria-expanded') === 'true') { closePanel(); return; }
    panelAnimation?.cancel();
    range.value = String(depths.indexOf(depthSelect.value)); updateSlider();
    panel.hidden = false; trigger.setAttribute('aria-expanded', 'true'); positionPanel(); range.focus({preventScroll:true});
    if (!reducedMotion() && panel.animate) panelAnimation = panel.animate(
      [{opacity:0,transform:'translateY(7px) scale(.97)'},{opacity:1,transform:'translateY(0) scale(1)'}],
      {duration:180,easing:'cubic-bezier(.2,.8,.2,1)'});
  });
  document.addEventListener('pointerdown', event => {
    if (!panel.hidden && !panel.contains(event.target) && !trigger.contains(event.target)) closePanel();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) { event.preventDefault(); closePanel(true); }
  });
  window.addEventListener('resize', () => { if (!panel.hidden) positionPanel(); });
  document.addEventListener('scroll', event => {
    if (!panel.hidden && !panel.contains(event.target)) closePanel();
  }, true);
  const settleSlider = () => {
    delete panel.dataset.dragging;
    range.value = String(Math.round(Number(range.value)));
    updateSlider();
  };
  // A track click glides to a stop; a deliberate drag follows the pointer directly.
  // Own pointer input so the native range cannot jump before the transition starts.
  let pointer = null;
  function pointerPosition(event) {
    const rect = range.getBoundingClientRect();
    const travel = Math.max(1, rect.width - 24);
    return Math.max(0, Math.min(2, (event.clientX - rect.left - 12) / travel * 2));
  }
  range.addEventListener('pointerdown', event => {
    if (range.disabled || event.button !== 0) return;
    event.preventDefault(); range.focus({preventScroll:true});
    pointer = {id:event.pointerId, x:event.clientX};
    range.setPointerCapture(event.pointerId);
    range.value = String(Math.round(pointerPosition(event))); updateSlider();
  });
  range.addEventListener('pointermove', event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    if (!panel.dataset.dragging && Math.abs(event.clientX - pointer.x) < 3) return;
    panel.dataset.dragging = 'true';
    range.value = String(pointerPosition(event)); updateSlider();
  });
  range.addEventListener('pointerup', event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    pointer = null;
    range.releasePointerCapture(event.pointerId);
    commitDepth();
  });
  function cancelPointer() {
    if (!pointer) return;
    pointer = null; delete panel.dataset.dragging;
    range.value = String(Math.max(0, depths.indexOf(depthSelect.value))); updateSlider();
  }
  range.addEventListener('pointercancel', cancelPointer);
  range.addEventListener('lostpointercapture', cancelPointer);
  range.addEventListener('input', updateSlider);
  range.addEventListener('keydown', event => {
    const direction = {ArrowLeft:-1, ArrowDown:-1, ArrowRight:1, ArrowUp:1}[event.key];
    if (direction === undefined && !['Home','End'].includes(event.key)) return;
    event.preventDefault();
    range.value = String(event.key === 'Home' ? 0 : event.key === 'End' ? 2 : Math.max(0, Math.min(2, Math.round(Number(range.value)) + direction)));
    updateSlider(); commitDepth();
  });
  function commitDepth() {
    if (!canChoosePreset) return;
    settleSlider();
    if (depthSelect.value === depths[Number(range.value)]) return;
    depthSelect.value = depths[Number(range.value)]; save();
  }
  range.addEventListener('change', commitDepth);
  container.append(presetHost, depthHost);
  let pendingSelection = null;
  let pendingSave = null;
  let saveError = null;
  let selectionRevision = 0;
  function sync() {
    presetSelect.disabled = !canChoosePreset;
    depthSelect.disabled = !canChoosePreset || presetSelect.value === 'single';
    depthHost.hidden = presetSelect.value === 'single';
    enhanceSelects(presetHost);
    trigger.disabled = depthSelect.disabled;
    trigger.textContent = depthSelect.selectedOptions[0]?.textContent || '标准';
    trigger.setAttribute('aria-label', `研究深度：${trigger.textContent}`);
    range.disabled = depthSelect.disabled;
    [...ticks.children].forEach(button => { button.disabled = depthSelect.disabled; });
    range.value = String(Math.max(0, depths.indexOf(depthSelect.value))); updateSlider();
    if (depthHost.hidden) closePanel();
    const presetTrigger = presetHost.querySelector('.choice-trigger');
    if (presetTrigger) {
      presetTrigger.title = `推荐预设：${presetSelect.selectedOptions[0]?.textContent || '未配置'}`;
      presetTrigger.setAttribute('aria-label', presetTrigger.title);
    }
    const depthTrigger = depthHost.querySelector('.choice-trigger');
    if (depthTrigger) depthTrigger.setAttribute('aria-label', `研究档位：${depthSelect.selectedOptions[0]?.textContent || '标准'}`);
  }
  async function refresh() {
    const revision = selectionRevision;
    const data = await request('/api/settings/multi-agent-presets');
    if (revision !== selectionRevision) return;
    const single = document.createElement('option');
    single.value = 'single'; single.textContent = '单智能体';
    const multi = document.createElement('optgroup');
    multi.label = '多智能体';
    for (const preset of (data.presets || []).filter(item => item.enabled)) {
      const option = document.createElement('option');
      option.value = preset.preset_id; option.textContent = preset.display_name;
      multi.append(option);
    }
    presetSelect.replaceChildren(single, ...(multi.children.length ? [multi] : []));
    presetSelect.value = data.execution_mode === 'multi' ? data.selected_preset_id : 'single';
    depthSelect.value = ['quick','standard','deep'].includes(data.research_depth) ? data.research_depth : 'standard';
    sync();
  }
  function save() {
    if (!canChoosePreset) return;
    selectionRevision += 1;
    pendingSelection = {preset:presetSelect.value, depth:depthSelect.value};
    saveError = null;
    sync();
    if (!pendingSave) pendingSave = persistSelections().finally(() => { pendingSave = null; });
    return pendingSave;
  }
  async function persistSelections() {
    // Preserve write order, while allowing the user to choose another preset.
    // Intermediate choices are coalesced; the last visible choice wins.
    while (pendingSelection) {
      const {preset, depth} = pendingSelection;
      pendingSelection = null;
      try {
        if (preset !== 'single') await request('/api/settings/multi-agent-preset/default', {method:'POST',body:JSON.stringify({preset_id:preset,research_depth:depth})});
        await request('/api/settings/recommendation-execution', {method:'POST',body:JSON.stringify({execution_mode:preset === 'single' ? 'single' : 'multi'})});
        saveError = null;
      } catch (error) {
        saveError = error;
        onError(error);
        if (!pendingSelection) await refresh().catch(refreshError => { onError(refreshError); });
      }
    }
  }
  async function whenSettled() {
    while (pendingSave) await pendingSave;
    if (saveError) throw saveError;
  }
  presetSelect.addEventListener('change', save);
  depthSelect.addEventListener('change', save);
  return { refresh, whenSettled, hide() { presetHost.hidden = true; depthHost.hidden = true; closePanel(); } };
}
