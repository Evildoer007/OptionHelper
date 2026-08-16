/* Pricer日期输入的唯一显示、解析与ISO协议边界。 */
(function attachDateControl(global) {
  const DATE_PATTERN = /^(\d{4})[/-](\d{2})[/-](\d{2})$/;

  function toIso(value, label) {
    const text = String(value ?? '').trim();
    if (!text) return null;
    const match = DATE_PATTERN.exec(text);
    if (!match) throw new Error(`${label}格式必须为yyyy/mm/dd`);
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const calendar = new Date(Date.UTC(year, month - 1, day));
    if (calendar.getUTCFullYear() !== year || calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) {
      throw new Error(`${label}不是有效日历日期`);
    }
    return `${match[1]}-${match[2]}-${match[3]}`;
  }

  function toDisplay(value, label = '日期') {
    const iso = toIso(value, label);
    return iso === null ? '' : iso.replaceAll('-', '/');
  }

  function set(input, value) {
    input.value = value ? toDisplay(value) : '';
  }

  function latestWeekday(reference = new Date()) {
    const date = new Date(reference.getFullYear(), reference.getMonth(), reference.getDate());
    while (date.getDay() === 0 || date.getDay() === 6) date.setDate(date.getDate() - 1);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  function formatTyping(value) {
    const digits = String(value ?? '').replace(/\D/g, '').slice(0, 8);
    return [digits.slice(0, 4), digits.slice(4, 6), digits.slice(6, 8)].filter(Boolean).join('/');
  }

  function bind(input, label) {
    input.addEventListener('input', () => {
      const formatted = formatTyping(input.value);
      if (formatted !== input.value) input.value = formatted;
    });
    input.addEventListener('blur', () => {
      if (!input.value.trim()) return;
      try {
        input.value = toDisplay(input.value, label);
      } catch (_error) {
        // 保留用户输入，统一在提交边界给出校验提示，避免浏览器控制台异常。
      }
    });
  }

  global.OptionHelperDate = Object.freeze({ toIso, toDisplay, set, bind, latestWeekday, formatTyping });
}(window));
