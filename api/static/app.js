'use strict';

const KEY_STORAGE = 'kingpilot.apiKey';

const PHASE_LABELS = {
  plan: '规划',
  guard: '安全审查',
  execute: '执行',
  reflect: '反思',
  generate: '生成答复',
};

const STATUS_LABELS = {
  start: '开始',
  success: '完成',
  error: '失败',
};

const STATE_LABELS = {
  running: '进行中',
  success: '完成',
  error: '失败',
};

// 打字机节奏：总时长控制在 1.6 秒左右，长文本自动提高每帧 token 数
const TYPING_INTERVAL_MS = 24;
const TYPING_TARGET_MS = 1600;
const TYPING_TOKEN_PATTERN = /[\u4e00-\u9fff]|[A-Za-z0-9_]+|\s+|[^\s]/g;

const state = {
  apiKey: '',
  sessionId: '',
  sessions: [],
  traces: [],
  approvals: [],
  streaming: false,
  receivedFinal: false,
  pending: null,
  finalRender: null,
  activeTraceId: '',
};

const loginLayer = document.getElementById('login-layer');
const loginForm = document.getElementById('login-form');
const apiKeyInput = document.getElementById('api-key');
const loginError = document.getElementById('login-error');
const appShell = document.getElementById('app-shell');
const logoutButton = document.getElementById('logout');
const streamState = document.getElementById('stream-state');
const sessionList = document.getElementById('session-list');
const newSessionButton = document.getElementById('new-session');
const phaseStatus = document.getElementById('phase-status');
const messagesEl = document.getElementById('messages');
const composer = document.getElementById('composer');
const messageInput = document.getElementById('message-input');
const sendButton = document.getElementById('send-button');
const traceToggle = document.getElementById('trace-toggle');
const tracePanel = document.getElementById('trace-panel');
const traceBackdrop = document.getElementById('trace-backdrop');
const traceHint = document.getElementById('trace-hint');
const traceList = document.getElementById('trace-list');
const traceDetail = document.getElementById('trace-detail');
const traceReport = document.getElementById('trace-report');
const refreshTracesButton = document.getElementById('refresh-traces');
const closeTracesButton = document.getElementById('close-traces');
const closeTraceDetailButton = document.getElementById('close-trace-detail');
const approvalToggle = document.getElementById('approval-toggle');
const approvalPanel = document.getElementById('approval-panel');
const approvalBackdrop = document.getElementById('approval-backdrop');
const approvalList = document.getElementById('approval-list');
const refreshApprovalsButton = document.getElementById('refresh-approvals');
const closeApprovalsButton = document.getElementById('close-approvals');

function readStoredKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE) || '';
  } catch {
    return '';
  }
}

function storeKey(key) {
  try {
    sessionStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* 隐私模式下 sessionStorage 可能不可用：仅保留内存态 */
  }
}

function clearStoredKey() {
  try {
    sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    /* 忽略 */
  }
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': state.apiKey,
      ...(options.headers || {}),
    },
  });

  if (response.status === 401) {
    logout();
    throw new Error('API Key 无效或已过期');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || `请求失败（${response.status}）`);
  }
  return response;
}

function showLogin() {
  appShell.hidden = true;
  loginLayer.hidden = false;
  apiKeyInput.focus();
}

function showApp() {
  loginLayer.hidden = true;
  appShell.hidden = false;
  renderTraceList([]);
  renderApprovalList([]);
  setTraceOpen(false);
  setApprovalOpen(false);
  messageInput.focus();
}

function logout() {
  state.apiKey = '';
  state.sessionId = '';
  state.sessions = [];
  state.traces = [];
  state.approvals = [];
  state.streaming = false;
  state.pending = null;
  state.finalRender = null;
  clearStoredKey();
  messagesEl.replaceChildren();
  sessionList.replaceChildren();
  traceList.replaceChildren();
  approvalList.replaceChildren();
  closeTraceDetail();
  setTraceOpen(false);
  setApprovalOpen(false);
  setPhase('', '');
  setStreaming(false);
  apiKeyInput.value = '';
  loginError.textContent = '';
  showLogin();
}

async function handleLogin(event) {
  event.preventDefault();
  const candidate = apiKeyInput.value.trim();
  if (!candidate) return;

  state.apiKey = candidate;
  loginError.textContent = '';
  try {
    await apiFetch('/v1/sessions');
    storeKey(candidate);
    showApp();
    await refreshSessions();
  } catch (error) {
    state.apiKey = '';
    clearStoredKey();
    loginError.textContent = error.message;
    showLogin();
  }
}

