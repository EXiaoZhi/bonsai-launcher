/* ════════════════════════════════════════════════════════════════
   app.js · 本地 LLM 网关聊天前端（零构建 / 零依赖 / 原生 ES Module）
   契约：docs/api-contract.md（唯一权威）
   行为：docs/design/ux-spec.md  视觉：docs/design/visual-direction.md
   ════════════════════════════════════════════════════════════════ */

import { renderInto } from './markdown.js';

/* ─────────────────────────── 0. 小工具 ─────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function icon(id, size = 16) {
  const s = document.createElementNS(SVG_NS, 'svg');
  s.setAttribute('class', 'icon');
  s.setAttribute('width', size);
  s.setAttribute('height', size);
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVG_NS, 'use');
  u.setAttribute('href', '#' + id);
  s.appendChild(u);
  return s;
}

const EFFORT_LABEL = { none: '关', low: '低', medium: '中', high: '高' };
// 消息头部用完整说法，和启动器一致（选择器里的芯片仍用单字，省空间）
const EFFORT_FULL = { none: '直答', low: '轻度思考', medium: '标准思考', high: '深度思考' };
const EFFORT_ORDER = ['none', 'low', 'medium', 'high'];
const effortLabel = (e) => EFFORT_LABEL[e] || e || '—';

const fmtNum = (n) => Number(n || 0).toLocaleString('zh-CN');
const clamp = (n, a, b) => Math.min(b, Math.max(a, n));
const nowSec = () => Math.floor(Date.now() / 1000);
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function hhmm(ts) {
  const d = new Date((ts || nowSec()) * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function relTime(ts) {
  const diff = nowSec() - (ts || 0);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  const d = new Date((ts || 0) * 1000);
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

/* ─────────────────────────── 1. 状态 ─────────────────────────── */

const S = {
  authed: false,
  models: [],
  selMode: false,          // 会话多选模式
  sel: new Set(),          // 已选会话 id
  modelId: null,
  modelSig: '',
  compress: false,          // 本会话是否已开启上下文优化（手动）
  effort: 'none',
  sessions: [],
  session: null,                 // 已保存的会话对象；null = 草稿态
  messages: [],
  usage: { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0 },
  tokensAfter: null,
  lastCost: null,                // {ms, tok}
  gen: null,                     // {ac, msg, t0}
  pendingModel: null,
  follow: true,
  newCount: 0,
  snippets: [],
  blocks: [],
  blockCache: new Map(),
  expandLimit: null,             // 由 expand_limit_exceeded.limit 学习，不写死
  selectedBlocks: new Set(),
  pendingExpand: [],
  editing: null,                 // {index, text} 编辑上一条
  layers: [],                    // 浮层栈（Esc 逐层）
  renaming: null,                // 正在重命名的元素
  sheetH: '88vh',
  drawerOpen: false,
  drawerTab: 'config',
  overflowRetried: false,
  rateFails: 0,
  focusSessionId: null,
  unreadLog: false,
  budget: null,
  lastUserText: '',
};

const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 忽略 */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* 忽略 */ } },
};

/* DOM 引用 */
const D = {};
function bindDom() {
  const ids = ['login', 'loginForm', 'tokenInput', 'loginError', 'loginSubmit', 'app', 'boot', 'netBanner',
    'sessionTitle', 'sessionTotal', 'effortTop', 'modelBtn', 'modelLabel', 'modelDot', 'themeBtn', 'copyAllBtn', 'exportBtn',
    'drawerBtn', 'sidebar', 'sessionList', 'newSessionBtn', 'searchInput', 'searchClear', 'snippetBtn',
    'selModeBtn', 'selBar', 'selCount', 'selAllBtn', 'selDelBtn', 'selCancelBtn',
    'compressBtn', 'compressNote',
    'railToggle', 'railSearch', 'railSnippets', 'layout', 'mainPane', 'chatTitle', 'chatMeta',
    'scrollWrap', 'scroller', 'msgList', 'jumpPill', 'jumpPillText', 'composer', 'editBar', 'editCancel',
    'input', 'snippetInline', 'saveSnippetBtn', 'charCount', 'sendBtn', 'composerHint', 'statusBar',
    'connDot', 'connText', 'budgetItem', 'costItem', 'vramNote', 'logBtn', 'logUnread', 'drawer', 'drawerGrip',
    'drawerClose', 'drawerBody', 'tabConfig', 'tabBlocks', 'tabLogs', 'paneConfig', 'paneLogs',
    'modelRow', 'modelRowLabel', 'modelRowSub', 'effortConfig', 'effortNote', 'samplingList',
    'contextFill', 'contextText', 'contextWindow', 'blocksSection', 'blocksRefresh', 'blocksHint',
    'blockList', 'expandBtn', 'logLevels', 'logFollow', 'logFilter', 'logRegex', 'logFilterErr',
    'logTagChips', 'logList', 'logPill', 'logPillText', 'logStatus', 'logCopy', 'modelOverlay',
    'modelPop', 'modelList', 'modelClose', 'snippetOverlay', 'snipSearch', 'snipList', 'snipDelete',
    'peekOverlay', 'peekTitle', 'peekWarn', 'peekBody', 'peekOk', 'peekClose', 'varsOverlay',
    'varsTitle', 'varsBody', 'varsError', 'varsSkip', 'varsOk', 'varsCancel', 'confirmOverlay',
    'cfTitle', 'cfBody', 'cfOk', 'cfCancel', 'promptOverlay', 'pmTitle', 'pmInput', 'pmError',
    'pmOk', 'pmCancel', 'helpOverlay', 'helpClose', 'toasts'];
  for (const id of ids) D[id] = document.getElementById(id);
}

/* ─────────────────────────── 2. HTTP ─────────────────────────── */

class ApiError extends Error {
  constructor(status, payload) {
    const p = payload || {};
    const code = p.code || (p.error && p.error.code) || `http_${status}`;
    const message = p.message || (p.error && p.error.message) || code;
    super(message);
    this.status = status;
    this.code = code;
    this.payload = p;
  }
}

async function api(path, { method = 'GET', body, signal, skipAuthRedirect = false } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      signal,
      credentials: 'same-origin',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, { code: 'network_error', message: '无法连接网关' });
  }

  let data = null;
  const ctype = res.headers.get('content-type') || '';
  if (ctype.includes('json')) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (res.status === 401) {
    if (!skipAuthRedirect) requireLogin();
    throw new ApiError(401, data || { code: 'unauthorized' });
  }
  if (!res.ok) throw new ApiError(res.status, data || {});
  return data || {};
}

/* ─────────────────────────── 3. Toast / 确认 / 输入 ─────────────────────────── */

function toast(text, kind = '') {
  const t = el('div', 'toast' + (kind ? ` is-${kind}` : ''), text);
  D.toasts.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 160ms'; }, 2400);
  setTimeout(() => t.remove(), 2650);
}

function openLayer(id) {
  const node = document.getElementById(id);
  if (!node) return;
  node.hidden = false;
  if (!S.layers.includes(id)) S.layers.push(id);
}
function closeLayer(id) {
  const node = document.getElementById(id);
  if (node) node.hidden = true;
  S.layers = S.layers.filter((x) => x !== id);
}
function closeTopLayer() {
  const id = S.layers[S.layers.length - 1];
  if (!id) return false;
  if (id === 'modelOverlay') D.modelBtn.setAttribute('aria-expanded', 'false');
  closeLayer(id);
  if (id === 'snippetOverlay') D.input.focus();
  return true;
}

