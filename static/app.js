/* ============ 视频转笔记 前端逻辑 ============ */

const API = '/api';
const TOKEN_KEY = 'vn_token';
const PHONE_KEY = 'vn_phone';

let token = localStorage.getItem(TOKEN_KEY) || '';
let phone = localStorage.getItem(PHONE_KEY) || '';
let presets = [];
let currentPreset = '';
let balance = 0;
let parsedVideo = null;       // 当前解析的视频 {bvid,title,owner,pages}
let selectedPages = new Set(); // 选中的分P
let rechargeAmount = 30;       // 充值金额
let rechargeOrderId = null;    // 当前充值订单
let rechargePollTimer = null;  // 支付到账轮询
let pollTimer = null;
let lastTasks = [];            // 最近一次任务列表
let expandedPreview = {};      // taskId -> 预览内容

const STATUS = {
  running: { label: '转换中', color: '#D97706', bg: '#FEF3C7' },
  completed: { label: '已完成', color: '#16A34A', bg: '#DCFCE7' },
  failed: { label: '失败', color: '#DC2626', bg: '#FEE2E2' },
  cancelled: { label: '已停止', color: '#6B7280', bg: '#F3F4F6' },
  interrupted: { label: '已中断', color: '#9CA3AF', bg: '#F3F4F6' },
};

const TAG_COLORS = ['#4F46E5', '#10B981', '#F59E0B', '#EC4899', '#3B82F6', '#8B5CF6', '#14B8A6', '#F97316'];

const RECHARGE_ENABLED = false; // 测试期：禁用充值模拟支付

// mermaid 初始化（渲染流程图/逻辑图/脑图）
if (typeof mermaid !== 'undefined') {
  mermaid.initialize({ startOnLoad: false, theme: 'neutral' });
}

/* ---------- 工具 ---------- */
function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function sanitizeFilename(name) {
  return String(name || 'note').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').trim().slice(0, 100) || 'note';
}

function tasksEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i], y = b[i];
    if (x.id !== y.id || x.status !== y.status || x.message !== y.message ||
        x.cost !== y.cost || x.has_file !== y.has_file || x.updated_at !== y.updated_at) return false;
  }
  return true;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtMoney(n) { return '¥' + (Number(n) || 0).toFixed(2); }

function calcAmount(durationSeconds) {
  if (durationSeconds <= 0) return 0.6;
  return Math.ceil(durationSeconds / 900) * 0.6;
}

/* ---------- API 封装 ---------- */
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(API + path, { ...opts, headers });
  if (resp.status === 401) { doLogout(false); throw new Error('登录已过期，请重新登录'); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '请求失败');
  return data;
}

/* ---------- 登录 ---------- */
async function login() {
  const ph = $('phone-input').value.trim();
  if (!/^1\d{10}$/.test(ph)) { setLoginMsg('请输入正确的手机号'); return; }
  try {
    const r = await api('/auth/login', { method: 'POST', body: JSON.stringify({ phone: ph }) });
    token = r.token;
    phone = r.phone;
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(PHONE_KEY, phone);
    enterMain();
  } catch (e) {
    setLoginMsg(e.message);
  }
}

async function doLogout(callApi = true) {
  if (callApi && token) { try { await api('/auth/logout', { method: 'POST' }); } catch (e) {} }
  token = '';
  phone = '';
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(PHONE_KEY);
  stopPolling();
  $('login-view').classList.remove('hidden');
  $('main-view').classList.add('hidden');
}

function setLoginMsg(m) { $('login-msg').textContent = m; }

/* ---------- 进入主界面 ---------- */
function enterMain() {
  $('login-view').classList.add('hidden');
  $('main-view').classList.remove('hidden');
  $('avatar').textContent = phone.slice(-2);
  $('topbar-phone').textContent = phone.slice(0, 3) + '****' + phone.slice(-4);
  loadPresets().then(() => refreshTasks());
  startPolling();
}

/* ---------- 标签（pills + 管理）---------- */
async function loadPresets() {
  presets = await api('/presets');
  if (!presets.length) presets = [{ name: '金融分析', prompt: '' }];
  if (!presets.some(p => p.name === currentPreset)) currentPreset = presets[0].name;
  renderPresetSelector();
}

