/* 视频转笔记 · 监控看板逻辑 */

const ADMIN_KEY = 'vn_admin_pwd';
let password = sessionStorage.getItem(ADMIN_KEY) || '';
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
    await fetchStats();
    sessionStorage.setItem(ADMIN_KEY, password);
    showDash();
  } catch (e) {
    $('gate-err').textContent = e.message === 'unauthorized' ? '密码错误' : '请求失败，请稍后重试';
  }
}

function logout() {
  sessionStorage.removeItem(ADMIN_KEY);
  location.reload();
}

function showDash() {
  $('gate').style.display = 'none';
  $('dash').style.display = 'block';
  initCharts();
  renderTableTabs();
  loadTable();
  refresh();
  setInterval(refresh, 20000);
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
    renderTableTabs();
    loadTable();
  }));
}

async function loadTable() {
  try {
    const resp = await fetch('/api/admin/table/' + currentTable, { headers: { 'X-Admin-Password': password } });
    if (resp.status === 401) { logout(); return; }
    const rows = await resp.json();
    currentRows = rows;
    renderTable(rows);
  } catch (e) {
    $('table-container').innerHTML = '<div class="table-empty">加载失败</div>';
  }
}

function renderTable(rows) {
  if (!rows || !rows.length) { $('table-container').innerHTML = '<div class="table-empty">暂无数据</div>'; return; }
  const keys = Object.keys(rows[0]);
  const editable = EDITABLE_COLUMNS[currentTable] || [];
  const head = keys.map(k => `<th>${escapeHtml(k)}</th>`).join('') + (editable.length ? '<th>操作</th>' : '');
  const body = rows.map(r => '<tr>' + keys.map(k => {
    const v = r[k];
    if (k === 'url' && v) return `<td><a href="${escapeHtml(v)}" target="_blank">查看</a></td>`;
    return `<td>${escapeHtml(v)}</td>`;
  }).join('') + (editable.length ? `<td><button class="table-edit-btn" data-id="${r.id}">编辑</button></td>` : '') + '</tr>').join('');
  $('table-container').innerHTML = `<table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  document.querySelectorAll('.table-edit-btn').forEach(btn => btn.addEventListener('click', () => openEditModal(Number(btn.dataset.id))));
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
  $('pwd-input').addEventListener('keydown', e => { if (e.key === 'Enter') tryLogin(); });
  $('logout-btn').addEventListener('click', logout);
  $('admin-adjust-btn').addEventListener('click', adminAdjustBalance);
  $('edit-save-btn').addEventListener('click', saveEdit);
  document.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.close)));
  document.querySelectorAll('.modal-overlay').forEach(ov => ov.addEventListener('click', e => { if (e.target === ov) ov.classList.remove('show'); }));
  if (password) showDash();
});
