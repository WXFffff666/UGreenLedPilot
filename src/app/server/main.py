#!/usr/bin/env python3
"""UGreenLedPilot v1.1 — DXP4800 Plus LED controller for fnOS."""

import json
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

from auth_manager import AuthManager
from pilot_core import (
    VALID_MODES, LED_BASE, MAX_DISK_LEDS, COLOR_PRESETS, DXP4800_PLUS_PROFILE,
    detect_model, probe_leds, disk_count_from_leds,
    make_cli_runner, load_json, save_json,
    PilotController,
)

APP_VERSION = '1.1.0'
MAX_BODY_BYTES = 65536
LED_NAME_RE = re.compile(r'^(power|netdev|disk[1-4])$')

_BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ugreen_leds_cli')
CLI = _BUNDLED if os.path.exists(_BUNDLED) else '/usr/local/bin/ugreen_leds_cli'
run = make_cli_runner(CLI)

PORT = int(os.environ.get('TRIM_SERVICE_PORT', 19580))
VAR = os.environ.get('TRIM_PKGVAR', '/tmp')
STATE_FILE = os.path.join(VAR, 'led_state.json')
SETTINGS_FILE = os.path.join(VAR, 'led_settings.json')
CONFIG_FILE = os.path.join(VAR, 'device_config.json')
AUTH_FILE = os.path.join(VAR, 'auth.json')

print(f'UGreenLedPilot v{APP_VERSION}  port={PORT}  var={VAR}  target=DXP4800 Plus')

auth = AuthManager(AUTH_FILE)
model_info = detect_model()
led_statuses, probe_error = probe_leds(run)
detected = disk_count_from_leds(led_statuses)
hardware_modes = {led: d['mode'] for led, d in led_statuses.items()}

initialized = os.path.exists(CONFIG_FILE)
default_disk_count = DXP4800_PLUS_PROFILE['disk_count']
cfg = load_json(CONFIG_FILE, {
    'disk_count': default_disk_count,
    'model': DXP4800_PLUS_PROFILE['id'],
    'model_name': DXP4800_PLUS_PROFILE['name'],
    'product_name': model_info.get('product_name', ''),
    'auto_detected': True,
    'ata_map': DXP4800_PLUS_PROFILE['ata_map'],
    'hctl_map': DXP4800_PLUS_PROFILE['hctl_map'],
})

if not initialized:
    save_json(CONFIG_FILE, cfg)
    initialized = True

disk_count = cfg['disk_count']
led_names = LED_BASE + [f'disk{i}' for i in range(1, disk_count + 1)]

ctrl = PilotController(
    led_names, run, STATE_FILE, SETTINGS_FILE,
    ata_map=cfg.get('ata_map', DXP4800_PLUS_PROFILE['ata_map']),
    hctl_map=cfg.get('hctl_map', DXP4800_PLUS_PROFILE['hctl_map']),
    disk_count=disk_count,
)
ctrl.restore_state(hardware_modes=hardware_modes, apply_hardware=True)
ctrl.start_monitor()


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except IOError as e:
        return str(e)
    return None


def validate_led(led):
    return bool(led and LED_NAME_RE.match(led))


def validate_mode(mode):
    return mode in VALID_MODES


def client_ip(headers):
    forwarded = headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return headers.get('X-Real-IP') or ''


def bay_html(i):
    presets = ''.join(
        f'<button class="preset" data-led="disk{i}" data-preset="{p["id"]}" title="{p["name"]}" '
        f'style="background:rgb({p["color"][0]},{p["color"][1]},{p["color"][2]})"></button>'
        for p in COLOR_PRESETS
    )
    return f'''<div class="dbay" data-led="disk{i}">
  <div class="bframe">
    <div class="bhdr"><span class="bnum">{i:02d}</span><span class="presence" id="disk{i}-presence">—</span><div class="led sm" id="disk{i}-led"></div></div>
    <div class="bbody"><div class="dicon"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M2 12h20v6H2zm0-4h20v2H2zm2-6h16c1.1 0 2 .9 2 2v2H2V4c0-1.1.9-2 2-2z"/></svg></div></div>
    <div class="ctrl-row"><input type="color" class="cpick" data-led="disk{i}" value="#ffffff"><input type="range" class="brit" data-led="disk{i}" min="0" max="255" value="255"></div>
    <div class="presets">{presets}</div>
    <div class="tgl3 sm3" data-led="disk{i}"><div class="trk3"><div class="thm3"></div></div><span class="tlbl3"></span></div>
  </div></div>'''

