/* Backtester唯一日期控件逻辑：页面显示斜杠格式，协议始终使用ISO日期。 */
(() => {
  function normalize(value, label='日期') {
    const text=String(value ?? '').trim();
    if(!text) return null;
    const match=/^(\d{4})[/-](\d{2})[/-](\d{2})$/.exec(text);
    if(!match) throw new Error(`${label}必须为yyyy/mm/dd`);
    const [,yearText,monthText,dayText]=match;
    const year=Number(yearText),month=Number(monthText),day=Number(dayText);
    const date=new Date(Date.UTC(year,month-1,day));
    if(year<1000||date.getUTCFullYear()!==year||date.getUTCMonth()!==month-1||date.getUTCDate()!==day) throw new Error(`${label}不是有效日历日期`);
    return `${yearText}-${monthText}-${dayText}`;
  }

  function display(value, label='日期') {
    const iso=normalize(value,label);
    return iso ? iso.replaceAll('-','/') : '';
  }

  function bind(input, label) {
    input.placeholder='yyyy/mm/dd';
    if(input.value) input.value=display(input.value,label);
    input.addEventListener('blur',()=>{
      if(!input.value.trim()) return;
      try { input.value=display(input.value,label); input.removeAttribute('aria-invalid'); }
      catch(error) { input.setAttribute('aria-invalid','true'); }
    });
  }

  window.BacktesterDate=Object.freeze({normalize,display,bind});
})();