let confirmResolve = null;
function confirmDialog({ title, text, okText = '确定', cancelText = '取消', danger = false, focus = 'cancel' }) {
  if (confirmResolve) { const prev = confirmResolve; confirmResolve = null; prev(false); } // 防重入泄漏
  D.cfTitle.textContent = title;
  D.cfBody.textContent = text;
  D.cfOk.textContent = okText;
  D.cfCancel.textContent = cancelText;
  D.cfOk.className = 'btn btn--sm ' + (danger ? 'btn--danger' : 'btn--primary');
  openLayer('confirmOverlay');
  (focus === 'ok' ? D.cfOk : D.cfCancel).focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function resolveConfirm(v) {
  closeLayer('confirmOverlay');
  const fn = confirmResolve;
  confirmResolve = null;
  if (fn) fn(v);
}

let promptResolve = null;
function promptDialog({ title, value = '', okText = '保存' }) {
  D.pmTitle.textContent = title;
  D.pmInput.value = value;
  D.pmError.hidden = true;
  D.pmOk.textContent = okText;
  openLayer('promptOverlay');
  D.pmInput.focus();
  D.pmInput.select();
  return new Promise((resolve) => { promptResolve = resolve; });
}
function resolvePrompt(ok) {
  const v = D.pmInput.value.trim();
  if (ok && !v) {
    D.pmError.hidden = false;
    D.pmError.textContent = '标题不能为空';
    D.pmInput.focus();
    return;
  }
  closeLayer('promptOverlay');
  const fn = promptResolve;
  promptResolve = null;
  if (fn) fn(ok ? v : null);
}

/* ─────────────────────────── 4. 登录 ─────────────────────────── */

function requireLogin(reason) {
  S.authed = false;
  if (D.boot) D.boot.hidden = true;
  D.app.hidden = true;
  D.login.hidden = false;
  D.loginError.hidden = !reason;
  if (reason) D.loginError.textContent = reason;
  stopGeneration({ silent: true });
  setTimeout(() => D.tokenInput.focus(), 30);
}

async function tryLogin(token) {
  D.loginError.hidden = true;
  D.loginSubmit.setAttribute('aria-disabled', 'true');
  try {
    await api('/api/login', { method: 'POST', body: { token }, skipAuthRedirect: true });
    await enterApp();
  } catch (err) {
    D.loginError.hidden = false;
    D.loginError.textContent = err.status === 401
      ? '令牌不正确。请检查 gateway/data/token.txt 或环境变量 BONSAI_TOKEN。'
      : (err.code === 'network_error'
        ? '无法连接 launcher（127.0.0.1:18300）。请确认 launcher 已启动，且端口未被占用。'
        : `登录失败：${err.code}`);
    D.tokenInput.select();
  } finally {
    D.loginSubmit.removeAttribute('aria-disabled');
  }
}

/* ─────────────────────────── 5. 启动 ─────────────────────────── */

async function boot() {
  bindDom();
  applyTheme(LS.get('bonsai.theme', 'light'));
  bindGlobalEvents();
  try {
    await api('/api/me', { skipAuthRedirect: true });
    await enterApp();
  } catch (err) {
    requireLogin(err.status === 401 ? '' : '无法连接 launcher。请确认已启动（127.0.0.1:18300/chat）。');
  }
}

async function enterApp() {
  S.authed = true;
  if (D.boot) D.boot.hidden = true;
  D.login.hidden = true;
  D.app.hidden = false;
  renderDraftState();
  renderMessages();
  await Promise.all([loadModels(), loadSessions(), loadSnippets()]);
  restoreDraft();
  renderEfforts();
  renderModelPill();
  renderConfigMeta();
  updateUsageUi();
  startModelPoll();
  D.input.focus();
}

/* ─────────────────────────── 6. 模型 ─────────────────────────── */

const modelById = (id) => S.models.find((m) => m.id === id) || null;
const activeModel = () => modelById(S.modelId);
const effortsOf = (m) => (m && m.thinking && Array.isArray(m.thinking.efforts) ? m.thinking.efforts : []);

/* 思考强度默认沿用「上次选的那个」，不必每个对话重选一次。
   换到不支持的模型时才回落到该模型自己的默认档。 */
const EFFORT_KEY = 'bonsai.effort';
function savedEffort(m) {
  const e = LS.get(EFFORT_KEY, null);
  const list = effortsOf(m);
  if (e && list.includes(e)) return e;
  if (!m || !m.thinking || !m.thinking.supported) return 'none';
  return m.thinking.default || list[0] || 'none';
}
function rememberEffort(e) {
  if (e) LS.set(EFFORT_KEY, e);
}

async function loadModels(quiet) {
  if (!quiet) {
    D.modelBtn.setAttribute('aria-busy', 'true');
    renderModelSkeleton();
  }
  let changed = true;
  try {
    const data = await api('/api/models');
    const models = data.models || [];
    const sig = models.map((m) => m.id).join('|');
    changed = sig !== (S.modelSig || '');
    S.models = models;
    S.modelSig = sig;
    if (models.length) {
      // 原来选的模型被停掉了 → 自动落到第一个还在跑的，避免选了不存在的模型
      if (!modelById(S.modelId)) {
        const first = models.find((m) => m.enabled) || models[0];
        S.modelId = first.id;
        S.effort = savedEffort(first);
      }
    } else {
      S.modelId = null;
    }
    if (quiet && !changed) {
      D.modelBtn.removeAttribute('aria-busy');
      return;                       // 列表没变化就不重绘，避免打断正在看的弹层
    }
  } catch (err) {
    S.models = [];
    S.modelSig = '';
    if (!quiet) toast(`模型列表加载失败（${err.code}）`, 'error');
  }
  D.modelBtn.removeAttribute('aria-busy');
  renderModelList();
  renderModelPill();
  renderEfforts();
  updateSendState();
}

/* 后台轻量轮询：在启动器里新起/停掉模型后，聊天页几秒内自动跟上。
   （以前只有空列表才重试，所以"启动了 9B 但列表里没有"会一直卡着） */
let modelPollTimer = 0;
function startModelPoll() {
  if (modelPollTimer) return;
  modelPollTimer = setInterval(() => {
    if (S.gen || document.visibilityState === 'hidden' || !S.authed) return;
    loadModels(true);
  }, 6000);
}

/** 模型列表加载中：3 行骨架（ux §6），避免「加载模型…」两秒钟的空窗 */
function renderModelSkeleton() {
  if (!D.modelList) return;
  D.modelList.textContent = '';
  for (let i = 0; i < 3; i++) {
    const row = el('div', 'mitem');
    row.setAttribute('aria-hidden', 'true');
    const main = el('span', 'mitem__main');
    const l1 = el('span', 'sk-line');
    l1.style.width = '58%';
    const l2 = el('span', 'sk-line');
    l2.style.width = '38%';
    main.append(l1, l2);
    row.appendChild(main);
    D.modelList.appendChild(row);
  }
}

function renderModelPill() {
  const m = activeModel();
  D.modelLabel.textContent = m ? m.label || m.id : '无模型（未启动）';
  const offline = m && m.online === false;
  D.modelDot.hidden = !offline;
  D.modelBtn.classList.toggle('is-unavailable', !!offline);
  D.modelRowLabel.textContent = m ? m.label || m.id : '—';
  D.modelRowSub.textContent = m ? (m.subtitle || m.quant || m.id) : '模型列表为空';
}

function renderModelList() {
  D.modelList.textContent = '';
  if (!S.models.length) {
    D.modelList.appendChild(el('p', 'listempty', '还没有启动任何模型。回到启动器面板点「▶ 启动服务」，就绪后这里会自动出现。'));
    return;
  }
  for (const m of S.models) {
    const efforts = effortsOf(m);
    // 右侧能力灰字始终给能力真值；「未加载」由 label 内的 tag + 置灰承担，
    // 否则一屏全是「未加载」，用户看不出哪个模型有 4 档思考。
    const cap = !m.thinking || !m.thinking.supported
      ? '不支持思考'
      : `思考 ${efforts.length} 档`;
    const btn = el('button', 'mitem' + (m.id === S.modelId ? ' is-on' : '') + (m.online === false ? ' is-offline' : ''));
    btn.type = 'button';
    btn.setAttribute('role', 'option');
    btn.setAttribute('aria-selected', String(m.id === S.modelId));
    const main = el('span', 'mitem__main');
    const label = el('span', 'mitem__label', m.label || m.id);
    if (m.online === false) label.appendChild(el('span', 'tag', '未加载'));
    main.appendChild(label);
    main.appendChild(el('span', 'mitem__sub', m.subtitle || m.quant || m.id));
    btn.appendChild(main);
    btn.appendChild(el('span', 'mitem__cap', cap));
    btn.addEventListener('click', () => { closeLayer('modelOverlay'); D.modelBtn.setAttribute('aria-expanded', 'false'); switchModel(m.id); });
    D.modelList.appendChild(btn);
  }
}

function renderEfforts() {
  const m = activeModel();
  for (const host of [D.effortTop, D.effortConfig]) {
    if (!host) continue;
    host.textContent = '';
    if (!m) continue;
    const efforts = effortsOf(m);
    const unsupported = !m.thinking || !m.thinking.supported || !efforts.length;
    if (unsupported) {
      // 不支持思考：原位换单个禁用「直答」芯片（不是「关」）
      const chip = el('span', 'chip');
      chip.setAttribute('aria-disabled', 'true');
      chip.setAttribute('aria-label', '直答（该模型固定直答，不支持思考档位）');
      chip.setAttribute('aria-describedby', 'effortNote');
      chip.textContent = '直答';
      host.appendChild(chip);
      continue;
    }
    for (const e of EFFORT_ORDER.filter((x) => efforts.includes(x))) {
      const chip = el('button', 'chip' + (e === S.effort ? ' is-on' : ''));
      chip.type = 'button';
      chip.dataset.effort = e;
      chip.setAttribute('aria-pressed', String(e === S.effort));
      // 点数 = 该档在 supported_efforts 里的序位（不写死 4 档：3 档模型「高」是 2 点）
      const dots = efforts.indexOf(e);
      if (dots > 0) {
        const box = el('span', 'chip__dots');
        for (let i = 0; i < dots; i++) box.appendChild(el('i'));
        chip.appendChild(box);
      }
      chip.appendChild(document.createTextNode(effortLabel(e)));
      chip.addEventListener('click', () => {
        // 移动端顶栏是折叠芯片：点它打开底部面板选档
        if (window.innerWidth < 768 && host === D.effortTop) { openDrawer('config'); return; }
        pickEffort(e);
      });
      host.appendChild(chip);
    }
  }
  if (D.effortNote) {
    D.effortNote.textContent = !m ? ''
      : (!m.thinking || !m.thinking.supported
        ? '此模型固定直答，不支持思考档位'
        : `档位来自模型能力，共 ${effortsOf(m).length} 档（${effortsOf(m).map(effortLabel).join(' / ')}）`);
  }
  // 芯片与中栏「模型 · 档位」摘要必须同源同帧，避免出现「直答」配「高」选中的矛盾
  renderTitle();
}

async function pickEffort(effort) {
  if (effort === S.effort) return;
  if (!effortsOf(activeModel()).includes(effort)) {
    toast('该档位不在当前模型能力内', 'warn');
    return;
  }
  S.effort = effort;
  rememberEffort(effort);
  renderEfforts();
  if (S.session) {
    try { await patchSession(S.session.id, { effort }); } catch { toast('思考强度保存失败', 'error'); }
  }
}

async function switchModel(id) {
  const m = modelById(id);
  if (!m || id === S.modelId) return;
  const targetEfforts = effortsOf(m);
  const nextEffort = (!m.thinking || !m.thinking.supported) ? 'none'
    : (targetEfforts.includes(S.effort) ? S.effort : (m.thinking.default || targetEfforts[0] || 'none'));

  if (S.gen) {
    S.pendingModel = { model: id, effort: nextEffort };
    toast('当前回答生成中，切换将在本条完成后生效', 'warn');
    return;
  }

  if (targetEfforts.length && !targetEfforts.includes(S.effort) && m.thinking.supported) {
    const ok = await confirmDialog({
      title: '切换模型',
      text: `${m.label || m.id} 可用档位为 ${targetEfforts.map(effortLabel).join(' / ')}，当前「${effortLabel(S.effort)}」不可用，改为「${effortLabel(nextEffort)}」？`,
      okText: `改为「${effortLabel(nextEffort)}」并切换`,
      cancelText: '取消切换',
      focus: 'cancel',
    });
    if (!ok) return;
  }
  await applyModelEffort(id, nextEffort);
}

async function applyModelEffort(modelId, effort) {
  const prev = activeModel();
  const prevLabel = prev ? (prev.label || prev.id) : '—';
  S.modelId = modelId;
  S.effort = effort;
  rememberEffort(effort);
  renderModelPill();
  renderEfforts();  renderConfigMeta();
  updateUsageUi();
  if (S.session) {
    try { await patchSession(S.session.id, { model: modelId, effort }); } catch { toast('模型设置保存失败', 'error'); }
  }
  const m = activeModel();
  if (S.messages.length && prev && prev.id !== modelId) {
    insertDivider(`模型已切换：${prevLabel} → ${m ? (m.label || m.id) : modelId} · ${hhmm()}`);
  }
  toast(`已切换为 ${m ? (m.label || m.id) : modelId} · ${effortLabel(effort)}${effort !== 'none' ? '思考' : ''}`);
}

function insertDivider(text) {
  const bar = el('div', 'compbar');
  bar.style.background = 'transparent';
  bar.style.borderLeft = '0';
  bar.style.color = 'var(--c-ink-3)';
  bar.style.justifyContent = 'center';
  bar.style.borderTop = '1px dashed var(--c-line-2)';
  bar.style.paddingTop = '8px';
  bar.style.margin = '0 auto';
  const span = el('span', 'compbar__main', text);
  span.style.textAlign = 'center';
  bar.appendChild(span);
  D.msgList.appendChild(bar);
  if (S.follow) scrollToBottom();
}

/* 配置区只读信息 */
function renderConfigMeta() {
  const m = activeModel();
  const list = D.samplingList;
  list.textContent = '';
  const rows = [];
  if (m) {
    rows.push(['量化', m.quant || '—']);
    rows.push(['运行时', m.runtime || '—']);
    rows.push(['端点模式', m.endpoint_mode || '—']);
    rows.push(['状态', m.online === false ? '未加载' : '已就绪']);
  } else {
    rows.push(['模型', '—']);
  }
  for (const [k, v] of rows) {
    const row = el('div', 'kv__row');
    row.appendChild(el('dt', null, k));
    row.appendChild(el('dd', null, String(v)));
    list.appendChild(row);
  }
  updateContextUi();
  renderCompressBtn();
}

function contextBudget() {
  const m = activeModel();
  if (S.budget) return S.budget;
  if (!m || !m.context) return null;
  return S.effort !== 'none' && m.context.budget_thinking ? m.context.budget_thinking : m.context.budget;
}

function updateContextUi() {
  const budget = contextBudget();
  const used = S.tokensAfter != null ? S.tokensAfter : S.usage.prompt_tokens || null;
  D.contextText.textContent = used == null ? `— / ${budget ? fmtNum(budget) : '—'}` : `${fmtNum(used)} / ${budget ? fmtNum(budget) : '—'}`;
  const m = activeModel();
  D.contextWindow.textContent = m && m.context && m.context.window ? fmtNum(m.context.window) : '—';
  const pct = budget && used ? clamp((used / budget) * 100, 0, 100) : 0;
  D.contextFill.style.width = pct + '%';
  D.contextFill.classList.toggle('is-alert', pct >= 90);
}

/* ── 上下文优化：手动按钮，不再每次对话自动压缩 ── */
function renderCompressBtn() {
  const btn = D.compressBtn;
  if (!btn) return;
  const on = !!S.compress && !!S.session;
  btn.textContent = on ? '已优化 · 还原' : '优化上下文';
  btn.classList.toggle('is-on', on);
  btn.setAttribute('aria-pressed', String(on));
  btn.title = on
    ? '本对话已开启优化，点一下还原（还原后不再折叠较早消息）'
    : '把较早的消息折叠成块，腾出上下文。只对当前对话生效，不会自动执行';
  if (D.compressNote) {
    D.compressNote.textContent = on
      ? '本对话已开启优化：下次发送会把较早的消息折叠成块，原文可在「折叠块」里预览。'
      : '自动优化已关闭，需要时点输入框上方「优化上下文」。';
  }
}

async function toggleCompress() {
  if (!S.session) { toast('先发一句话，再优化上下文', 'warn'); return; }
  const btn = D.compressBtn;
  if (btn) btn.disabled = true;
  try {
    if (S.compress) {
      await api(`/api/sessions/${S.session.id}/compress`, { method: 'DELETE' });
      S.compress = false;
      toast('已关闭本对话的上下文优化');
    } else {
      const r = await api(`/api/sessions/${S.session.id}/compress`,
        { method: 'POST', body: { model: S.modelId } });
      S.compress = true;
      if (r.tokens_after != null) S.tokensAfter = r.tokens_after;
      updateUsageUi();
      if (r.saved > 0) flashTokenSavings(r.saved);
      toast(r.saved > 0
        ? `已优化：下次发送少带约 ${fmtNum(r.saved)} tokens`
        : '已开启，但这段对话还很短，暂时没有可折叠的内容');
      insertDivider(`上下文优化已开启${r.saved > 0 ? ` · 释放 ${fmtNum(r.saved)} tokens` : ''} · ${hhmm()}`);
    }
  } catch (err) {
    toast(`优化失败（${err.code || 'network'}）`, 'error');
  } finally {
    if (btn) btn.disabled = false;
    renderCompressBtn();
  }
}

/* ─────────────────────────── 7. 会话列表 ─────────────────────────── */

async function loadSessions(q = '') {
  try {
    const data = await api('/api/sessions' + (q ? `?q=${encodeURIComponent(q)}` : ''));
    S.sessions = data.sessions || [];
  } catch (err) {
    if (err.status === 401) return;
    S.sessions = [];
    D.sessionList.textContent = '';
    const wrap = el('div', 'listempty');
    wrap.appendChild(document.createTextNode('会话列表加载失败。'));
    const btn = el('button', 'linkbtn', '重试');
    btn.type = 'button';
    btn.addEventListener('click', () => loadSessions(D.searchInput.value.trim()));
    wrap.appendChild(btn);
    D.sessionList.appendChild(wrap);
    return;
  }
  renderSessionList();
}

/* 顺序完全按服务端返回；只做时间分组，不二次排序 */
function renderSessionList() {
  const host = D.sessionList;
  host.textContent = '';
  host.classList.toggle('is-selecting', !!S.selMode);
  if (D.selBar) D.selBar.hidden = !S.selMode;
  if (D.selModeBtn) D.selModeBtn.setAttribute('aria-pressed', String(!!S.selMode));
  if (D.selCount) D.selCount.textContent = '已选 ' + S.sel.size;
  if (!S.sessions.length) {
    const q = D.searchInput.value.trim();
    host.appendChild(el('p', 'listempty',
      q ? `没有匹配「${q}」的对话。` : '还没有对话。点「＋ 新对话」，或按 Alt+P 从片段库开始。'));
    return;
  }
  const pinned = [];
  const groups = new Map();
  const order = ['今天', '近 7 天', '更早'];
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const t0 = dayStart.getTime() / 1000;
  for (const s of S.sessions) {
    if (s.pinned) { pinned.push(s); continue; }
    const ts = s.updated_at || 0;
    const key = ts >= t0 ? '今天' : (ts >= t0 - 6 * 86400 ? '近 7 天' : '更早');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  if (pinned.length) addGroup(host, '置顶', pinned);
  for (const key of order) {
    const items = groups.get(key);
    if (items && items.length) addGroup(host, key, items);
  }
}

function addGroup(host, title, items) {
  const group = el('div', 'group');
  group.appendChild(el('div', 'group__title', title));
  for (const s of items) group.appendChild(sessionRow(s));
  host.appendChild(group);
}

function sessionRow(s) {
  const picked = S.selMode && S.sel.has(s.id);
  const row = el('div', 'sitem' + (S.session && S.session.id === s.id ? ' is-active' : '') + (picked ? ' is-sel' : ''));
  row.tabIndex = 0;
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', String(!!(S.session && S.session.id === s.id)));
  row.dataset.id = s.id;
  if (S.selMode) row.appendChild(el('span', 'sitem__check', ''));

  const titleLine = el('div', 'sitem__title');
  if (s.pinned) titleLine.appendChild(el('span', 'sitem__pin'));
  const titleText = el('span', null, s.title || '未命名');
  titleText.style.overflow = 'hidden';
  titleText.style.textOverflow = 'ellipsis';
  titleText.style.whiteSpace = 'nowrap';
  titleLine.appendChild(titleText);
  if (S.gen && S.gen.sessionId === s.id) titleLine.appendChild(el('span', 'sitem__gen'));
  row.appendChild(titleLine);

  const sub = el('div', 'sitem__sub', `${s.msg_count || 0} 条 · ${relTime(s.updated_at)}`);
  row.appendChild(sub);

  const actions = el('div', 'sitem__actions');
  const more = el('button', 'iconbtn');
  more.type = 'button';
  more.title = '更多操作';
  more.setAttribute('aria-label', '更多操作');
  more.appendChild(icon('i-more'));
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    openRowMenu(row, s);
  });
  actions.appendChild(more);
  if (!S.selMode) row.appendChild(actions);

  row.addEventListener('click', () => {
    if (S.selMode) { toggleSelect(s.id); return; }
    openSession(s.id);
  });
  row.addEventListener('dblclick', () => startRowRename(row, s));
  row.addEventListener('keydown', (e) => {
    const items = $$('.sitem', D.sessionList);
    const i = items.indexOf(row);
    if (e.key === 'ArrowDown') { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0]?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1]?.focus(); }
    else if (e.key === 'Enter') { e.preventDefault(); openSession(s.id); }
    else if (e.key === 'F2') { e.preventDefault(); startRowRename(row, s); }
    else if (e.key === 'Delete') { e.preventDefault(); deleteSession(s); }
    else if (e.key === 'ContextMenu') { e.preventDefault(); openRowMenu(row, s); }
  });
  row.addEventListener('focus', () => { S.focusSessionId = s.id; });
  return row;
}

let openMenu = null;
function openRowMenu(row, s) {
  if (openMenu) { openMenu.remove(); openMenu = null; }
  const menu = el('div', 'sitem__menu');
  const mk = (label, fn, danger) => {
    const b = el('button', danger ? 'is-danger' : null, label);
    b.type = 'button';
    b.addEventListener('click', (e) => { e.stopPropagation(); menu.remove(); openMenu = null; fn(); });
    menu.appendChild(b);
  };
  mk('重命名', () => startRowRename(row, s));
  mk(s.pinned ? '取消置顶' : '置顶', () => togglePin(s));
  mk('删除', () => deleteSession(s), true);
  row.appendChild(menu);
  openMenu = menu;
  const off = (e) => {
    if (!menu.contains(e.target)) { menu.remove(); openMenu = null; document.removeEventListener('click', off, true); }
  };
  setTimeout(() => document.addEventListener('click', off, true), 0);
}

/* 行内重命名 */
function startRowRename(row, s) {
  if (S.renaming) return;
  const original = s.title || '';
  const box = el('div', 'renamebox');
  const input = el('input');
  input.type = 'text';
  input.value = original;
  input.maxLength = 200;
  input.setAttribute('aria-label', '会话标题');
  const okBtn = el('button', 'iconbtn');
  okBtn.type = 'button';
  okBtn.setAttribute('aria-label', '保存');
  okBtn.appendChild(icon('i-check', 12));
  const cancelBtn = el('button', 'iconbtn');
  cancelBtn.type = 'button';
  cancelBtn.setAttribute('aria-label', '取消');
  cancelBtn.appendChild(icon('i-close', 12));
  box.append(input, okBtn, cancelBtn);

  const prevChildren = Array.from(row.childNodes);
  row.textContent = '';
  row.appendChild(box);
  input.focus();
  input.select();

  let cancelled = false;
  let done = false;
  const restore = () => {
    row.textContent = '';
    for (const c of prevChildren) row.appendChild(c);
    S.renaming = null;
  };
  const finish = async (commit) => {
    if (done) return;
    if (!commit) { done = true; restore(); return; }
    const value = input.value.trim();
    if (!value) {
      const err = el('div', 'sitem__err', '标题不能为空');
      row.appendChild(err);
      input.focus();
      setTimeout(() => err.remove(), 1800);
      return;
    }
    done = true;
    try {
      await patchSession(s.id, { title: value });
      restore();
      renderSessionList();
      renderTitle();
    } catch (e) {
      done = false;
      const errBox = el('div', 'sitem__err', '保存失败，检查网关是否在运行');
      const retry = el('button', 'linkbtn', '重试');
      retry.type = 'button';
      retry.addEventListener('click', () => { errBox.remove(); done = false; finish(true); });
      errBox.appendChild(retry);
      row.appendChild(errBox);
    }
  };
  S.renaming = { cancel: () => { if (!done) { done = true; restore(); } } };

  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); finish(true); }
    else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      cancelled = true;
      finish(false);
    }
  });
  input.addEventListener('blur', () => { if (!cancelled) setTimeout(() => finish(true), 0); });
  okBtn.addEventListener('mousedown', (e) => { e.preventDefault(); finish(true); });
  cancelBtn.addEventListener('mousedown', (e) => { e.preventDefault(); cancelled = true; finish(false); });
}

async function patchSession(id, fields) {
  const { session } = await api(`/api/sessions/${id}`, { method: 'PATCH', body: fields });
  const i = S.sessions.findIndex((x) => x.id === id);
  if (i >= 0 && session) S.sessions[i] = { ...S.sessions[i], ...session };
  if (S.session && S.session.id === id && session) S.session = { ...S.session, ...session };
  return session;
}

async function togglePin(s) {
  // 乐观更新 + 800ms 内不重排（touch updated_at 会把它顶到最前）
  const next = s.pinned ? 0 : 1;
  s.pinned = next;
  renderSessionList();
  try { await patchSession(s.id, { pinned: next }); } catch { toast('置顶失败', 'error'); }
  setTimeout(async () => {
    await loadSessions(D.searchInput.value.trim());
    const row = $(`.sitem[data-id="${s.id}"]`, D.sessionList);
    if (row) {
      row.scrollIntoView({ block: 'nearest' });
      row.classList.add('is-flash');
      row.focus({ preventScroll: true });
      setTimeout(() => row.classList.remove('is-flash'), 600);
    }
  }, 800);
}

async function deleteSession(s) {
  const ok = await confirmDialog({
    title: '删除会话',
    text: `删除「${s.title || '未命名'}」？此操作不可撤销。`,
    okText: '删除',
    danger: true,
    focus: 'cancel',
  });
  if (!ok) return;
  try {
    await api(`/api/sessions/${s.id}`, { method: 'DELETE' });
    S.sessions = S.sessions.filter((x) => x.id !== s.id);
    if (S.session && S.session.id === s.id) newDraft();
    else renderSessionList();
    toast('已删除');
  } catch {
    toast('删除失败', 'error');
  }
}

/* ── 会话多选 / 批量删除 ── */
function setSelMode(on){
  S.selMode = !!on;
  if (!on) S.sel.clear();
  renderSessionList();
}

