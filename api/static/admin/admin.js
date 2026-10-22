'use strict';

const KEY_STORAGE = 'kingpilot.apiKey';
const STATS_DATASETS = [
  'sales_orders',
  'purchase_orders',
  'purchase_inbounds',
  'customers',
  'suppliers',
  'materials',
  'inventory',
];

const TABS = [
  { kind: 'changes', label: '变更记录' },
  { kind: 'sales_orders', label: '销售订单' },
  { kind: 'purchase_orders', label: '采购订单' },
  { kind: 'purchase_inbounds', label: '采购入库单' },
  { kind: 'customers', label: '客户' },
  { kind: 'suppliers', label: '供应商' },
  { kind: 'materials', label: '物料' },
  { kind: 'inventory', label: '即时库存' },
];

const FORM_LABELS = {
  SAL_Order: '销售订单',
  PUR_PurchaseOrder: '采购订单',
  STK_InStock: '采购入库单',
};

const ACTION_LABELS = {
  save: '创建',
  edit: '修改',
  submit: '提交',
  audit: '审核',
  unaudit: '反审核',
  delete: '删除',
};

const STATUS_LABELS = {
  Z: '暂存',
  A: '创建',
  B: '审核中',
  C: '已审核',
  D: '重新审核',
};

const RISK_LABELS = {
  auto: '自动',
  confirm: '单票确认',
  dual_approve: '双人复核',
  read_only: '只读',
};

const FIELD_LABELS = {
  FBillNo: '单号',
  FDate: '日期',
  FDocumentStatus: '状态',
  'FCustomerID.FNumber': '客户编码',
  'FCustomerID.FName': '客户',
  'FSupplierID.FNumber': '供应商编码',
  'FSupplierID.FName': '供应商',
  'FMaterialId.FNumber': '物料编码',
  'FMaterialId.FName': '物料',
  'FStockId.FName': '仓库',
  FSrcBillNo: '源采购订单',
  FQty: '数量',
  FTaxPrice: '含税单价',
  FRealQty: '实收数量',
  FAmount: '金额',
  FBillAllAmount: '金额',
  'FSalerId.FName': '销售员',
  FNote: '备注',
  FNumber: '编码',
  FID: '内码',
  FName: '名称',
  FShortName: '简称',
  FBaseQty: '库存量',
};

const DATA_LABELS = {
  sales_orders: '销售订单',
  purchase_orders: '采购订单',
  purchase_inbounds: '采购入库单',
  customers: '客户',
  suppliers: '供应商',
  materials: '物料',
  inventory: '即时库存',
};

const state = {
  active: 'changes',
  changes: null,
  dataset: null,
};

const $ = (id) => document.getElementById(id);

function buildNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function getKey() {
  return sessionStorage.getItem(KEY_STORAGE) || '';
}

function showLogin(message) {
  $('login-layer').hidden = false;
  $('app-shell').hidden = true;
  $('login-error').textContent = message || '';
}

function hideLogin() {
  $('login-layer').hidden = true;
  $('app-shell').hidden = false;
}

async function api(path) {
  const response = await fetch(path, {
    headers: { 'X-API-Key': getKey() },
    cache: 'no-store',
  });
  if (response.status === 401) {
    sessionStorage.removeItem(KEY_STORAGE);
    showLogin('API Key 无效或已过期');
    throw new Error('unauthorized');
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body && body.error ? body.error.message : '请求失败';
    throw new Error(message);
  }
  return body;
}

