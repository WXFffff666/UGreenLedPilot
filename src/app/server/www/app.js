(function () {
  'use strict';

  const LABELS = ['关闭', '常亮', '自动'];
  // 效果选项：breath/blink/chase 为扩展效果，后端 Wave 5 对接 /api/control 的 action 校验后生效
  const EFFECT_LABELS = { off: '关闭', on: '常亮', breath: '呼吸', blink: '闪烁', chase: '跑马灯', auto: '自动' };
  let csrf = '';
  let modes = {};
  let settings = {};
  let leds = [];
  let presets = [];
  let eventSource = null;
  // E15: led → 最近一次本地操作时间戳(ms)，SSE 合并 modes 时跳过保护窗内的盘位，避免旧帧回跳
  let lastLocalTouch = {};

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
      if (mode === 'on' || mode === 'breath' || mode === 'chase') el.classList.add(mode);
      else if (mode === 'auto') el.classList.add(activity ? 'blink' : 'auto');
      else if (mode === 'blink') el.classList.add('blink');
    }
    const tgl = document.querySelector('.tgl3[data-led="' + led + '"]');
    if (tgl) {
      // 三档开关仅映射 off/on/auto；呼吸/闪烁/跑马灯归入「常亮」档位
      const idx = mode === 'auto' ? 2 : (mode === 'off' ? 0 : 1);
      tgl.className = 'tgl3 st' + idx;
      const lbl = tgl.querySelector('.tlbl3');
      if (lbl) lbl.textContent = (mode && EFFECT_LABELS[mode]) || LABELS[idx] || '';
    }
    const sel = document.getElementById('effect-' + led);
    if (sel && mode !== undefined && EFFECT_LABELS[mode]) {
      sel.value = mode;
      sel.dataset.prev = mode;
    }
    const pres = document.getElementById(led + '-presence');
    if (pres && present !== undefined) {
      const act = present && mode === 'auto' && activity;
      pres.textContent = act ? '活动' : (present ? '已连接' : '未连接');
      pres.className = 'presence ' + (act ? 'active' : (present ? 'on' : 'off'));
    }
    const bay = document.querySelector('.dbay[data-led="' + led + '"]');
    if (bay) bay.classList.toggle('active', mode !== 'off');
  }

  function applyStatus(data, src) {
    if (data.csrf_token) csrf = data.csrf_token;
    if (data.presets) presets = data.presets;
    if (data.settings) settings = data.settings;
    if (data.leds) leds = data.leds;
    if (data.modes) {
      if (src === 'sse') {
        // E15 保守策略：后端 get_live_status 无 version 字段（version 留给后端添加），
        // 无法跨流排序，故 SSE 帧仅按盘位合并，且跳过用户 1s 内手动操作过的盘位，
        // 避免旧的 SSE 快照覆盖新 status / 本地操作导致 modes 闪跳；窗口外旧帧风险可容忍。
        const now = Date.now();
        for (const led in data.modes) {
          if (now - (lastLocalTouch[led] || 0) < 1000) continue;
          modes[led] = data.modes[led];
        }
      } else {
        // /api/status 完整快照，整体替换
        modes = data.modes;
      }
    }

    const netChip = document.getElementById('net-chip');
    if (netChip) {
      netChip.textContent = '网络: ' + (data.net_iface || '未连接');
      netChip.classList.toggle('on', !!data.net_iface);
    }
    const monChip = document.getElementById('monitor-chip');
    if (monChip) {
      const tier = data.monitor_tier || (data.hotplug_monitor ? 'active' : 'sleep');
      const labels = { sleep: '休眠', hotplug: '事件驱动', activity: '活动监测' };
      monChip.textContent = '监控: ' + (labels[tier] || tier);
      monChip.classList.toggle('on', tier !== 'sleep');
    }

    const ab = document.getElementById('activity-blink');
    if (ab && data.activity_blink !== undefined) ab.checked = !!data.activity_blink;
    const sb = document.getElementById('speed-blink');
    if (sb && data.activity_blink !== undefined) sb.checked = !!data.activity_blink;

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
        lastLocalTouch[led] = Date.now();
        const m = led.match(/disk(\d+)/);
        const name = m ? '磁盘' + m[1] : led;
        toast(name + ' → ' + LABELS[(i + 1) % 3]);
      } else toast(r.message, 'err');
    });
  }

  function allMode(m) {
    api('POST', '/api/all/' + m, {}).then((r) => {
      if (r.success) {
        leds.forEach((l) => {
          updateUI(l, m);
          lastLocalTouch[l] = Date.now();
        });
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
        '<select class="effect" id="effect-' + led + '" data-led="' + led + '" aria-label="' + led + '效果">' +
        '<option value="off">关闭</option><option value="on">常亮</option>' +
        '<option value="breath">呼吸</option><option value="blink">闪烁</option>' +
        '<option value="chase">跑马灯</option><option value="auto">自动</option></select>' +
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
        if (e.target.closest('.tgl3,.cpick,.brit,.preset,.effect')) return;
        cycleMode(el.dataset.led);
      };
    });
    document.querySelectorAll('.effect').forEach((sel) => {
      sel.dataset.prev = sel.value;
      sel.onchange = () => {
        const val = sel.value;
        // 跑马灯为演示模式，需确认开启
        if (val === 'chase' && !confirm('跑马灯为演示模式，将循环闪烁所有磁盘灯，继续？')) {
          sel.value = sel.dataset.prev;
          return;
        }
        // TODO(Wave 5): 后端实现 breath/blink/chase 效果；当前经 /api/control 传递 action
        api('POST', '/api/control', { led: sel.dataset.led, action: val }).then((r) => {
          if (r.success) {
            sel.dataset.prev = val;
            updateUI(sel.dataset.led, val);
            lastLocalTouch[sel.dataset.led] = Date.now();
            toast(sel.dataset.led + ' → ' + (EFFECT_LABELS[val] || val));
          } else {
            sel.value = sel.dataset.prev;
            toast(r.message || '设置失败', 'err');
          }
        });
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

    const ab = document.getElementById('activity-blink');
    if (ab) {
      ab.onchange = () => api('POST', '/api/activity-blink', { enabled: ab.checked }).then((r) => {
        if (r.success) {
          const sb = document.getElementById('speed-blink');
          if (sb) sb.checked = ab.checked;
        }
        toast(r.success ? (ab.checked ? '活动闪烁已开启' : '活动闪烁已关闭，仅事件驱动') : r.message, r.success ? 'ok' : 'err');
      });
    }

    // 速度感知：Todo 22 新增；当前对接 /api/activity-blink，Wave 5 若拆分为独立 IO 速率感知 API 再切换
    const sb = document.getElementById('speed-blink');
    if (sb) {
      sb.onchange = () => api('POST', '/api/activity-blink', { enabled: sb.checked }).then((r) => {
        if (r.success) {
          const ab = document.getElementById('activity-blink');
          if (ab) ab.checked = sb.checked;
        }
        toast(r.success ? (sb.checked ? '速度感知已开启' : '速度感知已关闭') : r.message, r.success ? 'ok' : 'err');
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

    bindCalibrate();
  }

  function bindCalibrate() {
    const calDlg = document.getElementById('cal-dialog');
    const list = document.getElementById('cal-list');
    if (!calDlg || !list) return;
    document.getElementById('btn-calibrate').onclick = () => { buildCalList(list); calDlg.showModal(); };
    const close = () => calDlg.close();
    document.getElementById('cal-cancel').onclick = close;
    document.getElementById('cal-close').onclick = close;
  }

  function buildCalList(list) {
    list.innerHTML = '';
    leds.filter((l) => l.startsWith('disk')).forEach((led) => {
      const n = led.replace(/^disk/, '');
      const row = document.createElement('div');
      row.className = 'cal-row';
      row.innerHTML =
        '<span class="cal-slot">盘位 ' + n + '</span>' +
        '<button type="button" class="abtn s cal-id" data-led="' + led + '">识别</button>' +
        '<input type="text" class="cal-hctl" data-led="' + led + '" placeholder="HCTL 如 0:0:0:0" spellcheck="false">' +
        '<button type="button" class="abtn a cal-bind" data-led="' + led + '">绑定</button>';
      list.appendChild(row);
      row.querySelector('.cal-id').onclick = () => {
        // TODO(Wave 6, Todo 20): 后端实现 /api/calibrate/identify 点亮对应盘位 LED
        api('POST', '/api/calibrate/identify', { led }).then((r) => {
          toast(r.success ? '盘位 ' + n + ' LED 已点亮识别' : (r.message || '识别失败'), r.success ? 'ok' : 'err');
        });
      };
      row.querySelector('.cal-bind').onclick = () => {
        const hctl = row.querySelector('.cal-hctl').value.trim();
        if (!hctl) { toast('请输入 HCTL', 'err'); return; }
        // TODO(Wave 6, Todo 20): 后端实现 /api/calibrate/bind 写入 slot → hctl 映射
        api('POST', '/api/calibrate/bind', { slot: led, hctl }).then((r) => {
          toast(r.success ? '盘位 ' + n + ' 已绑定 ' + hctl : (r.message || '绑定失败'), r.success ? 'ok' : 'err');
        });
      };
    });
  }

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/events');
    eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        // E15: SSE payload 始终含 modes（get_live_status 快照），无需合并占位；
        // 竞态由 applyStatus(src='sse') 内的本地操作保护窗处理
        applyStatus(data, 'sse');
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