function setPhase(text, tone = '') {
  phaseStatus.textContent = text;
  phaseStatus.className = 'phase-status' + (tone ? ` ${tone}` : '');
}

function setStreaming(active) {
  state.streaming = active;
  messageInput.disabled = active;
  sendButton.disabled = active;
  newSessionButton.disabled = active;
  streamState.textContent = active ? '处理中' : '';
  streamState.classList.toggle('active', active);
}

function scrollMessages() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendNotice(text, isError = false) {
  const notice = document.createElement('div');
  notice.className = 'notice' + (isError ? ' notice-error' : '');
  notice.textContent = text;
  messagesEl.appendChild(notice);
  scrollMessages();
}

function appendMessage(role, content, traceId = '') {
  const article = document.createElement('article');
  article.className = `message message-${role}`;

  const body = document.createElement('div');
  body.className = 'message-body';
  body.textContent = content || '（空）';
  article.appendChild(body);

  if (traceId) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'link-button';
    button.textContent = '查看 Trace';
    button.addEventListener('click', () => openTrace(traceId));
    article.appendChild(button);
  }

  messagesEl.appendChild(article);
  scrollMessages();
}

/* ── 思考过程：阶段时间线 + 可展开的工具调用 ── */

function addDetailRow(list, label, value) {
  const term = document.createElement('dt');
  term.textContent = label;
  const detail = document.createElement('dd');
  detail.textContent = value;
  list.append(term, detail);
  return detail;
}