function formatTime(value) {
  if (!value) {
    return '—';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

function chip(text, className) {
  return buildNode('span', `chip ${className || ''}`.trim(), text);
}

function statusChip(value) {
  const label = STATUS_LABELS[value] || value || '—';
  return chip(label, `status-${value || 'unknown'}`);
}

function resultChip(value) {
  if (value === 'success') {
    return chip('成功', 'success');
  }
  if (value === 'failed') {
    return chip('失败', 'failed');
  }
  return chip(value || '—');
}

/* ── 总览 ── */

function renderStats(overview) {
  const container = $('stats');
  container.replaceChildren();

  const cards = [
    { label: '变更记录', value: overview.changes_total, tab: 'changes' },
    { label: '成功', value: overview.changes_success, tab: 'changes', status: 'success' },
    {
      label: '失败',
      value: overview.changes_failed,
      danger: overview.changes_failed > 0,
      tab: 'changes',
      status: 'failed',
    },
  ];
  for (const kind of STATS_DATASETS) {
    const count = overview.datasets ? overview.datasets[kind] : undefined;
    cards.push({
      label: DATA_LABELS[kind] || kind,
      value: count === undefined || count < 0 ? '—' : count,
      muted: count === undefined || count < 0,
      tab: kind,
    });
  }

  for (const card of cards) {
    const box = buildNode('button', 'stat');
    box.type = 'button';
    box.title = `查看${card.label}`;
    box.append(buildNode('div', 'stat-label', card.label));
    const value = buildNode('div', 'stat-value', card.value);
    if (card.danger) {
      value.classList.add('danger');
    }
    if (card.muted) {
      value.classList.add('muted-value');
    }
    box.append(value);
    box.addEventListener('click', () => {
      state.active = card.tab;
      if (card.tab === 'changes') {
        $('status-filter').value = card.status || '';
      }
      renderTabs();
      syncFilters();
      loadActive();
    });
    container.append(box);
  }

  const badge = $('chain-badge');
  badge.classList.remove('ok', 'bad');
  if (overview.chain_valid) {
    badge.textContent = '账本链完整';
    badge.classList.add('ok');
  } else {
    badge.textContent = '账本链异常';
    badge.classList.add('bad');
  }
}

/* ── 标签页 ── */

function renderTabs() {
  const nav = $('tabs');
  nav.replaceChildren();
  for (const tab of TABS) {
    const button = buildNode('button', 'tab', tab.label);
    button.type = 'button';
    button.dataset.kind = tab.kind;
    if (tab.kind === state.active) {
      button.classList.add('active');
    }
    button.addEventListener('click', () => {
      state.active = tab.kind;
      renderTabs();
      syncFilters();
      loadActive();
    });
    nav.append(button);
  }
}

function syncFilters() {
  const isChanges = state.active === 'changes';
  $('form-filter').hidden = !isChanges;
  $('action-filter').hidden = !isChanges;
  $('status-filter').hidden = !isChanges;
}

/* ── 数据加载 ── */

async function loadOverview() {
  const overview = await api('/v1/admin/overview');
  renderStats(overview);
}

async function loadActive() {
  const hint = $('result-hint');
  hint.textContent = '加载中…';
  $('empty').hidden = true;
  try {
    if (state.active === 'changes') {
      const params = new URLSearchParams();
      const form = $('form-filter').value;
      const action = $('action-filter').value;
      const status = $('status-filter').value;
      const search = $('search').value.trim();
      if (form) {
        params.set('form_id', form);
      }
      if (action) {
        params.set('action', action);
      }
      if (status) {
        params.set('status', status);
      }
      if (search) {
        params.set('q', search);
      }
      params.set('limit', '200');
      const body = await api(`/v1/admin/changes?${params.toString()}`);
      state.changes = body;
      state.dataset = null;
      renderChanges(body);
      hint.textContent = `共 ${body.total} 条`;
    } else {
      const params = new URLSearchParams();
      const search = $('search').value.trim();
      if (search) {
        params.set('q', search);
      }
      const body = await api(`/v1/admin/datasets/${state.active}?${params.toString()}`);
      state.dataset = body;
      state.changes = null;
      renderDataset(body);
      hint.textContent = `${body.label} · 共 ${body.total} 条`;
    }
  } catch (error) {
    hint.textContent = error.message || '加载失败';
    renderEmpty();
  }
}

/* ── 渲染：变更记录 ── */

function renderChanges(body) {
  const head = $('table-head');
  head.replaceChildren();
  const headerRow = buildNode('tr');
  for (const label of ['时间', '单据', '单号', '操作', '结果', '操作人', '风险', '变更字段']) {
    headerRow.append(buildNode('th', '', label));
  }
  head.append(headerRow);

  const tbody = $('table-body');
  tbody.replaceChildren();
  if (!body.items.length) {
    renderEmpty();
    return;
  }
  $('empty').hidden = true;

  body.items.forEach((item, index) => {
    const row = buildNode('tr');
    row.append(buildNode('td', 'mono', formatTime(item.timestamp)));
    row.append(buildNode('td', '', FORM_LABELS[item.form_id] || item.form_id));
    row.append(buildNode('td', 'mono strong', item.business_key || '—'));
    row.append(buildNode('td', '', ACTION_LABELS[item.action] || item.action));
    const result = buildNode('td');
    result.append(resultChip(item.status));
    row.append(result);
    row.append(buildNode('td', '', item.user_id || '—'));
    const risk = buildNode('td');
    const riskText = `${Number(item.risk_score || 0).toFixed(1)} · ${
      RISK_LABELS[item.risk_level] || item.risk_level || '—'
    }`;
    risk.append(chip(riskText, `risk-${item.risk_level || 'unknown'}`));
    row.append(risk);
    const fields = item.field_diff.map((diff) => diff.field_name);
    row.append(buildNode('td', 'dim', fields.length ? fields.join(', ') : '—'));
    row.addEventListener('click', () => openChangeDrawer(item));
    row.dataset.index = String(index);
    tbody.append(row);
  });
}

/* ── 渲染：数据集 ── */

function renderDataset(body) {
  const head = $('table-head');
  head.replaceChildren();
  const headerRow = buildNode('tr');
  for (const field of body.fields) {
    headerRow.append(buildNode('th', '', FIELD_LABELS[field] || field));
  }
  head.append(headerRow);

  const tbody = $('table-body');
  tbody.replaceChildren();
  if (!body.rows.length) {
    renderEmpty();
    return;
  }
  $('empty').hidden = true;

  body.rows.forEach((rowData) => {
    const row = buildNode('tr');
    for (const field of body.fields) {
      const value = rowData[field];
      const cell = buildNode('td');
      if (field === 'FDocumentStatus') {
        cell.append(statusChip(value));
      } else if (field === 'FBillNo' || field === 'FNumber' || field === 'FSrcBillNo') {
        cell.className = 'mono strong';
        cell.textContent = value === null || value === undefined ? '—' : String(value);
      } else {
        cell.textContent =
          value === null || value === undefined || value === '' ? '—' : String(value);
      }
      row.append(cell);
    }
    row.addEventListener('click', () => openDatasetDrawer(rowData, body.fields));
    tbody.append(row);
  });
}

function renderEmpty() {
  $('table-head').replaceChildren();
  $('table-body').replaceChildren();
  $('empty').hidden = false;
}

/* ── 抽屉 ── */

function openDrawer(title) {
  $('drawer-title').textContent = title;
  $('drawer-body').replaceChildren();
  $('drawer-backdrop').hidden = false;
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  $('drawer-backdrop').hidden = true;
}

function section(title, nodes) {
  const wrapper = buildNode('section');
  wrapper.append(buildNode('h3', 'section-title', title));
  for (const node of nodes) {
    wrapper.append(node);
  }
  return wrapper;
}

function metaGrid(items) {
  const grid = buildNode('div', 'meta-grid');
  for (const item of items) {
    const box = buildNode('div', 'meta-item');
    box.append(buildNode('span', 'meta-label', item.label));
    const value =
      item.value === undefined || item.value === null || item.value === '' ? '—' : item.value;
    box.append(buildNode('span', 'meta-value', value));
    grid.append(box);
  }
  return grid;
}

function diffTable(diffs) {
  if (!diffs.length) {
    return buildNode('p', 'dim', '该记录没有字段级 diff（创建类记录通常只保留写后结果）。');
  }
  const table = buildNode('table', 'diff-table');
  const head = buildNode('thead');
  const headRow = buildNode('tr');
  headRow.append(buildNode('th', '', '字段'));
  headRow.append(buildNode('th', '', '写前'));
  headRow.append(buildNode('th', '', '写后'));
  head.append(headRow);
  table.append(head);

  const body = buildNode('tbody');
  for (const diff of diffs) {
    const row = buildNode('tr');
    row.append(buildNode('td', 'mono', diff.field_name));
    row.append(buildNode('td', 'old-value', diff.old_value === null ? '—' : String(diff.old_value)));
    row.append(buildNode('td', 'new-value', diff.new_value === null ? '—' : String(diff.new_value)));
    body.append(row);
  }
  table.append(body);
  return table;
}

function jsonBlock(value) {
  const pre = buildNode('pre');
  pre.textContent = JSON.stringify(value === undefined ? null : value, null, 2);
  return pre;
}

function openChangeDrawer(item) {
  openDrawer(`变更详情 · ${item.business_key || item.record_id}`);
  const body = $('drawer-body');

  body.append(
    section('基本信息', [
      metaGrid([
        { label: '单据类型', value: FORM_LABELS[item.form_id] || item.form_id },
        { label: '单号', value: item.business_key },
        { label: '操作', value: ACTION_LABELS[item.action] || item.action },
        { label: '执行结果', value: item.status === 'success' ? '成功' : '失败' },
        { label: '操作人', value: item.user_id },
        {
          label: '风险',
          value: `${Number(item.risk_score || 0).toFixed(1)} · ${
            RISK_LABELS[item.risk_level] || item.risk_level || '—'
          }`,
        },
        { label: '时间', value: formatTime(item.timestamp) },
        { label: 'Trace ID', value: item.trace_id },
      ]),
    ])
  );

  const chainNodes = [];
  if (item.approval_chain.length) {
    const chain = buildNode('div', 'chain');
    for (const step of item.approval_chain) {
      chain.append(chip(step));
    }
    chainNodes.push(chain);
  } else {
    chainNodes.push(buildNode('p', 'dim', '无审批链记录'));
  }
  body.append(section('审批链', chainNodes));

  if (item.user_input) {
    body.append(section('用户原始输入', [buildNode('p', '', item.user_input)]));
  }

  body.append(section('字段变更（diff）', [diffTable(item.field_diff)]));

  const snapshots = buildNode('div', 'snapshot-grid');
  const before = buildNode('div');
  before.append(buildNode('h3', 'section-title', '写前快照'));
  before.append(jsonBlock(item.before_snapshot));
  const after = buildNode('div');
  after.append(buildNode('h3', 'section-title', '写后快照'));
  after.append(jsonBlock(item.after_snapshot));
  snapshots.append(before, after);
  body.append(section('快照', [snapshots]));

  body.append(section('工具参数', [jsonBlock(item.tool_params)]));
  body.append(section('ERP 响应', [jsonBlock(item.api_response)]));
  body.append(section('记录哈希', [buildNode('p', 'mono', item.record_hash || '—')]));
}

function openDatasetDrawer(rowData, fields) {
  const label = DATA_LABELS[state.active] || state.active;
  const billNo = rowData.FBillNo || rowData.FNumber || rowData['FMaterialId.FNumber'] || '';
  openDrawer(`${label}${billNo ? ' · ' + billNo : ''}`);
  const body = $('drawer-body');

  const items = fields.map((field) => ({
    label: FIELD_LABELS[field] || field,
    value: rowData[field],
  }));
  body.append(section('字段', [metaGrid(items)]));
  body.append(section('原始数据', [jsonBlock(rowData)]));
}

/* ── 启动 ── */

async function boot() {
  hideLogin();
  renderTabs();
  syncFilters();
  await loadOverview();
  await loadActive();
}

function bindEvents() {
  let searchTimer = 0;

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const key = $('api-key').value.trim();
    if (!key) {
      $('login-error').textContent = '请输入 API Key';
      return;
    }
    sessionStorage.setItem(KEY_STORAGE, key);
    $('login-error').textContent = '';
    try {
      await boot();
    } catch (error) {
      if (error.message !== 'unauthorized') {
        showLogin(error.message || '进入失败');
      }
    }
  });

  $('logout').addEventListener('click', () => {
    sessionStorage.removeItem(KEY_STORAGE);
    $('api-key').value = '';
    showLogin('');
  });

  $('refresh').addEventListener('click', async () => {
    const button = $('refresh');
    button.disabled = true;
    try {
      await loadOverview();
      await loadActive();
    } catch (error) {
      $('result-hint').textContent = error.message || '刷新失败';
    } finally {
      button.disabled = false;
    }
  });

  $('apply').addEventListener('click', () => loadActive());

  $('search').addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadActive(), 350);
  });

  $('search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      window.clearTimeout(searchTimer);
      loadActive();
    }
  });

  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-backdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeDrawer();
    }
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  bindEvents();
  if (getKey()) {
    try {
      await boot();
      return;
    } catch (error) {
      if (error.message !== 'unauthorized') {
        showLogin(error.message || '进入失败');
      }
      return;
    }
  }
  showLogin('');
});
