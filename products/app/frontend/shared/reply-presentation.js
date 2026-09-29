/* Display-only normalization. Frozen results, references and model inputs stay exact. */
const terms = new Map([
  ['HTML Card', 'HTML研究简报'], ['Card', '研究简报'],
  ['monthly', '每月首个交易日入场'], ['daily', '每日入场'],
  ['complete_tenor=true', '仅使用完整期限合同'],
  ['complete_tenor=false', '允许非完整期限样本'],
  ['kind=card', '研究简报'], ['format=html', 'HTML格式'],
  ['payoffer_run', '收益结构分析'], ['pricer_run', '估值定价'],
  ['backtester_run', '历史回测'], ['reporter_run', '报告生成'],
  ['recommendation_delivery_run', '研究交付'], ['交付状态机', '报告交付流程'],
]);
const superscripts = {'-':'⁻','0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹'};
function displayNumber(raw) {
  const value = Number(raw.replace('−', '-'));
  if (!Number.isFinite(value)) return raw;
  if (value !== 0 && Math.abs(value) < .01) {
    const [mantissa, exponent] = value.toExponential(2).split('e');
    return `${Number(mantissa).toString().replace('-', '−')}×10${String(Number(exponent)).split('').map(c => superscripts[c] || c).join('')}`;
  }
  return new Intl.NumberFormat('zh-CN', {maximumFractionDigits:2,minimumFractionDigits:2}).format(value).replace('-', '−');
}
function prose(value) {
  for (const [term, label] of terms) {
    value = value.replace(new RegExp(`(?<![\\w])${term}(?![\\w])`, 'g'), label);
  }
  return value
    .replace(/\bchat-report-[a-f0-9]{12,64}\b/g, '历史报告')
    .replace(/(?<![\w./−-])([−-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(%|CNY|count)(?![A-Za-z])/gi, (_, number, unit) => {
      if (unit.toLowerCase() === 'count') return `${Number.isInteger(Number(number)) ? Number(number) : number}个`;
      return displayNumber(number) + (unit.toUpperCase() === 'CNY' ? '元' : '%');
    })
    .replace(/(?<![\w./-])-(?=\d)/g, '−');
}
export function presentAssistantReply(value) {
  // Keep executable examples, formulas, links and URLs byte-for-byte intact.
  return String(value ?? '').split(/(```[\s\S]*?```|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|\[[^\]\n]*\]\([^\s)]*\)|https?:\/\/[^\s<>]+|`[^`\n]+`)/g).map(part => {
    if (part.startsWith('`') && !part.startsWith('```')) {
      const raw = part.slice(1,-1);
      return terms.has(raw) ? terms.get(raw) : /^chat-report-[a-f0-9]{12,64}$/.test(raw) ? '历史报告' : part;
    }
    if (/^(?:```|\$|\\\(|\\\[|\[|https?:\/\/)/.test(part)) return part;
    return prose(part);
  }).join('');
}
