/* 视频转笔记 · 监控看板逻辑 */

const ADMIN_KEY = 'vn_admin_pwd';
const ROLE_KEY = 'vn_admin_role';
let password = sessionStorage.getItem(ADMIN_KEY) || '';
let isAdmin = sessionStorage.getItem(ROLE_KEY) === 'admin';
let trendChart, statusChart, subtitleChart, failChart;

function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function fetchStats() {
  const resp = await fetch('/api/admin/stats', { headers: { 'X-Admin-Password': password } });
  if (resp.status === 401) { throw new Error('unauthorized'); }
  if (!resp.ok) throw new Error('error');
  return resp.json();
}

/* ---------- 密码门 ---------- */
async function tryLogin() {
  password = $('pwd-input').value.trim();
  if (!password) { $('gate-err').textContent = '请输入密码'; return; }
  try {
    const resp = await fetch('/api/admin/role', { headers: { 'X-Admin-Password': password } });
    if (resp.status === 401) { $('gate-err').textContent = '密码错误'; return; }
    const data = await resp.json();
    isAdmin = data.role === 'admin';
    sessionStorage.setItem(ADMIN_KEY, password);
    sessionStorage.setItem(ROLE_KEY, isAdmin ? 'admin' : 'viewer');
    showDash();
  } catch (e) {
    $('gate-err').textContent = '请求失败，请稍后重试';
  }
}

async function viewerLogin() {
  try {
    const resp = await fetch('/api/admin/viewer-login');
    if (!resp.ok) { $('gate-err').textContent = '访客模式未启用'; return; }
    const data = await resp.json();
    password = data.password;
    isAdmin = false;
    sessionStorage.setItem(ADMIN_KEY, password);
    sessionStorage.setItem(ROLE_KEY, 'viewer');
    showDash();
  } catch (e) {
    $('gate-err').textContent = '请求失败，请稍后重试';
  }
}

function logout() {
  sessionStorage.removeItem(ADMIN_KEY);
  sessionStorage.removeItem(ROLE_KEY);
  location.reload();
}

function showDash() {
  $('gate').style.display = 'none';
  $('dash').style.display = 'block';
  initCharts();
  renderTableTabs();
  loadTable();
  if (isAdmin) {
    loadWhitelist();
    loadBlacklist();
  }
  applyRole();
  refresh();
  setInterval(refresh, 20000);
}

function applyRole() {
  const adminTab = document.querySelector('.dash-tab[data-tab="admin"]');
  if (adminTab) adminTab.style.display = isAdmin ? '' : 'none';
  if (!isAdmin) {
    document.querySelectorAll('.dash-tab').forEach(b => b.classList.remove('active'));
    const dataTab = document.querySelector('.dash-tab[data-tab="data"]');
    if (dataTab) dataTab.classList.add('active');
    $('tab-data').style.display = 'block';
    $('tab-admin').style.display = 'none';
  }
}

/* ---------- 概览卡片 ---------- */
function renderCards(s) {
  const o = s.overview, t = s.tasks;
  const items = [
    ['总用户', o.users_total, ''],
    ['今日新增用户', o.users_today, ''],
    ['累计收入', '¥' + o.revenue_total.toFixed(2), 'green'],
    ['累计成本', '¥' + o.cost_total.toFixed(4), 'amber'],
    ['毛利', '¥' + o.margin_total.toFixed(4), o.margin_total >= 0 ? 'green' : 'red'],
    ['用户余额合计', '¥' + o.balance_total.toFixed(2), ''],
    ['任务总数', t.total, ''],
    ['成功率', t.success_rate + '%', 'green'],
  ];
  $('cards').innerHTML = items.map(([lbl, num, cls]) =>
    `<div class="card"><div class="lbl">${lbl}</div><div class="num ${cls}">${num}</div></div>`).join('');
}

/* ---------- 图表 ---------- */
function initCharts() {
  trendChart = echarts.init($('trend-chart'));
  statusChart = echarts.init($('status-chart'));
  subtitleChart = echarts.init($('subtitle-chart'));
  failChart = echarts.init($('fail-chart'));
  window.addEventListener('resize', () => {
    [trendChart, statusChart, subtitleChart, failChart].forEach(c => c && c.resize());
  });
}

