const result = document.querySelector('#activation-result');
const fileInput = document.querySelector('#license-file');
const fileName = document.querySelector('#file-name');
const form = document.querySelector('#activation-form');

fileInput.addEventListener('change', () => { fileName.textContent = fileInput.files?.[0]?.name || '尚未选择文件'; });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const [file] = fileInput.files || [];
  if (!file) { result.textContent = '请选择许可证文件。'; result.className = 'activation-result error'; return; }
  try {
    const license = JSON.parse(await file.text());
    result.textContent = '正在校验许可证。'; result.className = 'activation-result';
    const response = await fetch('/api/activation/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ license }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '许可证导入失败。');
    location.href = './setup.html';
  } catch (error) { result.textContent = error.message || '许可证文件无法读取。'; result.className = 'activation-result error'; }
});