function renderPresetSelector() {
  const container = $('preset-selector');
  container.innerHTML = presets.map((p, i) => {
    const color = TAG_COLORS[i % TAG_COLORS.length];
    const active = p.name === currentPreset ? 'active' : '';
    const def = i === 0 ? '<span class="t-default">默认</span>' : '';
    return `<span class="tag-pill ${active}" data-name="${escapeHtml(p.name)}">
      <span class="t-dot" style="background:${color}"></span>${escapeHtml(p.name)}${def}
    </span>`;
  }).join('');
  container.querySelectorAll('.tag-pill').forEach(el => el.addEventListener('click', () => {
    currentPreset = el.dataset.name;
    renderPresetSelector();
  }));
}

async function openTagsModal() {
  renderTagManager();
  $('new-tag-form').classList.add('hidden');
  openModal('tags-modal');
}

function renderTagManager() {
  const list = $('mgr-list');
  list.innerHTML = presets.map((p, i) => `
    <div class="card mgr-card" data-idx="${i}">
      <div class="mgr-head">
        <span class="t-dot" style="width:8px;height:8px;border-radius:50%;background:${TAG_COLORS[i % TAG_COLORS.length]}"></span>
        <span class="name">${escapeHtml(p.name)}</span>
        ${i === 0 ? '<span class="def-badge">默认</span>' : ''}
        <span class="spacer"></span>
        <button class="icon-btn" data-del="${i}" title="删除">🗑</button>
      </div>
      <textarea class="input" rows="3" data-prompt="${i}">${escapeHtml(p.prompt)}</textarea>
    </div>`).join('');
  list.querySelectorAll('[data-del]').forEach(btn => btn.addEventListener('click', () => deletePreset(Number(btn.dataset.del))));
  list.querySelectorAll('[data-prompt]').forEach(ta => ta.addEventListener('change', () => savePreset(Number(ta.dataset.prompt), ta.value)));
}

async function savePreset(idx, prompt) {
  const p = presets[idx];
  if (!p) return;
  try {
    presets = await api('/presets/save', { method: 'POST', body: JSON.stringify({ selected: p.name, name: p.name, prompt }) });
    renderTagManager();
  } catch (e) { alert(e.message); }
}

async function createNewTag() {
  const name = $('new-tag-name').value.trim();
  const prompt = $('new-tag-prompt').value.trim();
  if (!name || !prompt) { alert('请填写标签名称和提示词'); return; }
  try {
    presets = await api('/presets/save', { method: 'POST', body: JSON.stringify({ selected: '', name, prompt }) });
    $('new-tag-name').value = '';
    $('new-tag-prompt').value = '';
    $('new-tag-form').classList.add('hidden');
    renderTagManager();
    renderPresetSelector();
  } catch (e) { alert(e.message); }
}

async function deletePreset(idx) {
  const p = presets[idx];
  if (!p) return;
  try {
    presets = await api('/presets/delete', { method: 'POST', body: JSON.stringify({ name: p.name }) });
    if (!presets.some(x => x.name === currentPreset)) currentPreset = presets[0].name;
    renderTagManager();
    renderPresetSelector();
  } catch (e) { alert(e.message); }
}

/* ---------- 转换流程 ---------- */
async function startConvert() {
  const url = $('url-input').value.trim();
  if (!url) { alert('请先粘贴视频链接'); return; }
  try {
    parsedVideo = await api('/convert/parse', { method: 'POST', body: JSON.stringify({ url }) });
    selectedPages = new Set(parsedVideo.pages.map(p => p.page));
    $('confirm-title').textContent = parsedVideo.title;
    renderEpList();
    updateEstimate();
    openModal('confirm-modal');
  } catch (e) {
    alert(e.message);
  }
}

function renderEpList() {
  const list = $('ep-list');
  list.innerHTML = parsedVideo.pages.map(p => `
    <div class="ep-item ${selectedPages.has(p.page) ? 'checked' : ''}" data-page="${p.page}">
      <span class="num">P${p.page}</span>
      <span class="t">${escapeHtml(p.title)}</span>
      <span class="time">${Math.floor(p.duration / 60)}:${String(p.duration % 60).padStart(2, '0')}</span>
    </div>`).join('');
  list.querySelectorAll('.ep-item').forEach(el => el.addEventListener('click', () => {
    const page = Number(el.dataset.page);
    if (selectedPages.has(page)) selectedPages.delete(page); else selectedPages.add(page);
    el.classList.toggle('checked', selectedPages.has(page));
    updateEstimate();
  }));
}