function createProcessController() {
  const wrapper = document.createElement('div');
  wrapper.className = 'process-block';

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'process-toggle';
  toggle.setAttribute('aria-expanded', 'false');

  const spinner = document.createElement('span');
  spinner.className = 'process-spinner';

  const label = document.createElement('span');
  label.className = 'process-label';
  label.textContent = '思考过程 · 准备中';

  const chevron = document.createElement('span');
  chevron.className = 'process-chevron';
  chevron.textContent = '▸';

  toggle.append(spinner, label, chevron);

  const timeline = document.createElement('ol');
  timeline.className = 'process-timeline';
  timeline.hidden = true;

  const footer = document.createElement('div');
  footer.className = 'process-footer';
  footer.hidden = true;

  const traceLink = document.createElement('button');
  traceLink.type = 'button';
  traceLink.className = 'link-button';
  traceLink.textContent = '查看完整 Trace';
  traceLink.hidden = true;
  footer.appendChild(traceLink);

  wrapper.append(toggle, timeline, footer);

  const openPhases = new Map();
  const pendingTools = new Map();
  let expanded = false;
  let finished = false;
  let hasError = false;
  let toolCount = 0;

  function refreshSummary() {
    const phases = timeline.querySelectorAll('.process-item:not(.process-item-tool)').length;
    const parts = [];
    if (phases) parts.push(`${phases} 个阶段`);
    if (toolCount) parts.push(`${toolCount} 次工具调用`);
    const detail = parts.length ? parts.join(' / ') : '暂无步骤';
    const suffix = hasError ? '（有异常）' : '';
    label.textContent = finished
      ? `思考过程 · ${detail}${suffix}`
      : `思考过程 · 进行中 · ${detail}${suffix}`;
  }

  function setExpanded(next) {
    expanded = next;
    timeline.hidden = !expanded;
    footer.hidden = !expanded || traceLink.hidden;
    toggle.setAttribute('aria-expanded', String(expanded));
    wrapper.classList.toggle('open', expanded);
  }

  toggle.addEventListener('click', () => setExpanded(!expanded));

  function createPhaseItem(name, stateKey, elapsedMs = 0) {
    const item = document.createElement('li');
    item.className = `process-item ${stateKey}`;

    const title = document.createElement('div');
    title.className = 'process-title';

    const nameEl = document.createElement('span');
    nameEl.className = 'process-name';
    nameEl.textContent = name;

    const stateEl = document.createElement('span');
    stateEl.className = 'process-state';
    const elapsed = Math.round(elapsedMs);
    stateEl.textContent =
      STATE_LABELS[stateKey] + (elapsed > 0 ? ` · ${elapsed}ms` : '');

    title.append(nameEl, stateEl);
    item.appendChild(title);
    return { item, stateEl };
  }

  function createToolItem(name) {
    const item = document.createElement('li');
    item.className = 'process-item process-item-tool running';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tool-toggle';
    button.setAttribute('aria-expanded', 'false');

    const nameEl = document.createElement('span');
    nameEl.className = 'process-name';
    nameEl.textContent = `工具 ${name}`;

    const stateEl = document.createElement('span');
    stateEl.className = 'process-state';
    stateEl.textContent = STATE_LABELS.running;

    const itemChevron = document.createElement('span');
    itemChevron.className = 'process-chevron';
    itemChevron.textContent = '▸';

    button.append(nameEl, stateEl, itemChevron);

    const detail = document.createElement('div');
    detail.className = 'process-detail';
    detail.hidden = true;

    const list = document.createElement('dl');
    addDetailRow(list, '工具', name);
    const statusRow = addDetailRow(list, '状态', STATE_LABELS.running);
    const latencyRow = addDetailRow(list, '耗时', '-');
    detail.appendChild(list);

    const hint = document.createElement('p');
    hint.className = 'process-detail-hint';
    hint.textContent = '参数与结果按安全策略不下发，可在完整 Trace 中查看脱敏详情。';
    detail.appendChild(hint);

    button.addEventListener('click', () => {
      detail.hidden = !detail.hidden;
      button.setAttribute('aria-expanded', String(!detail.hidden));
    });

    item.append(button, detail);
    return { item, stateEl, statusRow, latencyRow };
  }

  function setRunningState(entry, stateKey, elapsedMs = 0) {
    entry.item.classList.remove('running', 'success', 'error');
    entry.item.classList.add(stateKey);
    const elapsed = Math.round(elapsedMs);
    entry.stateEl.textContent =
      STATE_LABELS[stateKey] + (elapsed > 0 ? ` · ${elapsed}ms` : '');
  }

  function addPhase(phase, status) {
    const name = PHASE_LABELS[phase] || phase || '未知阶段';
    if (status === 'start') {
      const entry = createPhaseItem(name, 'running');
      entry.startedAt = window.performance.now();
      timeline.appendChild(entry.item);
      openPhases.set(phase, entry);
    } else {
      const stateKey = status === 'error' ? 'error' : 'success';
      const entry = openPhases.get(phase);
      if (entry) {
        openPhases.delete(phase);
        setRunningState(entry, stateKey, window.performance.now() - entry.startedAt);
      } else {
        const created = createPhaseItem(name, stateKey);
        timeline.appendChild(created.item);
      }
      if (stateKey === 'error') hasError = true;
    }
    refreshSummary();
  }

  function startTool(name) {
    const entry = createToolItem(name);
    timeline.appendChild(entry.item);
    if (!pendingTools.has(name)) pendingTools.set(name, []);
    pendingTools.get(name).push(entry);
    toolCount += 1;
    refreshSummary();
  }

  function finishTool(name, status, latencyMs) {
    const stateKey = status === 'error' ? 'error' : 'success';
    const queue = pendingTools.get(name) || [];
    const entry = queue.shift();
    if (entry) {
      setRunningState(entry, stateKey);
      entry.statusRow.textContent = STATE_LABELS[stateKey];
      entry.latencyRow.textContent =
        typeof latencyMs === 'number' && latencyMs > 0 ? `${Math.round(latencyMs)}ms` : '-';
    } else {
      const created = createToolItem(name);
      setRunningState(created, stateKey);
      created.statusRow.textContent = STATE_LABELS[stateKey];
      created.latencyRow.textContent =
        typeof latencyMs === 'number' && latencyMs > 0 ? `${Math.round(latencyMs)}ms` : '-';
      timeline.appendChild(created.item);
      toolCount += 1;
    }
    if (stateKey === 'error') hasError = true;
    refreshSummary();
  }

  function finish(traceId) {
    if (finished) return;
    finished = true;
    spinner.classList.add('done');
    if (traceId) {
      traceLink.hidden = false;
      traceLink.onclick = () => openTrace(traceId);
    }
    footer.hidden = !expanded || traceLink.hidden;
    refreshSummary();
  }

  return { wrapper, addPhase, startTool, finishTool, finish };
}