function toggleSelect(id){
  if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  renderSessionList();
}

function selectAllSessions(){
  const ids = S.sessions.map(s => s.id);
  if (S.sel.size === ids.length) S.sel.clear();
  else ids.forEach(id => S.sel.add(id));
  renderSessionList();
}

async function bulkDeleteSessions(){
  const ids = [...S.sel];
  if (!ids.length) { toast('先勾选要删除的对话', 'warn'); return; }
  const ok = await confirmDialog({
    title: '批量删除会话',
    text: `将删除 ${ids.length} 个对话及其全部消息，不可恢复。`,
    okText: `删除 ${ids.length} 个`,
    cancelText: '取消',
    danger: true,
    focus: 'cancel',
  });
  if (!ok) return;
  try {
    const r = await api('/api/sessions/bulk-delete', { method: 'POST', body: { ids } });
    S.sessions = S.sessions.filter(x => !S.sel.has(x.id));
    if (S.session && S.sel.has(S.session.id)) newDraft();
    S.sel.clear();
    setSelMode(false);
    toast(`已删除 ${r.deleted || ids.length} 个对话`);
  } catch (err) {
    toast(`删除失败（${err.code || 'network'}）`, 'error');
  }
}

/* 新建 / 切换 */
function newDraft() {
  S.session = null;
  S.compress = false;        // 新对话默认不优化
  // 新对话沿用上一次选的思考强度（savedEffort 会校验当前模型是否支持）
  S.effort = savedEffort(activeModel());
  S.messages = [];
  S.usage = { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0 };
  S.tokensAfter = null;
  S.budget = null;
  S.blocks = [];
  S.selectedBlocks.clear();
  S.pendingExpand = [];
  S.overflowRetried = false;
  S.editing = null;
  renderDraftState();
  renderMessages();
  renderSessionList();
  renderBlocks();
  updateUsageUi();
  renderCompressBtn();
  if (window.innerWidth < 768) closeSidebar();
  D.input.focus();
}

function renderDraftState() {
  renderTitle();
  renderConfigMeta();
  updateStatus();
}

function renderTitle() {
  const title = S.session ? (S.session.title || '未命名') : '新对话（未保存）';
  D.sessionTitle.textContent = title;
  D.chatTitle.textContent = title;
  const m = activeModel();
  D.chatMeta.textContent = m ? `${m.label || m.id} · ${S.effort === 'none' ? '直答' : effortLabel(S.effort) + '思考'}` : '';
}

async function openSession(id) {
  if (S.session && S.session.id === id) { if (window.innerWidth < 768) closeSidebar(); return; }
  const meta = S.sessions.find((s) => s.id === id);
  S.session = meta ? { ...meta } : { id, title: '会话' };
  S.modelId = S.session.model || S.modelId;
  S.effort = S.session.effort || savedEffort(activeModel()) || S.effort;
  S.compress = !!S.session.compress;
  S.messages = [];
  S.blocks = [];
  S.selectedBlocks.clear();
  S.pendingExpand = [];
  S.overflowRetried = false;
  S.tokensAfter = null;
  S.budget = null;
  S.usage = { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0 };
  renderTitle();
  renderModelPill();
  renderEfforts();
  renderMessages({ skeleton: true });
  renderSessionList();
  updateUsageUi();
  if (window.innerWidth < 768) closeSidebar();

  try {
    const data = await api(`/api/sessions/${id}/messages`);
    S.session = { ...S.session, ...(data.session || {}) };
    S.compress = !!S.session.compress;
    renderCompressBtn();
    S.modelId = S.session.model || S.modelId;
    S.effort = S.session.effort || S.effort;
    S.messages = (data.messages || []).map((m) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content || '',
      tokens: m.tokens,
      createdAt: m.created_at,
      state: 'complete',
      model: null,
      id: m.id || null,
      thinking: m.thinking || '',
      thinkMs: m.think_ms || 0,
      effort: m.effort || '',
      versions: (m.versions || []).map((v) => ({
        content: v.content || '',
        thinking: v.thinking || '',
        thinkMs: v.think_ms || v.thinkMs || 0,
        effort: v.effort || '',
      })),
      vIndex: m.v_index || 0,
      tools: [],
      comps: [],
    }));
    S.blocks = data.blocks || [];
    renderTitle();
    renderModelPill();
    renderEfforts();
    renderMessages();
    renderBlocks();
    forceFollow();
    loadSessionUsage(id);
  } catch (err) {
    D.msgList.textContent = '';
    const box = el('div', 'empty');
    box.appendChild(el('p', 'empty__title', '消息加载失败，可能是网关已重启'));
    const acts = el('div', 'empty__actions');
    const retry = el('button', 'btn btn--primary btn--sm', '重新加载');
    retry.type = 'button';
    retry.addEventListener('click', () => openSession(id));
    const fresh = el('button', 'btn btn--sm', '新开对话');
    fresh.type = 'button';
    fresh.addEventListener('click', newDraft);
    acts.append(retry, fresh);
    box.appendChild(acts);
    D.msgList.appendChild(box);
  }
}

async function loadSessionUsage(id) {
  try {
    const data = await api(`/api/sessions/${id}/usage`);
    const rows = data.usage || [];
    const sum = rows.reduce((a, r) => {
      a.prompt_tokens += r.prompt_tokens || 0;
      a.completion_tokens += r.completion_tokens || 0;
      a.reasoning_tokens += r.reasoning_tokens || 0;
      return a;
    }, { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0 });
    if (S.session && S.session.id === id) {
      S.usage = sum;
      if (S.tokensAfter == null) S.tokensAfter = sum.prompt_tokens || null;
      updateUsageUi();
    }
  } catch { /* 用量失败不阻断 */ }
}

/* ─────────────────────────── 8. 消息渲染 ─────────────────────────── */

function renderMessages({ skeleton = false } = {}) {
  D.msgList.textContent = '';
  if (skeleton) {
    const sk = el('div', 'skeleton');
    sk.append(el('i'), el('i'), el('i'));
    D.msgList.appendChild(sk);
    return;
  }
  if (!S.messages.length) { renderEmptyState(); return; }
  for (const m of S.messages) {
    if (m.role === 'user') D.msgList.appendChild(userNode(m));
    else D.msgList.appendChild(aiNode(m));
  }
}

/** 追加一条消息节点：先摘掉空态，否则首屏空态会一直留在消息流上方 */
function appendToStream(node) {
  const empty = D.msgList.querySelector('.empty');
  if (empty) empty.remove();
  const skeleton = D.msgList.querySelector('.skeleton');
  if (skeleton) skeleton.remove();
  D.msgList.appendChild(node);
  return node;
}

function renderEmptyState() {
  const box = el('div', 'empty');
  box.appendChild(el('p', 'empty__title', '开始一个新对话'));
  box.appendChild(el('p', 'empty__sub', '消息只在本机流转；切换模型不会丢失历史。'));
  const acts = el('div', 'empty__actions');
  const snip = el('button', 'btn btn--sm');
  snip.type = 'button';
  snip.append(icon('i-book', 14), document.createTextNode(` 从片段库插入 Alt+P`));
  snip.addEventListener('click', openSnippets);
  acts.appendChild(snip);
  box.appendChild(acts);
  if (S.snippets.length) box.appendChild(el('p', 'empty__foot', `共 ${S.snippets.length} 个片段`));
  const m = activeModel();
  if (m) {
    const btn = el('button', 'linkbtn', `${m.label || m.id} · ${S.effort === 'none' ? '直答' : effortLabel(S.effort) + '中等思考'} · 更换`);
    btn.type = 'button';
    btn.addEventListener('click', () => { D.modelBtn.click(); });
    const wrap = el('p', 'empty__foot');
    wrap.appendChild(btn);
    box.appendChild(wrap);
  }
  if (!S.session) box.appendChild(el('p', 'empty__foot', '首次调用需加载模型，可能等待 20–60 秒。'));
  D.msgList.appendChild(box);
}

function userNode(m) {
  const wrap = el('div', 'msg msg--user');
  const bubble = el('div', 'bubble');
  bubble.textContent = m.content;
  wrap.appendChild(bubble);
  const meta = el('div', 'bubble__meta');
  meta.textContent = `${hhmm(m.createdAt || nowSec())} · ${(m.content || '').length} 字`;
  // 每条用户消息都可以点「编辑」重新提问（编辑后会删除这条之后的对话并重发）
  const editBtn = el('button', 'bubble__editbtn');
  editBtn.type = 'button';
  editBtn.title = '编辑这条提问';
  editBtn.setAttribute('aria-label', '编辑这条提问');
  editBtn.appendChild(icon('i-pencil', 12));
  editBtn.appendChild(document.createTextNode(' 编辑'));
  editBtn.addEventListener('click', () => {
    const idx = S.messages.indexOf(m);
    if (idx < 0) return;
    loadUserToEdit(m, idx);
  });
  meta.appendChild(document.createTextNode(' · '));
  meta.appendChild(editBtn);
  wrap.appendChild(meta);
  if (m.editing) {
    const tag = el('div', 'bubble__edit', '编辑中');
    wrap.appendChild(tag);
  }
  return wrap;
}

function aiNode(m) {
  const article = el('article', 'msg msg--ai');
  if (m.noAnim) article.classList.add('no-anim');
  if (m.state === 'stopped') article.classList.add('is-stopped');
  if (m.state === 'streaming') article.classList.add('is-streaming-msg');
  else article.classList.add('is-done');
  m._node = article;

  const head = el('div', 'aihead');
  head.appendChild(el('span', 'aihead__model', m.model || (activeModel() ? (activeModel().label || activeModel().id) : '模型')));
  head.appendChild(el('span', 'aihead__sep', '·'));
  head.appendChild(el('span', null, hhmm(m.createdAt || nowSec())));
  if (m.effort) head.appendChild(el('span', 'aihead__effort', EFFORT_FULL[m.effort] || (effortLabel(m.effort) + '思考')));
  const actions = el('div', 'aihead__actions');
  const copyBtn = el('button', 'iconbtn');
  copyBtn.type = 'button';
  copyBtn.title = '复制回答';
  copyBtn.setAttribute('aria-label', '复制回答');
  copyBtn.appendChild(icon('i-copy', 14));
  copyBtn.addEventListener('click', async () => {
    await copyText(m.content || '');
    toast('已复制');
  });
  const retryBtn = el('button', 'iconbtn');
  retryBtn.type = 'button';
  retryBtn.title = '重新生成';
  retryBtn.setAttribute('aria-label', '重新生成');
  retryBtn.appendChild(icon('i-spinner', 14));
  retryBtn.addEventListener('click', () => regenerateFrom(m));
  actions.append(copyBtn, retryBtn);
  head.appendChild(actions);
  article.appendChild(head);
  renderVersions(m);

  if (m.comps && m.comps.length) article.appendChild(compBar(m.comps));

  // 思考区
  if (m.thinking || m.state === 'streaming') {
    const think = el('div', 'think' + (m.thinkOpen ? ' is-open' : ''));
    const btn = el('button', 'think__head');
    btn.type = 'button';
    btn.setAttribute('aria-expanded', String(!!m.thinkOpen));
    const chev = icon('i-chevron', 12);
    chev.classList.add('think__chev');
    btn.appendChild(chev);
    btn.appendChild(document.createTextNode('思考'));
    const count = el('span', 'think__count');
    btn.appendChild(count);
    const collapse = el('div', 'think__collapse');
    const inner = el('div', 'think__inner');
    const body = el('div', 'think__body');
    body.textContent = m.thinking || '';
    inner.appendChild(body);
    collapse.appendChild(inner);
    think.append(btn, collapse);
    btn.addEventListener('click', () => {
      m.thinkOpen = !m.thinkOpen;
      m.userToggledThink = true;
      think.classList.toggle('is-open', m.thinkOpen);
      btn.setAttribute('aria-expanded', String(m.thinkOpen));
    });
    m._think = think;
    m._thinkCount = count;
    m._thinkBody = body;
    article.appendChild(think);
  }

  // 工具行
  m._toolsHost = el('div', 'tools');
  if (!m.tools || !m.tools.length) m._toolsHost.hidden = true;
  article.appendChild(m._toolsHost);
  if (m.tools) for (const t of m.tools) m._toolsHost.appendChild(toolRow(t));

  // 正文
  const md = el('div', 'md');
  m._md = md;
  article.appendChild(md);
  paintBody(m);

  // 状态行 / 错误条
  m._state = el('div', 'msg__state');
  article.appendChild(m._state);
  paintState(m);

  m._errHost = el('div');
  article.appendChild(m._errHost);
  return article;
}

function paintBody(m, { streaming } = {}) {
  if (!m._md) return;
  const isStream = streaming != null ? streaming : m.state === 'streaming';
  renderInto(m._md, m.content || '', { streaming: isStream });
  if (m._think) {
    m._think.hidden = !(m.thinking || m.state === 'streaming');
    m._thinkBody.textContent = m.thinking || '';
    const chars = (m.thinking || '').length;
    let meta = chars ? ` · ${fmtNum(chars)} 字` : '';
    if (m.thinkMs) meta += ` · 思考 ${(m.thinkMs / 1000).toFixed(1)}s`;
    m._thinkCount.textContent = meta;
  }
}

function paintState(m) {
  if (!m._state) return;
  const host = m._state;
  host.textContent = '';

  if (m.state === 'stopped') {
    markStopped(m);
    // 三段递降：「已停止」是整行最高级（视觉数值见 visual-direction §4b）
    host.appendChild(el('span', 'state__dot'));
    host.appendChild(el('span', 'state__word', '已停止'));
    const kept = el('span', 'state__kept');
    kept.append(document.createTextNode('保留 '),
      el('span', 'num', fmtNum(m.keptChars != null ? m.keptChars : (m.content || '').length)),
      document.createTextNode(' 字'));
    host.appendChild(kept);
    // 停止态不报 tok/s：被中止的生成没有真实吞吐；只报已用时长
    if (m.ms) host.appendChild(el('span', 'state__dim', `已用 ${m.ms < 100 ? '<0.1' : (m.ms / 1000).toFixed(1)}s`));
    const actions = el('span', 'state__actions');
    const again = el('button', 'btn btn--sm', '重新生成');
    again.type = 'button';
    again.addEventListener('click', () => regenerateFrom(m));
    const cont = el('button', 'btn btn--sm', '继续');
    cont.type = 'button';
    cont.addEventListener('click', () => { D.input.value = '继续'; sendMessage(); });
    actions.append(again, cont);
    host.appendChild(actions);
  } else {
    const parts = [];
    if (m.state === 'streaming') parts.push('生成中…');
    if (m.ms) parts.push(m.ms < 100 ? '<0.1s' : `${(m.ms / 1000).toFixed(1)}s`);
    if (m.tokPerSec) parts.push(`${fmtNum(Math.round(m.tokPerSec))} tok/s`);
    if (parts.length) host.appendChild(el('span', 'tag', parts.join(' · ')));
  }

  // paintBody 会重建正文容器，可能把状态宿主摘出文档：重新挂回消息节点，保证"已停止"可见
  if (!host.isConnected && m._node) m._node.appendChild(host);
}

/** 末尾短横只在末块是段落型时出现（代码块 / 表格 / 折叠区不画，空段落先 trim） */
function endBlockIsProse(mdEl) {
  if (!mdEl) return false;
  const node = mdEl.lastElementChild;
  if (!node) return false;
  const tag = node.tagName;
  if (tag === 'DIV' || tag === 'PRE' || tag === 'FIGURE' || tag === 'HR') return false; // 代码块/表格/图
  if (tag === 'UL' || tag === 'OL') {
    const li = node.lastElementChild;
    return !!li && !!li.textContent.trim();
  }
  if (/^H[1-4]$/.test(tag) || tag === 'P' || tag === 'BLOCKQUOTE') return !!node.textContent.trim();
  return false;
}

function markStopped(m) {
  const node = m._node;
  if (node) {
    node.classList.remove('is-streaming-msg');
    node.classList.add('is-stopped', 'is-done');
  }
  if (m._md) m._md.classList.toggle('is-truncated', endBlockIsProse(m._md));
}

function toolRow(t) {
  const row = el('button', 'toolrow');
  row.type = 'button';
  const name = el('span', null, t.name || 'tool');
  row.appendChild(name);
  if (t.arguments) {
    const args = el('span', 'toolrow__args', String(t.arguments).slice(0, 120));
    row.appendChild(args);
  }
  const state = el('span', 'toolrow__state');
  if (t.state === 'running') {
    const sp = icon('i-spinner', 14);
    sp.classList.add('spinner');
    state.appendChild(sp);
  } else if (t.state === 'error') {
    state.dataset.s = 'err';
    state.textContent = '✗';
  } else {
    state.dataset.s = 'ok';
    state.textContent = '✓';
  }
  row.appendChild(state);
  return row;
}

/* 压缩提示条：一次请求多条事件只出 1 条，取最严重 */
const COMP_SEVERITY = { clean: 0, aggregate: 1, trim: 2, fold: 3, summary: 4 };

function compSeverity(c) {
  if (c.action === 'summary' && c.lossless === false) return 5;
  return COMP_SEVERITY[c.action] || 0;
}