function renderTrend(s) {
  const t = s.trend_7d;
  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['任务数', '消费(元)', '成本(元)'], top: 0 },
    grid: { left: 44, right: 44, top: 44, bottom: 28 },
    xAxis: { type: 'category', data: t.labels },
    yAxis: [
      { type: 'value', name: '任务数', minInterval: 1 },
      { type: 'value', name: '元' },
    ],
    series: [
      { name: '任务数', type: 'line', data: t.tasks, smooth: true, color: '#4F46E5', areaStyle: { opacity: 0.08 } },
      { name: '消费(元)', type: 'line', yAxisIndex: 1, data: t.revenue, smooth: true, color: '#16A34A' },
      { name: '成本(元)', type: 'line', yAxisIndex: 1, data: t.cost, smooth: true, color: '#D97706' },
    ],
  });
}

function renderStatus(s) {
  const st = s.tasks.status;
  statusChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '45%'],
      data: [
        { name: '转换中', value: st.running, itemStyle: { color: '#D97706' } },
        { name: '已完成', value: st.completed, itemStyle: { color: '#16A34A' } },
        { name: '失败', value: st.failed, itemStyle: { color: '#DC2626' } },
        { name: '已停止', value: st.cancelled, itemStyle: { color: '#6B7280' } },
      ],
      label: { formatter: '{b}: {c}' },
    }],
  });
}

function renderSubtitle(s) {
  const c = s.cost_detail;
  subtitleChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '45%'],
      data: [
        { name: '字幕命中', value: c.subtitle, itemStyle: { color: '#4F46E5' } },
        { name: '音频转写', value: c.transcribe, itemStyle: { color: '#D97706' } },
      ],
      label: { formatter: '{b}: {c}' },
    }],
  });
}

function renderFail(s) {
  const fr = s.fail_reasons;
  const keys = Object.keys(fr);
  failChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: keys },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'bar', data: keys.map(k => fr[k]), itemStyle: { color: '#DC2626', borderRadius: [4, 4, 0, 0] }, barMaxWidth: 44 }],
  });
}

/* ---------- 系统资源 ---------- */
function renderSystem(s) {
  const sy = s.system;
  const bars = [
    ['CPU', sy.cpu, sy.cpu + '%'],
    ['内存', sy.mem_percent, sy.mem_used_gb + ' / ' + sy.mem_total_gb + ' GB'],
    ['磁盘', sy.disk_percent, sy.disk_used_gb + ' / ' + sy.disk_total_gb + ' GB'],
  ];
  let html = bars.map(([lbl, pct, val]) =>
    `<div class="sys-item"><div class="lbl">${lbl}</div><div class="sys-bar"><i style="width:${pct}%"></i></div><div class="val">${val}</div></div>`).join('');
  html += `<div class="sys-item"><div class="lbl">负载 1/5/15 分钟</div><div class="val" style="font-size:13px">${sy.load.join(' / ')}</div></div>`;
  html += `<div class="sys-item"><div class="lbl">API 错误计数</div><div class="val" style="font-size:13px">LLM 限流 ${sy.llm_rate_limits} ｜ LLM 错误 ${sy.llm_errors} ｜ ASR 错误 ${sy.asr_errors}</div></div>`;
  $('sys-grid').innerHTML = html;
}

/* ---------- 充值意愿 ---------- */
function renderRechargeIntent(s) {
  const r = s.recharge_intent;
  const items = [
    ['充值点击（累计）', r.clicks_total, ''],
    ['充值点击（今日）', r.clicks_today, ''],
    ['有意向用户数', r.users, 'amber'],
  ];
  $('recharge-cards').innerHTML = items.map(([lbl, num, cls]) =>
    `<div class="card"><div class="lbl">${lbl}</div><div class="num ${cls}">${num}</div></div>`).join('');
}

/* ---------- 数据库表查看器 ---------- */
const TABLES = [
  { key: 'users', label: '用户' },
  { key: 'tasks', label: '任务' },
  { key: 'billing', label: '账单' },
  { key: 'orders', label: '订单' },
  { key: 'events', label: '事件' },
  { key: 'sms_codes', label: '验证码' },
  { key: 'feedback', label: '反馈' },
];
let currentTable = 'users';
let currentRows = [];
let currentEdit = null;
let currentPage = 1;

