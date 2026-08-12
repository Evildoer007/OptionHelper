/* Payoffer合同日期的唯一显示与协议转换边界。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.PayofferDateInput = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  const DATE_PATTERN = /^(\d{4})[\/-](\d{2})[\/-](\d{2})$/;

  function parseDateInput(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';
    const match = DATE_PATTERN.exec(raw);
    if (!match) throw new Error('合同起始日必须为yyyy/mm/dd');
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
      throw new Error('合同起始日不是有效日历日期');
    }
    return `${match[1]}-${match[2]}-${match[3]}`;
  }

  function formatDateInput(value) {
    const isoDate = parseDateInput(value);
    return isoDate ? isoDate.replaceAll('-', '/') : '';
  }

  return { parseDateInput, formatDateInput };
});