function compBar(comps) {
  const worst = comps.slice().sort((a, b) => compSeverity(b) - compSeverity(a))[0] || comps[0];
  const lossy = worst.action === 'summary' && worst.lossless === false;
  const bar = el('div', 'compbar' + (lossy ? ' compbar--lossy' : ''));
  const main = el('div', 'compbar__main');

  // clean 单独出现不出条（状态条 token 高亮已给反馈）；与其它事件同现时并入开头
  const cleaned = comps.filter((c) => c.action === 'clean' && c.saved_tokens);
  const prefix = cleaned.length
    ? `已清理格式省 ${fmtNum(cleaned.reduce((a, c) => a + (c.saved_tokens || 0), 0))}、`
    : '';
  main.textContent = prefix + compText(worst);
  if (lossy) main.textContent = '⚠ ' + main.textContent;   // 有损：⚠ + 文案含「有损」+ 错误色边框，三通道
  bar.appendChild(main);

  const rest = comps.filter((c) => c !== worst);
  if (rest.length) {
    // 「+N 步」点开明细；每步一行，可点定位到该步折叠位置
    const more = el('button', 'compbar__more', `+${rest.length} 步`);
    more.type = 'button';
    more.setAttribute('aria-expanded', 'false');
    const steps = el('div', 'compbar__steps');
    steps.hidden = true;
    for (const c of [worst, ...rest]) steps.appendChild(compStep(c));
    more.addEventListener('click', () => {
      steps.hidden = !steps.hidden;
      more.setAttribute('aria-expanded', String(!steps.hidden));
    });
    bar.appendChild(more);
    bar.appendChild(steps);
  }

  const link = el('button', 'compbar__link', '查看折叠块');
  link.type = 'button';
  link.addEventListener('click', () => { openDrawer('config'); setTimeout(() => D.blocksSection.scrollIntoView({ block: 'start' }), 120); });
  bar.appendChild(link);
  return bar;
}

/** 明细行：grid 24px | 1fr | auto；tier 定宽统一色；saved 右对齐不加负号 */
function compStep(c) {
  const row = el('button', 'compstep');
  row.type = 'button';
  const tier = el('span', 'compstep__tier', c.tier || '—');
  const label = el('span', 'compstep__label');
  label.textContent = stepLabel(c) + (c.action === 'summary' && c.lossless === false ? '（有损）' : '');
  const saved = el('span', 'compstep__saved', c.saved_tokens != null ? `${fmtNum(c.saved_tokens)} tok` : '');
  row.append(tier, label, saved);
  row.addEventListener('click', () => focusBlock(c.block_id));
  return row;
}

function stepLabel(c) {
  const map = {
    clean: '清理格式', trim: '折叠较早消息', fold: '超长内容折叠',
    aggregate: '折叠块合并', summary: '摘要压缩',
  };
  return map[c.action] || c.action || '压缩';
}

/** 点明细定位：定位不到（已被聚合）就打开折叠块面板并高亮该行 */
async function focusBlock(blockId) {
  if (!blockId) { toast('这一步没有对应的折叠块', 'warn'); return; }
  openDrawer('config');
  await loadBlocks();                        // 等列表就绪，否则重渲染会把高亮洗掉
  D.blocksSection.scrollIntoView({ block: 'start' });
  const row = document.querySelector(`#blockList .blockrow[data-block="${CSS.escape(blockId)}"]`);
  if (!row) {
    toast(`找不到 ${blockId}（可能已被合并），已打开折叠块面板`, 'warn');
    return;
  }
  row.classList.add('is-flash');
  row.focus({ preventScroll: true });
  setTimeout(() => row.classList.remove('is-flash'), 600);
}

function compText(c) {
  const saved = c.saved_tokens != null ? fmtNum(c.saved_tokens) : null;
  const n = c.count != null ? c.count : null;
  const bid = c.block_id || '';
  const tail = c.detail ? `（${c.detail}）` : '';
  switch (c.action) {
    case 'clean':
      return `已清理格式，释放 ${saved} tokens`;
    case 'trim':
      return n != null
        ? `已折叠 ${n} 条较早消息，释放 ${saved} tokens（原文完整保留）`
        : `已折叠较早消息，释放 ${saved} tokens（原文完整保留）`;
    case 'fold':
      return `1 条超长内容已折叠为首尾摘录，释放 ${saved} tokens${tail}`;
    case 'aggregate':
      return `${n != null ? n + ' 个折叠块' : '折叠块'}已合并为 ${bid || '上层块'}`;
    case 'summary':
      return c.lossless === false
        ? `摘要压缩（有损）：较早消息已摘要为 ${bid || '摘要块'}，释放 ${saved} tokens${tail}`
        : `已用规则摘要压缩较早消息，释放 ${saved} tokens${tail}`;
    default:
      return `已压缩上下文，释放 ${saved != null ? saved : '—'} tokens`;
  }
}

/* 折叠块数（N 条）来自 block.source_range，真值优先；拿不到就不编数字 */
async function fillCompCounts(sid) {
  if (!sid) return;
  let blocks = S.blockCache.get(sid);
  if (!blocks) {
    try {
      const data = await api(`/api/sessions/${sid}/blocks`);
      blocks = data.blocks || [];
      S.blockCache.set(sid, blocks);
    } catch { return; }
  }
  let dirty = false;
  for (const m of S.messages) {
    if (!m.comps) continue;
    for (const c of m.comps) {
      if (c.count != null || !c.block_id) continue;
      const b = blocks.find((x) => x.block_id === c.block_id);
      if (!b) continue;
      const range = Array.isArray(b.source_range) ? b.source_range : null;
      if (!range || range.length < 2) continue;
      const count = range[1] - range[0] + 1;
      if (count > 0) { c.count = count; dirty = true; }
    }
  }
  if (dirty) {
    for (const m of S.messages) {
      if (m._node && m.comps && m.comps.length) {
        const old = m._node.querySelector('.compbar');
        if (old) old.replaceWith(compBar(m.comps));
      }
    }
  }
}

/* ─────────────────────────── 9. 发送 / 流式 ─────────────────────────── */

function historyPayload(except) {
  // except = 正在重新生成的那条：不能把它的旧内容再发上去，否则模型会接着旧答案写
  const out = [];
  for (const m of S.messages) {
    if (m === except) continue;
    if (m.role === 'user') out.push({ role: 'user', content: m.content });
    else if (m.content) out.push({ role: 'assistant', content: m.content });
  }
  return out;
}

function deriveTitle(text) {
  const t = (text || '').replace(/\s+/g, ' ').trim();
  return t.slice(0, 24) || '新对话';
}

async function ensureSession(text) {
  if (S.session) return S.session;
  const body = { model: S.modelId, title: deriveTitle(text), reasoning_effort: S.effort };
  const { session } = await api('/api/sessions', { method: 'POST', body });
  S.session = session;
  S.sessions.unshift(session);
  renderSessionList();
  renderTitle();
  LS.del('bonsai.draft.session');
  return session;
}

async function sendMessage(rawText) {
  if (S.gen) { stopGeneration(); return; }
  if (!S.modelId) { toast('模型列表还在加载，请稍候…', 'warn'); return; }
  const text = (rawText != null ? rawText : D.input.value).trim();
  if (!text) return;

  if (S.editing) {
    const removed = S.messages.length - S.editing.index - 1;
    if (removed > 0) {
      const ok = await confirmDialog({
        title: '重新发送',
        text: `将删除此后的 ${removed} 条消息，继续？`,
        okText: '删除并重发',
        danger: true,
        focus: 'cancel',
      });
      if (!ok) return;
    }
    S.messages = S.messages.slice(0, S.editing.index);
    S.editing = null;
    D.editBar.hidden = true;
    renderMessages();
  }

  try {
    await ensureSession(text);
  } catch (err) {
    if (err.status === 401) return;
    toast(`新建会话失败（${err.code}）`, 'error');
    return;
  }

  const userMsg = { role: 'user', content: text, createdAt: nowSec(), state: 'complete' };
  S.messages.push(userMsg);
  appendToStream(userNode(userMsg));
  S.lastUserText = text;
  D.input.value = '';
  autoGrow();
  updateCount();
  D.composerHint.hidden = true;
  forceFollow();
  scrollToBottom();
  await runChat();
}

function regenerateFrom(m) {
  if (S.gen) { toast('正在生成，先点停止', 'warn'); return; }
  const idx = S.messages.indexOf(m);
  let lastUser = null;
  for (let i = (idx >= 0 ? idx : S.messages.length) - 1; i >= 0; i--) {
    if (S.messages[i].role === 'user') { lastUser = S.messages[i]; break; }
  }
  if (!lastUser) { toast('找不到可重发的提问', 'warn'); return; }
  // 同一条消息生成新版本（旧回答不会重复进入上下文，见 historyPayload 只取当前版本）
  runChat(m);
}

async function runChat(regen) {
  if (S.gen) return;
  const m = activeModel();
  const payload = {
    session_id: S.session ? S.session.id : undefined,
    model: S.modelId,
    messages: historyPayload(regen),
    compress: !!S.compress,     // 默认 false：不自动优化上下文
  };
  if (m && m.thinking && m.thinking.supported) payload.reasoning_effort = S.effort;
  else payload.enable_thinking = false;
  if (S.pendingExpand.length) {
    // 「下一条请求」包含重发生效：发起这一刻即消费并同步状态条（不等响应）
    payload.expand = S.pendingExpand.slice();
    const consumed = S.pendingExpand.length;
    S.pendingExpand = [];
    showExpandStatus(consumed, { consumed: true });
  }

  let msg;
  if (regen && S.messages.includes(regen)) {
    // 重新生成：复用同一条消息，旧内容存进 versions[0]，新结果作为新版本
    msg = regen;
    if (!Array.isArray(msg.versions) || !msg.versions.length) {
      msg.versions = [{ content: msg.content || '', thinking: msg.thinking || '',
                        thinkMs: msg.thinkMs || 0, effort: msg.effort || '' }];
      msg.vIndex = 0;
    }
    msg.content = '';
    msg.thinking = '';
    msg.thinkMs = 0;
    msg.tools = [];
    msg.comps = [];
    msg.stopReason = null;
    msg.state = 'streaming';
    msg.effort = S.effort;
    msg.thinkOpen = false;
    if (regen.id) payload.regen_of = regen.id;
    const oldNode = msg._node;
    const fresh = aiNode(msg);
    if (oldNode) oldNode.replaceWith(fresh);
    else appendToStream(fresh);
  } else {
    msg = {
      role: 'assistant',
      model: m ? (m.label || m.id) : null,
      content: '',
      thinking: '',
      tools: [],
      comps: [],
      state: 'streaming',
      createdAt: nowSec(),
      thinkOpen: false,
      effort: S.effort,
      thinkMs: 0,
      versions: [],
      vIndex: 0,
      id: null,
    };
    S.messages.push(msg);
    appendToStream(aiNode(msg));
  }
  const t0 = performance.now();
  const ac = new AbortController();
  const genRef = { ac, msg, t0, sessionId: S.session ? S.session.id : null };
  S.gen = genRef;
  setGenerating(true);
  hideVramNote();
  setConn('warn', '正在连接本地模型…');
  const slowTimer = setTimeout(() => {
    if (S.gen === genRef) setConn('warn', '首次加载较慢，请勿重复点击');
  }, 3000);

  let sawDone = false;
  try {
    await streamChat(payload, ac.signal, {
      onEvent(ev) {
        if (ev.type === 'done') sawDone = true;
        handleStreamEvent(ev, msg);
      },
    });
  } catch (err) {
    handleChatError(err, msg);
  } finally {
    clearTimeout(slowTimer);
    msg.ms = performance.now() - t0;
    finishGeneration(msg, sawDone);
  }
}

async function streamChat(payload, signal, { onEvent }) {
  let res;
  try {
    res = await fetch('/api/chat', {
      method: 'POST',
      signal,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, { code: 'network_error', message: '无法连接网关' });
  }

  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (res.status === 401) requireLogin('登录状态已过期，请重新登录。');
    throw new ApiError(res.status, data || {});
  }
  if (!res.body) throw new ApiError(0, { code: 'no_stream', message: '响应不是流' });

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  const dispatch = (frame) => {
    const lines = frame.split('\n');
    const dataLines = [];
    for (const line of lines) {
      if (!line || line.startsWith(':')) continue;
      if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
      // id: / event: 本期不使用（契约不发送 id，也不支持 Last-Event-ID）
    }
    if (!dataLines.length) return;
    const raw = dataLines.join('\n');
    if (raw === '[DONE]') return;
    let obj;
    try { obj = JSON.parse(raw); } catch { return; }
    try { onEvent(obj); } catch (e) { console.warn('事件处理失败', e); }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx = buf.search(/\r?\n\r?\n/);
    while (idx >= 0) {
      const match = buf.slice(idx).match(/^\r?\n\r?\n/);
      const sepLen = match ? match[0].length : 2;
      dispatch(buf.slice(0, idx));
      buf = buf.slice(idx + sepLen);
      idx = buf.search(/\r?\n\r?\n/);
    }
  }
  buf += decoder.decode();
  if (buf.trim()) dispatch(buf);

  if (!signal.aborted) return; // 正常结束
}

function handleStreamEvent(ev, msg) {
  switch (ev.type) {
    case 'message_start': {
      S.budget = ev.budget != null ? ev.budget : S.budget;
      S.tokensAfter = ev.tokens_after != null ? ev.tokens_after : S.tokensAfter;
      if (ev.model) msg.model = (modelById(ev.model) || {}).label || ev.model;
      setConn('ok', '已连接');
      updateUsageUi();          // 状态条预算随 message_start 即时刷新，不等 usage
      paintState(msg);
      break;
    }
    case 'compression': {
      msg.comps.push(ev);
      const host = msg._node;
      if (host) {
        const old = host.querySelector('.compbar');
        const bar = compBar(msg.comps);
        if (old) old.replaceWith(bar); else host.insertBefore(bar, host.children[1] || null);
      }
      if (ev.action === 'clean' && ev.saved_tokens) flashTokenSavings(ev.saved_tokens);
      fillCompCounts(S.session ? S.session.id : null);
      break;
    }
    case 'delta': {
      if (ev.role === 'thinking') {
        msg.thinking = (msg.thinking || '') + (ev.text || '');
        if (!msg._thinkAt) msg._thinkAt = performance.now();
        ensureThink(msg);
        if (!msg.userToggledThink) setThinkOpen(msg, true);
      } else {
        if (!msg.content && msg.thinking && !msg.userToggledThink) setThinkOpen(msg, false);
        if (msg._thinkAt && msg.thinkMs == null) msg.thinkMs = performance.now() - msg._thinkAt;
        msg.content = (msg.content || '') + (ev.text || '');
      }
      schedulePaint(msg);
      break;
    }
    case 'tool_call': {
      for (const t of msg.tools) if (t.state === 'running') t.state = 'ok';
      msg.tools.push({ name: ev.name, arguments: ev.arguments, state: 'running' });
      if (msg._toolsHost) {
        msg._toolsHost.hidden = false;
        msg._toolsHost.textContent = '';
        for (const t of msg.tools) msg._toolsHost.appendChild(toolRow(t));
      }
      schedulePaint(msg);
      break;
    }
    case 'usage': {
      if (ev.session_usage) {
        S.usage = {
          prompt_tokens: ev.session_usage.prompt_tokens || 0,
          completion_tokens: ev.session_usage.completion_tokens || 0,
          reasoning_tokens: ev.session_usage.reasoning_tokens || 0,
        };
      } else if (ev.usage) {
        S.usage = {
          prompt_tokens: ev.usage.prompt_tokens || 0,
          completion_tokens: ev.usage.completion_tokens || 0,
          reasoning_tokens: ev.usage.reasoning_tokens || 0,
        };
      }
      if (ev.usage && ev.usage.prompt_tokens != null) S.tokensAfter = ev.usage.prompt_tokens;
      if (ev.usage && S.gen) {
        const secs = (performance.now() - S.gen.t0) / 1000;
        msg.ms = performance.now() - S.gen.t0;
        msg.tokPerSec = secs > 0.2 ? (ev.usage.completion_tokens || 0) / secs : null;
        S.lastCost = { ms: msg.ms, tok: ev.usage.completion_tokens || 0, tps: msg.tokPerSec };
      }
      updateUsageUi();
      updateContextUi();
      break;
    }
    case 'error': {
      msg.upstreamError = { code: ev.code, retryable: !!ev.retryable };
      for (const t of msg.tools) if (t.state === 'running') t.state = 'error';
      if (msg._toolsHost) {
        msg._toolsHost.textContent = '';
        for (const t of msg.tools) msg._toolsHost.appendChild(toolRow(t));
      }
      if (msg._node) msg._node.classList.add('is-failed');
      renderErrorBar(msg, {
        code: ev.code,
        text: ev.code === 'upstream_timeout'
          ? '上游 30 秒无响应，已保留已收到的内容。'
          : '上游返回错误，已保留已收到的内容。',
        actions: [
          { label: '重试', kind: 'primary', onClick: () => regenerateFrom(msg) },
          { label: '继续', onClick: () => { D.input.value = '继续'; sendMessage(); } },
        ],
      });
      break;
    }
    case 'done': {
      msg.doneData = ev;
      if (typeof ev.content === 'string') {
        // 以服务端完整正文为准，防分片丢失
        msg.content = ev.content;
      }
      if (typeof ev.thinking === 'string' && ev.thinking) msg.thinking = ev.thinking;
      if (ev.thinking_ms != null) msg.thinkMs = ev.thinking_ms;
      if (ev.effort) msg.effort = ev.effort;
      if (ev.message_id) msg.id = ev.message_id;
      if (ev.tokens_after != null) S.tokensAfter = ev.tokens_after;
      // 落一个版本：旧版本已在 versions 里（重新生成时），这里补当前这版
      if (!Array.isArray(msg.versions)) msg.versions = [];
      msg.versions.push({
        content: msg.content || '',
        thinking: msg.thinking || '',
        thinkMs: msg.thinkMs || 0,
        effort: msg.effort || '',
      });
      msg.vIndex = msg.versions.length - 1;
      renderVersions(msg);
      msg.stopReason = ev.stop_reason;
      // 收尾：仍在运行的工具行落成成功态（本期只展示不执行）
      for (const t of msg.tools) if (t.state === 'running') t.state = 'ok';
      if (msg._toolsHost && msg.tools.length) {
        msg._toolsHost.textContent = '';
        for (const t of msg.tools) msg._toolsHost.appendChild(toolRow(t));
      }
      setThinkOpen(msg, false);
      schedulePaint(msg, { streaming: false });
      break;
    }
    default:
      break;
  }
}

