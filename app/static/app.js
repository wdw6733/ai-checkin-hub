const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let state = { sites: [], overview: null, settings: null };
let currentSiteId = null;

function toast(msg, ms = 3200) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), ms);
}

async function api(path, opt = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opt,
  });
  if (res.status === 401) { showLogin(); throw new Error('登录已过期'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

/* ---------------- 会话 ---------------- */
async function boot() {
  try {
    const s = await api('/api/session');
    if (s.authed) { showApp(); } else { showLogin(); if (s.default_password) $('#login-hint').textContent = '当前使用默认密码 admin，请尽快在部署配置中修改 WEB_PASSWORD'; }
  } catch (e) { showLogin(); }
}

function showLogin() {
  $('#login').classList.remove('hidden');
  $('#app').classList.add('hidden');
  setTimeout(() => $('#password').focus(), 50);
}
function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  $('#tz-pill').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
  route();
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('#password').value }) });
    $('#password').value = '';
    showApp();
  } catch (err) { toast(err.message); }
});

$('#btn-logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  showLogin();
});

/* ---------------- 路由 ---------------- */
const TITLES = { overview: '概览', sites: '站点', logs: '日志', settings: '设置' };
function route() {
  const view = (location.hash || '#overview').slice(1);
  const name = TITLES[view] ? view : 'overview';
  $$('.view').forEach((v) => v.classList.add('hidden'));
  $('#view-' + name).classList.remove('hidden');
  $$('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  $('#view-title').textContent = TITLES[name];
  if (name === 'overview') loadOverview();
  if (name === 'sites') loadSites();
  if (name === 'logs') loadLogs();
  if (name === 'settings') loadSettings();
}
window.addEventListener('hashchange', route);

/* ---------------- 概览 ---------------- */
async function loadOverview() {
  try {
    const d = await api('/api/overview');
    state.overview = d;
    $('#kpis').innerHTML = [
      kpi('总余额', '$' + (d.total_quota ?? 0).toFixed(3), '按各站汇率折算'),
      kpi('今日已签', `${d.today_done}/${d.accounts}`, '账号维度'),
      kpi('7 日成功率', d.success_rate == null ? '-' : Math.round(d.success_rate * 100) + '%', '含失败重试'),
      kpi('7 日新增', '$' + (d.week_gained ?? 0).toFixed(3), '签到累计'),
    ].join('');
    renderPlan(d.plan || []);
    renderChart(d.series || []);
  } catch (e) { toast(e.message); }
}

const kpi = (label, value, sub) => `<div class="kpi"><div class="label">${esc(label)}</div>
  <div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;

function renderPlan(plan) {
  const box = $('#plan-list');
  if (!plan.length) { box.innerHTML = '<p class="muted sm">暂无启用的账号</p>'; return; }
  $('#plan-sub').textContent = `${plan.filter((p) => p.done).length}/${plan.length} 已完成`;
  box.innerHTML = plan.map((p) => {
    const cls = p.running ? 'run' : p.done ? 'done' : '';
    const time = p.target.slice(11, 16);
    return `<div class="plan-row"><span class="st ${cls}"></span>
      <span class="name">${esc(p.site)} · ${esc(p.label || '-')}</span>
      <span class="time">${esc(time)}</span></div>`;
  }).join('');
}

function renderChart(series) {
  const map = new Map(series.map((s) => [s.d, Number(s.g)]));
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(); d.setDate(d.getDate() - i);
    const key = d.toISOString().slice(0, 10);
    days.push({ key, v: map.get(key) || 0, label: key.slice(5) });
  }
  const max = Math.max(...days.map((d) => d.v), 0.0001);
  $('#chart').innerHTML = days.map((d) => {
    const h = Math.max((d.v / max) * 100, d.v > 0 ? 6 : 2);
    return `<div class="bar-wrap" title="${d.key}：$${d.v.toFixed(4)}">
      <div class="bar ${d.v > 0 ? '' : 'zero'}" style="height:${h}%"></div>
      <span>${d.label}</span></div>`;
  }).join('');
}

/* ---------------- 站点 ---------------- */
async function loadSites() {
  try {
    const d = await api('/api/sites');
    state.sites = d.sites;
    renderSites();
  } catch (e) { toast(e.message); }
}

function renderSites() {
  const grid = $('#site-grid');
  $('#sites-empty').classList.toggle('hidden', state.sites.length > 0);
  grid.innerHTML = state.sites.map((s) => {
    const host = s.base_url.replace(/^https?:\/\//, '');
    const probe = s.probe_info || {};
    const tags = [];
    if (s.framework && s.framework !== 'unknown') tags.push(`<span class="badge newapi">${esc(s.framework)}</span>`);
    if (probe.waf) tags.push('<span class="badge waf">WAF</span>');
    if (s.browser_mode) tags.push('<span class="badge">浏览器</span>');
    if (!s.enabled) tags.push('<span class="badge off">已停用</span>');

    const quota = s.accounts.reduce((sum, a) => sum + (a.quota || 0), 0);
    const doneToday = s.accounts.filter((a) => a.last_checkin_date === todayStr()).length;
    const sched = s.schedule_mode === 'fixed'
      ? `每日 ${s.fixed_time}${s.jitter_minutes ? ` ±${s.jitter_minutes}m` : ''}`
      : `${s.window_start}-${s.window_end} 随机`;

    const accRows = s.accounts.map((a) => `
      <div class="acc">
        <span class="st ${a.last_checkin_date === todayStr() ? 'done' : ''}"></span>
        <span class="who">${esc(a.label || a.username || '账号' + a.id)}
          ${a.last_error ? `<span class="bad" title="${esc(a.last_error)}">!</span>` : ''}</span>
        <span class="q">${a.quota == null ? '-' : '$' + Number(a.quota).toFixed(3)}</span>
        <button class="btn ghost sm" onclick="act.checkAccount(${a.id}, this)">签到</button>
      </div>`).join('') || '<div class="acc"><span class="who muted">尚未配置账号</span></div>';

    return `<div class="site-card">
      <div class="site-head">
        <div style="min-width:0">
          <div class="site-name">${esc(s.name)}</div>
          <div class="site-host">${esc(host)}</div>
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end">${tags.join('')}</div>
      </div>
      <div class="site-meta">
        <div class="metric"><b>$${quota.toFixed(3)}</b><span>余额</span></div>
        <div class="metric"><b>${doneToday}/${s.accounts.length}</b><span>今日已签</span></div>
        <div class="metric"><b>${esc(sched)}</b><span>调度</span></div>
      </div>
      <div class="acc-list">${accRows}</div>
      ${probe.notes && probe.notes.length ? `<div class="site-note">${esc(probe.notes.join('；'))}</div>` : ''}
      <div class="site-actions">
        <button class="btn ghost sm" onclick="act.checkSite(${s.id}, this)">全签</button>
        <button class="btn ghost sm" onclick="act.refreshSite(${s.id}, this)">刷新</button>
        <button class="btn ghost sm" onclick="act.probe(${s.id}, this)">探测</button>
        <button class="btn ghost sm" onclick="act.models(${s.id})">模型</button>
        <button class="btn ghost sm" onclick="act.editSite(${s.id})">编辑</button>
        <button class="btn ghost sm" onclick="act.addAccount(${s.id})">+ 账号</button>
        <button class="btn ghost sm" onclick="act.delSite(${s.id})">删除</button>
      </div>
    </div>`;
  }).join('');
}

const todayStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

/* ---------------- 操作 ---------------- */
const act = {
  async checkAccount(id, btn) {
    await withBusy(btn, '签到中', async () => {
      const r = await api(`/api/accounts/${id}/checkin`, { method: 'POST' });
      toast(r.ok ? `签到成功：${r.message}${r.gained ? `（+$${Number(r.gained).toFixed(4)}）` : ''}` : `失败：${r.message}`);
      loadSites();
    });
  },
  async checkSite(siteId, btn) {
    const site = state.sites.find((s) => s.id === siteId);
    if (!site || !site.accounts.length) return toast('该站点还没有账号');
    await withBusy(btn, '执行中', async () => {
      let ok = 0;
      for (const a of site.accounts) {
        try {
          const r = await api(`/api/accounts/${a.id}/checkin`, { method: 'POST' });
          if (r.ok) ok++;
        } catch (e) { /* 单账号失败不影响其它 */ }
      }
      toast(`完成：成功 ${ok}/${site.accounts.length}`);
      loadSites();
    });
  },
  async refreshSite(siteId, btn) {
    const site = state.sites.find((s) => s.id === siteId);
    if (!site) return;
    await withBusy(btn, '刷新中', async () => {
      for (const a of site.accounts) { await api(`/api/accounts/${a.id}/refresh`, { method: 'POST' }).catch(() => {}); }
      loadSites();
      toast('余额已刷新');
    });
  },
  async probe(siteId, btn) {
    await withBusy(btn, '探测中', async () => {
      const r = await api(`/api/sites/${siteId}/probe`, { method: 'POST' });
      const p = r.probe || {};
      const bits = [p.framework ? `框架 ${p.framework}` : '框架未知'];
      if (p.version) bits.push(p.version);
      if (p.waf) bits.push('命中 WAF');
      if (p.notes && p.notes.length) bits.push(p.notes[0]);
      toast(bits.join(' · '), 5000);
      loadSites();
    });
  },
  async models(siteId) {
    openModal('模型与定价', '<p class="muted sm">正在拉取…</p>');
    try {
      const d = await api(`/api/sites/${siteId}/models`);
      const usable = new Set(d.usable || []);
      const rows = (d.pricing || []).slice(0, 200).map((m) => `<tr>
        <td class="mono">${esc(m.model)} ${usable.size && usable.has(m.model) ? '<span class="ok">·可用</span>' : ''}</td>
        <td>${m.price_in_per_1m == null ? '按次' : '$' + m.price_in_per_1m}</td>
        <td>${m.price_out_per_1m == null ? '-' : '$' + m.price_out_per_1m}</td>
        <td class="muted">${esc((m.groups || []).join(','))}</td></tr>`).join('');
      $('#modal-body').innerHTML = `
        <p class="muted sm">定价按站点公开倍率换算为「美元 / 百万 tokens」，仅供参考。可用模型数：${usable.size}</p>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>模型</th><th>输入/1M</th><th>输出/1M</th><th>分组</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="4" class="muted">未获取到公开定价</td></tr>'}</tbody>
        </table></div>`;
    } catch (e) {
      $('#modal-body').innerHTML = `<p class="bad">${esc(e.message)}</p>`;
    }
  },
  editSite(siteId) {
    const s = state.sites.find((x) => x.id === siteId);
    if (s) openSiteModal(s);
  },
  async delSite(siteId) {
    if (!confirm('删除站点会同时删除其账号与历史日志，确认？')) return;
    await api(`/api/sites/${siteId}`, { method: 'DELETE' });
    toast('已删除');
    loadSites();
  },
  addAccount(siteId) { openAccountModal(siteId); },
};

async function withBusy(btn, label, fn) {
  if (!btn) { try { await fn(); } catch (e) { toast(e.message); } return; }
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = label;
  try { await fn(); } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}

/* ---------------- 表单弹窗 ---------------- */
function openModal(title, bodyHtml) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); }
$('#modal-close').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });

const AUTH_OPTIONS = [
  ['token', 'API Key / 访问令牌（推荐）'],
  ['password', '账号密码'],
  ['cookie', '浏览器 Cookie'],
  ['linuxdo', 'linux.do OAuth'],
];

function openSiteModal(site) {
  const isEdit = !!site;
  const authOpts = AUTH_OPTIONS.map(([v, t]) => `<option value="${v}" ${site && site.auth_mode === v ? 'selected' : ''}>${t}</option>`).join('');
  const acc = isEdit ? null : { auth_mode: 'token' };
  openModal(isEdit ? '编辑站点' : '添加站点', `
    <div class="grid2">
      <div class="field"><label>站点名称</label><input id="f-name" value="${esc(site?.name || '')}" placeholder="例如 AnyRouter"></div>
      <div class="field"><label>Base URL</label><input id="f-url" value="${esc(site?.base_url || '')}" placeholder="https://xxx.com/v1"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>调度模式</label>
        <select id="f-mode">
          <option value="random" ${!site || site.schedule_mode === 'random' ? 'selected' : ''}>随机窗口（推荐，防风控）</option>
          <option value="fixed" ${site?.schedule_mode === 'fixed' ? 'selected' : ''}>固定时间</option>
        </select>
      </div>
      <div class="field"><label>时区</label><input id="f-tz" value="${esc(site?.timezone || 'Asia/Shanghai')}"></div>
    </div>
    <div class="grid3" id="win-fields">
      <div class="field"><label>窗口开始</label><input id="f-ws" value="${esc(site?.window_start || '07:00')}"></div>
      <div class="field"><label>窗口结束</label><input id="f-we" value="${esc(site?.window_end || '23:00')}"></div>
      <div class="field"><label>固定时间</label><input id="f-ft" value="${esc(site?.fixed_time || '08:30')}"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>固定时间抖动 ± 分钟（0-180）</label><input id="f-jit" type="number" min="0" max="180" value="${site?.jitter_minutes ?? 0}"></div>
      <div class="field"><label>签到端点</label><input id="f-path" value="${esc(site?.checkin_path || '/api/user/checkin')}"></div>
    </div>
    <div class="field inline"><label>启用浏览器通道（WAF 站点必开）</label>
      <input type="checkbox" id="f-browser" ${site?.browser_mode ? 'checked' : ''}></div>
    <div class="field"><label>备注</label><input id="f-note" value="${esc(site?.note || '')}"></div>

    ${isEdit ? '' : `
    <div class="section-title">首个账号</div>
    <div class="grid2">
      <div class="field"><label>认证方式</label><select id="f-auth">${authOpts}</select></div>
      <div class="field"><label>用户名（密码模式必填）</label><input id="f-user" placeholder="可留空"></div>
    </div>
    <div class="field"><label id="f-secret-label">密钥</label>
      <input id="f-secret" type="password" placeholder="API Key / 密码 / Cookie">
      <div class="form-hint">API Key 模式填 sk-…；Cookie 模式填 name=value; name2=value2；linux.do 模式填 _t 的值。</div>
    </div>`}

    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn primary" id="f-submit">${isEdit ? '保存' : '创建并探测'}</button>
    </div>
  `);

  $('#f-submit').addEventListener('click', async () => {
    const payload = {
      name: $('#f-name').value.trim(),
      base_url: $('#f-url').value.trim(),
      schedule_mode: $('#f-mode').value,
      timezone: $('#f-tz').value.trim() || 'Asia/Shanghai',
      window_start: $('#f-ws').value.trim(),
      window_end: $('#f-we').value.trim(),
      fixed_time: $('#f-ft').value.trim(),
      jitter_minutes: Number($('#f-jit').value) || 0,
      checkin_path: $('#f-path').value.trim() || '/api/user/checkin',
      browser_mode: $('#f-browser').checked,
      note: $('#f-note').value.trim(),
    };
    if (!payload.name || !payload.base_url) return toast('名称和地址必填');
    try {
      if (isEdit) {
        await api(`/api/sites/${site.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
        closeModal(); toast('已保存'); loadSites();
      } else {
        payload.accounts = [{
          auth_mode: $('#f-auth').value,
          username: $('#f-user').value.trim(),
          secret: $('#f-secret').value.trim(),
          label: $('#f-user').value.trim(),
        }];
        const r = await api('/api/sites', { method: 'POST', body: JSON.stringify(payload) });
        closeModal(); toast('已创建，正在探测…');
        await api(`/api/sites/${r.id}/probe`, { method: 'POST' }).catch(() => {});
        loadSites();
      }
    } catch (e) { toast(e.message); }
  });
}

