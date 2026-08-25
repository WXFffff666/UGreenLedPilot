(function () {
  'use strict';

  const LABELS = ['关闭', '常亮', '自动'];
  // 效果选项：off/on/auto 走 /api/control 三态；breath/blink 走 /api/effect；
  // chase 为全局演示（/api/chase），仅由 fbar 的 btn-chase 切换，不在 per-LED 下拉中
  const EFFECT_LABELS = { off: '关闭', on: '常亮', breath: '呼吸', blink: '闪烁', chase: '跑马灯', auto: '自动' };
  let csrf = '';
  let modes = {};
  let settings = {};
  let leds = [];
  let presets = [];
  let eventSource = null;
  // R3: SSE 鉴权失败标志——置位后 onerror 不再重连，直接跳转登录页
  let sseAuthFailed = false;
  // M7: 状态单调版本水位——后端 StatusBroadcaster._version 单调递增，
  // 随 /api/status 与 SSE 帧（get_live_status）下发。SSE 帧 version 旧于水位则整体丢弃，
  // 取代原 E15 lastLocalTouch 1s 本地操作时间窗补偿（对缺失 version 的变通）。
  let lastAppliedVersion = 0;
  // K1: 全局跑马灯演示开关状态（后端 /api/status 未下发 chase_enabled，以本地 toggle 状态为准）
  let chaseOn = false;

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
    // M7: version 排序取代时间窗补偿——先做帧级去重，再应用任何状态
    if (data.version !== undefined) {
      if (src === 'sse') {
        // SSE 帧 version 旧于最近已应用的状态 → 整帧丢弃，避免旧快照回跳
        if (data.version < lastAppliedVersion) return;
        lastAppliedVersion = data.version;
      } else if (typeof data.version === 'number') {
        // /api/status 快照：version 为单调计数器时推进水位，使后续旧 SSE 帧被丢弃。
        // 注：http_handler._api_status 会把 version 覆写为 APP_VERSION 字符串
        // （如 '2.2.0'）——非数字不可与 int 水位比较，故忽略；快照本身整体替换仍保证优先。
        lastAppliedVersion = data.version;
      }
    }
    if (data.csrf_token) csrf = data.csrf_token;
    if (data.presets) presets = data.presets;
    if (data.settings) settings = data.settings;
    if (data.leds) leds = data.leds;
    if (data.modes) {
      // M7: 帧级 version 排序已保证 data 不旧于已应用状态，整体替换即可（含 SSE）
      modes = data.modes;
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
    if (sb && data.speed_blink !== undefined) sb.checked = !!data.speed_blink;
    // M7: effects 与 modes 同帧同步——帧级 version 排序已去重，无需时间窗
    if (data.effects) {
      for (const led in data.effects) {
        const sel = document.getElementById('effect-' + led);
        if (sel && EFFECT_LABELS[data.effects[led]]) {
          sel.value = data.effects[led];
          sel.dataset.prev = data.effects[led];
        }
      }
    }

    // K1: 状态含 chase_enabled 时同步全局跑马灯按钮激活态
    if (data.chase_enabled !== undefined) {
      chaseOn = !!data.chase_enabled;
      setChaseBtn(document.getElementById('btn-chase'), chaseOn);
    }

    // T3/T5: night schedule + alert state from status/SSE
    if (data.night_schedule !== undefined) {
      const ne = document.getElementById('night-enabled');
      const ns = document.getElementById('night-start');
      const nend = document.getElementById('night-end');
      const nb = document.getElementById('night-brightness');
      if (ne) ne.checked = !!data.night_schedule.enabled;
      if (ns && data.night_schedule.start_hour !== undefined) ns.value = data.night_schedule.start_hour;
      if (nend && data.night_schedule.end_hour !== undefined) nend.value = data.night_schedule.end_hour;
      if (nb && data.night_schedule.night_brightness !== undefined) {
        nb.value = data.night_schedule.night_brightness;
        const val = document.getElementById('night-brightness-val');
        if (val) val.textContent = data.night_schedule.night_brightness;
      }
    }
    if (data.alerts !== undefined) renderAlertBanner(data.alerts);

    if (data.presence) {
      for (const led in data.presence) {
        updateUI(led, modes[led] || 'off', !!(data.activity && data.activity[led]), data.presence[led]);
      }
    }
  }

  function renderAlertBanner(alerts) {
    const banner = document.getElementById('alert-banner');
    const text = document.getElementById('alert-text');
    if (!banner || !text) return;
    const parts = [];
    const lost = alerts.disk_lost || {};
    const diskBays = Object.keys(lost).filter((k) => lost[k]);
    if (diskBays.length) parts.push('磁盘盘位故障: ' + diskBays.map((k) => k.replace('disk', '#')).join(', '));
    if (alerts.network_down) parts.push('网络已断开');
    if (parts.length) {
      text.textContent = parts.join(' · ');
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function cycleMode(led) {
    const order = ['off', 'on', 'auto'];
    const i = order.indexOf(modes[led] || 'off');
    const next = order[(i + 1) % 3];
    api('POST', '/api/control', { led, action: next }).then((r) => {
      if (r.success) {
        updateUI(led, next);
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
        });
        toast('已全部设为 ' + LABELS[['off', 'on', 'auto'].indexOf(m)]);
      } else toast(r.message, 'err');
    });
  }

  // K1: 全局跑马灯按钮激活态——开启时用主色（abtn.p）并标注 aria-pressed
  function setChaseBtn(btn, on) {
    if (!btn) return;
    btn.className = 'abtn ' + (on ? 'p' : 'a');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = on ? '跑马灯演示已开启，点击关闭' : '跑马灯演示：全局循环闪烁所有磁盘灯';
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
        '<option value="auto">自动</option></select>' +
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
        const led = sel.dataset.led;
        let req;
        if (val === 'breath' || val === 'blink') {
          // 闪烁为手动闪烁效果，经 /api/effect 映射为 manual-blink
          req = api('POST', '/api/effect', { led, effect: val === 'blink' ? 'manual-blink' : 'breath' });
        } else {
          // off/on/auto 保持三态，经 /api/control
          req = api('POST', '/api/control', { led, action: val });
        }
        req.then((r) => {
          if (r.success) {
            sel.dataset.prev = val;
            updateUI(led, val);
            toast(led + ' → ' + (EFFECT_LABELS[val] || val));
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

    const sb = document.getElementById('speed-blink');
    if (sb) {
      sb.onchange = () => api('POST', '/api/speed-blink', { enabled: sb.checked }).then((r) => {
        toast(r.success ? (sb.checked ? '速度感知已开启' : '速度感知已关闭') : r.message, r.success ? 'ok' : 'err');
      });
    }

    // T3: night schedule settings
    const nightEnabled = document.getElementById('night-enabled');
    const nightStart = document.getElementById('night-start');
    const nightEnd = document.getElementById('night-end');
    const nightBright = document.getElementById('night-brightness');
    function postNightSchedule() {
      if (!nightEnabled || !nightStart || !nightEnd || !nightBright) return;
      api('POST', '/api/night-schedule', {
        enabled: nightEnabled.checked,
        start_hour: parseInt(nightStart.value, 10) || 22,
        end_hour: parseInt(nightEnd.value, 10) || 7,
        night_brightness: parseInt(nightBright.value, 10) || 13,
      }).then((r) => {
        toast(r.success ? '定时策略已保存' : r.message, r.success ? 'ok' : 'err');
      });
    }
    if (nightEnabled) nightEnabled.onchange = postNightSchedule;
    if (nightStart) nightStart.onchange = postNightSchedule;
    if (nightEnd) nightEnd.onchange = postNightSchedule;
    if (nightBright) {
      nightBright.oninput = () => {
        const val = document.getElementById('night-brightness-val');
        if (val) val.textContent = nightBright.value;
      };
      nightBright.onchange = postNightSchedule;
    }

    // T5: alerts toggle
    const alertsToggle = document.getElementById('alerts-enabled');
    if (alertsToggle) {
      alertsToggle.onchange = () => api('POST', '/api/alerts', {
        enabled: alertsToggle.checked, disk_failure: true, network_down: true,
      }).then((r) => {
        toast(r.success ? (alertsToggle.checked ? '故障告警已开启' : '故障告警已关闭') : r.message, r.success ? 'ok' : 'err');
      });
    }
    const alertDismiss = document.getElementById('alert-dismiss');
    if (alertDismiss) {
      alertDismiss.onclick = () => { const b = document.getElementById('alert-banner'); if (b) b.hidden = true; };
    }

    document.getElementById('btn-all-on').onclick = () => allMode('on');
    document.getElementById('btn-all-auto').onclick = () => allMode('auto');
    document.getElementById('btn-all-off').onclick = () => allMode('off');
    // K1: 全局跑马灯演示开关（POST /api/chase 切换），与 per-LED 效果下拉分离
    const btnChase = document.getElementById('btn-chase');
    if (btnChase) {
      setChaseBtn(btnChase, chaseOn);
      btnChase.onclick = () => {
        const enabled = !chaseOn;
        api('POST', '/api/chase', { enabled }).then((r) => {
          if (r.success) {
            chaseOn = enabled;
            setChaseBtn(btnChase, enabled);
            toast(enabled ? '跑马灯演示已开启' : '跑马灯已关闭');
          } else toast(r.message || '切换失败', 'err');
        });
      };
    }
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
        // 调用 /api/calibrate/identify 点亮盘位 LED
        api('POST', '/api/calibrate/identify', { led }).then((r) => {
          toast(r.success ? '盘位 ' + n + ' LED 已点亮识别' : (r.message || '识别失败'), r.success ? 'ok' : 'err');
        });
      };
      row.querySelector('.cal-bind').onclick = () => {
        const hctl = row.querySelector('.cal-hctl').value.trim();
        if (!hctl) { toast('请输入 HCTL', 'err'); return; }
        // 调用 /api/calibrate/bind 保存绑定
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
        // R3: SSE 流中出现鉴权失败响应 → 置位标志并跳转登录，不再重连
        if (data && data.success === false &&
            (data.status === 401 || /请先登录/.test(data.message || ''))) {
          sseAuthFailed = true;
          eventSource.close();
          location.href = '/login';
          return;
        }
        // M7: SSE payload 始终含 modes + version（get_live_status 快照）；
        // 旧帧回跳竞态由 applyStatus(src='sse') 内的 version 水位处理
        applyStatus(data, 'sse');
      } catch (_) { /* ignore */ }
    };
    eventSource.onerror = () => {
      eventSource.close();
      // R3: 已确认鉴权失败则直接跳转，停止重连
      if (sseAuthFailed) { location.href = '/login'; return; }
      // R3: 重连前探测会话有效性——若返回 401（登录过期/未授权）则终止重连循环并跳转登录；
      // 网络抖动等临时错误仍按原 3s 重连
      fetch('/api/status', { headers: { 'X-Requested-With': 'UGreenLedPilot' } })
        .then((r) => {
          if (r.status === 401) {
            sseAuthFailed = true;
            location.href = '/login';
            return;
          }
          setTimeout(connectSSE, 3000);
        })
        .catch(() => setTimeout(connectSSE, 3000));
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
      if (r.must_change_password) forceChangePassword();
    });
  }

  // First-login / default-credential flow: the change-password dialog becomes
  // a blocking modal — no cancel, no backdrop/Escape close — until the admin
  // sets a real password. Guards against leaving the default admin123 in place.
  function forceChangePassword() {
    const dlg = document.getElementById('pw-dialog');
    const cancel = document.getElementById('pw-cancel');
    if (!dlg) return;
    dlg.dataset.force = 'true';
    dlg.classList.add('forced');
    if (cancel) cancel.style.display = 'none';
    document.getElementById('pw-desc').textContent = '首次登录，请先修改默认密码后才能使用';
    document.getElementById('pw-title').textContent = '设置新密码';
    dlg.showModal();
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); }, { once: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