function ensureThink(msg) {
  if (msg._think || !msg._node) return;
  const node = aiNode2Think(msg);
  if (!node) return;
  const before = msg._node.querySelector('.md');
  msg._node.insertBefore(node, before || null);
}

/* ── 回答多版本：同一条消息可翻页查看历次重新生成 ── */
function renderVersions(m){
  if (!m || !m._node) return;
  const head = m._node.querySelector('.aihead');
  if (!head) return;
  const total = (m.versions || []).length;
  let box = head.querySelector('.vers');
  if (total <= 1 || m.state === 'streaming') { if (box) box.remove(); return; }
  if (!box) {
    box = el('span', 'vers');
    head.insertBefore(box, head.querySelector('.aihead__actions'));
  }
  box.textContent = '';
  const mk = (txt, title, to) => {
    const b = el('button', null, txt);
    b.type = 'button';
    b.title = title;
    b.disabled = to < 0 || to >= total;
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      switchVersion(m, to).catch((err) => {
        console.error('[vers]', err);
        toast('切换版本失败：' + ((err && err.message) || err), 'error');
      });
    });
    return b;
  };
  box.append(mk('‹', '上一版', m.vIndex - 1),
    el('span', 'vers__n', `${m.vIndex + 1}/${total}`),
    mk('›', '下一版', m.vIndex + 1));
}

async function switchVersion(m, i){
  const vs = m.versions || [];
  if (i < 0 || i >= vs.length || i === m.vIndex || m.state === 'streaming') return;
  const v = vs[i];
  m.vIndex = i;
  m.content = v.content || '';
  m.thinking = v.thinking || '';
  m.thinkMs = v.thinkMs || 0;
  if (v.effort) m.effort = v.effort;
  // 注意：aiNode() 会把 m._node 指向新节点，所以必须先记住旧节点，否则 replaceWith 等于替换自己
  const oldNode = m._node;
  const fresh = aiNode(m);
  if (oldNode) oldNode.replaceWith(fresh);
  if (m.id) {
    try { await api(`/api/messages/${m.id}`, { method: 'PATCH', body: { v_index: i } }); }
    catch { toast('版本已切换，但没保存到会话', 'warn'); }
  }
}

/* 结构上重建思考区（首次出现 thinking delta 时） */
function aiNode2Think(m) {
  if (m._think) return m._think;
  const think = el('div', 'think');
  const btn = el('button', 'think__head');
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'false');
  const chev = icon('i-chevron', 12);
  chev.classList.add('think__chev');
  btn.append(chev, document.createTextNode('思考'));
  const count = el('span', 'think__count');
  btn.appendChild(count);
  const collapse = el('div', 'think__collapse');
  const inner = el('div', 'think__inner');
  const body = el('div', 'think__body');
  inner.appendChild(body);
  collapse.appendChild(inner);
  think.append(btn, collapse);
  btn.addEventListener('click', () => {
    m.thinkOpen = !m.thinkOpen;
    m.userToggledThink = true;
    think.classList.toggle('is-open', m.thinkOpen);
    btn.setAttribute('aria-expanded', String(m.thinkOpen));
  });
  m._think = think;
  m._thinkCount = count;
  m._thinkBody = body;
  return think;
}

function setThinkOpen(msg, open) {
  if (!msg._think) return;
  msg.thinkOpen = open;
  msg._think.classList.toggle('is-open', open);
  const btn = msg._think.querySelector('.think__head');
  if (btn) btn.setAttribute('aria-expanded', String(open));
}

let paintRaf = 0;
function schedulePaint(msg, opts) {
  if (paintRaf) return;
  paintRaf = requestAnimationFrame(() => {
    paintRaf = 0;
    paintBody(msg, opts);
    paintState(msg);
    if (S.follow) scrollToBottom();
    else bumpNewCount();
  });
}

function finishGeneration(msg, sawDone) {
  const gen = S.gen;
  S.gen = null;
  setGenerating(false);

  if (msg._thinkCount) {
    const chars = (msg.thinking || '').length;
    msg._thinkCount.textContent = chars ? ` · ${fmtNum(chars)} 字` : '';
    if (msg.thinkMs) msg._thinkCount.textContent += ` · ${(msg.thinkMs / 1000).toFixed(1)}s`;
  }
  // 不按字符数反推 tok/s：没有 usage 就不报（宁缺勿假）

  const wasStreaming = msg.state === 'streaming';
  if (wasStreaming) msg.state = sawDone ? 'complete' : 'interrupted';
  if (msg.stopReason === 'aborted' || msg.abortedByUser) msg.state = 'stopped';
  if (msg._node) {
    msg._node.classList.remove('is-streaming-msg');
    msg._node.classList.add('is-done');
  }
  msg.keptChars = (msg.content || '').length;
  // 状态条与气泡 meta 用同一组数字（收尾后以最终耗时为准）
  if (msg.ms && msg.state === 'complete') {
    S.lastCost = { ms: msg.ms, tok: S.lastCost ? S.lastCost.tok : 0, tps: msg.tokPerSec };
  }
  paintBody(msg, { streaming: false });
  paintState(msg);
  renderVersions(msg);   // 流结束后才能显示"1/2"翻页（流式中不显示）

  if (msg.state === 'interrupted') {
    // 断线：不重连同一请求，直接给结论（ux §2g）
    if (navigator.onLine === false) {
      renderErrorBar(msg, { code: 'interrupted', text: '网络已断开，恢复后自动继续。', actions: [] });
      setConn('error', '网络已断开');
    } else {
      renderErrorBar(msg, {
        code: 'interrupted',
        text: '连接已中断，本机网关不支持断点续传。',
        actions: [
          { label: '重新生成', kind: 'primary', onClick: () => regenerateFrom(msg) },
          { label: '保留并继续', onClick: () => { D.input.value = '继续'; sendMessage(); } },
        ],
      });
      setConn('error', '连接已中断');
    }
  } else if (msg.state === 'stopped') {
    flashStatus('已停止生成（已产出的内容保留）', { vramNote: true });
    if (window.innerWidth < 768) toast('已停止生成（已产出的内容保留）', 'warn');
    if (S.session) loadSessionUsage(S.session.id);
  } else if (msg.state === 'complete') {
    setConn('ok', '就绪');
  }

  // expand 令牌已在 runChat 发起时消费，这里不再改动（避免撤销按钮闪断）

  S.blockCache.delete(S.session ? S.session.id : '');
  loadSessions(D.searchInput.value.trim());
  updateUsageUi();
  updateStatus();
  if (gen && gen.msg === msg && msg.content && window.innerWidth >= 768) D.input.focus();
}

function renderErrorBar(msg, { code, text, actions = [], neutral = false }) {
  if (!msg._errHost) return;
  msg._errHost.textContent = '';
  // 中断 = 系统事件（中性条）；失败 = 需干预（错误条）。两者不共用 modifier。
  const isNeutral = neutral || code === 'interrupted' || code === 'offline';
  const bar = el('div', 'errbar' + (isNeutral ? ' errbar--neutral' : ''));
  const main = el('div', 'errbar__text', text);
  bar.appendChild(main);
  // code.length ≤ 18 才展示（过长的是异常串，不是错误码）
  if (code && String(code).length <= 18) bar.appendChild(el('div', 'errbar__code', code));
  if (actions.length) {
    const box = el('div', 'errbar__actions');
    let first = null;
    for (const a of actions) {
      const b = el('button', 'btn btn--sm ' + (a.kind === 'primary' ? 'btn--primary' : 'btn--line'), a.label);
      b.type = 'button';
      b.addEventListener('click', () => { a.onClick(); });
      if (a.kind === 'primary' && !first) first = b;
      box.appendChild(b);
    }
    bar.appendChild(box);
    // 主按钮 autofocus，帮助键盘用户一键恢复
    if (first) setTimeout(() => { if (first.isConnected && document.activeElement === document.body) first.focus({ preventScroll: true }); }, 30);
  }
  msg._errHost.appendChild(bar);
  if (S.follow) scrollToBottom();
}

function handleChatError(err, msg) {
  if (err && (err.name === 'AbortError' || err.code === 'aborterror')) {
    msg.state = 'stopped'; if (msg._node) msg._node.classList.add('is-stopped');
    return;
  }
  if (err.status === 401) {
    msg.state = 'stopped'; if (msg._node) msg._node.classList.add('is-stopped');
    requireLogin('登录状态已过期，请重新登录。');
    return;
  }
  if (err.code === 'network_error') {
    msg.state = navigator.onLine === false ? 'interrupted' : 'stopped';
    setConn('error', '连接已断开');
    renderErrorBar(msg, {
      code: 'network_error',
      text: navigator.onLine === false ? '网络已断开，恢复后自动继续。' : '连接已断开，网关可能已停止。',
      actions: [
        { label: '重新连接', kind: 'primary', onClick: () => { if (S.session) openSession(S.session.id); } },
        { label: '复制已收到内容', onClick: () => copyText(msg.content || '') },
      ],
    });
    return;
  }

  const supported = (err.payload && err.payload.error && err.payload.error.supported_efforts)
    || (err.payload && err.payload.supported_efforts) || null;
  msg.state = 'stopped'; if (msg._node) msg._node.classList.add('is-stopped');

  switch (err.code) {
    case 'unsupported_effort': {
      const list = supported || [];
      const mdl = activeModel();
      // 若服务端说当前档位不可用，就在它列出的档位里挑最接近的；若出现自相矛盾（列表里含当前档），退回模型默认
      const nearest = list.includes(S.effort)
        ? ((mdl && mdl.thinking && mdl.thinking.default) || list[0] || null)
        : nearestEffort(S.effort, list);
      renderErrorBar(msg, {
        code: err.code,
        text: list.length
          ? `当前模型可用档位：${list.map(effortLabel).join(' / ')}，不含本会话的「${effortLabel(S.effort)}」。`
          : '当前模型不支持该思考档位。',
        actions: [
          nearest && { label: `改为「${effortLabel(nearest)}」并重发`, kind: 'primary', onClick: async () => { await pickEffort(nearest); runChat(); } },
          { label: '改去选模型', onClick: () => { D.modelBtn.click(); } },
        ].filter(Boolean),
      });
      break;
    }
    case 'context_overflow': {
      const p = err.payload || {};
      const tokens = p.tokens != null ? p.tokens : (p.error && p.error.tokens);
      const budget = p.budget != null ? p.budget : (p.error && p.error.budget);
      const over = tokens != null && budget != null ? tokens - budget : null;
      const exhausted = S.overflowRetried;
      renderErrorBar(msg, {
        code: err.code,
        text: exhausted
          ? '已压缩仍超限，请新开对话或删减长附件。'
          : (over != null ? `上下文超出 ${fmtNum(over)} tokens：已用 ${fmtNum(tokens)} / ${fmtNum(budget)}。` : '上下文已超出预算。'),
        actions: exhausted
          ? [{ label: '新开对话', kind: 'primary', onClick: () => newDraft() }]
          : [
            { label: '优化后重发', kind: 'primary', onClick: async () => {
              try {
                if (S.session) {
                  const r = await api(`/api/sessions/${S.session.id}/compress`,
                    { method: 'POST', body: { model: S.modelId } });
                  S.compress = true;
                  renderCompressBtn();
                  if (r.saved > 0) toast(`已释放 ${fmtNum(r.saved)} tokens`);
                }
              } catch (_) { /* 失败也继续重试，服务端会再算一次 */ }
              S.overflowRetried = true;
              runChat();
            } },
            { label: '查看折叠块', onClick: () => { openDrawer('config'); setTimeout(() => D.blocksSection.scrollIntoView({ block: 'start' }), 120); } },
          ],
      });
      break;
    }
    case 'model_offline':
    case 'model_reloading': {
      renderErrorBar(msg, {
        code: err.code,
        text: err.code === 'model_reloading' ? '模型正在加载，通常 20–60 秒。' : '本地模型未加载。',
        actions: [
          { label: '重试', kind: 'primary', onClick: () => startCountdownRetry(() => runChat(), 5) },
          { label: '切换模型', onClick: () => { D.modelBtn.click(); } },
        ],
      });
      if (err.payload && err.payload.error && err.payload.error.retryable !== false) {
        startCountdownRetry(() => runChat(), 5, { auto: true });
      }
      break;
    }
    case 'upstream_timeout': {
      renderErrorBar(msg, {
        code: err.code,
        text: '上游 30 秒无响应，已保留已收到的内容。',
        actions: [
          { label: '重试', kind: 'primary', onClick: () => runChat() },
          { label: '继续', onClick: () => { D.input.value = '继续'; sendMessage(); } },
        ],
      });
      break;
    }
    case 'rate_limited': {
      S.rateFails += 1;
      const wait = 4;
      renderErrorBar(msg, {
        code: err.code,
        text: `请求过于频繁，${wait} 秒后自动重试。`,
        actions: [{ label: '立即重试', onClick: () => runChat() }],
      });
      if (S.rateFails <= 3) startCountdownRetry(() => runChat(), wait, { auto: true });
      else S.rateFails = 0;
      break;
    }
    case 'bad_origin': {
      renderErrorBar(msg, { code: err.code, text: 'Origin 不在网关白名单，请通过 127.0.0.1 访问。', actions: [] });
      break;
    }
    case 'expand_limit_exceeded': {
      renderErrorBar(msg, { code: err.code, text: '还原块数量超过单次上限。', actions: [] });
      break;
    }
    default: {
      renderErrorBar(msg, {
        code: err.code || 'unknown',
        text: '请求失败，已保留已收到的内容。',
        actions: [{ label: '重试', kind: 'primary', onClick: () => runChat() }],
      });
    }
  }
  if (S.follow) scrollToBottom();
}

function nearestEffort(current, list) {
  if (!list || !list.length) return null;
  if (list.includes(current)) return current;
  const ci = EFFORT_ORDER.indexOf(current);
  let best = list[0];
  let bestD = Infinity;
  for (const e of list) {
    const d = Math.abs(EFFORT_ORDER.indexOf(e) - ci);
    if (d < bestD) { bestD = d; best = e; }
  }
  return best;
}

function startCountdownRetry(fn, seconds, { auto = false } = {}) {
  let left = seconds;
  const note = el('div', 'errbar__code', `${left} 秒后自动重试`);
  const host = Q_errActions();
  if (host) host.appendChild(note);
  const timer = setInterval(() => {
    left -= 1;
    if (left > 0) { note.textContent = `${left} 秒后自动重试`; return; }
    clearInterval(timer);
    note.remove();
    if (auto) fn();
  }, 1000);
  setTimeout(() => clearInterval(timer), (seconds + 1) * 1000);
}
function Q_errActions() {
  const bars = $$('.errbar');
  return bars.length ? bars[bars.length - 1].querySelector('.errbar__actions') : null;
}

function stopGeneration({ silent = false } = {}) {
  const gen = S.gen;
  if (!gen) return;
  gen.msg.state = 'stopped';
  gen.msg.abortedByUser = true;
  gen.msg.keptChars = (gen.msg.content || '').length;
  try { gen.ac.abort(); } catch { /* 忽略 */ }
  S.gen = null;
  setGenerating(false);
  paintBody(gen.msg, { streaming: false });
  paintState(gen.msg);
  setConn('ok', '就绪');
  if (!silent) {
    flashStatus('已停止生成（已产出的内容保留）', { vramNote: true });
    if (window.innerWidth < 768) toast('已停止生成（已产出的内容保留）', 'warn');
    // 不承诺释放显存（KV 池按启动配置常驻）；usage 可能迟到，未收到则保持上次值
    if (S.session) loadSessionUsage(S.session.id);
  }
}

function setGenerating(on) {
  const btn = D.sendBtn;
  btn.textContent = '';
  if (on) {
    btn.classList.add('is-stop');
    btn.setAttribute('aria-label', '停止生成');
    btn.removeAttribute('aria-disabled');
    btn.appendChild(icon('i-stop', 16));
  } else {
    btn.classList.remove('is-stop');
    btn.setAttribute('aria-label', '发送');
    btn.appendChild(icon('i-send', 16));
    updateSendState();
  }
}

function updateSendState() {
  if (S.gen) return;
  const blocked = !S.modelId;                 // 模型还没到（/api/models 探活可能 2s+）
  const empty = !D.input.value.trim();
  D.sendBtn.setAttribute('aria-disabled', blocked || empty ? 'true' : 'false');
  D.sendBtn.title = blocked ? '正在加载模型列表…' : '';
}