function appendAssistantPlaceholder() {
  const article = document.createElement('article');
  article.className = 'message message-assistant';

  const controller = createProcessController();
  article.appendChild(controller.wrapper);

  const body = document.createElement('div');
  body.className = 'message-body message-loading';

  const spinner = document.createElement('span');
  spinner.className = 'loading-spinner';
  const text = document.createElement('span');
  text.textContent = '正在思考…';
  body.append(spinner, text);

  article.appendChild(body);
  messagesEl.appendChild(article);
  scrollMessages();

  return { article, body, controller };
}

/* ── 打字机：按 token 粒度渐进渲染最终答复 ── */

function splitTokens(text) {
  return text.match(TYPING_TOKEN_PATTERN) || [];
}

function typeText(element, text) {
  if (!text) {
    element.textContent = '（空）';
    return Promise.resolve();
  }

  const tokens = splitTokens(text);
  const totalTicks = Math.max(1, Math.ceil(TYPING_TARGET_MS / TYPING_INTERVAL_MS));
  const perTick = Math.max(1, Math.ceil(tokens.length / totalTicks));
  const ticks = Math.max(1, Math.ceil(tokens.length / perTick));
  // 短回答放慢单步、长回答加快批量，整体都接近目标时长
  const interval = Math.max(TYPING_INTERVAL_MS, Math.round(TYPING_TARGET_MS / ticks));

  return new Promise((resolve) => {
    let index = 0;
    let timer = null;
    let settled = false;

    element.classList.add('typing');

    const finish = () => {
      if (settled) return;
      settled = true;
      if (timer !== null) window.clearInterval(timer);
      element.textContent = text;
      element.classList.remove('typing');
      scrollMessages();
      resolve();
    };

    // 点击消息体可直接跳过动画
    element.addEventListener('click', finish, { once: true });

    timer = window.setInterval(() => {
      index = Math.min(tokens.length, index + perTick);
      element.textContent = tokens.slice(0, index).join('');
      scrollMessages();
      if (index >= tokens.length) finish();
    }, interval);
  });
}

function renderFinal(payload) {
  const content = payload.content || '（空）';
  const traceId = payload.trace_id || '';
  const pending = state.pending;

  if (!pending) {
    appendMessage('assistant', content, traceId);
    return Promise.resolve();
  }

  state.pending = null;
  const { article, body, controller } = pending;
  controller.finish(traceId);

  body.classList.remove('message-loading');
  body.textContent = '';

  if (traceId) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'link-button';
    button.textContent = '查看 Trace';
    button.addEventListener('click', () => openTrace(traceId));
    article.appendChild(button);
  }

  return typeText(body, content).then(() => {
    if (!payload.blocked) setPhase('完成', 'success');
  });
}

function failPending(message) {
  const pending = state.pending;
  if (!pending) return;
  state.pending = null;
  pending.controller.finish('');
  pending.body.classList.remove('message-loading');
  pending.body.textContent = message || '（未生成答复）';
}

/* ── 会话列表 ── */

function renderSessions() {
  if (!state.sessions.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '暂无会话';
    sessionList.replaceChildren(empty);
    return;
  }

  const nodes = state.sessions.map((session) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className =
      'session-item' + (session.session_id === state.sessionId ? ' active' : '');
    button.textContent = `${session.rounds} 轮 · ${session.session_id}`;
    button.addEventListener('click', () => selectSession(session.session_id));
    return button;
  });
  sessionList.replaceChildren(...nodes);
}

async function refreshSessions() {
  const response = await apiFetch('/v1/sessions');
  const body = await response.json();
  state.sessions = Array.isArray(body.sessions) ? body.sessions : [];
  renderSessions();
}

async function selectSession(sessionId) {
  if (state.streaming) {
    setPhase('请等待当前回答完成', 'error');
    return;
  }

  state.sessionId = sessionId;
  renderSessions();
  messagesEl.replaceChildren();
  renderTraceList([]);
  closeTraceDetail();
  setPhase('加载历史…', 'loading');

  try {
    const response = await apiFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
    );
    const body = await response.json();
    for (const message of body.messages || []) {
      appendMessage(message.role, message.content, message.trace_id || '');
    }
    await refreshTraces();
    setPhase('', '');
  } catch (error) {
    setPhase(error.message, 'error');
    appendNotice(error.message, true);
  }
}

