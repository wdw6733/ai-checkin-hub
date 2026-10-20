const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let state = { sites: [], overview: null, settings: null };

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

/* ---------------- 会话（面板访问密码） ---------------- */
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

/* ---------------- 通用 ---------------- */
const hostOf = (url) => String(url || '').replace(/^https?:\/\//, '').replace(/\/+$/, '');
const fmtTime = (t) => (t ? String(t).replace('T', ' ').slice(5, 16) : '-');

/* ---------------- 概览 ---------------- */
async function loadOverview() {
  try {
    const d = await api('/api/overview');
    state.overview = d;
    $('#kpis').innerHTML = [
      kpi('站点数', String(d.sites ?? 0), '已启用'),
      kpi('账号数', String(d.accounts ?? 0), '已启用'),
      kpi('总余额', '$' + (d.total_quota ?? 0).toFixed(3), '按各站汇率折算'),
      kpi('最近刷新', fmtTime(d.last_refresh), '余额同步时间'),
    ].join('');
    renderQuick(d.cards || []);
  } catch (e) { toast(e.message); }
}

const kpi = (label, value, sub) => `<div class="kpi"><div class="label">${esc(label)}</div>
  <div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;

/* 概览页：整张卡片即外链，新标签打开站点，手动签到 */
function renderQuick(cards) {
  const grid = $('#quick-grid');
  if (!grid) return;
  $('#quick-empty').classList.toggle('hidden', cards.length > 0);
  grid.innerHTML = cards.map((c) => `
    <a class="site-card quick-card" href="${esc(c.base_url)}" target="_blank" rel="noopener noreferrer">
      <div class="site-head">
        <div style="min-width:0">
          <div class="site-name">${esc(c.name)} <span class="ext">↗</span></div>
          <div class="site-host">${esc(hostOf(c.base_url))}</div>
        </div>
        ${c.enabled ? '' : '<span class="badge off">已停用</span>'}
      </div>
      <div class="site-meta">
        <div class="metric"><b>$${Number(c.quota || 0).toFixed(3)}</b><span>余额</span></div>
        <div class="metric"><b>${c.acc_count}</b><span>账号</span></div>
      </div>
      <div class="site-note">点击在新标签页打开站点，手动完成签到</div>
    </a>`).join('');
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
    const host = hostOf(s.base_url);
    const probe = s.probe_info || {};
    const tags = [];
    if (s.framework && s.framework !== 'unknown') tags.push(`<span class="badge newapi">${esc(s.framework)}</span>`);
    if (probe.waf) tags.push('<span class="badge waf">WAF</span>');
    if (!s.enabled) tags.push('<span class="badge off">已停用</span>');

    const quota = s.accounts.reduce((sum, a) => sum + (a.quota || 0), 0);
    const lastAt = s.accounts.reduce((t, a) => (a.last_checkin_at && a.last_checkin_at > t ? a.last_checkin_at : t), '');

    const accRows = s.accounts.map((a) => `
      <div class="acc">
        <span class="who">${esc(a.label || a.username || '账号' + a.id)}
          ${a.last_error ? `<span class="bad" title="${esc(a.last_error)}">!</span>` : ''}</span>
        <span class="q">${a.quota == null ? '-' : '$' + Number(a.quota).toFixed(3)}</span>
        <button class="btn ghost sm" onclick="act.editAccount(${s.id}, ${a.id})">编辑CK</button>
      </div>`).join('') || '<div class="acc"><span class="who muted">尚未配置账号</span></div>';

    return `<div class="site-card">
      <div class="site-head">
        <div style="min-width:0">
          <a class="site-name site-link" href="${esc(s.base_url)}" target="_blank" rel="noopener noreferrer" title="在新标签页打开站点">${esc(s.name)} <span class="ext">↗</span></a>
          <a class="site-host site-link" href="${esc(s.base_url)}" target="_blank" rel="noopener noreferrer" title="在新标签页打开站点">${esc(host)}</a>
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end">${tags.join('')}</div>
      </div>
      <div class="site-meta">
        <div class="metric"><b>$${quota.toFixed(3)}</b><span>余额</span></div>
        <div class="metric"><b>${s.accounts.length}</b><span>账号</span></div>
        <div class="metric"><b>${esc(fmtTime(lastAt))}</b><span>最近刷新</span></div>
      </div>
      <div class="acc-list">${accRows}</div>
      ${probe.notes && probe.notes.length ? `<div class="site-note">${esc(probe.notes.join('；'))}</div>` : ''}
      <div class="site-actions">
        <a class="btn ghost sm" href="${esc(s.base_url)}" target="_blank" rel="noopener noreferrer">打开站点</a>
        <button class="btn ghost sm" onclick="act.refreshSite(${s.id}, this)">刷新余额</button>
        <button class="btn ghost sm" onclick="act.probe(${s.id}, this)">探测</button>
        <button class="btn ghost sm" onclick="act.models(${s.id})">模型</button>
        <button class="btn ghost sm" onclick="act.editSite(${s.id})">编辑</button>
        <button class="btn ghost sm" onclick="act.addAccount(${s.id})">+ 账号</button>
        <button class="btn ghost sm" onclick="act.delSite(${s.id})">删除</button>
      </div>
    </div>`;
  }).join('');
}

/* ---------------- 操作 ---------------- */
const act = {
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
  editAccount(siteId, accountId) {
    const s = state.sites.find((x) => x.id === siteId);
    const a = s?.accounts?.find((x) => x.id === accountId);
    if (a) openEditAccountModal(siteId, a);
  },
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
function closeModal() {
  $('#modal').classList.add('hidden');
}
$('#modal-close').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });

const AUTH_OPTIONS = [
  ['token', 'API Key / 访问令牌（推荐）'],
  ['cookie', '浏览器 Cookie'],
  ['password', '账号密码'],
  ['linuxdo', 'linux.do Cookie（_t）'],
];

function openSiteModal(site) {
  const isEdit = !!site;
  const authOpts = AUTH_OPTIONS.map(([v, t]) => `<option value="${v}" ${site && site.auth_mode === v ? 'selected' : ''}>${t}</option>`).join('');
  openModal(isEdit ? '编辑站点' : '添加站点', `
    <div class="grid2">
      <div class="field"><label>站点名称</label><input id="f-name" value="${esc(site?.name || '')}" placeholder="例如 AnyRouter"></div>
      <div class="field"><label>Base URL</label><input id="f-url" value="${esc(site?.base_url || '')}" placeholder="https://xxx.com"></div>
    </div>
    <div class="field"><label>备注</label><input id="f-note" value="${esc(site?.note || '')}" placeholder="可选"></div>
    <div class="field inline"><label>启用</label>
      <input type="checkbox" id="f-enabled" ${!site || site.enabled ? 'checked' : ''}></div>

    ${isEdit ? `
    <div class="section-title">已有账号 (${(site.accounts || []).length})</div>
    <div class="account-list">
      ${(site.accounts || []).map((a) => `
        <div class="account-row">
          <span class="account-label">${esc(a.label || a.username || '未命名')}</span>
          <span class="account-mode tag">${esc(a.auth_mode)}</span>
          <span class="account-uid muted">${esc(a.uid || '-')}</span>
          <button class="btn ghost sm" onclick="act.editAccount(${site.id}, ${a.id})">编辑CK</button>
        </div>`).join('') || '<p class="muted">暂无账号</p>'}
    </div>
    <button class="btn sm" onclick="act.addAccount(${site.id})" style="margin-bottom:12px">+ 添加账号</button>
    ` : `
    <div class="section-title">首个账号（可选，仅用于读取余额）</div>
    <div class="grid2">
      <div class="field"><label>认证方式</label><select id="f-auth">${authOpts}</select></div>
      <div class="field"><label>标签</label><input id="f-user" placeholder="可留空"></div>
    </div>
    <div class="field"><label>密钥</label>
      <input id="f-secret" type="password" placeholder="API Key / Cookie / 密码">
      <div class="form-hint">凭据只用于读取余额与模型目录。签到请点站点名称或「打开站点」，到站点页面手动完成。</div>
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
      note: $('#f-note').value.trim(),
      enabled: $('#f-enabled').checked,
    };
    if (!payload.name || !payload.base_url) return toast('名称和地址必填');
    try {
      if (isEdit) {
        await api(`/api/sites/${site.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
        closeModal(); toast('已保存'); loadSites();
      } else {
        const secret = $('#f-secret').value.trim();
        payload.accounts = secret ? [{
          auth_mode: $('#f-auth').value,
          username: $('#f-user').value.trim(),
          secret,
          label: $('#f-user').value.trim(),
        }] : [];
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
    <div class="field"><label>密钥</label><input id="a-secret" type="password" placeholder="API Key / Cookie / 密码"></div>
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

function openEditAccountModal(siteId, account) {
  openModal('编辑账号 CK 信息', `
    <div class="grid2">
      <div class="field"><label>认证方式</label><select id="ea-auth">
        ${AUTH_OPTIONS.map(([v, t]) => `<option value="${v}" ${account.auth_mode === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
      <div class="field"><label>标签</label><input id="ea-label" value="${esc(account.label || '')}" placeholder="例如 主号"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>用户名</label><input id="ea-user" value="${esc(account.username || '')}" placeholder="密码模式必填"></div>
      <div class="field"><label>用户 ID（uid）</label><input id="ea-uid" value="${esc(account.uid || '')}" placeholder="New API 系常需要，可留空"></div>
    </div>
    <div class="field"><label>密钥 / Cookie / 令牌（留空则不修改）</label>
      <input id="ea-secret" type="password" placeholder="sk-… / session=xxx; … / access token">
      <div class="form-hint">凭据只用于刷新余额与模型目录，不参与任何自动签到。</div>
    </div>
    <p class="hint" id="ea-auth-hint"></p>

    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn" id="ea-verify">保存并验证</button>
      <button class="btn primary" id="ea-submit">保存</button>
    </div>
  `);
  const authHint = (msg) => { const el = $('#ea-auth-hint'); if (el) el.textContent = msg || ''; };

  async function saveAccount() {
    const body = {
      auth_mode: $('#ea-auth').value,
      label: $('#ea-label').value.trim(),
      username: $('#ea-user').value.trim(),
      uid: $('#ea-uid').value.trim(),
    };
    const secret = $('#ea-secret').value.trim();
    if (secret) body.secret = secret;
    await api(`/api/accounts/${account.id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    loadSites();
  }

  $('#ea-submit').addEventListener('click', async () => {
    try {
      await saveAccount();
      closeModal(); toast('已保存');
    } catch (e) { toast(e.message); }
  });

  $('#ea-verify').addEventListener('click', async () => {
    const btn = $('#ea-verify');
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = '验证中…';
    try {
      await saveAccount();
      const r = await api(`/api/accounts/${account.id}/refresh`, { method: 'POST' });
      if (r.ok) {
        const q = r.quota == null ? '-' : '$' + Number(r.quota).toFixed(3);
        authHint(`凭据有效：余额 ${q}，可用模型 ${r.models ?? '-'} 个`);
        toast('凭据有效');
      } else {
        authHint(`凭据无效：${r.message || '未知错误'}`);
        toast('凭据无效');
      }
    } catch (e) {
      authHint(e.message); toast(e.message);
    } finally {
      btn.disabled = false; btn.textContent = old;
    }
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
$('#btn-refresh-all').addEventListener('click', async (e) => {
  await withBusy(e.target, '刷新中', async () => {
    const r = await api('/api/accounts/refresh-all', { method: 'POST' });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`刷新完成：成功 ${ok}/${(r.results || []).length}`);
    route();
  });
});

window.act = act;
window.closeModal = closeModal;
boot();