/* ─────────────────────────── 10. 滚动与跟随 ─────────────────────────── */

function gap() {
  const c = D.scroller;
  return c.scrollHeight - c.scrollTop - c.clientHeight;
}

function scrollToBottom() {
  const c = D.scroller;
  if (c.scrollHeight - c.scrollTop - c.clientHeight <= 1) {
    S.lastAutoTop = c.scrollTop;
    S.newCount = 0;
    updateJumpPill();
    return;
  }
  c.scrollTop = c.scrollHeight;      // 瞬时赋值，禁止 smooth / scrollIntoView
  S.lastAutoTop = c.scrollTop;       // 记录"这次是我们自己滚的"，供 scroll 监听区分用户滚动
  S.newCount = 0;
  updateJumpPill();
}

function forceFollow() {
  S.follow = true;
  S.newCount = 0;
  updateJumpPill();
}

function bumpNewCount() {
  S.newCount += 1;
  updateJumpPill();
}

function updateJumpPill() {
  const show = !S.follow;
  D.jumpPill.hidden = !show;
  if (!show) return;
  const text = S.newCount > 0 ? `新消息 ${S.newCount} ↓` : '回到底部 ↓';
  D.jumpPillText.textContent = text;
  D.jumpPill.querySelector('.pill__dot')?.remove();
  if (S.newCount > 0) {
    const dot = el('span', 'pill__dot');
    D.jumpPill.prepend(dot);
  }
}

/* ─────────────────────────── 11. 日志 ─────────────────────────── */

const Logs = {
  lines: [],
  dropped: 0,
  level: 'info',
  query: '',
  regex: false,
  tag: '',
  follow: true,
  newCount: 0,
  ac: null,
  connected: false,
  retry: 0,
  retryTimer: null,
  lastValid: [],

  start() {
    if (this.ac) return;
    this.connect();
  },

  async connect() {
    if (this.ac) return;
    const ac = new AbortController();
    this.ac = ac;
    try {
      const res = await fetch('/api/logs/stream', {
        signal: ac.signal,
        credentials: 'same-origin',
        headers: { Accept: 'text/event-stream' },
      });
      if (res.status === 401) { requireLogin('登录状态已过期，请重新登录。'); return; }
      if (!res.ok || !res.body) throw new Error('bad stream');
      this.connected = true;
      this.retry = 0;
      setLogStatus();
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx = buf.search(/\r?\n\r?\n/);
        while (idx >= 0) {
          const frame = buf.slice(0, idx);
          const m = buf.slice(idx).match(/^\r?\n\r?\n/);
          buf = buf.slice(idx + (m ? m[0].length : 2));
          this.frame(frame);
          idx = buf.search(/\r?\n\r?\n/);
        }
      }
      throw new Error('closed');
    } catch (err) {
      if (ac.signal.aborted) return;
      this.connected = false;
      this.ac = null;
      this.scheduleRetry();
    }
  },

  frame(frame) {
    const data = frame.split('\n').filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).replace(/^ /, '')).join('\n');
    if (!data) return;
    let obj;
    try { obj = JSON.parse(data); } catch { return; }
    if (obj.type !== 'log') return;
    this.push(obj);
  },

  push(item) {
    const line = {
      id: item.id, ts: item.ts, source: item.source || '',
      text: item.line || '',
      level: detectLevel(item.line || ''),
    };
    this.lines.push(line);
    if (this.lines.length > 2000) { this.lines.shift(); this.dropped += 1; }
    if (!S.drawerOpen || S.drawerTab !== 'logs') {
      if (line.level === 'error') setUnreadLog(true);
    }
    if (this.follow) {
      this.renderAppend(line);
      this.scrollEnd();
    } else {
      this.newCount += 1;
      this.updatePill();
    }
  },

  scheduleRetry() {
    if (this.retryTimer) return;
    if (this.retry >= 3) { setLogStatus('日志流已断开'); return; }
    this.retry += 1;
    setLogStatus(`重连中 ${this.retry}/3`);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, 5000);
  },

  stop() {
    if (this.ac) { try { this.ac.abort(); } catch { /* 忽略 */ } }
    this.ac = null;
    this.connected = false;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
  },

  scrollEnd() {
    const host = D.logList;
    host.scrollTop = host.scrollHeight;
  },

  updatePill() {
    D.logPill.hidden = this.follow;
    if (!this.follow) D.logPillText.textContent = `已暂停 · ${this.newCount} 条新日志 ↓`;
  },

  match(line) {
    const lv = { all: 0, info: 1, warn: 2, error: 3 }[this.level] ?? 1;
    if (this.level !== 'all') {
      const rank = { debug: 0, info: 1, ok: 1, warn: 2, error: 3 }[line.level] ?? 1;
      if (rank < lv) return false;
    }
    if (this.tag && !line.text.includes(`[${this.tag}]`) && line.source !== this.tag) return false;
    const q = this.query.trim();
    if (q) {
      if (this.regex) {
        try {
          const re = new RegExp(q, 'i');
          if (!re.test(line.text) && !re.test(line.source)) return false;
        } catch { /* 非法正则时保留上次结果，见 setQuery */ }
      } else {
        const parts = q.split(/\s+/).filter(Boolean);
        const hay = `${line.source} ${line.text}`.toLowerCase();
        for (const p of parts) {
          if (p.startsWith('-')) {
            if (hay.includes(p.slice(1).toLowerCase())) return false;
          } else if (!hay.includes(p.toLowerCase())) return false;
        }
      }
    }
    return true;
  },

  filtered() {
    return this.lines.filter((l) => this.match(l));
  },

  render() {
    const host = D.logList;
    host.textContent = '';
    const list = this.filtered().slice(-800);
    const older = this.filtered().length - list.length;
    if (!this.lines.length) {
      host.appendChild(el('p', 'logempty', '暂无日志。发起一次对话后这里会出现网关请求记录。'));
      return;
    }
    if (!list.length) {
      host.appendChild(el('p', 'logempty', '没有匹配当前筛选的日志。'));
      return;
    }
    if (older > 0) host.appendChild(el('div', 'logellipsis', `已省略 ${fmtNum(older)} 行`));
    for (const line of list) host.appendChild(logRow(line, this.query, this.regex));
    this.scrollEnd();
  },

  renderAppend(line) {
    const host = D.logList;
    if (!this.match(line)) return;
    host.appendChild(logRow(line, this.query, this.regex));
    // DOM 只保留末 800 行
    const rows = host.querySelectorAll('.logrow');
    if (rows.length > 800) {
      const excess = rows.length - 800;
      for (let i = 0; i < excess; i++) rows[i].remove();
      if (!host.querySelector('.logellipsis')) {
        host.insertBefore(el('div', 'logellipsis', `已省略 ${fmtNum(this.dropped + excess)} 行`), host.firstChild);
      }
    }
  },
};

function detectLevel(text) {
  if (/\b(fatal|error|traceback|exception|err)\b/i.test(text) || /\b5\d\d\b/.test(text)) return 'error';
  if (/\bwarn(ing)?\b/i.test(text)) return 'warn';
  if (/\bdebug\b/i.test(text) || /\bdbg\b/i.test(text)) return 'debug';
  if (/\b(ok|success|200)\b/i.test(text)) return 'ok';
  return 'info';
}

const LEVEL_LETTER = { debug: 'D', info: 'I', warn: 'W', error: 'E', ok: 'K' };

function logRow(line, query, isRegex) {
  const row = el('div', 'logrow' + (line.level === 'error' ? ' is-error' : ''));
  const dot = el('span', 'logrow__lvl');
  dot.dataset.l = line.level;
  const letter = el('span', 'logrow__letter', LEVEL_LETTER[line.level] || 'I');
  letter.dataset.l = line.level;
  const ts = el('span', 'logrow__ts', hhmm(line.ts) + ':' + String(new Date((line.ts || 0) * 1000).getSeconds()).padStart(2, '0'));
  const text = el('span', 'logrow__text');
  buildLogText(text, line.text, query, isRegex);
  row.append(dot, letter, ts, text);
  return row;
}

/** 行内 [tag] 可点 + 命中高亮，全程用 DOM 节点，不拼 innerHTML */
function buildLogText(host, text, query, isRegex) {
  const tagRe = /\[([a-zA-Z0-9_.-]{1,24})\]/g;
  let last = 0;
  let m;
  const needles = [];
  if (query && !isRegex) {
    for (const p of query.split(/\s+/)) if (p && !p.startsWith('-')) needles.push(p.toLowerCase());
  }
  while ((m = tagRe.exec(text))) {
    if (m.index > last) appendHighlighted(host, text.slice(last, m.index), needles);
    const btn = el('button', 'logtag', m[0]);
    btn.type = 'button';
    btn.title = `按 [${m[1]}] 筛选`;
    btn.addEventListener('click', () => {
      Logs.tag = m[1];
      renderTagChips();
      Logs.render();
    });
    host.appendChild(btn);
    last = m.index + m[0].length;
  }
  if (last < text.length) appendHighlighted(host, text.slice(last), needles);
}

function appendHighlighted(host, text, needles) {
  if (!needles || !needles.length) { host.appendChild(document.createTextNode(text)); return; }
  let rest = text;
  let guard = 0;
  while (rest && guard++ < 200) {
    let bestIdx = -1;
    let bestLen = 0;
    const lower = rest.toLowerCase();
    for (const n of needles) {
      const i = lower.indexOf(n);
      if (i >= 0 && (bestIdx < 0 || i < bestIdx)) { bestIdx = i; bestLen = n.length; }
    }
    if (bestIdx < 0) break;
    if (bestIdx > 0) host.appendChild(document.createTextNode(rest.slice(0, bestIdx)));
    const mark = el('mark', null, rest.slice(bestIdx, bestIdx + bestLen));
    host.appendChild(mark);
    rest = rest.slice(bestIdx + bestLen);
  }
  if (rest) host.appendChild(document.createTextNode(rest));
}

function setLogStatus(text) {
  if (text) { D.logStatus.textContent = text; D.logStatus.dataset.state = 'err'; return; }
  D.logStatus.textContent = '日志已脱敏：密钥、令牌、路径中的用户名显示为 ***。聊天内容不会出现在这里。';
  delete D.logStatus.dataset.state;
}

function renderTagChips() {
  D.logTagChips.textContent = '';
  if (!Logs.tag) return;
  const chip = el('span', 'tagchip', `标签：${Logs.tag}`);
  const x = el('button', null, '×');
  x.type = 'button';
  x.setAttribute('aria-label', '移除标签筛选');
  x.addEventListener('click', () => { Logs.tag = ''; renderTagChips(); Logs.render(); });
  chip.appendChild(x);
  D.logTagChips.appendChild(chip);
}

/* ─────────────────────────── 12. 片段库 ─────────────────────────── */

async function loadSnippets() {
  try {
    const data = await api('/api/snippets');
    S.snippets = data.snippets || [];
  } catch {
    S.snippets = [];
  }
}

let snipIndex = 0;
let snipFiltered = [];

function openSnippets() {
  if (S.layers.includes('snippetOverlay')) return;
  openLayer('snippetOverlay');
  D.snipSearch.value = '';
  snipIndex = 0;
  renderSnippets();
  setTimeout(() => D.snipSearch.focus(), 20);
}

function renderSnippets() {
  const q = D.snipSearch.value.trim().toLowerCase();
  snipFiltered = S.snippets.filter((s) => {
    if (!q) return true;
    const vars = varNames(s.body || '').join(' ').toLowerCase();
    return (s.title || '').toLowerCase().includes(q)
      || (s.body || '').toLowerCase().includes(q)
      || vars.includes(q);
  });
  snipIndex = clamp(snipIndex, 0, Math.max(0, snipFiltered.length - 1));
  const host = D.snipList;
  host.textContent = '';
  if (!S.snippets.length) {
    host.appendChild(el('p', 'listempty', '片段库是空的。把常写的一段话粘进输入框，按 Ctrl+Shift+S 存为片段。'));
    D.snipDelete.hidden = true;
    return;
  }
  if (!snipFiltered.length) {
    const wrap = el('p', 'listempty', `没有匹配「${q}」的片段。`);
    const clear = el('button', 'linkbtn', '清空搜索');
    clear.type = 'button';
    clear.addEventListener('click', () => { D.snipSearch.value = ''; renderSnippets(); D.snipSearch.focus(); });
    wrap.appendChild(clear);
    host.appendChild(wrap);
    D.snipDelete.hidden = true;
    return;
  }
  snipFiltered.forEach((s, i) => {
    const item = el('button', 'snipitem' + (i === snipIndex ? ' is-on' : ''));
    item.type = 'button';
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', String(i === snipIndex));
    item.appendChild(el('span', 'snipitem__title', s.title || '未命名片段'));
    const body = el('span', 'snipitem__body', (s.body || '').replace(/\s+/g, ' ').slice(0, 80));
    item.appendChild(body);
    item.addEventListener('click', () => { snipIndex = i; insertSnippet(s); });
    item.addEventListener('mouseenter', () => {
      if (snipIndex === i) return;
      snipIndex = i;
      $$('.snipitem', host).forEach((n, j) => {
        n.classList.toggle('is-on', j === i);
        n.setAttribute('aria-selected', String(j === i));
      });
    });
    host.appendChild(item);
  });
  D.snipDelete.hidden = false;
  const on = host.querySelectorAll('.snipitem')[snipIndex];
  if (on) on.scrollIntoView({ block: 'nearest' });
}

function varNames(body) {
  const out = [];
  const re = /\{\{\s*([^{}]{1,40}?)\s*\}\}/g;
  let m;
  while ((m = re.exec(body))) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

async function insertSnippet(snippet) {
  closeLayer('snippetOverlay');
  const body = snippet.body || '';
  const names = varNames(body);
  if (!names.length) { insertAtCursor(body); return; }
  const values = await promptVars(names, body);
  if (values == null) { openSnippets(); return; }
  let text = body;
  for (const [k, v] of Object.entries(values)) {
    const re = new RegExp('\\{\\{\\s*' + escapeRe(k) + '\\s*\\}\\}', 'g');
    text = text.replace(re, () => v);   // 用函数替换，避免 $ 被当作替换模式
  }
  insertAtCursor(text);
}

let varsResolve = null;
let varsState = null;

function promptVars(names, body) {
  openLayer('varsOverlay');
  D.varsError.hidden = true;
  D.varsTitle.textContent = names.length > 5 ? '填充变量（1/' + names.length + '）' : '填充变量';
  varsState = { names, body, values: {}, index: 0, step: names.length > 5 };
  renderVarsFields();
  return new Promise((resolve) => { varsResolve = resolve; });
}

function renderVarsFields() {
  const { names, step, index, values } = varsState;
  const host = D.varsBody;
  host.textContent = '';
  const list = step ? [names[index]] : names;
  for (const name of list) {
    const box = el('label', 'varfield');
    const label = el('span', 'varfield__label');
    label.append(document.createTextNode('{{' + name + '}}'));
    label.appendChild(el('b', null, ' *'));
    if (step) label.appendChild(el('span', 'varfield__step', ` ${index + 1}/${names.length}`));
    const input = el('input', 'field__input');
    input.type = 'text';
    input.maxLength = 200;
    input.value = values[name] || '';
    input.dataset.var = name;
    input.addEventListener('input', () => { values[name] = input.value; });
    input.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        if (step) nextVar(); else submitVars();
      }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelVars(); }
    });
    box.append(label, input);
    host.appendChild(box);
  }
  const first = host.querySelector('input');
  if (first) setTimeout(() => first.focus(), 20);
}

function nextVar() {
  if (!varsState.step) { submitVars(); return; }
  const { names, index, values } = varsState;
  const name = names[index];
  const val = (values[name] || '').trim();
  if (!D.varsSkip.checked && !val) { varsError(`「${name}」不能为空`); return; }
  if (index + 1 < names.length) {
    varsState.index += 1;
    D.varsTitle.textContent = `填充变量（${varsState.index + 1}/${names.length}）`;
    renderVarsFields();
  } else {
    submitVars();
  }
}

function varsError(msg) {
  D.varsError.hidden = false;
  D.varsError.textContent = msg;
}

function submitVars() {
  const { names, values } = varsState;
  for (const name of names) {
    const v = (values[name] || '').trim().replace(/\s+/g, ' ');
    values[name] = v;
    if (!v && !D.varsSkip.checked) {
      varsError(`「${name}」不能为空`);
      const input = D.varsBody.querySelector(`input[data-var="${CSS.escape(name)}"]`);
      if (input) input.focus();
      return;
    }
  }
  const out = {};
  for (const name of names) {
    let v = values[name] || '';
    if (v.length > 200) { v = v.slice(0, 200); toast('已截断到 200 字', 'warn'); }
    out[name] = v || `{{${name}}}`;
  }
  closeLayer('varsOverlay');
  const fn = varsResolve;
  varsResolve = null;
  if (fn) fn(out);
}

function cancelVars() {
  closeLayer('varsOverlay');
  const fn = varsResolve;
  varsResolve = null;
  if (fn) fn(null);
}