function openAccountModal(siteId) {
  openModal('添加账号', `
    <div class="grid2">
      <div class="field"><label>认证方式</label><select id="a-auth">
        ${AUTH_OPTIONS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select></div>
      <div class="field"><label>标签</label><input id="a-label" placeholder="例如 主号"></div>
    </div>
    <div class="field"><label>用户名</label><input id="a-user" placeholder="密码模式必填"></div>
    <div class="field"><label>密钥</label><input id="a-secret" type="password" placeholder="API Key / 密码 / Cookie"></div>
    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn primary" id="a-submit">添加</button>
    </div>
  `);
  $('#a-submit').addEventListener('click', async () => {
    try {
      await api(`/api/sites/${siteId}/accounts`, {
        method: 'POST',
        body: JSON.stringify({
          auth_mode: $('#a-auth').value,
          label: $('#a-label').value.trim(),
          username: $('#a-user').value.trim(),
          secret: $('#a-secret').value.trim(),
        }),
      });
      closeModal(); toast('已添加'); loadSites();
    } catch (e) { toast(e.message); }
  });
}

/* ---------------- 日志 ---------------- */
async function loadLogs() {
  try {
    const d = await api('/api/logs?limit=200');
    $('#log-table').innerHTML = `<thead><tr>
      <th>时间</th><th>站点</th><th>账号</th><th>结果</th><th>余额变化</th><th>消息</th></tr></thead><tbody>
      ${(d.logs || []).map((l) => {
        const before = l.quota_before == null ? '-' : '$' + Number(l.quota_before).toFixed(4);
        const after = l.quota_after == null ? '-' : '$' + Number(l.quota_after).toFixed(4);
        const gain = l.gained ? `<span class="ok">+$${Number(l.gained).toFixed(4)}</span>` : '<span class="muted">0</span>';
        return `<tr>
          <td class="mono">${esc((l.ts || '').replace('T', ' ').slice(0, 19))}</td>
          <td>${esc(l.site || '-')}</td><td>${esc(l.who || '-')}</td>
          <td class="${l.ok ? 'ok' : 'bad'}">${l.ok ? '成功' : '失败'}</td>
          <td class="mono">${before} → ${after} ${gain}</td>
          <td class="muted">${esc(l.message || '')}</td></tr>`;
      }).join('') || '<tr><td colspan="6" class="muted">暂无记录</td></tr>'}
      </tbody>`;
  } catch (e) { toast(e.message); }
}
$('#btn-reload-logs').addEventListener('click', loadLogs);

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  try {
    const d = await api('/api/settings');
    $('#wh-type').value = d.webhook_type || 'generic';
    $('#wh-url').value = d.webhook_url || '';
    $('#daily-refresh').checked = d.daily_refresh === '1';
  } catch (e) { toast(e.message); }
}

$('#btn-save-settings').addEventListener('click', async () => {
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({
        webhook_type: $('#wh-type').value,
        webhook_url: $('#wh-url').value.trim(),
        daily_refresh: $('#daily-refresh').checked ? '1' : '0',
      }),
    });
    $('#settings-hint').textContent = '已保存';
    setTimeout(() => ($('#settings-hint').textContent = ''), 2000);
  } catch (e) { toast(e.message); }
});

$('#btn-test-webhook').addEventListener('click', async () => {
  try {
    const r = await api('/api/settings/test-webhook', { method: 'POST' });
    toast(r.message);
  } catch (e) { toast(e.message); }
});

/* ---------------- 顶栏 ---------------- */
$('#btn-add-site').addEventListener('click', () => openSiteModal(null));
$('#btn-checkin-all').addEventListener('click', async (e) => {
  await withBusy(e.target, '执行中', async () => {
    const r = await api('/api/checkin/all', { method: 'POST' });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`全签完成：成功 ${ok}/${(r.results || []).length}`);
    route();
  });
});

window.act = act;
window.closeModal = closeModal;
boot();