CSS = r'''
:root{--bg:#0b0f14;--card:#151d2b;--edge:rgba(255,255,255,.08);--accent:#00e5a0;--accent2:#00b4ff;--warn:#ffb020;--danger:#ff5c6c;--t1:#eef3fb;--t2:#9aa8bc;--t3:#6b7a90}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:radial-gradient(900px 500px at 0 0,rgba(0,180,255,.1),transparent),var(--bg);color:var(--t1);padding:20px;min-height:100vh}
.shell{max-width:780px;margin:0 auto}
.topbar{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.logo{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center}
.title h1{font-size:20px}.title p{font-size:12px;color:var(--t2)}
.chip{margin-left:auto;font-size:11px;padding:5px 10px;border-radius:999px;border:1px solid var(--edge);color:var(--t2)}
.chip.on{border-color:rgba(0,229,160,.3);color:var(--accent)}
.panel{background:var(--card);border:1px solid var(--edge);border-radius:16px;overflow:hidden}
.panel-hd{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid var(--edge)}
.panel-hd h2{font-size:13px;color:var(--t2);letter-spacing:1.2px;text-transform:uppercase}
.global-bright{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2)}
.global-bright input{width:110px;accent-color:var(--accent)}
.legend{display:flex;gap:12px;padding:0 18px 12px;flex-wrap:wrap}
.leg{font-size:11px;color:var(--t3);display:flex;align-items:center;gap:5px}
.dot{width:8px;height:8px;border-radius:50%}
.dot.off{background:#3a4555}.dot.on{background:var(--accent)}.dot.auto{background:var(--accent2)}
.sys-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px 18px}
.sys-card{background:#101722;border:1px solid var(--edge);border-radius:12px;padding:12px}
.sys-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.sys-name{font-size:13px;color:var(--t2);font-weight:600}
.presence{font-size:10px;color:var(--t3)}.presence.on{color:var(--accent)}.presence.off{color:var(--danger)}
.led{width:16px;height:16px;border-radius:50%;background:#2a3444;border:1px solid #111}
.led.on{background:radial-gradient(circle at 30% 30%,var(--accent),#007a55);box-shadow:0 0 10px rgba(0,229,160,.5)}
.led.auto{background:radial-gradient(circle at 30% 30%,var(--accent2),#005f99);animation:pulse 1.2s infinite}
.led.blink{background:radial-gradient(circle at 30% 30%,var(--warn),#aa6600);animation:blink .35s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}
.tgl3{display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;margin-top:6px}
.trk3{width:54px;height:26px;background:#2a3444;border-radius:13px;position:relative}
.thm3{width:20px;height:20px;background:#556070;border-radius:50%;position:absolute;top:3px;left:3px;transition:.2s}
.tgl3.st1 .thm3{transform:translateX(13px);background:var(--accent)}.tgl3.st2 .thm3{transform:translateX(27px);background:var(--accent2)}
.tlbl3{font-size:9px;color:var(--t3);text-transform:uppercase}
.ctrl-row{display:flex;gap:8px;align-items:center;margin:6px 0}
.cpick{width:28px;height:28px;border:none;background:transparent;cursor:pointer}
.brit{flex:1;accent-color:var(--accent)}
.presets{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px}
.preset{width:16px;height:16px;border-radius:4px;border:1px solid rgba(255,255,255,.15);cursor:pointer;padding:0}
.dsection{padding:8px 18px 16px}
.dtitle{font-size:11px;color:var(--t3);letter-spacing:1px;margin-bottom:10px}
.dgrid{display:grid;gap:8px}
.dbay{cursor:pointer}.bframe{background:#101722;border:1px solid var(--edge);border-radius:10px;padding:10px}
.bhdr{display:flex;align-items:center;gap:6px}.bnum{font-size:11px;color:var(--t3);font-family:monospace}
.bbody{display:grid;place-items:center;padding:4px 0}.dicon{width:26px;height:26px;color:var(--t3);opacity:.5}
.dbay.active .dicon{color:var(--accent);opacity:.9}
.fbar{display:flex;gap:8px;padding:14px 18px;border-top:1px solid var(--edge);flex-wrap:wrap}
.abtn{flex:1;min-width:110px;padding:11px;border:none;border-radius:8px;color:#fff;font-weight:600;font-size:12px;cursor:pointer}
.abtn.p{background:linear-gradient(135deg,#00c98a,#009f6b)}.abtn.a{background:linear-gradient(135deg,#0096d6,#006ea0)}
.abtn.s{background:#3a4555}.abtn.d{background:linear-gradient(135deg,#c94a5a,#8f2f3c)}
.hint{font-size:11px;color:var(--t3);padding:0 18px 12px;line-height:1.5}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(60px);background:#1f2937;padding:10px 16px;border-radius:8px;opacity:0;transition:.25s;z-index:99;font-size:13px}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}.toast.ok{border-left:3px solid var(--accent)}.toast.err{border-left:3px solid var(--danger)}
.login-wrap{min-height:100vh;display:grid;place-items:center}
.login-box{background:var(--card);padding:32px;border-radius:16px;border:1px solid var(--edge);width:320px;text-align:center}
.login-box h2{margin-bottom:6px}.login-box p{font-size:12px;color:var(--t2);margin-bottom:16px}
.login-box input{width:100%;padding:12px;border-radius:8px;border:1px solid var(--edge);background:#0b0f14;color:#fff;margin-bottom:10px}
.login-box button{width:100%;padding:12px;border:none;border-radius:8px;background:var(--accent);color:#041018;font-weight:700;cursor:pointer}
@media(max-width:560px){.sys-grid{grid-template-columns:1fr 1fr}.dgrid{grid-template-columns:repeat(2,1fr)!important}}
'''

