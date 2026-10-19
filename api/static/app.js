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

const state = {
  apiKey: '',
  sessionId: '',
  sessions: [],
  streaming: false,
  receivedFinal: false,
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
const traceList = document.getElementById('trace-list');
const traceReport = document.getElementById('trace-report');
const refreshTracesButton = document.getElementById('refresh-traces');

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
  messageInput.focus();
}

function logout() {
  state.apiKey = '';
  state.sessionId = '';
  state.sessions = [];
  state.streaming = false;
  clearStoredKey();
  messagesEl.replaceChildren();
  sessionList.replaceChildren();
  traceList.replaceChildren();
  traceReport.textContent = '';
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
  streamState.textContent = active ? '处理中…' : '';
}

function appendNotice(text, isError = false) {
  const notice = document.createElement('div');
  notice.className = 'notice' + (isError ? ' notice-error' : '');
  notice.textContent = text;
  messagesEl.appendChild(notice);
  messagesEl.scrollTop = messagesEl.scrollHeight;
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
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

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
  traceReport.textContent = '';
  setPhase('加载历史…', '');

  try {
    const response = await apiFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
    );
    const body = await response.json();
    for (const message of body.messages || []) {
      appendMessage(message.role, message.content);
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
  traceReport.textContent = '';
  setPhase('', '');
  renderSessions();
  messageInput.focus();
}

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
    setPhase(`${phase} · ${status}`, payload.status === 'error' ? 'error' : '');
    return;
  }

  if (type === 'tool') {
    const status = STATUS_LABELS[payload.status] || payload.status;
    const latency = payload.latency_ms
      ? `（${Math.round(payload.latency_ms)}ms）`
      : '';
    setPhase(`工具 ${payload.tool_name} · ${status}${latency}`, '');
    return;
  }

  if (type === 'final') {
    state.receivedFinal = true;
    state.sessionId = payload.session_id || state.sessionId;
    appendMessage('assistant', payload.content, payload.trace_id);
    if (payload.hallucination_warning) {
      appendNotice('提示：部分内容未能与工具原始结果直接核对，请留意。');
    }
    setPhase(payload.blocked ? '已按安全策略阻断' : '完成', payload.blocked ? 'error' : 'success');
    return;
  }

  if (type === 'error') {
    setPhase(payload.message || '请求失败', 'error');
    appendNotice(payload.message || '请求失败', true);
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
  setStreaming(true);
  setPhase('等待响应…', '');

  try {
    await streamChat(message);
    if (!state.receivedFinal) {
      throw new Error('流式连接提前结束，请重试');
    }
  } catch (error) {
    setPhase(error.message, 'error');
    appendNotice(error.message, true);
  } finally {
    setStreaming(false);
    await Promise.allSettled([refreshSessions(), refreshTraces()]);
    messageInput.focus();
  }
}

function renderTraceList(traces) {
  if (!traces.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '暂无 Trace';
    traceList.replaceChildren(empty);
    return;
  }

  const nodes = traces.map((trace) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'trace-item';
    const input = trace.user_input || '（无输入）';
    button.textContent = `${input} · ${Math.round(trace.total_latency_ms)}ms`;
    button.addEventListener('click', () => openTrace(trace.trace_id));
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

async function openTrace(traceId) {
  try {
    const response = await apiFetch(`/v1/traces/${encodeURIComponent(traceId)}`);
    const body = await response.json();
    traceReport.textContent = body.report_text || '（无报告）';
  } catch (error) {
    traceReport.textContent = error.message;
  }
}

function bindEvents() {
  loginForm.addEventListener('submit', handleLogin);
  logoutButton.addEventListener('click', logout);
  newSessionButton.addEventListener('click', newSession);
  refreshTracesButton.addEventListener('click', () => {
    refreshTraces().catch((error) => setPhase(error.message, 'error'));
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