function newSession() {
  if (state.streaming) return;
  state.sessionId = '';
  messagesEl.replaceChildren();
  renderTraceList([]);
  closeTraceDetail();
  setPhase('', '');
  renderSessions();
  messageInput.focus();
}

/* ── Trace 抽屉 ── */

function setTraceOpen(open) {
  if (open) setApprovalOpen(false);
  tracePanel.classList.toggle('open', open);
  tracePanel.setAttribute('aria-hidden', String(!open));
  traceToggle.setAttribute('aria-expanded', String(open));
  traceBackdrop.hidden = !open;
}

function renderTraceList(traces) {
  state.traces = Array.isArray(traces) ? traces : [];
  if (!state.traces.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '暂无 Trace';
    traceList.replaceChildren(empty);
    return;
  }

  const nodes = state.traces.map((trace) => {
    const button = document.createElement('button');
    button.type = 'button';
    const active = trace.trace_id === state.activeTraceId;
    button.className = 'trace-item' + (active ? ' active' : '');
    button.setAttribute('aria-current', active ? 'true' : 'false');
    const input = trace.user_input || '（无输入）';
    button.textContent = `${input} · ${Math.round(trace.total_latency_ms)}ms`;
    button.title = '点击查看 / 收起详情';
    button.addEventListener('click', () => toggleTraceDetail(trace.trace_id));
    return button;
  });
  traceList.replaceChildren(...nodes);
}

async function refreshTraces() {
  if (!state.sessionId) {
    renderTraceList([]);
    return;
  }
  const response = await apiFetch(
    `/v1/traces?session_id=${encodeURIComponent(state.sessionId)}`,
  );
  const body = await response.json();
  renderTraceList(Array.isArray(body.traces) ? body.traces : []);
}

function closeTraceDetail() {
  state.activeTraceId = '';
  traceDetail.hidden = true;
  traceHint.hidden = false;
  traceReport.textContent = '';
  renderTraceList(state.traces);
}

function toggleTraceDetail(traceId) {
  if (state.activeTraceId === traceId && !traceDetail.hidden) {
    closeTraceDetail();
    return;
  }
  openTrace(traceId);
}

async function openTrace(traceId) {
  state.activeTraceId = traceId;
  renderTraceList(state.traces);
  setTraceOpen(true);
  traceDetail.hidden = false;
  traceHint.hidden = true;
  traceReport.textContent = '加载中…';

  try {
    const response = await apiFetch(`/v1/traces/${encodeURIComponent(traceId)}`);
    const body = await response.json();
    traceReport.textContent = body.report_text || '（无报告）';
    traceReport.scrollTop = 0;
  } catch (error) {
    traceReport.textContent = error.message;
  }
}

/* ── 审批抽屉 ── */

const APPROVAL_STATUS_LABELS = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已拒绝',
  expired: '已过期',
};

const EXECUTION_STATUS_LABELS = {
  not_started: '待执行',
  running: '执行中',
  succeeded: '已执行',
  failed: '执行失败',
};

function setApprovalOpen(open) {
  if (open) setTraceOpen(false);
  approvalPanel.classList.toggle('open', open);
  approvalPanel.setAttribute('aria-hidden', String(!open));
  approvalToggle.setAttribute('aria-expanded', String(open));
  approvalBackdrop.hidden = !open;
  if (open) {
    refreshApprovals().catch((error) => setPhase(error.message, 'error'));
  }
}

function approvalMetaRow(card, label, value) {
  const row = document.createElement('div');
  row.className = 'approval-meta';
  const key = document.createElement('span');
  key.className = 'muted';
  key.textContent = label;
  const text = document.createElement('span');
  text.textContent = value;
  row.append(key, text);
  card.appendChild(row);
}