// 可编辑的表和字段（与后端白名单对应）
const EDITABLE_COLUMNS = {
  users: ['balance', 'phone'],
  tasks: ['status', 'title'],
  orders: ['status'],
  billing: ['amount', 'type'],
  feedback: ['category', 'content'],
};

function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }

function renderTableTabs() {
  $('table-tabs').innerHTML = TABLES.map(t =>
    `<button class="table-tab ${t.key === currentTable ? 'active' : ''}" data-table="${t.key}">${t.label}</button>`).join('');
  document.querySelectorAll('.table-tab').forEach(btn => btn.addEventListener('click', () => {
    currentTable = btn.dataset.table;
    currentPage = 1;
    renderTableTabs();
    loadTable();
  }));
}

async function loadTable() {
  try {
    const resp = await fetch(`/api/admin/table/${currentTable}?page=${currentPage}&page_size=20`, { headers: { 'X-Admin-Password': password } });
    if (resp.status === 401) { logout(); return; }
    const data = await resp.json();
    currentRows = data.rows || [];
    renderTable(data);
  } catch (e) {
    $('table-container').innerHTML = '<div class="table-empty">加载失败</div>';
  }
}

function renderTable(data) {
  const rows = data.rows || [];
  if (!rows.length) { $('table-container').innerHTML = '<div class="table-empty">暂无数据</div>'; return; }
  const keys = Object.keys(rows[0]);
  const editable = isAdmin ? (EDITABLE_COLUMNS[currentTable] || []) : [];
  const head = keys.map(k => `<th>${escapeHtml(k)}</th>`).join('') + (editable.length ? '<th>操作</th>' : '');
  const body = rows.map(r => '<tr>' + keys.map(k => {
    const v = r[k];
    if (k === 'url' && v) return `<td><a href="${escapeHtml(v)}" target="_blank">查看</a></td>`;
    return `<td>${escapeHtml(v)}</td>`;
  }).join('') + (editable.length ? `<td><button class="table-edit-btn" data-id="${r.id}">编辑</button></td>` : '') + '</tr>').join('');
  const pager = renderPager(data);
  $('table-container').innerHTML = `<table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${pager}`;
  document.querySelectorAll('.table-edit-btn').forEach(btn => btn.addEventListener('click', () => openEditModal(Number(btn.dataset.id))));
  document.querySelectorAll('.pager-btn').forEach(btn => btn.addEventListener('click', () => {
    currentPage = Number(btn.dataset.page);
    loadTable();
  }));
}

function renderPager(data) {
  if (data.pages <= 1) return '';
  return `<div class="pager"><span>共 ${data.total} 条 · 第 ${data.page}/${data.pages} 页</span>` +
    `<button class="pager-btn" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>上一页</button>` +
    `<button class="pager-btn" data-page="${data.page + 1}" ${data.page >= data.pages ? 'disabled' : ''}>下一页</button></div>`;
}

function openEditModal(rowId) {
  const row = currentRows.find(r => r.id === rowId);
  if (!row) return;
  const editable = EDITABLE_COLUMNS[currentTable] || [];
  currentEdit = { table: currentTable, rowId };
  $('edit-title').textContent = `编辑 ${currentTable} #${rowId}`;
  $('edit-fields').innerHTML = editable.map(col =>
    `<div class="field"><label class="field-label">${escapeHtml(col)}</label><input class="input" data-col="${escapeHtml(col)}" value="${escapeHtml(row[col] ?? '')}" /></div>`).join('');
  openModal('edit-modal');
}

