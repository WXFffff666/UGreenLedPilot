(function () {
  'use strict';

  document.getElementById('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value;
    const password = document.getElementById('pw').value;
    if (!username || !password) return;
    fetch('/api/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'UGreenLedPilot',
      },
      body: JSON.stringify({ username, password }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (d.success) location.href = '/';
        else alert(d.message || '密码错误');
      });
  });
})();