function renderApprovalList(approvals) {
  state.approvals = Array.isArray(approvals) ? approvals : [];
  if (!state.approvals.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '暂无待审批单据';
    approvalList.replaceChildren(empty);
    return;
  }

  const nodes = state.approvals.map((approval) => {
    const card = document.createElement('article');
    card.className = 'approval-card';

    const title = document.createElement('div');
    title.className = 'approval-title';
    const tool = document.createElement('strong');
    tool.textContent = approval.tool_name;
    const badge = document.createElement('span');
    badge.className = `approval-badge approval-${approval.status}`;
    badge.textContent = APPROVAL_STATUS_LABELS[approval.status] || approval.status;
    title.append(tool, badge);
    card.appendChild(title);

    if (approval.summary) {
      const summary = document.createElement('p');
      summary.className = 'approval-summary';
      summary.textContent = approval.summary;
      card.appendChild(summary);
    }

    approvalMetaRow(card, '发起人', `${approval.requester_id}（${approval.requester_role}）`);
    approvalMetaRow(
      card,
      '风险',
      `${Math.round(approval.risk_score)} 分 · ${approval.risk_level || '未评级'}`,
    );
    const approvedVotes = (approval.decisions || []).filter(
      (item) => item.decision === 'approve',
    ).length;
    approvalMetaRow(
      card,
      '票数',
      `${approvedVotes}/${approval.required_approvals}`,
    );
    approvalMetaRow(
      card,
      '执行',
      EXECUTION_STATUS_LABELS[approval.execution_status] || approval.execution_status,
    );
    if (approval.business_key) {
      approvalMetaRow(card, '单号', approval.business_key);
    }

    const actions = document.createElement('div');
    actions.className = 'approval-actions';

    const run = async (action, label) => {
      actions.querySelectorAll('button').forEach((button) => {
        button.disabled = true;
      });
      try {
        const response = await apiFetch(
          `/v1/approvals/${encodeURIComponent(approval.approval_id)}/${action}`,
          { method: 'POST', body: JSON.stringify({}) },
        );
        let result = null;
        try {
          result = await response.json();
        } catch {
          result = null;
        }
        let text = `${label}成功`;
        const execution = result?.execution_status;
        if (action !== 'reject' && execution) {
          text += ` · ${EXECUTION_STATUS_LABELS[execution] || execution}`;
        }
        if (result?.execution_error) {
          text += `（${result.execution_error}）`;
        }
        setPhase(text, result?.execution_error ? 'error' : 'success');
        await refreshApprovals();
      } catch (error) {
        setPhase(error.message, 'error');
        appendNotice(error.message, true);
        actions.querySelectorAll('button').forEach((button) => {
          button.disabled = false;
        });
      }
    };

    if (approval.can_decide) {
      const approveButton = document.createElement('button');
      approveButton.type = 'button';
      approveButton.textContent = '通过';
      approveButton.addEventListener('click', () => run('approve', '审批通过'));

      const rejectButton = document.createElement('button');
      rejectButton.type = 'button';
      rejectButton.className = 'ghost';
      rejectButton.textContent = '拒绝';
      rejectButton.addEventListener('click', () => run('reject', '拒绝'));

      actions.append(approveButton, rejectButton);
    }

    if (
      approval.status === 'approved' &&
      approval.execution_status !== 'succeeded' &&
      approval.execution_status !== 'running'
    ) {
      const executeButton = document.createElement('button');
      executeButton.type = 'button';
      executeButton.textContent = '执行';
      executeButton.addEventListener('click', () => run('execute', '执行'));
      actions.appendChild(executeButton);
    }

    if (actions.childElementCount) card.appendChild(actions);
    return card;
  });
  approvalList.replaceChildren(...nodes);
}

async function refreshApprovals() {
  const response = await apiFetch('/v1/approvals?scope=inbox');
  const body = await response.json();
  renderApprovalList(Array.isArray(body.approvals) ? body.approvals : []);
}

/* ── SSE 解析与事件处理 ── */

function parseFrame(frame) {
  let eventName = 'message';
  const dataLines = [];

  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') eventName = value;
    if (field === 'data') dataLines.push(value);
  }

  if (!dataLines.length) return null;
  try {
    return { type: eventName, payload: JSON.parse(dataLines.join('\n')) };
  } catch {
    return null;
  }
}