function updateEstimate() {
  const sel = parsedVideo.pages.filter(p => selectedPages.has(p.page));
  const dur = sel.reduce((s, p) => s + p.duration, 0);
  const amount = calcAmount(dur);
  $('selected-count').textContent = `已选 ${sel.length} / ${parsedVideo.pages.length}`;
  $('est-balance').textContent = fmtMoney(balance);
  $('est-amount').textContent = '−' + fmtMoney(amount);
  $('est-after').textContent = fmtMoney(balance - amount);
}

async function confirmConvert() {
  if (!parsedVideo || selectedPages.size === 0) { alert('请至少选择一个分P'); return; }
  try {
    const r = await api('/convert/start', {
      method: 'POST',
      body: JSON.stringify({ url: parsedVideo.bvid, pages: [...selectedPages], preset: currentPreset }),
    });
    closeModal('confirm-modal');
    parsedVideo = null;
    if (r.cached) {
      alert('已复用之前的笔记，不重复扣费');
    }
    refreshTasks();
  } catch (e) {
    alert(e.message);
  }
}

async function stopTask(id) {
  try { await api('/task/' + id + '/stop', { method: 'POST' }); refreshTasks(); }
  catch (e) { alert(e.message); }
}

async function renderNoteHtml(content) {
  // 渲染 markdown + mermaid 到 HTML 字符串（下载用）
  const tmp = document.createElement('div');
  tmp.innerHTML = marked.parse(content || '');
  const codes = tmp.querySelectorAll('pre code.language-mermaid');
  for (const code of codes) {
    try {
      const { svg } = await mermaid.render('dl-' + Math.random().toString(36).slice(2, 10), code.textContent.trim());
      const wrap = document.createElement('div');
      wrap.className = 'mermaid-diagram';
      wrap.innerHTML = svg;
      code.parentElement.replaceWith(wrap);
    } catch (e) { /* 渲染失败保留原始代码 */ }
  }
  return tmp.innerHTML;
}

