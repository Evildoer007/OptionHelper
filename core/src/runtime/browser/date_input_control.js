/* OptionHelper页面统一日期输入边界：编辑时保留原文，失焦后统一显示。 */
(function attachDateInputControl(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && typeof root === 'object') root.OptionHelperDateInput = api;
})(typeof globalThis === 'object' ? globalThis : this, function createDateInputControl() {
  const SEPARATED_DATE = /^(\d{4})([/-])(\d{2})\2(\d{2})$/;
  const COMPACT_DATE = /^(\d{4})(\d{2})(\d{2})$/;

  function dateParts(value, label = '日期') {
    const text = String(value ?? '').trim();
    if (!text) return null;
    const separated = SEPARATED_DATE.exec(text);
    const compact = separated ? null : COMPACT_DATE.exec(text);
    if (!separated && !compact) throw new Error(`${label}格式必须为yyyy/mm/dd、yyyy-mm-dd或yyyymmdd`);
    const parts = separated
      ? {year: separated[1], month: separated[3], day: separated[4]}
      : {year: compact[1], month: compact[2], day: compact[3]};
    const year = Number(parts.year);
    const month = Number(parts.month);
    const day = Number(parts.day);
    const calendar = new Date(Date.UTC(year, month - 1, day));
    if (
      year < 1000
      || calendar.getUTCFullYear() !== year
      || calendar.getUTCMonth() !== month - 1
      || calendar.getUTCDate() !== day
    ) {
      throw new Error(`${label}不是有效日历日期`);
    }
    return parts;
  }

  function toIso(value, label = '日期') {
    const parts = dateParts(value, label);
    return parts ? `${parts.year}-${parts.month}-${parts.day}` : null;
  }

  function toDisplay(value, label = '日期') {
    const iso = toIso(value, label);
    return iso ? iso.replaceAll('-', '/') : '';
  }

  function parseAndFormat(value, label = '日期') {
    try {
      const iso = toIso(value, label);
      return iso ? {iso, display: iso.replaceAll('-', '/')} : null;
    } catch (_error) {
      return null;
    }
  }

  function synchronize(input, iso) {
    input.value = iso ? iso.replaceAll('-', '/') : '';
    if (input.dataset) {
      if (iso) input.dataset.optionhelperDateIso = iso;
      else delete input.dataset.optionhelperDateIso;
    }
    if (input._optionhelperDatePicker) input._optionhelperDatePicker.value = iso;
    input.setCustomValidity?.('');
    input.removeAttribute?.('aria-invalid');
    return iso || null;
  }

  function set(input, value, label = '日期') {
    const iso = value ? toIso(value, label) : '';
    return synchronize(input, iso);
  }

  function read(input, label = '日期') {
    const raw = String(input?.value ?? '');
    if (!raw.trim()) return synchronize(input, '');
    try {
      return synchronize(input, toIso(raw, label));
    } catch (error) {
      if (input?.dataset) delete input.dataset.optionhelperDateIso;
      if (input?._optionhelperDatePicker) input._optionhelperDatePicker.value = '';
      const message = error instanceof Error ? error.message : `${label}格式不正确`;
      input?.setCustomValidity?.(message);
      input?.setAttribute?.('aria-invalid', 'true');
      throw error;
    }
  }

  function attachPicker(input, label) {
    const doc = input.ownerDocument || (typeof document === 'object' ? document : null);
    const parent = input.parentNode;
    if (!doc?.createElement || !parent?.insertBefore || input._optionhelperDatePicker) return;
    const wrapper = doc.createElement('span');
    wrapper.className = 'optionhelper-date-control';
    parent.insertBefore(wrapper, input);
    wrapper.appendChild(input);
    input.dataset.optionhelperDateText = 'true';

    const pickerShell = doc.createElement('span');
    pickerShell.className = 'optionhelper-date-picker';
    const picker = doc.createElement('input');
    picker.type = 'date';
    picker.tabIndex = 0;
    picker.setAttribute('aria-label', `选择${label}`);
    picker.title = `选择${label}`;
    picker.value = parseAndFormat(input.value, label)?.iso || '';
    picker.addEventListener('change', () => {
      set(input, picker.value, label);
      if (typeof Event === 'function') {
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
      }
    });
    pickerShell.appendChild(picker);
    wrapper.appendChild(pickerShell);
    input._optionhelperDatePicker = picker;
  }

  function bind(input, label = '日期') {
    if (input.dataset?.optionhelperDateBound === 'true') return input;
    if (input.dataset) input.dataset.optionhelperDateBound = 'true';
    input.placeholder = 'yyyy/mm/dd';
    if (input.value) set(input, input.value, label);
    attachPicker(input, label);
    const clearError = () => {
      input.setCustomValidity?.('');
      input.removeAttribute?.('aria-invalid');
    };
    input.addEventListener('input', () => {
      if (input.dataset) delete input.dataset.optionhelperDateIso;
      clearError();
    });
    input.addEventListener('blur', () => {
      try {
        read(input, label);
      } catch (_error) {}
    });
    return input;
  }

  return Object.freeze({bind, parseAndFormat, read, set, toDisplay, toIso});
});
