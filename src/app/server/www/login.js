(function () {
  'use strict';

  document.getElementById('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const password = document.getElementById('pw').value;
    if (!password) return;
    fetch('/api/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'UGreenLedPilot',
      },
      body: JSON.stringify({ password }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (d.success) location.href = '/';
        else alert(d.message || '密码错误');
      });
  });
})();