async function downloadTask(id, title) {
  try {
    const r = await api('/task/' + id + '/preview');
    const body = await renderNoteHtml(r.content || '');
    const fullHtml = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>${escapeHtml(title)}</title>
<style>
body { max-width: 800px; margin: 0 auto; padding: 32px 20px; font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif; line-height: 1.8; color: #1A1A2E; }
h1 { font-size: 26px; border-bottom: 2px solid #eee; padding-bottom: 10px; }
h2 { font-size: 20px; margin-top: 28px; }
h3 { font-size: 17px; margin-top: 20px; }
strong { font-weight: 700; }
ul, ol { padding-left: 24px; }
li { margin: 4px 0; }
code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
pre { background: #f8f8f8; padding: 14px; border-radius: 8px; overflow-x: auto; }
.mermaid-diagram { text-align: center; margin: 20px 0; }
.mermaid-diagram svg { max-width: 100%; height: auto; }
blockquote { border-left: 3px solid #ccc; margin: 12px 0; padding: 6px 14px; color: #555; background: #fafafa; }
hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
th { background: #f5f5f5; }
</style>
</head>
<body>${body}</body>
</html>`;
    const blob = new Blob([fullHtml], { type: 'text/html;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = sanitizeFilename(title) + '.html';
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { alert(e.message); }
}

async function togglePreview(id) {
  if (id in expandedPreview) {
    delete expandedPreview[id];
  } else {
    try {
      const r = await api('/task/' + id + '/preview');
      expandedPreview[id] = marked.parse(r.content || '');
    } catch (e) { alert(e.message); return; }
  }
  renderTasks(lastTasks);
}

/* ---------- 任务列表 ---------- */
async function refreshTasks() {
  try {
    const r = await api('/tasks');
    balance = r.balance;
    $('topbar-balance').textContent = balance.toFixed(2);
    $('balance-amt').textContent = fmtMoney(balance);
    if (!tasksEqual(r.tasks || [], lastTasks)) {
      renderTasks(r.tasks || []);
    }
  } catch (e) { /* 轮询静默失败 */ }
}

function renderTasks(tasks) {
  lastTasks = tasks;
  const list = $('task-list');
  if (!tasks.length) { list.innerHTML = '<div class="empty">暂无转换任务</div>'; return; }
  list.innerHTML = tasks.map(t => {
    const s = STATUS[t.status] || STATUS.interrupted;
    const badge = `<span class="badge" style="background:${s.bg};color:${s.color};">${s.label}</span>`;
    const meta = [];
    if (t.message) meta.push(`<span>${escapeHtml(t.message)}</span>`);
    let body = `
      <div class="task-top">${badge}<span class="task-time">${fmtTime(t.created_at)}</span></div>
      <div class="task-title">${escapeHtml(t.title)}</div>`;
    if (t.status === 'running' || t.status === 'interrupted') body += `<div class="progress"><i style="width:60%"></i></div>`;
    if (meta.length) body += `<div class="task-meta">${meta.join(' · ')}</div>`;
    if (t.status === 'failed') body += `<div class="reason"><span>${escapeHtml(t.message)}</span></div>`;
    const actions = [];
    if (t.status === 'running') actions.push(`<button class="btn btn-danger btn-sm" data-stop="${t.id}">停止</button>`);
    if (t.has_file) {
      actions.push(`<button class="btn btn-ghost btn-sm" data-preview="${t.id}">${t.id in expandedPreview ? '收起预览' : '预览'}</button>`);
      actions.push(`<button class="btn btn-primary btn-sm" data-download="${t.id}">下载文档</button>`);
    }
    if (actions.length) body += `<div class="task-actions">${actions.join('')}</div>`;
    if (t.id in expandedPreview) body += `<div class="preview-box markdown-body">${expandedPreview[t.id]}</div>`;
    return `<div class="card task-card" data-id="${t.id}">${body}</div>`;
  }).join('');
  list.querySelectorAll('[data-stop]').forEach(b => b.addEventListener('click', () => stopTask(b.dataset.stop)));
  list.querySelectorAll('[data-download]').forEach(b => b.addEventListener('click', () => {
    const t = tasks.find(x => x.id == b.dataset.download);
    downloadTask(b.dataset.download, t ? t.title : '');
  }));
  list.querySelectorAll('[data-preview]').forEach(b => b.addEventListener('click', () => togglePreview(b.dataset.preview)));
  renderMermaidBlocks(list);
}

async function renderMermaidBlocks(container) {
  if (typeof mermaid === 'undefined') return;
  const codes = container.querySelectorAll('pre code.language-mermaid');
  for (const code of codes) {
    const src = code.textContent.trim();
    if (!src) continue;
    try {
      const id = 'mmd-' + Math.random().toString(36).slice(2, 10);
      const { svg } = await mermaid.render(id, src);
      const wrap = document.createElement('div');
      wrap.className = 'mermaid-diagram';
      wrap.innerHTML = svg;
      code.parentElement.replaceWith(wrap);
    } catch (e) {
      // 渲染失败，保留原始代码块
    }
  }
}

/* ---------- 充值 ---------- */
async function trackEvent(type) {
  if (!token) return;
  try { await api('/event', { method: 'POST', body: JSON.stringify({ type }) }); } catch (e) { /* 静默 */ }
}

function openRecharge() {
  trackEvent('recharge_click');
  rechargeAmount = 30;
  rechargeOrderId = null;
  stopRechargePoll();
  $('recharge-msg').textContent = '';
  hideRechargeQr();
  updateRechargeUI();
  openModal('recharge-modal');
}

function updateRechargeUI() {
  document.querySelectorAll('.amt').forEach(el => {
    const a = Number(el.dataset.amount);
    el.classList.toggle('active', a === rechargeAmount);
  });
  if (RECHARGE_ENABLED) {
    $('pay-btn').textContent = `去支付 ${fmtMoney(rechargeAmount)}`;
    $('pay-btn').disabled = false;
  } else {
    $('pay-btn').textContent = '测试期间暂不开放充值';
    $('pay-btn').disabled = true;
  }
}

function showRechargeQr(dataUrl) {
  $('recharge-qr-img').src = dataUrl;
  $('recharge-qr').classList.remove('hidden');
}

function hideRechargeQr() {
  $('recharge-qr').classList.add('hidden');
  $('recharge-qr-img').src = '';
}

function stopRechargePoll() {
  if (rechargePollTimer) { clearInterval(rechargePollTimer); rechargePollTimer = null; }
}

function pollOrderStatus() {
  stopRechargePoll();
  rechargePollTimer = setInterval(async () => {
    try {
      const r = await api('/recharge/order/' + rechargeOrderId);
      if (r.status === 'paid') {
        stopRechargePoll();
        hideRechargeQr();
        $('recharge-msg').textContent = `充值成功，当前余额 ${fmtMoney(r.balance)}`;
        rechargeOrderId = null;
        refreshTasks();
      }
    } catch (e) { /* 轮询静默失败 */ }
  }, 2000);
}

async function pay() {
  if (!rechargeOrderId) {
    try {
      const r = await api('/recharge/order', { method: 'POST', body: JSON.stringify({ amount: rechargeAmount }) });
      rechargeOrderId = r.order_id;
      // 已配置支付宝 → 展示二维码并轮询到账；未配置 → 走模拟支付兜底
      if (r.qr_data_url) {
        showRechargeQr(r.qr_data_url);
        $('recharge-msg').textContent = '请使用支付宝扫码支付';
        pollOrderStatus();
        return;
      }
      $('recharge-msg').textContent = `订单已创建：${r.out_trade_no}`;
    } catch (e) { $('recharge-msg').textContent = e.message; return; }
  }
  try {
    const r = await api('/recharge/simulate', { method: 'POST', body: JSON.stringify({ order_id: rechargeOrderId }) });
    $('recharge-msg').textContent = `充值成功，当前余额 ${fmtMoney(r.balance)}`;
    rechargeOrderId = null;
    refreshTasks();
  } catch (e) { $('recharge-msg').textContent = e.message; }
}

/* ---------- 弹窗 ---------- */
function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }

async function openLegal(type) {
  const titles = { terms: '用户协议', privacy: '隐私政策' };
  $('legal-title').textContent = titles[type] || '文档';
  $('legal-content').innerHTML = '<p>加载中...</p>';
  openModal('legal-modal');
  try {
    const r = await api('/docs/' + type);
    $('legal-content').innerHTML = marked.parse(r.content || '');
  } catch (e) {
    $('legal-content').innerHTML = '<p>加载失败</p>';
  }
}

function openFeedback() {
  $('fb-content').value = '';
  $('fb-category').value = '问题反馈';
  openModal('feedback-modal');
}

async function submitFeedback() {
  const content = $('fb-content').value.trim();
  const category = $('fb-category').value;
  if (!content) { alert('请填写反馈内容'); return; }
  try {
    await api('/feedback', { method: 'POST', body: JSON.stringify({ content, category }) });
    closeModal('feedback-modal');
    alert('反馈已提交，感谢你的建议！');
  } catch (e) {
    alert(e.message);
  }
}

/* ---------- 轮询 ---------- */
function startPolling() { stopPolling(); pollTimer = setInterval(refreshTasks, 5000); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ---------- 事件绑定 ---------- */
document.addEventListener('DOMContentLoaded', () => {
  $('login-btn').addEventListener('click', login);
  $('logout-btn').addEventListener('click', () => doLogout(true));
  $('start-btn').addEventListener('click', startConvert);
  $('confirm-convert-btn').addEventListener('click', confirmConvert);
  $('recharge-btn').addEventListener('click', openRecharge);
  $('pay-btn').addEventListener('click', pay);
  $('manage-tags-btn').addEventListener('click', openTagsModal);
  $('new-tag-btn').addEventListener('click', () => $('new-tag-form').classList.toggle('hidden'));
  $('cancel-new-tag').addEventListener('click', () => $('new-tag-form').classList.add('hidden'));
  $('save-new-tag').addEventListener('click', createNewTag);
  $('terms-link').addEventListener('click', e => { e.preventDefault(); openLegal('terms'); });
  $('privacy-link').addEventListener('click', e => { e.preventDefault(); openLegal('privacy'); });
  $('footer-terms-link').addEventListener('click', e => { e.preventDefault(); openLegal('terms'); });
  $('footer-privacy-link').addEventListener('click', e => { e.preventDefault(); openLegal('privacy'); });
  $('feedback-link').addEventListener('click', e => { e.preventDefault(); openFeedback(); });
  $('fb-submit').addEventListener('click', submitFeedback);

  // 充值金额选择
  document.querySelectorAll('.amt').forEach(el => el.addEventListener('click', () => {
    rechargeAmount = Number(el.dataset.amount);
    rechargeOrderId = null;
    stopRechargePoll();
    hideRechargeQr();
    $('recharge-msg').textContent = '';
    updateRechargeUI();
  }));

  // 关闭弹窗
  document.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.close)));
  document.querySelectorAll('.modal-overlay').forEach(ov => ov.addEventListener('click', e => {
    if (e.target === ov) ov.classList.remove('show');
  }));

  // 回车触发登录/转换
  $('phone-input').addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
  $('url-input').addEventListener('keydown', e => { if (e.key === 'Enter') startConvert(); });

  // 已有 token → 直接进主界面
  if (token) enterMain();
});