JS = r'''
function logout(){api('POST','/api/logout',{}).then(()=>location.href='/login')}
(function(){
var LEDS=__LEDS_JSON__,modes=__INIT_MODES__,settings=__INIT_SETTINGS__,LABELS=['关闭','常亮','自动'],csrf='';
function api(m,p,b){var o={method:m,headers:{'Content-Type':'application/json','X-Requested-With':'UGreenLedPilot'}};if(csrf)o.headers['X-CSRF-Token']=csrf;if(b)o.body=JSON.stringify(b);return fetch(p,o).then(r=>{if(r.status===401){location.href='/login';return{success:false}}return r.json()}).catch(e=>({success:false,message:e.message}))}
function hex(c){return'#'+[c[0],c[1],c[2]].map(x=>('0'+x.toString(16)).slice(-2)).join('')}
function toast(m,t){var e=document.getElementById('toast');e.textContent=m;e.className='toast show '+(t||'ok');setTimeout(()=>e.classList.remove('show'),2200)}
function updateUI(led,mode,activity,present){
  modes[led]=mode;var el=document.getElementById(led+'-led');
  if(el){el.className='led'+(led.startsWith('disk')?' sm':'');if(mode==='on')el.classList.add('on');else if(mode==='auto')el.classList.add(activity?'blink':'auto')}
  var t=document.querySelector('.tgl3[data-led="'+led+'"]');
  if(t){var i=['off','on','auto'].indexOf(mode);t.className='tgl3'+(led.startsWith('disk')?' sm3':'')+' st'+Math.max(i,0);if(t.querySelector('.tlbl3'))t.querySelector('.tlbl3').textContent=LABELS[i]||''}
  var p=document.getElementById(led+'-presence');if(p&&present!==undefined){p.textContent=present?'已连接':'未连接';p.className='presence '+(present?'on':'off')}
  document.querySelector('.dbay[data-led="'+led+'"]')?.classList.toggle('active',mode!=='off')
}
function cycleMode(led){var o=['off','on','auto'],i=o.indexOf(modes[led]||'off'),n=o[(i+1)%3];
  api('POST','/api/control',{led:led,action:n}).then(r=>{if(r.success){updateUI(led,n);toast((led.match(/disk(\d+)/)?'磁盘'+RegExp.$1:led)+' → '+LABELS[(i+1)%3])}else toast(r.message,'err')})}
function allMode(m){api('POST','/api/all/'+m,{}).then(r=>{if(r.success){LEDS.forEach(l=>updateUI(l,m));toast('已全部设为 '+LABELS[['off','on','auto'].indexOf(m)])}else toast(r.message,'err')})}
function poll(){api('GET','/api/status').then(r=>{if(!r.success)return;if(r.csrf_token)csrf=r.csrf_token;
  var chip=document.getElementById('net-chip');if(chip)chip.textContent='网络: '+(r.net_iface||'未连接');chip?.classList.toggle('on',!!r.net_iface);
  if(r.presence)for(var l in r.presence)updateUI(l,modes[l]||'off',!!(r.activity&&r.activity[l]),r.presence[l])})}
function bind(){
  document.querySelectorAll('.tgl3').forEach(t=>t.onclick=e=>{e.stopPropagation();cycleMode(t.dataset.led)});
  document.querySelectorAll('.dbay,.sys-card.sitem').forEach(el=>el.onclick=e=>{if(e.target.closest('.tgl3,.cpick,.brit,.preset'))return;cycleMode(el.dataset.led)});
  document.querySelectorAll('.cpick').forEach(cp=>{var s=settings[cp.dataset.led];if(s)cp.value=hex(s.color);
    cp.oninput=()=>{var v=cp.value;api('POST','/api/color',{led:cp.dataset.led,r:parseInt(v.slice(1,3),16),g:parseInt(v.slice(3,5),16),b:parseInt(v.slice(5,7),16)})}});
  document.querySelectorAll('.brit').forEach(sl=>{var s=settings[sl.dataset.led];if(s)sl.value=s.brightness;sl.onchange=()=>api('POST','/api/brightness',{led:sl.dataset.led,brightness:+sl.value})});
  document.querySelectorAll('.preset').forEach(b=>b.onclick=e=>{e.stopPropagation();api('POST','/api/preset',{led:b.dataset.led,preset:b.dataset.preset}).then(r=>{if(r.success){var c=r.color;document.querySelector('.cpick[data-led="'+b.dataset.led+'"]').value=hex(c);toast('颜色已应用')}})});
  var gb=document.getElementById('global-brightness');if(gb)gb.onchange=()=>api('POST','/api/brightness',{brightness:+gb.value}).then(r=>{if(r.success){document.querySelectorAll('.brit').forEach(s=>s.value=gb.value);toast('全局亮度已更新')}});
  document.getElementById('btn-all-on').onclick=()=>allMode('on');
  document.getElementById('btn-all-auto').onclick=()=>allMode('auto');
  document.getElementById('btn-all-off').onclick=()=>allMode('off');
  document.getElementById('btn-save').onclick=()=>api('POST','/api/save',{}).then(r=>toast(r.success?'已保存':'失败',r.success?'ok':'err'));
  document.getElementById('btn-remap').onclick=()=>api('POST','/api/remap',{}).then(r=>toast(r.success?'盘位已重映射':'失败',r.success?'ok':'err'));
  document.getElementById('btn-logout').onclick=logout;
  document.getElementById('btn-reset').onclick=()=>{if(confirm('重置配置？'))api('POST','/api/reset',{}).then(r=>{if(r.success)location.href='/'})};
  LEDS.forEach(l=>updateUI(l,modes[l]||'off'));setInterval(poll,800);poll();
}
document.readyState==='loading'?document.addEventListener('DOMContentLoaded',bind):bind();
})()
'''