function insertAtCursor(text) {
  const input = D.input;
  input.focus();
  const start = input.selectionStart != null ? input.selectionStart : input.value.length;
  const end = input.selectionEnd != null ? input.selectionEnd : start;
  const before = input.value.slice(0, start);
  const prefix = before && !/\s$/.test(before) ? '\n' : '';
  const payload = prefix + text;
  let done = false;
  try { done = document.execCommand('insertText', false, payload); } catch { done = false; }
  if (!done) {
    input.setRangeText(payload, start, end, 'end');
  }
  const idx = input.value.indexOf('{{', start);
  if (idx >= 0) {
    const close = input.value.indexOf('}}', idx);
    if (close > 0) input.setSelectionRange(idx, close + 2);
  }
  autoGrow();
  updateCount();
  updateSendState();
}

async function saveSnippet() {
  const body = D.input.value.trim();
  if (!body) { toast('输入框是空的，先写点内容', 'warn'); return; }
  const title = await promptDialog({ title: '存为片段', value: deriveTitle(body) });
  if (title == null) return;
  try {
    const { snippet } = await api('/api/snippets', { method: 'POST', body: { title, body } });
    S.snippets.unshift(snippet);
    toast('已存为片段');
  } catch (err) {
    toast(`保存失败（${err.code}）`, 'error');
  }
}

/* ─────────────────────────── 13. 折叠块 ─────────────────────────── */

async function loadBlocks() {
  if (!S.session) { S.blocks = []; renderBlocks(); return; }
  try {
    const data = await api(`/api/sessions/${S.session.id}/blocks`);
    S.blocks = data.blocks || [];
    S.blockCache.set(S.session.id, S.blocks);
  } catch {
    D.blockList.textContent = '';
    const wrap = el('p', 'sect__note', '折叠块加载失败。');
    const retry = el('button', 'linkbtn', '重试');
    retry.type = 'button';
    retry.addEventListener('click', loadBlocks);
    wrap.appendChild(retry);
    D.blockList.appendChild(wrap);
    return;
  }
  renderBlocks();
}

function renderBlocks() {
  const host = D.blockList;
  host.textContent = '';
  if (!S.blocks.length) {
    host.appendChild(el('p', 'sect__note', '本会话还没有折叠块。上下文接近上限时会自动生成，原文始终可预览。'));
    D.expandBtn.disabled = true;
    D.expandBtn.setAttribute('aria-disabled', 'true');
    D.blocksHint.textContent = '上下文接近上限时会自动生成，原文始终可预览。';
    return;
  }
  const limit = S.expandLimit;
  if (S.expandError) {
    D.blocksHint.textContent = S.expandError;
    D.blocksHint.style.color = 'var(--c-error)';
  } else {
    D.blocksHint.style.color = '';
    D.blocksHint.textContent = limit
      ? `本次还可还原 ${Math.max(0, limit - S.selectedBlocks.size)} 块（单次上限 ${limit}）`
      : '勾选后点「还原所选」：还原只登记意图，下条消息生效；还原会增大上下文，可能再次触发上下文超限。';
  }
  for (const b of S.blocks) {
    const row = el('div', 'blockrow');
    row.dataset.block = b.block_id;
    row.tabIndex = -1;                     // 供 locator / a11y hash 聚焦
    const checked = S.selectedBlocks.has(b.block_id);
    row.classList.toggle('is-checked', checked);
    const box = el('input');
    box.type = 'checkbox';
    box.checked = checked;
    box.setAttribute('aria-label', `选择 ${b.block_id}`);
    const locked = !checked && limit != null && S.selectedBlocks.size >= limit;
    if (locked) { box.setAttribute('aria-disabled', 'true'); row.setAttribute('aria-disabled', 'true'); }
    box.addEventListener('change', () => {
      if (box.checked) {
        if (limit != null && S.selectedBlocks.size >= limit) {
          box.checked = false;
          toast(`单次最多 ${limit} 块，请先取消一个`, 'warn');
          return;
        }
        S.selectedBlocks.add(b.block_id);
      } else {
        S.selectedBlocks.delete(b.block_id);
      }
      renderBlocks();
    });
    const id = el('span', 'blockrow__id', b.block_id);
    const meta = el('span', 'blockrow__meta', `${b.tier || ''} · ${fmtNum(b.token_count)} tokens · ${relTime(b.created_at)}`);
    const acts = el('div', 'blockrow__acts');
    const peek = el('button', 'iconbtn');
    peek.type = 'button';
    peek.title = '预览';
    peek.setAttribute('aria-label', `预览 ${b.block_id}`);
    peek.appendChild(icon('i-search', 14));
    peek.addEventListener('click', () => openPeek(b.block_id));
    acts.appendChild(peek);
    row.append(box, id, meta, acts);
    host.appendChild(row);
  }
  const n = S.selectedBlocks.size;
  D.expandBtn.textContent = n ? `还原所选（${n}）` : '还原所选';
  D.expandBtn.disabled = !n;
  D.expandBtn.setAttribute('aria-disabled', n ? 'false' : 'true');
}

async function openPeek(blockId) {
  if (!S.session) return;
  openLayer('peekOverlay');
  D.peekTitle.textContent = `${blockId} 预览 · 只读，不占用上下文`;
  D.peekBody.textContent = '加载中…';
  D.peekWarn.hidden = true;
  D.peekOk.focus();
  try {
    const data = await api(`/api/sessions/${S.session.id}/blocks/${encodeURIComponent(blockId)}/peek`);
    D.peekBody.textContent = data.text || '（空）';
    D.peekTitle.textContent = `${data.block_id || blockId} 预览 · ${fmtNum(data.tokens)} tokens · 只读，不占用上下文`;
    if (data.sha256_ok === false) {
      D.peekWarn.hidden = false;
      D.peekWarn.textContent = '校验不一致，此块可能已损坏，建议新开对话。';
    }
  } catch {
    D.peekBody.textContent = '预览加载失败，块可能已被合并。';
  }
}

async function submitExpand() {
  if (!S.session || !S.selectedBlocks.size) return;
  const ids = Array.from(S.selectedBlocks);
  try {
    const res = await api(`/api/sessions/${S.session.id}/expand`, { method: 'POST', body: { block_ids: ids } });
    S.pendingExpand = (res.block_ids || ids).slice();
    S.selectedBlocks.clear();
    S.expandError = null;
    renderBlocks();
    showExpandStatus(S.pendingExpand.length);
    toast(`已登记还原 ${S.pendingExpand.length} 块，下条消息生效`);
  } catch (err) {
    if (err.code === 'expand_limit_exceeded') {
      const p = err.payload || {};
      const limit = p.limit != null ? p.limit : (p.error && p.error.limit);
      if (limit != null) S.expandLimit = limit;
      S.expandError = `一次最多还原 ${limit != null ? limit : '若干'} 块，已选 ${ids.length} 块 —— 请取消多余的勾选。`;
      renderBlocks();
      toast(`一次最多还原 ${limit != null ? limit : '若干'} 块，已选 ${ids.length} 块`, 'error');
      clearTimeout(S.expandErrorTimer);
      S.expandErrorTimer = setTimeout(() => { S.expandError = null; renderBlocks(); }, 5000);
    } else {
      toast(`还原失败（${err.code}）`, 'error');
    }
  }
}

let expandStatusEl = null;
function showExpandStatus(n, { consumed = false } = {}) {
  if (expandStatusEl) expandStatusEl.remove();
  const wrap = el('span', 'statusbar__item');
  wrap.appendChild(el('span', null, consumed ? `已还原 ${n} 块 · 已用于本次请求` : `已还原 ${n} 块`));
  if (!consumed) {
    const undo = el('button', 'linkbtn', '撤销');
    undo.type = 'button';
    undo.addEventListener('click', () => { S.pendingExpand = []; wrap.remove(); expandStatusEl = null; });
    wrap.appendChild(undo);
  }
  D.statusBar.insertBefore(wrap, D.logBtn);
  expandStatusEl = wrap;
}

/* ─────────────────────────── 14. 顶栏 / 状态条 / 用量 ─────────────────────────── */

function updateUsageUi() {
  const total = (S.usage.prompt_tokens || 0) + (S.usage.completion_tokens || 0);
  D.sessionTotal.hidden = !total;
  D.sessionTotal.textContent = total ? `累计 ${fmtNum(total)} tokens` : '';
  const budget = contextBudget();
  const used = S.tokensAfter != null ? S.tokensAfter : S.usage.prompt_tokens;
  D.budgetItem.textContent = '';
  const u = el('span', 'num', used != null ? fmtNum(used) : '—');
  D.budgetItem.append(u, document.createTextNode(' / '));
  D.budgetItem.appendChild(el('span', 'num', budget ? fmtNum(budget) : '—'));
  const pct = budget && used ? used / budget : 0;
  D.budgetItem.classList.toggle('is-alert', pct >= 0.9);
  updateContextUi();
}

function flashTokenSavings(saved) {
  const budget = contextBudget();
  const used = S.tokensAfter != null ? S.tokensAfter : S.usage.prompt_tokens;
  D.budgetItem.textContent = `${fmtNum(used)} / ${fmtNum(budget)} ↓${fmtNum(saved)}`;
  D.budgetItem.classList.add('flash-ok');
  setTimeout(() => { D.budgetItem.classList.remove('flash-ok'); updateUsageUi(); }, 2000);
}

function setConn(state, text) {
  D.connDot.dataset.state = state;
  D.connText.textContent = text;
  D.connDot.parentElement.title = text;
}

function updateStatus() {
  if (!S.gen) setConn(navigator.onLine === false ? 'error' : 'ok', navigator.onLine === false ? '网络已断开' : '就绪');
  const cost = S.lastCost;
  D.costItem.textContent = cost
    ? `${cost.ms < 100 ? '<0.1s' : (cost.ms / 1000).toFixed(1) + 's'} · ${fmtNum(Math.round(cost.tps || 0))} tok/s`
    : '—';
  updateUsageUi();
}

function flashStatus(text, { vramNote = false } = {}) {
  D.connText.textContent = text;
  D.connDot.dataset.state = 'warn';
  setTimeout(() => { if (!S.gen) setConn('ok', '就绪'); }, 3000);
  if (vramNote) {
    // 3 秒后补一行说明：提前回答"按了停止怎么显存没降"
    clearTimeout(S.vramTimer);
    S.vramTimer = setTimeout(() => showVramNote(), 3000);
  }
}

function showVramNote() {
  if (!D.vramNote) return;
  D.vramNote.textContent = '显存池仍按启动配置常驻';
  D.vramNote.hidden = false;
}

function hideVramNote() {
  if (!D.vramNote) return;
  D.vramNote.hidden = true;
  D.vramNote.textContent = '';
}

function setUnreadLog(on) {
  S.unreadLog = on;
  D.logUnread.hidden = !on;
}

/* ─────────────────────────── 15. 抽屉 / 面板 ─────────────────────────── */

function openDrawer(tab = 'config') {
  S.drawerOpen = true;
  D.drawer.classList.add('is-open');
  D.drawer.setAttribute('aria-hidden', 'false');
  D.drawerBtn.setAttribute('aria-expanded', 'true');
  D.layout.dataset.drawer = '1';
  if (window.innerWidth < 768) D.drawer.style.height = S.sheetH;
  selectTab(tab);
  if (tab === 'logs') setUnreadLog(false);
}

function closeDrawer() {
  S.drawerOpen = false;
  D.drawer.classList.remove('is-open');
  D.drawer.setAttribute('aria-hidden', 'true');
  D.drawerBtn.setAttribute('aria-expanded', 'false');
  D.layout.dataset.drawer = '0';
  D.drawer.style.height = '';
}

function toggleDrawer() {
  if (S.drawerOpen) closeDrawer();
  else openDrawer(S.drawerTab || 'config');
}

function selectTab(tab) {
  if (tab === 'blocks') tab = 'config';
  S.drawerTab = tab;
  for (const [id, key] of [[D.tabConfig, 'config'], [D.tabBlocks, 'blocks'], [D.tabLogs, 'logs']]) {
    const on = key === tab;
    id.classList.toggle('is-active', on);
    id.setAttribute('aria-selected', String(on));
  }
  D.paneConfig.hidden = tab !== 'config';
  D.paneLogs.hidden = tab !== 'logs';
  if (tab === 'logs') { Logs.start(); Logs.render(); Logs.updatePill(); }
  if (tab === 'config') loadBlocks();
}

function openSidebar() {
  D.sidebar.classList.add('is-open');
  D.sidebar.setAttribute('aria-hidden', 'false');
  if (window.innerWidth >= 768 && window.innerWidth < 1024) setTimeout(() => D.searchInput.focus(), 60);
}
function closeSidebar() {
  D.sidebar.classList.remove('is-open');
  D.sidebar.setAttribute('aria-hidden', 'true');
}
function toggleSidebar() {
  if (D.sidebar.classList.contains('is-open')) closeSidebar();
  else openSidebar();
}

/* ─────────────────────────── 16. 输入区 ─────────────────────────── */

function autoGrow() {
  const t = D.input;
  t.style.height = 'auto';
  const max = Math.round(window.innerHeight * 0.4);
  t.style.height = Math.min(t.scrollHeight, max) + 'px';
}

function updateCount() {
  const n = D.input.value.length;
  D.charCount.textContent = `${fmtNum(n)} 字`;
  D.charCount.classList.toggle('is-over', n > 8000);
  updateSendState();
}

function loadUserToEdit(m, index) {
  // 清掉之前的编辑标记，再标当前这条
  S.messages.forEach((x) => { if (x.editing) x.editing = false; });
  D.input.value = m.content;
  S.editing = { index, text: m.content };
  m.editing = true;
  D.editBar.hidden = false;
  autoGrow();
  updateCount();
  D.input.setSelectionRange(m.content.length, m.content.length);
  D.input.focus();
  renderMessages();
  scrollToBottom();
}

function loadLastUser() {
  for (let i = S.messages.length - 1; i >= 0; i--) {
    const m = S.messages[i];
    if (m.role === 'user') {
      loadUserToEdit(m, i);
      return;
    }
  }
  toast('还没有可编辑的提问', 'warn');
}

function cancelEdit() {
  S.messages.forEach((x) => { if (x.editing) x.editing = false; });
  S.editing = null;
  D.editBar.hidden = true;
  D.input.value = '';
  autoGrow();
  updateCount();
  renderMessages();
}

function focusSearch() {
  if (window.innerWidth < 768) openSidebar();
  D.searchInput.focus();
  D.searchInput.select();
}

/* ─────────────────────────── 17. 主题 ─────────────────────────── */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  LS.set('bonsai.theme', document.documentElement.dataset.theme);
  const use = D.themeBtn && D.themeBtn.querySelector('use');
  if (use) use.setAttribute('href', document.documentElement.dataset.theme === 'dark' ? '#i-sun' : '#i-moon');
}

/* ─────────────────────────── 18. 草稿 ─────────────────────────── */

function saveDraft() {
  if (S.session) return;
  LS.set('bonsai.draft', { text: D.input.value, model: S.modelId, effort: S.effort });
}

function restoreDraft() {
  const d = LS.get('bonsai.draft', null);
  if (!d) return;
  if (d.text) { D.input.value = d.text; autoGrow(); updateCount(); }
  if (d.model && modelById(d.model)) S.modelId = d.model;
  const m = activeModel();
  if (m) {
    const list = effortsOf(m);
    // 草稿里的档位必须仍然合法，否则回落到「上次选的」/ 模型默认
    S.effort = (d.effort && list.includes(d.effort)) ? d.effort : savedEffort(m);
  }
  renderModelPill();
  renderEfforts();
}

/* ─────────────────────────── 19. 事件绑定 ─────────────────────────── */

let composing = false;