function handleEvent({ type, payload }) {
  if (type === 'phase') {
    const phase = PHASE_LABELS[payload.phase] || payload.phase;
    const status = STATUS_LABELS[payload.status] || payload.status;
    const tone = payload.status === 'error' ? 'error' : payload.status === 'start' ? 'loading' : '';
    setPhase(`${phase} · ${status}`, tone);
    if (state.pending) state.pending.controller.addPhase(payload.phase, payload.status);
    return;
  }

  if (type === 'tool') {
    if (payload.status === 'start') {
      setPhase(`调用工具 ${payload.tool_name}…`, 'loading');
      if (state.pending) state.pending.controller.startTool(payload.tool_name);
      return;
    }
    const latency = payload.latency_ms
      ? `（${Math.round(payload.latency_ms)}ms）`
      : '';
    const label = payload.status === 'error' ? '失败' : '完成';
    setPhase(
      `工具 ${payload.tool_name} · ${label}${latency}`,
      payload.status === 'error' ? 'error' : '',
    );
    if (state.pending) {
      state.pending.controller.finishTool(
        payload.tool_name,
        payload.status,
        payload.latency_ms,
      );
    }
    return;
  }

  if (type === 'final') {
    state.receivedFinal = true;
    state.sessionId = payload.session_id || state.sessionId;
    state.finalRender = renderFinal(payload);
    if (payload.hallucination_warning) {
      appendNotice('提示：部分内容未能与工具原始结果直接核对，请留意。');
    }
    if (payload.approval_pending) {
      appendNotice(`已提交审批（${payload.approval_id}），审批通过后自动执行。`);
      refreshApprovals().catch(() => {});
    }
    setPhase(
      payload.blocked ? '已按安全策略阻断' : '正在输出答复…',
      payload.blocked ? 'error' : 'loading',
    );
    return;
  }

  if (type === 'error') {
    const message = payload.message || '请求失败';
    setPhase(message, 'error');
    appendNotice(message, true);
    failPending(message);
    return;
  }

  if (type === 'done') {
    if (!phaseStatus.textContent) setPhase('完成', 'success');
  }
}

async function streamChat(message) {
  const response = await apiFetch('/v1/chat/stream', {
    method: 'POST',
    body: JSON.stringify({ message, session_id: state.sessionId }),
  });

  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
    let index;
    while ((index = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const event = parseFrame(frame);
      if (event) handleEvent(event);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    const event = parseFrame(buffer.replace(/\r\n/g, '\n'));
    if (event) handleEvent(event);
  }
}

async function sendMessage() {
  if (state.streaming) return;

  const message = messageInput.value.trim();
  if (!message) return;

  messageInput.value = '';
  appendMessage('user', message);
  state.receivedFinal = false;
  state.finalRender = null;
  state.pending = appendAssistantPlaceholder();
  setStreaming(true);
  setPhase('等待响应…', 'loading');

  try {
    await streamChat(message);
    if (!state.receivedFinal) {
      throw new Error('流式连接提前结束，请重试');
    }
    if (state.finalRender) await state.finalRender;
  } catch (error) {
    setPhase(error.message, 'error');
    appendNotice(error.message, true);
    failPending('（未生成答复，请重试）');
  } finally {
    state.pending = null;
    state.finalRender = null;
    setStreaming(false);
    await Promise.allSettled([refreshSessions(), refreshTraces()]);
    messageInput.focus();
  }
}

function bindEvents() {
  loginForm.addEventListener('submit', handleLogin);
  logoutButton.addEventListener('click', logout);
  newSessionButton.addEventListener('click', newSession);
  refreshTracesButton.addEventListener('click', () => {
    refreshTraces().catch((error) => setPhase(error.message, 'error'));
  });

  traceToggle.addEventListener('click', () => {
    setTraceOpen(!tracePanel.classList.contains('open'));
  });
  closeTracesButton.addEventListener('click', () => setTraceOpen(false));
  traceBackdrop.addEventListener('click', () => setTraceOpen(false));
  closeTraceDetailButton.addEventListener('click', closeTraceDetail);

  approvalToggle.addEventListener('click', () => {
    setApprovalOpen(!approvalPanel.classList.contains('open'));
  });
  closeApprovalsButton.addEventListener('click', () => setApprovalOpen(false));
  approvalBackdrop.addEventListener('click', () => setApprovalOpen(false));
  refreshApprovalsButton.addEventListener('click', () => {
    refreshApprovals().catch((error) => setPhase(error.message, 'error'));
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (tracePanel.classList.contains('open')) setTraceOpen(false);
    if (approvalPanel.classList.contains('open')) setApprovalOpen(false);
  });

  composer.addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage();
  });

  messageInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
}

async function init() {
  bindEvents();
  const stored = readStoredKey();
  if (!stored) {
    showLogin();
    return;
  }

  state.apiKey = stored;
  showApp();
  try {
    await refreshSessions();
  } catch {
    state.apiKey = '';
    clearStoredKey();
    loginError.textContent = '登录状态已失效，请重新输入 API Key';
    showLogin();
  }
}

init();