LOGIN_HTML = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>登录 - UGreenLedPilot</title><style>{css}</style></head>
<body><div class="login-wrap"><div class="login-box"><h2>UGreenLedPilot</h2><p>DXP4800 Plus · 管理员登录</p>
<input id="pw" type="password" placeholder="管理员密码" onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">登录</button><p style="font-size:11px;color:#6b7a90;margin-top:12px">默认密码 admin123，首次登录后请修改</p>
</div></div><script>function doLogin(){var p=document.getElementById('pw').value;if(!p)return;fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'UGreenLedPilot'},body:JSON.stringify({password:p})}).then(r=>r.json()).then(d=>{if(d.success)location.href='/';else alert(d.message||'密码错误')})}</script></body></html>'''

HTML = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>UGreenLedPilot</title><style>{css}</style></head><body>
<div class="shell">
<div class="topbar"><div class="logo"><svg width="20" height="20" viewBox="0 0 24 24" fill="#041018"><circle cx="12" cy="12" r="8"/></svg></div>
<div class="title"><h1>UGreenLedPilot</h1><p>DXP4800 Plus · {disk_count} 盘位</p></div>
<div class="chip" id="net-chip">网络: —</div></div>
<div class="panel">
<div class="panel-hd"><h2>指示灯控制</h2><div class="global-bright"><span>全局亮度</span><input type="range" id="global-brightness" min="0" max="255" value="255"></div></div>
<div class="legend"><div class="leg"><span class="dot off"></span>关闭</div><div class="leg"><span class="dot on"></span>常亮</div><div class="leg"><span class="dot auto"></span>自动</div></div>
<p class="hint">常亮/关闭模式不检测热插拔；仅自动模式在硬件变化时重映射。常亮时插拔盘不影响灯状态。</p>
<div class="sys-grid">
<div class="sys-card sitem" data-led="power"><div class="sys-top"><span class="sys-name">电源</span><div class="led" id="power-led"></div></div>
<div class="presence on" id="power-presence">系统运行</div>
<div class="ctrl-row"><input type="color" class="cpick" data-led="power" value="#ffffff"><input type="range" class="brit" data-led="power" min="0" max="255" value="255"></div>
<div class="presets">{power_presets}</div><div class="tgl3" data-led="power"><div class="trk3"><div class="thm3"></div></div><span class="tlbl3"></span></div></div>
<div class="sys-card sitem" data-led="netdev"><div class="sys-top"><span class="sys-name">网络</span><div class="led" id="netdev-led"></div></div>
<div class="presence" id="netdev-presence">检测中</div>
<div class="ctrl-row"><input type="color" class="cpick" data-led="netdev" value="#ffa500"><input type="range" class="brit" data-led="netdev" min="0" max="255" value="255"></div>
<div class="presets">{netdev_presets}</div><div class="tgl3" data-led="netdev"><div class="trk3"><div class="thm3"></div></div><span class="tlbl3"></span></div></div>
</div>
<div class="dsection"><div class="dtitle">磁盘 · 自动模式：插盘亮 / 拔盘灭 / 活动闪</div><div class="dgrid" style="grid-template-columns:repeat({disk_count},1fr)">{disk_bays}</div></div>
<div class="fbar"><button class="abtn p" id="btn-all-on">全部常亮</button><button class="abtn a" id="btn-all-auto">全部自动</button><button class="abtn s" id="btn-all-off">全部关闭</button></div>
<div class="fbar"><button class="abtn s" id="btn-cpw" onclick="document.getElementById('pwform').style.display='grid'">改密</button>
<button class="abtn a" id="btn-remap">重映射盘位</button><button class="abtn a" id="btn-save">保存</button>
<button class="abtn s" id="btn-logout">退出</button><button class="abtn d" id="btn-reset">重置</button></div>
</div></div>
<div class="pwform" id="pwform" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:99;place-items:center">
<div style="background:var(--card);padding:24px;border-radius:12px;width:280px"><h3 style="margin-bottom:12px">修改密码</h3>
<input id="pw-old" type="password" placeholder="旧密码" style="width:100%;padding:10px;margin:6px 0;border-radius:6px;border:1px solid var(--edge);background:#0b0f14;color:#fff">
<input id="pw-new" type="password" placeholder="新密码(至少8位)" style="width:100%;padding:10px;margin:6px 0;border-radius:6px;border:1px solid var(--edge);background:#0b0f14;color:#fff">
<button onclick="fetch('/api/change-password',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'UGreenLedPilot','X-CSRF-Token':document.querySelector('meta[name=csrf]')?.content||''},body:JSON.stringify({old_password:document.getElementById('pw-old').value,new_password:document.getElementById('pw-new').value})}).then(r=>r.json()).then(d=>{alert(d.message||'完成');if(d.success){document.getElementById('pwform').style.display='none';location.href='/login'}})" style="width:100%;padding:10px;border:none;border-radius:6px;background:var(--accent);font-weight:700;cursor:pointer;margin-top:8px">保存</button></div></div>
<div class="toast" id="toast"></div><script>{js}</script></body></html>'''


