/* Keep data, query controls and analysis directly accessible at every width. */
function installThreeColumnLayout(workbench) {
  workbench.dataset.dashboardLayout = 'columns';
  const library = workbench.querySelector('.library-panel');
  const inspector = workbench.querySelector('.inspector-panel');
  library.id = 'dashboard-library';
  inspector.id = 'dashboard-query';
  library.setAttribute('aria-label', '已获取数据');
  inspector.setAttribute('aria-label', '数据请求');
  library.querySelector('h1').textContent = '已获取数据';
  const form = inspector.querySelector('form');
  form.querySelector('h2').textContent = '数据请求';
  form.querySelector('label[for="asset-ids"] > span').textContent = '标的代码';
  form.querySelector('#asset-ids').rows = 1;
  workbench.querySelector('.canvas-state').setAttribute('role', 'status');
}
