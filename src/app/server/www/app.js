(function () {
  'use strict';

  const LABELS = ['关闭', '常亮', '自动'];
  let csrf = '';
  let modes = {};
  let settings = {};
  let leds = [];
  let presets = [];
  let eventSource = null;

  function api(method, path, body) {
    const opts = {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'UGreenLedPilot',
      },
    };
    if (csrf) opts.headers['X-CSRF-Token'] = csrf;
    if (body) opts.body = JSON.stringify(body);
    return fetch(path, opts)
      .then((r) => {
        if (r.status === 401) {
          location.href = '/login';
          return { success: false };
        }
        return r.json();
      })
      .catch((e) => ({ success: false, message: e.message }));
  }

  function hex(c) {
    return '#' + c.map((x) => ('0' + x.toString(16)).slice(-2)).join('');
  }

  function toast(msg, type) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.className = 'toast show ' + (type || 'ok');
    setTimeout(() => el.classList.remove('show'), 2200);
  }

  function updateUI(led, mode, activity, present) {
    if (mode !== undefined) modes[led] = mode;
    const el = document.getElementById(led + '-led');
    if (el) {
      el.className = 'led';
      if (mode === 'on') el.classList.add('on');
      else if (mode === 'auto') el.classList.add(activity ? 'blink' : 'auto');
    }
    const tgl = document.querySelector('.tgl3[data-led="' + led + '"]');
    if (tgl) {
      const idx = ['off', 'on', 'auto'].indexOf(mode || 'off');
      tgl.className = 'tgl3 st' + Math.max(idx, 0);
      const lbl = tgl.querySelector('.tlbl3');
      if (lbl) lbl.textContent = LABELS[idx] || '';
    }
    const pres = document.getElementById(led + '-presence');
    if (pres && present !== undefined) {
      pres.textContent = present ? '已连接' : '未连接';
      pres.className = 'presence ' + (present ? 'on' : 'off');
    }
    const bay = document.querySelector('.dbay[data-led="' + led + '"]');
    if (bay) bay.classList.toggle('active', mode !== 'off');
  }

  function applyStatus(data) {
    if (data.csrf_token) csrf = data.csrf_token;
    if (data.presets) presets = data.presets;
    if (data.settings) settings = data.settings;
    if (data.leds) leds = data.leds;
    if (data.modes) modes = data.modes;

    const netChip = document.getElementById('net-chip');
    if (netChip) {
      netChip.textContent = '网络: ' + (data.net_iface || '未连接');
      netChip.classList.toggle('on', !!data.net_iface);
    }
    const monChip = document.getElementById('monitor-chip');
    if (monChip) {
      monChip.textContent = '热插拔: ' + (data.hotplug_monitor ? '监听中' : '已跳过');
    }

    if (data.presence) {
      for (const led in data.presence) {
        updateUI(led, modes[led] || 'off', !!(data.activity && data.activity[led]), data.presence[led]);
      }
    }
  }

  function cycleMode(led) {
    const order = ['off', 'on', 'auto'];
    const i = order.indexOf(modes[led] || 'off');
    const next = order[(i + 1) % 3];
    api('POST', '/api/control', { led, action: next }).then((r) => {
      if (r.success) {
        updateUI(led, next);
        const name = led.match(/disk(\d+)/) ? '磁盘' + RegExp.$1 : led;
        toast(name + ' → ' + LABELS[(i + 1) % 3]);
      } else toast(r.message, 'err');
    });
  }

  function allMode(m) {
    api('POST', '/api/all/' + m, {}).then((r) => {
      if (r.success) {
        leds.forEach((l) => updateUI(l, m));
        toast('已全部设为 ' + LABELS[['off', 'on', 'auto'].indexOf(m)]);
      } else toast(r.message, 'err');
    });
  }

  function renderPresets(container, led) {
    container.innerHTML = '';
    presets.forEach((p) => {
      const b = document.createElement('button');
      b.className = 'preset';
      b.title = p.name;
      b.dataset.preset = p.id;
      b.style.background = 'rgb(' + p.color.join(',') + ')';
      b.onclick = (e) => {
        e.stopPropagation();
        api('POST', '/api/preset', { led, preset: p.id }).then((r) => {
          if (r.success) {
            const cp = document.querySelector('.cpick[data-led="' + led + '"]');
            if (cp && r.color) cp.value = hex(r.color);
            toast('颜色已应用');
          }
        });
      };
      container.appendChild(b);
    });
  }

  function buildDiskGrid(count) {
    const grid = document.getElementById('disk-grid');
    grid.innerHTML = '';
    for (let i = 1; i <= count; i++) {
      const led = 'disk' + i;
      const bay = document.createElement('div');
      bay.className = 'dbay';
      bay.dataset.led = led;
      bay.innerHTML =
        '<div class="bframe">' +
        '<div class="bhdr"><span class="bnum">' + String(i).padStart(2, '0') + '</span>' +
        '<span class="presence" id="' + led + '-presence">—</span>' +
        '<div class="led" id="' + led + '-led"></div></div>' +
        '<div class="bbody"><div class="dicon">' +
        '<svg viewBox="0 0 24 24" fill="currentColor" width="28" height="28">' +
        '<path d="M2 12h20v6H2zm0-4h20v2H2zm2-6h16c1.1 0 2 .9 2 2v2H2V4c0-1.1.9-2 2-2z"/></svg></div></div>' +
        '<div class="ctrl-row">' +
        '<input type="color" class="cpick" data-led="' + led + '" value="#ffffff">' +
        '<input type="range" class="brit" data-led="' + led + '" min="0" max="255" value="255">' +
        '</div><div class="presets" data-led="' + led + '"></div>' +
        '<div class="tgl3" data-led="' + led + '"><div class="trk3"><div class="thm3"></div></div>' +
        '<span class="tlbl3"></span></div></div>';
      grid.appendChild(bay);
    }
  }

  function bindControls() {
    document.querySelectorAll('.tgl3').forEach((t) => {
      t.onclick = (e) => { e.stopPropagation(); cycleMode(t.dataset.led); };
    });
    document.querySelectorAll('.dbay, .sys-card.sitem').forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest('.tgl3,.cpick,.brit,.preset')) return;
        cycleMode(el.dataset.led);
      };
    });
    document.querySelectorAll('.cpick').forEach((cp) => {
      const s = settings[cp.dataset.led];
      if (s) cp.value = hex(s.color);
      cp.oninput = () => {
        const v = cp.value;
        api('POST', '/api/color', {
          led: cp.dataset.led,
          r: parseInt(v.slice(1, 3), 16),
          g: parseInt(v.slice(3, 5), 16),
          b: parseInt(v.slice(5, 7), 16),
        });
      };
    });
    document.querySelectorAll('.brit').forEach((sl) => {
      const s = settings[sl.dataset.led];
      if (s) sl.value = s.brightness;
      sl.onchange = () => api('POST', '/api/brightness', { led: sl.dataset.led, brightness: +sl.value });
    });
    document.querySelectorAll('.presets[data-led]').forEach((c) => renderPresets(c, c.dataset.led));

    const gb = document.getElementById('global-brightness');
    if (gb) {
      gb.onchange = () => api('POST', '/api/brightness', { brightness: +gb.value }).then((r) => {
        if (r.success) {
          document.querySelectorAll('.brit').forEach((s) => { s.value = gb.value; });
          toast('全局亮度已更新');
        }
      });
    }

    document.getElementById('btn-all-on').onclick = () => allMode('on');
    document.getElementById('btn-all-auto').onclick = () => allMode('auto');
    document.getElementById('btn-all-off').onclick = () => allMode('off');
    document.getElementById('btn-save').onclick = () => api('POST', '/api/save', {}).then((r) => toast(r.success ? '已保存' : '失败', r.success ? 'ok' : 'err'));
    document.getElementById('btn-remap').onclick = () => api('POST', '/api/remap', {}).then((r) => toast(r.success ? '盘位已重映射' : '失败', r.success ? 'ok' : 'err'));
    document.getElementById('btn-logout').onclick = () => api('POST', '/api/logout', {}).then(() => { location.href = '/login'; });
    document.getElementById('btn-reset').onclick = () => {
      if (confirm('重置配置？')) api('POST', '/api/reset', {}).then((r) => { if (r.success) location.reload(); });
    };

    const dlg = document.getElementById('pw-dialog');
    document.getElementById('btn-cpw').onclick = () => dlg.showModal();
    document.getElementById('pw-cancel').onclick = () => dlg.close();
    document.getElementById('pw-form').onsubmit = (e) => {
      e.preventDefault();
      api('POST', '/api/change-password', {
        old_password: document.getElementById('pw-old').value,
        new_password: document.getElementById('pw-new').value,
      }).then((r) => {
        toast(r.message || '完成', r.success ? 'ok' : 'err');
        if (r.success) { dlg.close(); location.href = '/login'; }
      });
    };
  }

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/events');
    eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        applyStatus({ ...data, modes: data.modes || modes });
      } catch (_) { /* ignore */ }
    };
    eventSource.onerror = () => {
      eventSource.close();
      setTimeout(connectSSE, 3000);
    };
  }

  function init() {
    api('GET', '/api/status').then((r) => {
      if (!r.success) return;
      applyStatus(r);
      const count = (r.leds || []).filter((l) => l.startsWith('disk')).length || 4;
      buildDiskGrid(count);
      bindControls();
      leds.forEach((l) => updateUI(l, modes[l] || 'off'));
      connectSSE();
      if (r.must_change_password) toast('请尽快修改默认密码', 'err');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