def preset_buttons(led):
    return ''.join(
        f'<button class="preset" data-led="{led}" data-preset="{p["id"]}" title="{p["name"]}" '
        f'style="background:rgb({p["color"][0]},{p["color"][1]},{p["color"][2]})"></button>'
        for p in COLOR_PRESETS
    )


def build_page():
    js = (JS.replace('__LEDS_JSON__', json.dumps(led_names))
          .replace('__INIT_MODES__', json.dumps(ctrl.modes))
          .replace('__INIT_SETTINGS__', json.dumps(ctrl.settings)))
    return HTML.format(
        css=CSS, js=js, disk_count=disk_count,
        disk_bays='\n'.join(bay_html(i) for i in range(1, disk_count + 1)),
        power_presets=preset_buttons('power'), netdev_presets=preset_buttons('netdev'),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = 'UGreenLedPilot/1.1'

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ''

    def _send_security_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")

    def _check_origin(self):
        origin = self.headers.get('Origin', '')
        referer = self.headers.get('Referer', '')
        host = self.headers.get('Host', '')
        if not origin and not referer:
            return True
        if origin and host and host in origin:
            return True
        if referer and host and host in referer:
            return True
        return False

    def _require_auth(self):
        if not auth.is_authenticated(self.headers):
            self._json(401, {'success': False, 'message': '请先登录'})
            return False
        return True

    def _require_csrf(self):
        token = self.headers.get('X-CSRF-Token', '')
        if not auth.validate_csrf(token):
            self._json(403, {'success': False, 'message': 'CSRF 校验失败，请刷新页面'})
            return False
        return True

    def do_GET(self):
        if self.path in ('/login',):
            return self._html(200, LOGIN_HTML.format(css=CSS))
        if self.path in ('/', '/index.html'):
            if not auth.is_authenticated(self.headers):
                return self._html(200, LOGIN_HTML.format(css=CSS))
            return self._html(200, build_page())
        if not self._require_auth():
            return
        if self.path == '/api/status':
            status = ctrl.get_status()
            status['csrf_token'] = auth.get_csrf_token()
            status['must_change_password'] = auth.must_change_password()
            status['version'] = APP_VERSION
            status['model_warning'] = model_info.get('warning', '')
            return self._json(200, {'success': True, 'initialized': initialized, 'model': model_info, **status})
        if self.path == '/api/config':
            return self._json(200, {'success': True, 'config': cfg, 'detected': detected, 'probe_error': probe_error})
        return self._json(404, {'success': False})

    def do_POST(self):
        if not self._check_origin() and self.path not in ('/api/login',):
            return self._json(403, {'success': False, 'message': '来源校验失败'})

        body = self._body()
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json(400, {'success': False, 'message': 'Invalid JSON'})

        if self.path == '/api/login':
            return self._login(data)

        if not self._require_auth():
            return
        if not self._require_csrf():
            return

        routes = {
            '/api/logout': self._logout,
            '/api/control': lambda: self._control(data),
            '/api/color': lambda: self._color(data),
            '/api/preset': lambda: self._preset(data),
            '/api/brightness': lambda: self._brightness(data),
            '/api/save': lambda: (ctrl._persist(), self._json(200, {'success': True}))[1],
            '/api/change-password': lambda: self._change_password(data),
            '/api/remap': lambda: self._remap(),
            '/api/all/off': lambda: self._all('off'),
            '/api/all/on': lambda: self._all('on'),
            '/api/all/auto': lambda: self._all('auto'),
            '/api/reset': self._reset_config,
        }
        if self.path in routes:
            return routes[self.path]()
        return self._json(404, {'success': False})

    def _login(self, data):
        ip = self._client_ip()
        if auth.is_locked_out(ip):
            remaining = auth.lockout_remaining(ip)
            return self._json(429, {
                'success': False,
                'message': f'登录尝试过多，请 {remaining // 60 + 1} 分钟后再试',
            })
        if not auth.verify_password(data.get('password', '')):
            auth.record_failed_login(ip)
            return self._json(401, {'success': False, 'message': '密码错误'})
        auth.record_successful_login(ip)
        session, csrf = auth.create_session()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.send_header(
            'Set-Cookie',
            f'pilot_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={86400 * 7}',
        )
        self.end_headers()
        self.wfile.write(json.dumps({
            'success': True,
            'csrf_token': csrf,
            'must_change_password': auth.must_change_password(),
        }).encode())

    def _logout(self):
        auth.logout()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.send_header('Set-Cookie', 'pilot_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')
        self.end_headers()
        self.wfile.write(json.dumps({'success': True}).encode())

    def _control(self, data):
        led, mode = data.get('led', ''), data.get('action', '')
        if not validate_led(led) or not validate_mode(mode):
            return self._json(400, {'success': False, 'message': '无效参数'})
        ok, msg = ctrl.set_mode(led, mode)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _color(self, data):
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        ok, msg = ctrl.set_color(led, int(data['r']), int(data['g']), int(data['b']))
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _preset(self, data):
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        ok, msg = ctrl.apply_preset(led, data.get('preset', ''))
        color = ctrl.get_settings(led).get('color', [255, 255, 255])
        return self._json(200 if ok else 400, {'success': ok, 'message': msg, 'color': color})

    def _brightness(self, data):
        if 'led' in data:
            if not validate_led(data['led']):
                return self._json(400, {'success': False, 'message': '无效 LED'})
            ok, msg = ctrl.set_brightness(data['led'], data.get('brightness', 255))
        else:
            ok, msg = ctrl.set_global_brightness(data.get('brightness', 255))
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _all(self, mode):
        if not validate_mode(mode):
            return self._json(400, {'success': False, 'message': '无效模式'})
        errors = []
        for l in led_names:
            ok, m = ctrl.set_mode(l, mode)
            if not ok:
                errors.append(f'{l}: {m}')
        return self._json(500 if errors else 200, {'success': not errors, 'message': '; '.join(errors)})

    def _remap(self):
        ok, msg = ctrl.force_remap()
        return self._json(200 if ok else 500, {'success': ok, 'message': msg, 'disk_map': ctrl.get_status().get('disk_map', {})})

    def _reset_config(self):
        global initialized, cfg, ctrl, disk_count, led_names
        for p in (STATE_FILE, SETTINGS_FILE):
            remove_file(p)
        cfg = {
            'disk_count': default_disk_count,
            'model': DXP4800_PLUS_PROFILE['id'],
            'model_name': DXP4800_PLUS_PROFILE['name'],
            'product_name': model_info.get('product_name', ''),
            'auto_detected': True,
            'ata_map': DXP4800_PLUS_PROFILE['ata_map'],
            'hctl_map': DXP4800_PLUS_PROFILE['hctl_map'],
        }
        save_json(CONFIG_FILE, cfg)
        initialized = True
        disk_count = default_disk_count
        led_names = LED_BASE + [f'disk{i}' for i in range(1, disk_count + 1)]
        ctrl.stop_monitor()
        ctrl = PilotController(led_names, run, STATE_FILE, SETTINGS_FILE,
                               ata_map=cfg['ata_map'], hctl_map=cfg['hctl_map'],
                               disk_count=disk_count)
        ctrl.restore_state(apply_hardware=True)
        ctrl.start_monitor()
        return self._json(200, {'success': True})

    def _change_password(self, data):
        ok, msg = auth.change_password(data.get('old_password', ''), data.get('new_password', ''))
        if ok:
            auth.logout()
        return self._json(200 if ok else 400, {'success': ok, 'message': msg})

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n > MAX_BODY_BYTES:
            raise ValueError('Request body too large')
        return self.rfile.read(n) if n else ''

    def _json(self, status, data):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html(self, status, content):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content.encode())

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
