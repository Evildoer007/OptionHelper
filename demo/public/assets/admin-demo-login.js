(() => {
  const account = document.querySelector('#account');
  const password = document.querySelector('#password');
  const form = document.querySelector('#loginForm');
  if (!account || !password || !form) return;

  account.value = 'admin';
  account.readOnly = true;
  account.placeholder = '请输入管理员账号';
  account.closest('.login-field')?.querySelector('label')?.replaceChildren('管理员账号');
  password.value = '8888';

  form.onsubmit = event => {
    event.preventDefault();
    const accountField = document.querySelector('#loginAccountField');
    const passwordField = document.querySelector('#loginPasswordField');
    const status = document.querySelector('#loginStatus');
    const submit = document.querySelector('#loginSubmit');
    const valid = account.value.trim() === 'admin' && password.value === '8888';
    accountField?.classList.toggle('invalid', !valid);
    passwordField?.classList.toggle('invalid', !valid);
    if (!valid) {
      status.textContent = '管理员账号或密码错误。';
      status.classList.add('error');
      password.focus();
      return;
    }
    status.textContent = '正在进入Demo…';
    status.classList.remove('error');
    submit.disabled = true;
    submit.textContent = '进入中';
    setTimeout(() => {
      status.textContent = '';
      submit.disabled = false;
      submit.textContent = '登录';
      document.querySelector('#accountButton .account-mark').textContent = 'A';
      document.querySelector('#accountButton span:last-child').textContent = 'admin';
      document.querySelector('#accountMenu strong').textContent = 'admin';
      taskSelected = false;
      showView('appView');
      setMode('chat');
    }, 420);
  };
})();