async function saveEdit() {
  if (!currentEdit) return;
  const updates = {};
  $('edit-fields').querySelectorAll('input[data-col]').forEach(inp => { updates[inp.dataset.col] = inp.value; });
  try {
    const resp = await fetch(`/api/admin/table/${currentEdit.table}/${currentEdit.rowId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify({ updates }),
    });
    const data = await resp.json();
    if (!resp.ok) { alert(data.detail || '保存失败'); return; }
    closeModal('edit-modal');
    loadTable();
    refresh();
  } catch (e) { alert('保存失败'); }
}

/* ---------- 用户管理（调整余额）---------- */
async function adminAdjustBalance() {
  const phone = $('admin-phone').value.trim();
  const delta = parseFloat($('admin-delta').value);
  const msg = $('admin-msg');
  if (!phone) { msg.textContent = '请输入手机号'; return; }
  if (isNaN(delta) || delta === 0) { msg.textContent = '请输入非零的调整金额'; return; }
  try {
    const resp = await fetch('/api/admin/adjust-balance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify({ phone, delta }),
    });
    const data = await resp.json();
    if (!resp.ok) { msg.textContent = data.detail || '调整失败'; return; }
    msg.textContent = `✅ 已调整 ${phone}，当前余额 ¥${data.balance.toFixed(2)}`;
    $('admin-phone').value = '';
    $('admin-delta').value = '';
    refresh();
    loadTable();
  } catch (e) {
    msg.textContent = '请求失败，请稍后重试';
  }
}

/* ---------- B站 Cookie 更新 ---------- */
async function updateBiliCookie() {
  const cookie = $('bili-cookie').value.trim();
  const msg = $('bili-cookie-msg');
  if (!cookie) { msg.textContent = '请粘贴 cookie 内容'; return; }
  try {
    const resp = await fetch('/api/admin/bili-cookie', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify({ cookie }),
    });
    const data = await resp.json();
    if (!resp.ok) { msg.textContent = data.detail || '更新失败'; return; }
    msg.textContent = '✅ ' + data.message;
    $('bili-cookie').value = '';
  } catch (e) {
    msg.textContent = '请求失败，请稍后重试';
  }
}

/* ---------- 充值白名单管理 ---------- */
async function loadWhitelist() {
  try {
    const resp = await fetch('/api/admin/recharge-whitelist', { headers: { 'X-Admin-Password': password } });
    if (resp.status === 401) { logout(); return; }
    const rows = await resp.json();
    $('whitelist-list').innerHTML = (rows || []).map(r =>
      `<span style="background:#EEF2FF;border:1px solid #C7D2FE;border-radius:8px;padding:6px 10px;display:inline-flex;align-items:center;gap:6px;font-size:13px;">${escapeHtml(r.phone)}<button class="wl-del" data-phone="${escapeHtml(r.phone)}" style="border:none;background:transparent;color:#DC2626;cursor:pointer;font-size:14px;line-height:1;">✕</button></span>`).join('') || '<span style="color:#9CA3AF;font-size:13px;">白名单为空（充值全部禁用）</span>';
    $('whitelist-list').querySelectorAll('.wl-del').forEach(btn =>
      btn.addEventListener('click', () => removeWhitelist(btn.dataset.phone)));
  } catch (e) { /* 静默 */ }
}

async function addWhitelist() {
  const phone = $('whitelist-phone').value.trim();
  const msg = $('whitelist-msg');
  if (!/^1\d{10}$/.test(phone)) { msg.textContent = '请输入正确的手机号'; return; }
  try {
    const resp = await fetch('/api/admin/recharge-whitelist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify({ phone }),
    });
    const data = await resp.json();
    if (!resp.ok) { msg.textContent = data.detail || '添加失败'; return; }
    msg.textContent = '';
    $('whitelist-phone').value = '';
    loadWhitelist();
  } catch (e) { msg.textContent = '请求失败'; }
}

async function removeWhitelist(phone) {
  const msg = $('whitelist-msg');
  try {
    const resp = await fetch('/api/admin/recharge-whitelist/' + phone, {
      method: 'DELETE',
      headers: { 'X-Admin-Password': password },
    });
    if (!resp.ok) { const d = await resp.json(); msg.textContent = d.detail || '删除失败'; return; }
    msg.textContent = '';
    loadWhitelist();
  } catch (e) { msg.textContent = '请求失败'; }
}

/* ---------- 充值黑名单管理 ---------- */
async function loadBlacklist() {
  try {
    const resp = await fetch('/api/admin/recharge-blacklist', { headers: { 'X-Admin-Password': password } });
    if (resp.status === 401) { logout(); return; }
    const rows = await resp.json();
    $('blacklist-list').innerHTML = (rows || []).map(r =>
      `<span style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:6px 10px;display:inline-flex;align-items:center;gap:6px;font-size:13px;">${escapeHtml(r.phone)}<button class="bl-del" data-phone="${escapeHtml(r.phone)}" style="border:none;background:transparent;color:#DC2626;cursor:pointer;font-size:14px;line-height:1;">✕</button></span>`).join('') || '<span style="color:#9CA3AF;font-size:13px;">黑名单为空</span>';
    $('blacklist-list').querySelectorAll('.bl-del').forEach(btn =>
      btn.addEventListener('click', () => removeBlacklist(btn.dataset.phone)));
  } catch (e) { /* 静默 */ }
}

async function addBlacklist() {
  const phone = $('blacklist-phone').value.trim();
  const msg = $('blacklist-msg');
  if (!/^1\d{10}$/.test(phone)) { msg.textContent = '请输入正确的手机号'; return; }
  try {
    const resp = await fetch('/api/admin/recharge-blacklist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify({ phone }),
    });
    const data = await resp.json();
    if (!resp.ok) { msg.textContent = data.detail || '添加失败'; return; }
    msg.textContent = '';
    $('blacklist-phone').value = '';
    loadBlacklist();
  } catch (e) { msg.textContent = '请求失败'; }
}

