/* OptionHelper页面统一日期输入边界：输入时自动分隔，按数字位置保持光标，失焦后校验。 */
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

  // Format the visible edit, never silently truncate or discard invalid pasted text.
  function formatDateEdit(raw, cursor, event = {}, previous = null) {
    const unchanged = {value: raw, caret: cursor};
    if (event.isComposing || !/^[0-9/-]*$/.test(raw)) return unchanged;
    let digits = raw.replace(/[^0-9]/g, '');
    if (digits.length > 8) return unchanged;
    let before = raw.slice(0, cursor).replace(/[^0-9]/g, '').length;
    const deleting = String(event.inputType || '').startsWith('delete');
    // A native deletion of just a separator must also remove the adjacent digit.
    // Selection deletions are left to the browser, including selections spanning '/'.
    if (previous && previous.start === previous.end && deleting
        && digits === previous.value.replace(/[^0-9]/g, '')) {
      const backward = event.inputType === 'deleteContentBackward';
      const forward = event.inputType === 'deleteContentForward';
      const at = backward ? before - 1 : before;
      const separator = backward ? previous.value[previous.start - 1] : previous.value[previous.start];
      if ((backward || forward) && /[/-]/.test(separator || '') && at >= 0 && at < digits.length) {
        digits = digits.slice(0, at) + digits.slice(at + 1);
        if (backward) before = at;
      }
    }
    let display = digits.slice(0, 4);
    if (digits.length > 4 || (!deleting && digits.length === 4)) display += '/' + digits.slice(4, 6);
    if (digits.length > 6 || (!deleting && digits.length === 6)) display += '/' + digits.slice(6);
    let caret = 0, count = 0;
    while (caret < display.length && count < before) {
      if (/\d/.test(display[caret])) count++;
      caret++;
    }
    if (!deleting && display[caret] === '/') caret++;
    return {value: display, caret};
  }

  function formatEdit(input, event = {}, previous = null) {
    const raw = String(input.value ?? '');
    const edit = formatDateEdit(raw, input.selectionStart ?? raw.length, event, previous);
    if (edit.value !== raw) {
      input.value = edit.value;
      input.setSelectionRange?.(edit.caret, edit.caret);
    }
  }

  function bind(input, label = '日期') {
    if (input.dataset?.optionhelperDateBound === 'true') return input;
    if (input.dataset) input.dataset.optionhelperDateBound = 'true';
    input.placeholder = 'yyyy/mm/dd';
    input.inputMode = 'numeric';
    if (input.value) set(input, input.value, label);
    attachPicker(input, label);
    const clearError = () => {
      input.setCustomValidity?.('');
      input.removeAttribute?.('aria-invalid');
    };
    let previousEdit = null;
    input.addEventListener('beforeinput', () => {
      previousEdit = {value: input.value, start: input.selectionStart, end: input.selectionEnd};
    });
    input.addEventListener('input', (event = {}) => {
      formatEdit(input, event, previousEdit);
      previousEdit = null;
      if (input.dataset) delete input.dataset.optionhelperDateIso;
      clearError();
    });
    input.addEventListener('compositionend', () => {
      formatEdit(input);
    });
    input.addEventListener('blur', () => {
      try {
        read(input, label);
      } catch (_error) {}
    });
    return input;
  }

  function normalizeDateList(value) {
    return String(value ?? '').replace(/\r\n?/g, '\n').replace(/[\t ,，;；]+/g, '\n');
  }

  function formatListEdit(input, event = {}, previous = null) {
    if (event.isComposing) return;
    const raw = String(input.value ?? '');
    const text = normalizeDateList(raw);
    const cursor = normalizeDateList(raw.slice(0, input.selectionStart ?? raw.length)).length;
    const lineIndex = text.slice(0, cursor).split('\n').length - 1;
    const lineStart = cursor ? text.lastIndexOf('\n', cursor - 1) + 1 : 0;
    let previousLine = null;
    if (previous && previous.start === previous.end) {
      const oldText = normalizeDateList(previous.value);
      const oldCursor = normalizeDateList(previous.value.slice(0, previous.start)).length;
      const oldIndex = oldText.slice(0, oldCursor).split('\n').length - 1;
      if (oldIndex === lineIndex) {
        const oldStart = oldCursor ? oldText.lastIndexOf('\n', oldCursor - 1) + 1 : 0;
        previousLine = {value: oldText.split('\n')[oldIndex], start: oldCursor - oldStart, end: oldCursor - oldStart};
      }
    }
    let caret = cursor;
    const lines = text.split('\n').map((line, index) => {
      if (index !== lineIndex) return parseAndFormat(line)?.display || line;
      const edit = formatDateEdit(line, cursor - lineStart, event, previousLine);
      caret = edit.caret;
      return edit.value;
    });
    const value = lines.join('\n');
    caret += lines.slice(0, lineIndex).reduce((length, line) => length + line.length + 1, 0);
    if (value !== raw) {
      input.value = value;
      input.setSelectionRange?.(caret, caret);
    }
  }

  function readList(input, label = '日期') {
    const result = [];
    try {
      normalizeDateList(input.value).split('\n').forEach((line, index) => {
        if (line.trim()) result.push(toIso(line, `${label}第${index + 1}行`));
      });
      input.setCustomValidity?.('');
      input.removeAttribute?.('aria-invalid');
      return result;
    } catch (error) {
      input.setCustomValidity?.(error.message);
      input.setAttribute?.('aria-invalid', 'true');
      throw error;
    }
  }

  function bindList(input, label = '日期', onValidation = () => {}) {
    if (input.dataset?.optionhelperDateListBound === 'true') return input;
    if (input.dataset) input.dataset.optionhelperDateListBound = 'true';
    // Keep the normal multiline keyboard so Enter remains available on touch devices.
    input.inputMode = 'text';
    let previous = null;
    const clearError = () => {
      input.setCustomValidity?.('');
      input.removeAttribute?.('aria-invalid');
      onValidation('');
    };
    input.addEventListener('beforeinput', () => {
      previous = {value: input.value, start: input.selectionStart, end: input.selectionEnd};
    });
    input.addEventListener('input', (event = {}) => {
      formatListEdit(input, event, previous);
      previous = null;
      clearError();
    });
    input.addEventListener('compositionend', () => { formatListEdit(input); clearError(); });
    input.addEventListener('blur', () => {
      if (input.disabled) return;
      try { readList(input, label); onValidation(''); }
      catch (error) { onValidation(error.message); }
    });
    return input;
  }

  return Object.freeze({bind, bindList, parseAndFormat, read, readList, set, toDisplay, toIso});
});