function bindGlobalEvents() {
  /* 登录 */
  D.loginForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const token = D.tokenInput.value.trim();
    if (!token) { D.loginError.hidden = false; D.loginError.textContent = '请输入访问令牌。'; return; }
    tryLogin(token);
  });

  /* 主题 */
  D.themeBtn.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });

  /* 复制全部 / 导出对话 */
  D.copyAllBtn.addEventListener('click', copyAllConversation);
  D.exportBtn.addEventListener('click', exportConversation);

  /* 侧栏 */
  $$('.js-sidebar-toggle').forEach((b) => b.addEventListener('click', toggleSidebar));
  $$('.js-new-session').forEach((b) => b.addEventListener('click', newDraft));
  D.railToggle.addEventListener('click', () => openSidebar());
  D.railSearch.addEventListener('click', focusSearch);
  D.railSnippets.addEventListener('click', openSnippets);
  D.newSessionBtn.addEventListener('click', () => { if (S.selMode) setSelMode(false); newDraft(); });
  D.selModeBtn.addEventListener('click', () => setSelMode(!S.selMode));
  D.selAllBtn.addEventListener('click', selectAllSessions);
  D.selDelBtn.addEventListener('click', bulkDeleteSessions);
  D.selCancelBtn.addEventListener('click', () => setSelMode(false));
  D.compressBtn.addEventListener('click', toggleCompress);
  D.snippetBtn.addEventListener('click', openSnippets);
  D.snippetInline.addEventListener('click', openSnippets);
  D.saveSnippetBtn.addEventListener('click', saveSnippet);

  /* 搜索 */
  let searchTimer = 0;
  D.searchInput.addEventListener('input', () => {
    D.searchClear.hidden = !D.searchInput.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadSessions(D.searchInput.value.trim()), 220);
  });
  D.searchClear.addEventListener('click', () => {
    D.searchInput.value = '';
    D.searchClear.hidden = true;
    loadSessions('');
    D.searchInput.focus();
  });

  /* 标题内联重命名 */
  D.sessionTitle.addEventListener('click', () => {
    if (S.renaming) return;
    if (!S.session) { toast('先发一条消息，草稿才会保存', 'warn'); return; }
    startTitleRename();
  });

  /* 模型 */
  D.modelBtn.addEventListener('click', () => {
    const open = S.layers.includes('modelOverlay');
    if (open) {
      closeLayer('modelOverlay');
      D.modelBtn.setAttribute('aria-expanded', 'false');
      return;
    }
    openLayer('modelOverlay');
    D.modelBtn.setAttribute('aria-expanded', 'true');
    const r = D.modelBtn.getBoundingClientRect();
    const w = Math.min(320, window.innerWidth - 16);
    D.modelPop.style.width = w + 'px';
    D.modelPop.style.top = Math.min(r.bottom + 6, window.innerHeight - 80) + 'px';
    D.modelPop.style.left = clamp(r.right - w, 8, window.innerWidth - w - 8) + 'px';
    const first = D.modelList.querySelector('.mitem');
    if (first) first.focus();
  });
  D.modelClose.addEventListener('click', () => { closeLayer('modelOverlay'); D.modelBtn.setAttribute('aria-expanded', 'false'); });
  D.modelOverlay.addEventListener('click', (e) => {
    if (e.target === D.modelOverlay) { closeLayer('modelOverlay'); D.modelBtn.setAttribute('aria-expanded', 'false'); }
  });
  D.modelRow.addEventListener('click', () => D.modelBtn.click());

  /* 抽屉 */
  $$('.js-drawer-toggle').forEach((b) => b.addEventListener('click', toggleDrawer));
  D.drawerBtn.addEventListener('click', toggleDrawer);
  D.drawerClose.addEventListener('click', closeDrawer);
  D.tabConfig.addEventListener('click', () => selectTab('config'));
  D.tabLogs.addEventListener('click', () => selectTab('logs'));
  D.tabBlocks.addEventListener('click', () => {
    selectTab('config');
    setTimeout(() => D.blocksSection.scrollIntoView({ block: 'start' }), 60);
  });
  D.blocksRefresh.addEventListener('click', loadBlocks);
  D.expandBtn.addEventListener('click', submitExpand);
  D.logBtn.addEventListener('click', () => { openDrawer('logs'); setUnreadLog(false); });

  /* 日志筛选 */
  D.logLevels.addEventListener('click', (e) => {
    const btn = e.target.closest('.lvlchip');
    if (!btn) return;
    Logs.level = btn.dataset.level;
    $$('.lvlchip', D.logLevels).forEach((b) => b.classList.toggle('is-on', b === btn));
    Logs.render();
  });
  D.logFilter.addEventListener('input', () => {
    const q = D.logFilter.value;
    if (Logs.regex) {
      try { new RegExp(q); } catch (err) {
        D.logFilter.parentElement.classList.add('is-invalid');
        D.logFilterErr.hidden = false;
        D.logFilterErr.textContent = '正则不合法：' + (err.message || 'syntax error');
        return; // 保留上次结果
      }
    }
    D.logFilter.parentElement.classList.remove('is-invalid');
    D.logFilterErr.hidden = true;
    Logs.query = q;
    Logs.render();
  });
  D.logRegex.addEventListener('click', () => {
    Logs.regex = !Logs.regex;
    D.logRegex.setAttribute('aria-pressed', String(Logs.regex));
    D.logFilter.parentElement.classList.remove('is-invalid');
    D.logFilterErr.hidden = true;
    Logs.render();
  });
  D.logFollow.addEventListener('click', () => {
    Logs.follow = !Logs.follow;
    D.logFollow.setAttribute('aria-pressed', String(Logs.follow));
    if (Logs.follow) { Logs.newCount = 0; Logs.updatePill(); Logs.scrollEnd(); }
  });
  D.logPill.addEventListener('click', () => {
    Logs.follow = true;
    D.logFollow.setAttribute('aria-pressed', 'true');
    Logs.newCount = 0;
    Logs.updatePill();
    Logs.scrollEnd();
  });
  D.logList.addEventListener('scroll', () => {
    const g = D.logList.scrollHeight - D.logList.scrollTop - D.logList.clientHeight;
    const shouldFollow = g <= 24;
    if (shouldFollow !== Logs.follow) {
      Logs.follow = shouldFollow;
      D.logFollow.setAttribute('aria-pressed', String(Logs.follow));
      if (shouldFollow) Logs.newCount = 0;
      Logs.updatePill();
    }
  });
  D.logCopy.addEventListener('click', async () => {
    const text = Logs.filtered().map((l) => `${new Date((l.ts || 0) * 1000).toLocaleString('zh-CN')} [${l.source}] ${l.text}`).join('\n');
    await copyText(text);
    toast('已复制当前日志');
  });

  /* 发送 */
  D.sendBtn.addEventListener('click', () => {
    if (S.gen) stopGeneration();
    else sendMessage();
  });
  D.input.addEventListener('input', () => { autoGrow(); updateCount(); saveDraft(); });
  D.input.addEventListener('compositionstart', () => { composing = true; });
  D.input.addEventListener('compositionend', () => { setTimeout(() => { composing = false; }, 0); });
  D.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (composing || e.isComposing || e.keyCode === 229) return; // 输入法上屏，不发送
      e.preventDefault();
      if (S.gen) stopGeneration();
      else sendMessage();
      return;
    }
    if (e.key === 'ArrowUp' && !S.gen && !composing) {
      const atStart = D.input.selectionStart === 0 && D.input.selectionEnd === 0;
      if (!D.input.value.trim() || atStart) {
        if (S.messages.some((m) => m.role === 'user')) {
          e.preventDefault();
          loadLastUser();
        }
      }
    }
  });
  D.editCancel.addEventListener('click', cancelEdit);

  /* 滚动跟随 */
  D.scroller.addEventListener('scroll', () => {
    // 只认"用户滚动"：流式跟随里我们逐帧写 scrollTop，内容增长会把 gap 顶大，
    // 若不区分就会在流式中途误判成脱离跟随（症状：一直流、药丸一直显示）。
    if (S.lastAutoTop != null && Math.abs(D.scroller.scrollTop - S.lastAutoTop) <= 4) return;
    const g = gap();
    const threshold = window.innerWidth < 768 ? 48 : 32;
    const shouldFollow = g <= threshold;
    if (shouldFollow !== S.follow) {
      S.follow = shouldFollow;
      if (shouldFollow) S.newCount = 0;
      updateJumpPill();
    }
    if (!shouldFollow && getSelection().type === 'Range') {
      // 用户正在选中文本：不打断复制
      S.follow = false;
      updateJumpPill();
    }
  }, { passive: true });
  D.jumpPill.addEventListener('click', () => { forceFollow(); scrollToBottom(); });

  /* 浮层通用关闭 */
  D.peekClose.addEventListener('click', () => closeLayer('peekOverlay'));
  D.peekOk.addEventListener('click', () => closeLayer('peekOverlay'));
  D.peekOverlay.addEventListener('click', (e) => { if (e.target === D.peekOverlay) closeLayer('peekOverlay'); });
  D.snippetOverlay.addEventListener('click', (e) => {
    if (e.target === D.snippetOverlay) closeLayer('snippetOverlay');
  });
  D.helpOverlay.addEventListener('click', (e) => { if (e.target === D.helpOverlay) closeLayer('helpOverlay'); });
  D.helpClose.addEventListener('click', () => closeLayer('helpOverlay'));
  D.confirmOverlay.addEventListener('click', (e) => { if (e.target === D.confirmOverlay) resolveConfirm(false); });
  D.cfOk.addEventListener('click', () => resolveConfirm(true));
  D.cfCancel.addEventListener('click', () => resolveConfirm(false));
  D.promptOverlay.addEventListener('click', (e) => { if (e.target === D.promptOverlay) resolvePrompt(false); });
  D.pmOk.addEventListener('click', () => resolvePrompt(true));
  D.pmCancel.addEventListener('click', () => resolvePrompt(false));
  D.pmInput.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); resolvePrompt(true); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); resolvePrompt(false); }
  });
  D.varsOk.addEventListener('click', () => submitVars());
  D.varsCancel.addEventListener('click', cancelVars);
  D.varsSkip.addEventListener('change', () => { D.varsError.hidden = true; });

  /* 片段库 */
  D.snipSearch.addEventListener('input', () => { snipIndex = 0; renderSnippets(); });
  D.snipList.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); snipIndex = Math.min(snipIndex + 1, snipFiltered.length - 1); renderSnippets(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); snipIndex = Math.max(snipIndex - 1, 0); renderSnippets(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (snipFiltered[snipIndex]) insertSnippet(snipFiltered[snipIndex]); }
  });
  D.snipSearch.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); snipIndex = Math.min(snipIndex + 1, Math.max(0, snipFiltered.length - 1)); renderSnippets(); D.snipSearch.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); snipIndex = Math.max(snipIndex - 1, 0); renderSnippets(); D.snipSearch.focus(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (snipFiltered[snipIndex]) insertSnippet(snipFiltered[snipIndex]); }
  });
  D.snipDelete.addEventListener('click', async () => {
    const s = snipFiltered[snipIndex];
    if (!s) return;
    const ok = await confirmDialog({
      title: '删除片段',
      text: `删除「${s.title || '未命名'}」？`,
      okText: '删除', danger: true, focus: 'cancel',
    });
    if (!ok) return;
    try {
      await api(`/api/snippets/${s.id}`, { method: 'DELETE' });
      S.snippets = S.snippets.filter((x) => x.id !== s.id);
      renderSnippets();
      toast('已删除片段');
    } catch { toast('删除失败', 'error'); }
  });

  /* 代码块复制（事件委托，DOM 复用也不会丢） */
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy-code]');
    if (!btn) return;
    const block = btn.closest('.codeblock');
    const code = block && block.querySelector('pre code');
    if (!code) return;
    await copyText(code.textContent || '');
    const old = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = old; }, 1100);
  });

  /* 全局键盘 */
  window.addEventListener('keydown', onGlobalKeydown);

  /* 网络状态 */
  window.addEventListener('offline', () => {
    D.netBanner.hidden = false;
    setConn('error', '网络已断开');
  });
  window.addEventListener('online', () => {
    D.netBanner.hidden = true;
    setConn('ok', '就绪');
    const last = S.messages[S.messages.length - 1];
    if (last && last.state === 'interrupted') {
      renderErrorBar(last, {
        code: 'interrupted',
        text: '网络已恢复。本机网关不支持断点续传。',
        actions: [
          { label: '重新生成', kind: 'primary', onClick: () => regenerateFrom(last) },
          { label: '保留并继续', onClick: () => { D.input.value = '继续'; sendMessage(); } },
        ],
      });
    }
  });

  /* 窗口尺寸变化时修正抽屉形态 */
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 1024 && S.drawerOpen) { /* 常驻栏，不需要处理 */ }
    if (window.innerWidth >= 768) closeSidebarIfRail();
  });

  /* 移动端底部面板手势 */
  bindSheetDrag();

  /* 会话标题内联编辑在 startTitleRename 里绑定 */
}

function closeSidebarIfRail() {
  if (window.innerWidth >= 1024) closeSidebar();
}

function startTitleRename() {
  const host = D.sessionTitle;
  const original = S.session ? (S.session.title || '') : '';
  const box = el('div', 'renamebox');
  box.style.maxWidth = '40vw';
  const input = el('input');
  input.type = 'text';
  input.value = original;
  input.maxLength = 200;
  input.setAttribute('aria-label', '会话标题');
  const ok = el('button', 'iconbtn');
  ok.type = 'button';
  ok.setAttribute('aria-label', '保存');
  ok.appendChild(icon('i-check', 12));
  const cancel = el('button', 'iconbtn');
  cancel.type = 'button';
  cancel.setAttribute('aria-label', '取消');
  cancel.appendChild(icon('i-close', 12));
  box.append(input, ok, cancel);
  host.textContent = '';
  host.appendChild(box);
  input.focus();
  input.select();

  let cancelled = false;
  let done = false;
  const restore = () => {
    S.renaming = null;
    host.textContent = S.session ? (S.session.title || '未命名') : '新对话（未保存）';
  };
  const finish = async (commit) => {
    if (done) return;
    if (!commit) { done = true; restore(); return; }
    const v = input.value.trim();
    if (!v) { toast('标题不能为空', 'warn'); input.focus(); return; }
    done = true;
    try {
      await patchSession(S.session.id, { title: v });
      restore();
      renderTitle();
      renderSessionList();
    } catch {
      done = false;
      toast('保存失败，检查网关是否在运行', 'error');
    }
  };
  S.renaming = { cancel: () => { if (!done) { done = true; restore(); } } };
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelled = true; finish(false); }
  });
  input.addEventListener('blur', () => { if (!cancelled) setTimeout(() => finish(true), 0); });
  ok.addEventListener('mousedown', (e) => { e.preventDefault(); finish(true); });
  cancel.addEventListener('mousedown', (e) => { e.preventDefault(); cancelled = true; finish(false); });
}

function onGlobalKeydown(e) {
  const mod = e.ctrlKey || e.metaKey;
  const inField = e.target instanceof HTMLElement
    && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable);

  if (e.key === 'Escape') {
    // 严格栈式：重命名 > 浮层 > 停止生成 > 关抽屉（一次只弹一层）
    if (S.renaming) { S.renaming.cancel(); e.preventDefault(); return; }
    if (S.layers.length) { closeTopLayer(); e.preventDefault(); return; }
    if (S.gen) { stopGeneration(); e.preventDefault(); return; }
    if (S.drawerOpen && (window.innerWidth >= 1024 || window.innerWidth < 768)) { closeDrawer(); e.preventDefault(); return; }
    return;
  }

  if (composing || e.isComposing || e.keyCode === 229) return;

  if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); focusSearch(); return; }
  if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); toggleDrawer(); return; }
  if (e.altKey && !mod && e.key.toLowerCase() === 'p') { e.preventDefault(); openSnippets(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === 's') { e.preventDefault(); saveSnippet(); return; }
  if (e.shiftKey && e.key === '?') { e.preventDefault(); openLayer('helpOverlay'); return; }
  if (mod && e.key === 'Enter') {
    if (S.layers.includes('varsOverlay')) { e.preventDefault(); submitVars(); }
    return;
  }
  if (!inField && S.layers.includes('snippetOverlay')) {
    if (e.key === 'ArrowDown') { e.preventDefault(); snipIndex = Math.min(snipIndex + 1, Math.max(0, snipFiltered.length - 1)); renderSnippets(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); snipIndex = Math.max(snipIndex - 1, 0); renderSnippets(); }
  }
  // 日志抽屉内 Ctrl+F
  if (mod && e.key.toLowerCase() === 'f' && S.drawerOpen && S.drawerTab === 'logs') {
    e.preventDefault();
    D.logFilter.focus();
  }
}

/* 移动端面板三档拖动 */
function bindSheetDrag() {
  let startY = 0;
  let startH = 0;
  let dragging = false;
  D.drawerGrip.addEventListener('pointerdown', (e) => {
    if (window.innerWidth >= 768) return;
    dragging = true;
    startY = e.clientY;
    startH = D.drawer.getBoundingClientRect().height;
    D.drawerGrip.setPointerCapture(e.pointerId);
  });
  D.drawerGrip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dy = startY - e.clientY;
    const next = clamp(startH + dy, 80, window.innerHeight);
    D.drawer.style.height = next + 'px';
  });
  D.drawerGrip.addEventListener('pointerup', (e) => {
    if (!dragging) return;
    dragging = false;
    const h = D.drawer.getBoundingClientRect().height;
    const vh = window.innerHeight;
    const ratios = [0.30, 0.55, 0.88];
    if (h < vh * 0.22) { closeDrawer(); return; }
    const target = ratios.reduce((a, b) => (Math.abs(b * vh - h) < Math.abs(a * vh - h) ? b : a), ratios[0]);
    S.sheetH = Math.round(target * vh) + 'px';
    D.drawer.style.height = S.sheetH;
  });
}

/* ─────────────────────────── 20. 复制 ─────────────────────────── */

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 回退 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch { /* 忽略 */ }
  ta.remove();
  return true;
}

/* ─────────────────────────── 21. 导出对话 ─────────────────────────── */

function conversationText() {
  const lines = [];
  for (const m of S.messages) {
    const who = m.role === 'user' ? '用户' : '助手';
    lines.push(`【${who}】`);
    lines.push(m.content || '');
    lines.push('');
  }
  return lines.join('\n').trim();
}

async function copyAllConversation() {
  if (!S.messages.length) { toast('当前没有可复制的对话内容', 'error'); return; }
  await copyText(conversationText());
  toast('已复制全部对话');
}

function exportConversation() {
  if (!S.messages.length) { toast('当前没有可导出的对话内容', 'error'); return; }
  const title = (S.session && S.session.title) || '未命名对话';
  const stamp = new Date().toISOString().slice(0, 10);
  const lines = [];
  lines.push(`# ${title}`);
  lines.push('');
  lines.push(`> 导出时间：${new Date().toLocaleString('zh-CN')}`);
  lines.push('');
  for (const m of S.messages) {
    const who = m.role === 'user' ? '🧑 用户' : '🤖 助手';
    lines.push(`## ${who}`);
    lines.push('');
    lines.push(m.content || '');
    lines.push('');
  }
  const md = lines.join('\n');
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}_${stamp}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('已导出对话为 Markdown');
}

/* ─────────────────────────── 启动 ─────────────────────────── */

boot();