async function removeBlacklist(phone) {
  const msg = $('blacklist-msg');
  try {
    const resp = await fetch('/api/admin/recharge-blacklist/' + phone, {
      method: 'DELETE',
      headers: { 'X-Admin-Password': password },
    });
    if (!resp.ok) { const d = await resp.json(); msg.textContent = d.detail || '删除失败'; return; }
    msg.textContent = '';
    loadBlacklist();
  } catch (e) { msg.textContent = '请求失败'; }
}

/* ---------- 退款 ---------- */
async function doRefund() {
  const phone = $('refund-phone').value.trim();
  const out_trade_no = $('refund-order').value.trim();
  const amountStr = $('refund-amount').value.trim();
  const msg = $('refund-msg');
  if (!phone) { msg.textContent = '请输入手机号'; return; }
  if (!out_trade_no) { msg.textContent = '请输入订单号'; return; }
  const body = { phone, out_trade_no };
  if (amountStr) {
    const amt = parseFloat(amountStr);
    if (isNaN(amt) || amt <= 0) { msg.textContent = '退款金额不合法'; return; }
    body.refund_amount = amt;
  }
  if (!confirm(`确认退款？手机号 ${phone}，订单 ${out_trade_no}${amountStr ? '，金额 ' + amountStr : '（全额）'}`)) return;
  try {
    const resp = await fetch('/api/admin/refund', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) { msg.textContent = data.detail || '退款失败'; return; }
    msg.textContent = `✅ 退款成功，用户余额 ${data.balance}`;
    $('refund-phone').value = '';
    $('refund-order').value = '';
    $('refund-amount').value = '';
    loadTable();
    refresh();
  } catch (e) { msg.textContent = '请求失败'; }
}

/* ---------- 刷新 ---------- */
async function refresh() {
  try {
    const s = await fetchStats();
    renderCards(s);
    renderTrend(s);
    renderStatus(s);
    renderSubtitle(s);
    renderFail(s);
    renderSystem(s);
    renderRechargeIntent(s);
    $('last-refresh').textContent = '更新于 ' + new Date().toLocaleTimeString();
  } catch (e) {
    if (e.message === 'unauthorized') logout();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('gate-btn').addEventListener('click', tryLogin);
  $('viewer-btn').addEventListener('click', viewerLogin);
  $('pwd-input').addEventListener('keydown', e => { if (e.key === 'Enter') tryLogin(); });
  $('logout-btn').addEventListener('click', logout);
  $('admin-adjust-btn').addEventListener('click', adminAdjustBalance);
  $('bili-cookie-btn').addEventListener('click', updateBiliCookie);
  $('whitelist-add-btn').addEventListener('click', addWhitelist);
  $('blacklist-add-btn').addEventListener('click', addBlacklist);
  $('refund-btn').addEventListener('click', doRefund);
  $('edit-save-btn').addEventListener('click', saveEdit);

  // Tab 切换
  document.querySelectorAll('.dash-tab').forEach(btn => btn.addEventListener('click', () => {
    document.querySelectorAll('.dash-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $('tab-data').style.display = tab === 'data' ? 'block' : 'none';
    $('tab-admin').style.display = tab === 'admin' ? 'block' : 'none';
  }));
  document.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.close)));
  document.querySelectorAll('.modal-overlay').forEach(ov => ov.addEventListener('click', e => { if (e.target === ov) ov.classList.remove('show'); }));
  if (password) showDash();
});
