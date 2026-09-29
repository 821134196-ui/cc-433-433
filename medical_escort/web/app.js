/* 康柏陪诊前端（原生 JS，无构建依赖） */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  role: 'dispatch',
  escortId: null,
  patientCode: null,
  meta: { statuses: [], stages: [], scopes: [], incidents: [] },
  appts: [],
  view: 'calendar',
  calYear: 0, calMonth: 0, calDay: '',
  detailId: null,
};

const api = {
  async get(path) {
    const res = await fetch(path);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  },
  async post(path, body = {}) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, ...auth() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `HTTP ${res.status}`);
      err.escalation = !!data.escalation;
      throw err;
    }
    return data;
  },
};

function auth() {
  const a = { role: state.role };
  if (state.role === 'escort') a.escort_id = state.escortId;
  if (state.role === 'patient') a.patient_code = state.patientCode;
  return a;
}
function qs(extra = {}) {
  const p = new URLSearchParams(auth());
  Object.entries(extra).forEach(([k, v]) => v != null && v !== '' && p.set(k, v));
  return '?' + p.toString();
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  setTimeout(() => t.classList.add('hidden'), 2600);
}
function fmtDT(s) { return esc((s || '').replace('T', ' ')); }
function toInputDT(s) { return (s || '').replace(' ', 'T').slice(0, 16); }
function fromInputDT(v) { return v.replace('T', ' ').slice(0, 16); }
function statusBadge(a) {
  const cls = ['discharged'].includes(a.status) ? 'badge-green'
    : ['cancelled', 'rejected'].includes(a.status) ? 'badge-red'
    : ['arrived', 'waiting', 'in_exam', 'completed', 'claimed'].includes(a.status) ? ''
    : 'badge-amber';
  return `<span class="badge badge-status ${cls}">${esc(a.status_label)}</span>`;
}

// ============ 启动 ============
async function init() {
  state.meta = await api.get('/api/meta');
  await loadActors();
  bindUI();
  const today = new Date();
  state.calYear = today.getFullYear();
  state.calMonth = today.getMonth();
  state.calDay = today.toISOString().slice(0, 10);
  await refresh();
}

async function loadActors() {
  const role = state.role;
  if (role !== 'patient') {
    const { items } = await api.get('/api/escorts?' + qs());
    state.escorts = items;
    if (!state.escortId || !items.some(e => e.id === state.escortId)) {
      state.escortId = items[0]?.id ?? null;
    }
    $('#escortSelect').innerHTML = items.map(e =>
      `<option value="${e.id}" ${e.id === state.escortId ? 'selected' : ''}>${esc(e.code)} ${esc(e.name)}</option>`).join('');
  }
  if (role === 'dispatch') {
    const { items } = await api.get('/api/patients?' + qs());
    state.patients = items;
    $('#patientSelect').innerHTML = items.map(p =>
      `<option value="${esc(p.code)}">${esc(p.code)} ${esc(p.display_name)}</option>`).join('');
    if (!state.patientCode) state.patientCode = items[0]?.code ?? null;
  }
  if (role === 'patient') {
    // 演示：患者视角从一个固定的演示患者列表里选（数据均为模拟）
    state.patientOptions = [
      { code: 'P-1001', name: '王*' }, { code: 'P-1002', name: '李*' },
      { code: 'P-1003', name: '张*' }, { code: 'P-1004', name: '赵*' },
    ];
    if (!state.patientOptions.some(p => p.code === state.patientCode)) {
      state.patientCode = state.patientOptions[0].code;
    }
    $('#patientSelect').innerHTML = state.patientOptions.map(p =>
      `<option value="${p.code}" ${p.code === state.patientCode ? 'selected' : ''}>${p.code} ${p.name}</option>`).join('');
  }
}

function bindUI() {
  $('#roleSelect').addEventListener('change', async e => {
    state.role = e.target.value;
    applyRoleChrome();
    await loadActors();
    await refresh();
  });
  $('#escortSelect').addEventListener('change', async e => {
    state.escortId = Number(e.target.value);
    await refresh();
  });
  $('#patientSelect').addEventListener('change', async e => {
    state.patientCode = e.target.value;
    await refresh();
  });
  $('#refreshBtn').addEventListener('click', refresh);
  $$('.nav-item').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
  $('#taskStatusFilter').addEventListener('change', renderTasks);
  $('#newApptBtn').addEventListener('click', openCreateModal);
  $('#retryNotifyBtn').addEventListener('click', async () => {
    const r = await api.post('/api/notifications/retry', {});
    toast(`已重发，成功 ${r.retried_ok} 条`, 'ok');
    await refresh();
  });
  $('#timelinePicker').addEventListener('change', e => renderTimeline(Number(e.target.value)));
  $('#handoffPicker').addEventListener('change', e => renderHandoffs(Number(e.target.value)));
  $('#drawerMask').addEventListener('click', closeDrawer);

  document.addEventListener('click', delegatedClick);
}

function applyRoleChrome() {
  const r = state.role;
  $('#escortWrap').classList.toggle('hidden', r !== 'escort');
  $('#patientWrap').classList.toggle('hidden', r !== 'patient');
  $('#escortHome').classList.toggle('hidden', r !== 'escort');
  $$('[data-dispatch-only]').forEach(el => el.classList.toggle('hidden', r !== 'dispatch'));
  // 患者端不显示“授权事项”中的人工审核队列（在视图内部控制）
}

async function switchView(v) {
  state.view = v;
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('hidden', s.dataset.view !== v));
  await refresh();
}

async function refresh() {
  const data = await api.get('/api/appointments' + qs());
  state.appts = data.items;
  renderEscortHome();
  if (state.view === 'calendar') renderCalendar();
  if (state.view === 'tasks') renderTasks();
  if (state.view === 'timeline') renderTimelinePicker();
  if (state.view === 'consents') renderConsents();
  if (state.view === 'handoffs') renderHandoffPicker();
  if (state.view === 'reviews') renderReviews();
}

// ============ 陪诊员移动端首屏 ============
const NEXT_STAGE = {
  claimed: ['arrived', '到达签到'],
  arrived: ['waiting', '候诊签到'],
  waiting: ['in_exam', '进入检查'],
  in_exam: ['completed', '检查完成'],
  completed: ['discharged', '离院交接'],
};
function activeTask() {
  const order = ['in_exam', 'waiting', 'arrived', 'claimed', 'completed', 'scheduled'];
  const mine = state.appts.filter(a => a.escort_id === state.escortId);
  for (const st of order) {
    const a = mine.find(x => x.status === st);
    if (a) return a;
  }
  return null;
}
function renderEscortHome() {
  const box = $('#escortHome');
  if (state.role !== 'escort') { box.innerHTML = ''; return; }
  const a = activeTask();
  if (!a) {
    box.innerHTML = `<div class="eh-current">当前没有进行中的任务</div>
      <div class="eh-next"><div class="lab">下一步</div><div class="val">前往「陪诊任务」查看可接单预约</div></div>`;
    return;
  }
  const last = a.timeline[a.timeline.length - 1];
  const place = last ? last.location || last.stage_label : (a.route?.steps?.[0]?.place || '待出发');
  let nextText = '按院内路线前往检查科室';
  const nxt = NEXT_STAGE[a.status];
  if (nxt) {
    if (a.status === 'claimed' && a.route?.steps?.[0]) {
      const s0 = a.route.steps[0];
      nextText = `${s0.place} · ${s0.action}`;
    } else if (a.status === 'arrived') {
      nextText = '前往对应楼层分诊台提交申请单、候诊签到';
    } else if (a.status === 'waiting') {
      nextText = '听候叫号，陪同患者进入检查室';
    } else if (a.status === 'in_exam') {
      nextText = '检查结束后在出口等候，完成检查签到';
    } else if (a.status === 'completed') {
      nextText = '与家属/接收人完成离院交接并签到';
    }
  }
  box.innerHTML = `
    <div class="eh-row">
      <div>
        <div class="eh-current">当前地点</div>
        <div class="eh-place">${esc(place)}</div>
      </div>
      ${statusBadge(a)}
    </div>
    <div class="eh-next">
      <div class="lab">下一步 · ${esc(a.department)} / ${esc(a.exam_item)}</div>
      <div class="val">${esc(nextText)}</div>
    </div>
    <div class="eh-actions">
      ${nxt ? `<button class="eh-btn primary" data-act="checkin-quick" data-id="${a.id}" data-stage="${nxt[0]}">${nxt[1]}</button>`
        : '<button class="eh-btn primary" disabled>服务已结束</button>'}
      <button class="eh-btn secondary" data-act="open" data-id="${a.id}">任务详情</button>
    </div>
    <div class="eh-mini"><span>预约 ${esc(a.code)}</span><span>患者 ${esc(a.display_name)}</span>
      <span>时间 ${fmtDT(a.scheduled_start)}</span></div>`;
}

// ============ 日历 ============
function renderCalendar() {
  const y = state.calYear, m = state.calMonth;
  $('#calMonth').textContent = `${y} 年 ${m + 1} 月（点击日期查看当日预约）`;
  const first = new Date(y, m, 1);
  const startOffset = first.getDay();
  const days = new Date(y, m + 1, 0).getDate();
  const prevDays = new Date(y, m, 0).getDate();
  const cells = [];
  ['日', '一', '二', '三', '四', '五', '六'].forEach(d =>
    cells.push(`<div class="cal-dow">${d}</div>`));
  for (let i = 0; i < startOffset; i++) {
    cells.push(`<div class="cal-cell outside"><div class="cal-date">${prevDays - startOffset + i + 1}</div></div>`);
  }
  const today = new Date().toISOString().slice(0, 10);
  for (let d = 1; d <= days; d++) {
    const ds = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    const dayAppts = state.appts.filter(a => a.scheduled_start.slice(0, 10) === ds);
    const chips = dayAppts.slice(0, 3).map(a => {
      const cls = ['discharged'].includes(a.status) ? 'done'
        : ['cancelled', 'rejected'].includes(a.status) ? 'bad'
        : a.status === 'scheduled' ? '' : a.status === 'rescheduling' ? 'bad' : 'claimed';
      return `<div class="cal-chip ${cls}" title="${esc(a.department)} ${fmtDT(a.scheduled_start)}">${esc(a.department)} ${esc(a.scheduled_start.slice(11, 16))}</div>`;
    }).join('');
    const more = dayAppts.length > 3 ? `<div class="cal-chip">+${dayAppts.length - 3}</div>` : '';
    cells.push(`<div class="cal-cell ${ds === today ? 'cal-today' : ''} ${ds === state.calDay ? 'selected' : ''}" data-day="${ds}">
      <div class="cal-date">${d}</div>${chips}${more}</div>`);
  }
  $('#calGrid').innerHTML = cells.join('');
  $$('#calGrid .cal-cell[data-day]').forEach(c => c.addEventListener('click', () => {
    state.calDay = c.dataset.day;
    renderCalendar();
    renderDayList();
  }));
  renderDayList();
}
function renderDayList() {
  $('#calDayTitle').textContent = `${state.calDay} 当日预约`;
  const list = state.appts.filter(a => a.scheduled_start.slice(0, 10) === state.calDay);
  $('#calDayList').innerHTML = list.length ? list.map(apptCard).join('')
    : '<div class="hint">当日无预约</div>';
}

// ============ 任务卡片 ============
function renderTasks() {
  const sel = $('#taskStatusFilter');
  if (!sel.options.length) {
    sel.innerHTML = '<option value="">全部状态</option>' +
      state.meta.statuses.map(s => `<option value="${s.value}">${s.label}</option>`).join('');
  }
  const f = sel.value;
  const list = state.appts.filter(a => !f || a.status === f);
  $('#taskList').innerHTML = list.length ? list.map(apptCard).join('')
    : '<div class="hint">暂无任务</div>';
}

function apptCard(a) {
  const canClaim = state.role === 'escort' && a.status === 'scheduled';
  const mine = state.role === 'escort' && a.escort_id === state.escortId;
  const acts = [];
  if (canClaim) acts.push(`<button class="btn btn-amber btn-sm" data-act="claim" data-id="${a.id}">接单</button>`);
  if (mine && NEXT_STAGE[a.status]) {
    const n = NEXT_STAGE[a.status];
    acts.push(`<button class="btn btn-sm" data-act="checkin-quick" data-id="${a.id}" data-stage="${n[0]}">${n[1]}</button>`);
  }
  if (state.role === 'dispatch' && a.status === 'scheduled') {
    acts.push(`<button class="btn btn-ghost btn-sm" data-act="reschedule" data-id="${a.id}">临时改期</button>`);
  }
  if (state.role === 'dispatch' && !['discharged', 'cancelled', 'rejected'].includes(a.status)) {
    acts.push(`<button class="btn btn-ghost btn-sm" data-act="cancel" data-id="${a.id}">检查取消</button>`);
  }
  if (!['discharged', 'cancelled', 'rejected'].includes(a.status)) {
    acts.push(`<button class="btn btn-ghost btn-sm" data-act="refuse" data-id="${a.id}">患者拒绝陪同</button>`);
  }
  acts.push(`<button class="btn btn-ghost btn-sm" data-act="open" data-id="${a.id}">详情</button>`);
  return `<div class="card">
    <div class="card-top">
      <div>
        <div class="card-title">${esc(a.department)} · ${esc(a.exam_item)} ${statusBadge(a)}</div>
        <div class="card-sub">${esc(a.code)} · 患者 ${esc(a.display_name)}（${esc(a.gender)} ${a.age}岁）
          · ${fmtDT(a.scheduled_start)}–${fmtDT(a.scheduled_end)}
          ${a.escort_name ? '· 陪诊员 ' + esc(a.escort_name) : '· 待接单'}
          · 行动辅助：${esc(a.mobility_needs || '步行')}</div>
      </div>
      <div class="badges">${(a.consents || []).slice(0, 3).map(c =>
        `<span class="badge badge-amber">${esc(c.label)}</span>`).join('')}</div>
    </div>
    ${acts.length ? `<div class="card-actions">${acts.join('')}</div>` : ''}
  </div>`;
}

// ============ 检查时间线 ============
function renderTimelinePicker() {
  const sel = $('#timelinePicker');
  const cur = sel.value || state.detailId;
  sel.innerHTML = state.appts.map(a =>
    `<option value="${a.id}" ${String(a.id) === String(cur) ? 'selected' : ''}>${esc(a.code)} ${esc(a.department)} · ${esc(a.display_name)} · ${esc(a.status_label)}</option>`).join('');
  const id = Number(sel.value);
  if (id) renderTimeline(id);
}
function renderTimeline(id) {
  const a = state.appts.find(x => x.id === id);
  if (!a) { $('#timelinePanel').innerHTML = ''; return; }
  const stageTimes = {};
  const stageLocs = {};
  a.timeline.forEach(t => { stageTimes[t.stage] = t.created_at; stageLocs[t.stage] = t.location; });
  const statusOrder = ['claimed', 'arrived', 'waiting', 'in_exam', 'completed', 'discharged'];
  function modelsIndex(st) {
    // claimed->到达之前, arrived->0 ...
    return { claimed: -1, arrived: 0, waiting: 1, in_exam: 2, completed: 3, discharged: 4 }[st];
  }
  const bad = ['cancelled', 'rejected'].includes(a.status);
  const nodesHtml = state.meta.stages.map(s => {
    const idx = state.meta.stages.findIndex(x => x.value === s.value);
    const done = !!stageTimes[s.value];
    const cur = !done && !bad && modelsIndex(a.status) === idx;
    return `<div class="node ${done ? 'done' : ''} ${cur ? 'current' : ''} ${bad && !done ? 'missed' : ''}">
      <div class="node-dot">${done ? '✓' : idx + 1}</div>
      <div class="node-label">${esc(s.label)}</div>
      <div class="node-time">${done ? fmtDT(stageTimes[s.value]) : (bad ? '未发生' : '待签到')}</div>
      <div class="node-loc">${esc(stageLocs[s.value] || '')}</div>
    </div>`;
  }).join('');

  const routeSteps = (a.route?.steps || []).map(s =>
    `<div class="scope-item"><span><b>${s.seq}. ${esc(s.place)}</b>
      <div class="mini">${esc(s.action)} · 约 ${s.est_min} 分钟 · ${esc(s.access || '')}</div></span></div>`).join('');
  const incidents = (a.incidents || []).map(i =>
    `<div class="handoff-card"><b>${esc(state.meta.incidents.find(x => x.value === i.kind)?.label || i.kind)}</b>
      <div class="card-sub">${esc(i.detail)} ${i.old_value ? `（${fmtDT(i.old_value)} → ${fmtDT(i.new_value)}）` : ''}</div>
      <div class="audit">${fmtDT(i.created_at)}</div></div>`).join('') || '<div class="hint">无异常事件</div>';

  $('#timelinePanel').innerHTML = `
    ${bad ? `<div class="card" style="border-color:#e3b4af;margin-bottom:12px">
      <b class="badge badge-red">${esc(a.status_label)}</b>
      <span class="card-sub">${esc(a.cancel_reason || '')}</span></div>` : ''}
    <div class="timeline-wrap">
      <div class="node-line">${nodesHtml}</div>
    </div>
    <div class="tl-detail">
      <div class="card">
        <div class="section-title">院内路线 ${a.route ? `<span class="hint">版本 v${a.route.version}${a.route.reason ? ' · ' + esc(a.route.reason) : ''}</span>` : ''}</div>
        ${routeSteps || '<div class="hint">暂无路线</div>'}
        ${state.role !== 'patient' ? `<div class="card-actions">
          <button class="btn btn-ghost-amber btn-sm" data-act="route-change" data-id="${a.id}">模拟路线变更</button></div>` : ''}
      </div>
      <div class="card"><div class="section-title">异常事件留痕</div>${incidents}</div>
    </div>
    <div class="card-actions"><button class="btn btn-ghost btn-sm" data-act="open" data-id="${a.id}">打开完整详情</button></div>`;
}

// ============ 授权事项 ============
async function renderConsents() {
  const panels = state.appts.map(a => {
    const granted = new Set((a.consents || []).map(c => c.scope_code));
    const scopes = state.meta.scopes.map(s =>
      `<div class="scope-item ${granted.has(s.value) ? 'on' : 'off'}">
        <span>${granted.has(s.value) ? '✅' : '⬜'} <b>${esc(s.label)}</b></span></div>`).join('');
    return `<div class="card">
      <div class="card-top"><div class="card-title">${esc(a.code)} · ${esc(a.department)}</div>
        ${statusBadge(a)}</div>
      <div class="card-sub">患者 ${esc(a.display_name)} · ${fmtDT(a.scheduled_start)}</div>
      <div class="scope-grid" style="margin-top:10px">${scopes}</div>
      ${state.role === 'escort' && a.escort_id === state.escortId ? `
        <div class="card-actions">
          <button class="btn btn-ghost-amber btn-sm" data-act="scoped" data-id="${a.id}" data-scope="payment">申请代缴费用（未授权）</button>
          <button class="btn btn-ghost-amber btn-sm" data-act="scoped" data-id="${a.id}" data-scope="med_report">申请领取检查结果</button>
        </div>` : ''}
    </div>`;
  }).join('');
  $('#consentPanels').innerHTML = panels || '<div class="hint">暂无预约</div>';

  if (state.role === 'patient') {
    $('#escalationList').innerHTML = '<div class="hint">人工确认队列仅调度台可见</div>';
    return;
  }
  const { items } = await api.get('/api/escalations' + qs({ status: 'pending' }));
  $('#escalationList').innerHTML = items.length ? items.map(x => `
    <div class="card">
      <div class="card-top">
        <div><b>${esc(x.appointment_code || ('#' + x.appointment_id))}</b>
          申请 <span class="badge badge-amber">${esc(state.meta.scopes.find(s => s.value === x.requested_scope)?.label || x.requested_scope)}</span>
          <div class="card-sub">${esc(x.reason)}</div>
          <div class="audit">申请人：${esc(x.requested_by)} · ${fmtDT(x.created_at)}</div></div>
        <span class="badge badge-amber">待人工确认</span>
      </div>
      ${state.role === 'dispatch' ? `<div class="card-actions">
        <button class="btn btn-sm" data-act="escalate-ok" data-eid="${x.id}">批准并追加授权</button>
        <button class="btn btn-ghost btn-sm" data-act="escalate-no" data-eid="${x.id}">驳回</button>
      </div>` : ''}
    </div>`).join('') : '<div class="hint">没有待处理的越权申请</div>';
}

// ============ 交接记录 ============
function renderHandoffPicker() {
  const sel = $('#handoffPicker');
  const cur = sel.value;
  sel.innerHTML = state.appts.map(a =>
    `<option value="${a.id}" ${String(a.id) === String(cur) ? 'selected' : ''}>${esc(a.code)} ${esc(a.department)} · ${esc(a.display_name)}</option>`).join('');
  if (sel.value) renderHandoffs(Number(sel.value));
}
function renderHandoffs(id) {
  const a = state.appts.find(x => x.id === id);
  if (!a) { $('#handoffPanel').innerHTML = ''; return; }
  const rows = (a.handoffs || []).map(h => `
    <div class="handoff-row">
      <div class="handoff-seq">${h.seq}</div>
      <div class="handoff-card">
        <div><b>${esc(h.from_party)}</b> → <b>${esc(h.to_party)}</b>
          ${h.patient_confirmed ? '<span class="badge badge-green">患者/家属确认</span>' : ''}</div>
        <div class="card-sub">地点：${esc(h.location)} · 交接物：${esc(h.items || '—')}</div>
        ${h.note ? `<div class="card-sub">备注：${esc(h.note)}</div>` : ''}
        <div class="audit">交接时间（系统留痕）：<b>${fmtDT(h.handoff_time)}</b> · 记录于 ${fmtDT(h.created_at)}</div>
      </div>
    </div>`).join('') || '<div class="hint">暂无交接记录</div>';
  const canAdd = state.role !== 'patient' && a.status !== 'discharged';
  $('#handoffPanel').innerHTML = `
    <div class="card">${rows}
      ${canAdd ? `
      <div class="section-title">新增交接（对象、时间、地点必须留痕）</div>
      <div class="form-grid">
        <label>交出方<input id="hf-from" placeholder="如：陪诊员-陈岭"></label>
        <label>接收方<input id="hf-to" placeholder="如：检查科护士 / 家属"></label>
        <label>交接地点<input id="hf-loc" placeholder="如：二层放射分诊台"></label>
        <label>交接物品/资料<input id="hf-items" placeholder="如：申请单、医保卡、影像袋"></label>
        <label class="full">备注<input id="hf-note"></label>
        <label class="full"><input type="checkbox" id="hf-confirm"> 患者/家属在场确认</label>
      </div>
      <div class="card-actions"><button class="btn btn-amber btn-sm" data-act="handoff-add" data-id="${a.id}">提交交接记录</button></div>`
      : '<div class="hint">服务已离院归档，交接链已封存</div>'}
    </div>`;
}

// ============ 评价汇总 ============
async function renderReviews() {
  const sid = state.role === 'escort' ? state.escortId : null;
  const s = await api.get('/api/reviews/summary' + qs(sid ? { escort_id: sid } : {}));
  const stars = n => '★'.repeat(n) + '☆'.repeat(5 - n);
  $('#reviewPanel').innerHTML = `
    <div class="review-stats">
      <div class="stat-tile"><div class="num amber">${s.avg_rating}</div><div class="lab">平均评分</div></div>
      <div class="stat-tile"><div class="num">${s.count}</div><div class="lab">评价总数</div></div>
      <div class="stat-tile"><div class="num">${s.five}</div><div class="lab">5 星</div></div>
      <div class="stat-tile"><div class="num">${s.four}</div><div class="lab">4 星</div></div>
      <div class="stat-tile"><div class="num">${s.low}</div><div class="lab">3 星及以下</div></div>
    </div>
    <div class="card-list">${s.latest.map(r => `
      <div class="card"><div class="card-top">
        <div><span style="color:var(--amber)">${stars(r.rating)}</span>
          <b> ${esc(r.appointment_code)}</b>
          <div class="card-sub">${esc(r.tags || '')}</div>
          ${r.comment ? `<div class="card-sub">“${esc(r.comment)}”</div>` : ''}</div>
        <div class="audit">${fmtDT(r.created_at)}</div>
      </div></div>`).join('') || '<div class="hint">暂无已完成评价</div>'}</div>`;
}

// ============ 详情抽屉 ============
let drawerAppt = null;
async function openDrawer(id) {
  const a0 = state.appts.find(x => x.id === id);
  // 重新拉取（带角色裁剪的完整详情）
  const a = await api.get('/api/appointments/' + id + qs());
  drawerAppt = a;
  state.detailId = id;
  const granted = new Set((a.consents || []).map(c => c.scope_code));
  const scopeChips = state.meta.scopes.map(s =>
    `<span class="badge ${granted.has(s.value) ? 'badge-green' : ''}">${granted.has(s.value) ? '✓' : '—'} ${esc(s.label)}</span>`).join(' ');
  const atts = (a.attachments || []).map(t => `
    <div class="att-item">
      <span>📎 ${esc(t.filename)} <span class="att-sens">敏感 · ${esc(t.category_label)} · ${esc(t.visibility)}</span></span>
      <a class="btn btn-ghost btn-sm" href="/api/attachments/${t.id}/download${qs()}" target="_blank">按权限查看</a>
    </div>`).join('') || '<div class="hint">当前视角下无可展示附件（最小权限）</div>';

  // 操作按钮区
  const mine = state.role === 'escort' && a.escort_id === state.escortId;
  const acts = [];
  if (state.role === 'escort' && a.status === 'scheduled')
    acts.push(['claim', '接单', 'btn-amber']);
  if (mine && NEXT_STAGE[a.status])
    acts.push(['checkin', NEXT_STAGE[a.status][1] + '（填写地点）', '']);
  if (mine && a.status !== 'discharged')
    acts.push(['late', '上报陪诊员迟到', 'btn-ghost'], ['material', '上报资料缺失', 'btn-ghost']);
  if (state.role === 'dispatch' && !['discharged', 'cancelled', 'rejected'].includes(a.status))
    acts.push(['reschedule', '临时改期', 'btn-ghost'], ['cancel', '检查取消', 'btn-ghost']);
  if (!['discharged', 'cancelled', 'rejected'].includes(a.status))
    acts.push(['refuse', '患者拒绝陪同', 'btn-ghost']);
  if (state.role !== 'patient')
    acts.push(['route-change', '路线变更', 'btn-ghost-amber']);
  if (a.status === 'discharged' && !a.review)
    acts.push(['review', '提交服务评价', 'btn-amber']);
  const btns = acts.map(([act, label, cls]) =>
    `<button class="btn ${cls} btn-sm" data-drawer-act="${act}" data-id="${a.id}">${label}</button>`).join('');

  $('#drawer').innerHTML = `
    <div class="view-head"><h3>${esc(a.department)} · ${esc(a.exam_item)}</h3>
      <button class="btn btn-ghost btn-sm" id="drawerClose">关闭</button></div>
    <div class="badges" style="margin-bottom:10px">${statusBadge(a)}
      <span class="badge">${esc(a.code)}</span></div>
    <dl class="kv">
      <dt>患者</dt><dd>${esc(a.full_name ?? a.display_name)}（编号 ${esc(a.patient_code)}）</dd>
      <dt>证件号</dt><dd>${esc(a.id_document || '无权查看')}</dd>
      <dt>联系电话</dt><dd>${esc(a.contact_phone ?? '接单后可见')}</dd>
      <dt>检查时间</dt><dd>${fmtDT(a.scheduled_start)} – ${fmtDT(a.scheduled_end)}</dd>
      <dt>行动辅助</dt><dd>${esc(a.mobility_needs || '步行')}</dd>
      <dt>检查前要求</dt><dd>${esc(a.special_requirements ?? '接单后可见')}</dd>
      <dt>陪诊员</dt><dd>${esc(a.escort_name || '待接单')}</dd>
      ${state.role === 'dispatch' ? `<dt>内部备注</dt><dd>${esc(a.internal_note || '—')}</dd>` : ''}
      ${a.cancel_reason ? `<dt>终止原因</dt><dd>${esc(a.cancel_reason)}</dd>` : ''}
    </dl>
    <div class="section-title">授权范围</div><div class="badges">${scopeChips || '<span class="hint">无</span>'}</div>
    <div class="section-title">敏感附件（最小权限展示）</div>${atts}
    <div class="section-title">交接链</div>
    <div>${(a.handoffs || []).map(h =>
      `<div class="audit">${h.seq}. ${esc(h.from_party)} → ${esc(h.to_party)} @ ${esc(h.location)} · <b>${fmtDT(h.handoff_time)}</b></div>`).join('') || '<span class="hint">暂无</span>'}</div>
    <div class="section-title">操作</div><div class="card-actions">${btns || '<span class="hint">当前状态/视角无可执行操作</span>'}</div>`;
  $('#drawerMask').classList.remove('hidden');
  $('#drawer').classList.remove('hidden');
  $('#drawerClose').onclick = closeDrawer;
  $$('#drawer [data-drawer-act]').forEach(b => b.addEventListener('click', () =>
    drawerAction(b.dataset.drawerAct, id)));
}
function closeDrawer() {
  $('#drawerMask').classList.add('hidden');
  $('#drawer').classList.add('hidden');
}

async function quickCheckin(id, stage) {
  const loc = prompt(`「${state.meta.stages.find(s => s.value === stage)?.label}」签到地点：`, '检查科室');
  if (loc === null) return;
  await api.post(`/api/appointments/${id}/checkin`, { stage, location: loc, note: '' });
  toast('签到成功，状态已推进', 'ok');
  await refresh();
  if (!$('#drawer').classList.contains('hidden')) openDrawer(id);
}

// ============ 弹窗动作 ============
function openCreateModal() {
  const patients = state.patients || [];
  $('#modal').innerHTML = `
    <h3 style="margin-top:0">新建检查预约</h3>
    <div class="form-grid">
      <label>患者
        <select id="f-patient">${patients.map(p =>
          `<option value="${p.id}">${esc(p.code)} ${esc(p.full_name)}</option>`).join('')}</select></label>
      <label>检查科室<input id="f-dept" list="dept-list" value="放射科"></label>
      <datalist id="dept-list"><option>放射科</option><option>超声科</option><option>检验科</option><option>心电图室</option></datalist>
      <label>检查项目<input id="f-item" value="胸部 CT 平扫"></label>
      <label>开始时间<input type="datetime-local" id="f-start"></label>
      <label>结束时间<input type="datetime-local" id="f-end"></label>
      <label>行动辅助要求<input id="f-mob" value="轮椅；院内全程坡道"></label>
      <label>检查前注意<input id="f-req" value="无需空腹；带既往影像片袋"></label>
      <label class="full">内部备注（患者端不可见）<input id="f-note" value="患者易焦虑，沟通需放慢语速；家属要求检查后电话反馈。"></label>
    </div>
    <div class="section-title">授权范围</div>
    <div class="scope-grid" id="f-scopes">
      ${state.meta.scopes.map((s, i) =>
        `<label class="scope-item ${[0, 1, 2].includes(i) ? 'on' : ''}">
          <input type="checkbox" value="${s.value}" ${[0, 1, 2].includes(i) ? 'checked' : ''}> ${esc(s.label)}</label>`).join('')}
    </div>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="m-cancel">取消</button>
      <button class="btn btn-amber" id="m-ok">发布预约</button></div>`;
  showModal();
  const t = new Date(Date.now() + 86400000);
  const pad = n => String(n).padStart(2, '0');
  const localDay = `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
  $('#f-start').value = localDay + 'T09:30';
  $('#f-end').value = localDay + 'T11:00';
  $('#m-cancel').onclick = hideModal;
  $('#m-ok').onclick = async () => {
    const scopes = $$('#f-scopes input:checked').map(i => i.value);
    await api.post('/api/appointments', {
      patient_id: Number($('#f-patient').value),
      department: $('#f-dept').value.trim(),
      exam_item: $('#f-item').value.trim(),
      scheduled_start: fromInputDT($('#f-start').value),
      scheduled_end: fromInputDT($('#f-end').value),
      mobility_needs: $('#f-mob').value,
      special_requirements: $('#f-req').value,
      internal_note: $('#f-note').value,
      scope_codes: scopes,
    });
    hideModal(); toast('预约已发布，等待接单', 'ok'); await refresh();
  };
}

function rescheduleModal(id) {
  const a = state.appts.find(x => x.id === id);
  $('#modal').innerHTML = `<h3 style="margin-top:0">临时改期 · ${esc(a.code)}</h3>
    <div class="form-grid">
      <label>新开始时间<input type="datetime-local" id="r-start" value="${toInputDT(a.scheduled_start)}"></label>
      <label>新结束时间<input type="datetime-local" id="r-end" value="${toInputDT(a.scheduled_end)}"></label>
      <label class="full">改期原因<input id="r-reason" value="科室设备检修，检查顺延"></label>
    </div>
    <div class="modal-actions"><button class="btn btn-ghost" id="m-cancel">取消</button>
      <button class="btn btn-amber" id="m-ok">确认改期并重新派单</button></div>`;
  showModal();
  $('#m-cancel').onclick = hideModal;
  $('#m-ok').onclick = async () => {
    await api.post(`/api/appointments/${id}/reschedule`, {
      new_start: fromInputDT($('#r-start').value),
      new_end: fromInputDT($('#r-end').value),
      reason: $('#r-reason').value,
    });
    hideModal(); toast('改期完成，已释放回待接单', 'ok'); await refresh();
  };
}

function reviewModal(id) {
  $('#modal').innerHTML = `<h3 style="margin-top:0">服务评价</h3>
    <div class="form-grid">
      <label>评分
        <select id="v-rating"><option value="5">★★★★★ 5</option><option value="4">★★★★ 4</option>
        <option value="3">★★★ 3</option><option value="2">★★ 2</option><option value="1">★ 1</option></select></label>
      <label>标签<input id="v-tags" value="准时；耐心；路线熟悉"></label>
      <label class="full">评语<input id="v-comment" value="全程陪同顺畅，交接清楚。"></label>
    </div>
    <div class="modal-actions"><button class="btn btn-ghost" id="m-cancel">取消</button>
      <button class="btn btn-amber" id="m-ok">提交评价</button></div>`;
  showModal();
  $('#m-cancel').onclick = hideModal;
  $('#m-ok').onclick = async () => {
    await api.post(`/api/appointments/${id}/review`, {
      rating: Number($('#v-rating').value), tags: $('#v-tags').value,
      comment: $('#v-comment').value,
    });
    hideModal(); toast('感谢评价', 'ok'); await refresh();
  };
}

function showModal() { $('#modalMask').classList.remove('hidden'); }
function hideModal() { $('#modalMask').classList.add('hidden'); }

async function drawerAction(act, id) {
  const a = drawerAppt;
  if (act === 'claim') { await api.post(`/api/appointments/${id}/claim`, {}); toast('接单成功', 'ok'); }
  if (act === 'checkin') return hideModal(), quickCheckin(id, NEXT_STAGE[a.status][0]), undefined;
  if (act === 'reschedule') { closeDrawer(); return rescheduleModal(id); }
  if (act === 'cancel') {
    const reason = prompt('检查取消原因：', '科室临时停诊');
    if (reason === null) return;
    await api.post(`/api/appointments/${id}/cancel`, { reason });
    toast('检查已取消（终态）', 'ok');
  }
  if (act === 'refuse') {
    const d = prompt('患者拒绝陪同的情况说明：', '患者临时决定由家属陪同');
    if (d === null) return;
    await api.post(`/api/appointments/${id}/refuse`, { detail: d });
    toast('已登记患者拒绝陪同', 'ok');
  }
  if (act === 'route-change') {
    await api.post(`/api/appointments/${id}/route-change`, {
      reason: prompt('路线变更原因：', '一层东侧无障碍通道临时封闭，改走西门'),
    });
    toast('已生成新路线版本', 'ok');
  }
  if (act === 'late') {
    const minutes = Number(prompt('迟到分钟数：', '12'));
    await api.post(`/api/appointments/${id}/late`, { minutes, detail: '院内堵车' });
    toast('迟到已记录并通知调度台', 'ok');
  }
  if (act === 'material') {
    const material = prompt('缺失资料：', '既往 CT 影像片袋');
    await api.post(`/api/appointments/${id}/material-missing`, { material });
    toast('资料缺失已登记并通知调度台', 'ok');
  }
  if (act === 'review') { closeDrawer(); return reviewModal(id); }
  closeDrawer();
  await refresh();
}

// ============ 事件委托 ============
async function delegatedClick(e) {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const id = Number(b.dataset.id);
  try {
    switch (b.dataset.act) {
      case 'open': await openDrawer(id); break;
      case 'claim':
        await api.post(`/api/appointments/${id}/claim`, {});
        toast('接单成功', 'ok'); await refresh(); break;
      case 'checkin-quick':
        await quickCheckin(id, b.dataset.stage); break;
      case 'reschedule': rescheduleModal(id); break;
      case 'cancel': {
        const reason = prompt('检查取消原因：', '科室临时停诊');
        if (reason === null) break;
        await api.post(`/api/appointments/${id}/cancel`, { reason });
        toast('检查已取消（终态）', 'ok'); await refresh(); break;
      }
      case 'refuse': {
        const d = prompt('情况说明：', '患者临时决定由家属陪同');
        if (d === null) break;
        await api.post(`/api/appointments/${id}/refuse`, { detail: d });
        toast('已登记', 'ok'); await refresh(); break;
      }
      case 'route-change':
        await api.post(`/api/appointments/${id}/route-change`,
          { reason: '无障碍通道封闭，改走西门' });
        toast('路线已变更（新版本）', 'ok'); await refresh(); break;
      case 'handoff-add': {
        const body = {
          from_party: $('#hf-from').value.trim(),
          to_party: $('#hf-to').value.trim(),
          location: $('#hf-loc').value.trim(),
          items: $('#hf-items').value.trim(),
          note: $('#hf-note').value.trim(),
          patient_confirmed: $('#hf-confirm').checked,
        };
        if (!body.from_party || !body.to_party || !body.location) {
          return toast('交接双方与地点必须填写', 'err');
        }
        await api.post(`/api/appointments/${id}/handoffs`, body);
        toast('交接记录已留痕', 'ok'); await refresh(); break;
      }
      case 'scoped': {
        try {
          await api.post(`/api/appointments/${id}/action`,
            { scope: b.dataset.scope, reason: '现场需要，患者口头同意但授权书未含此项' });
          toast('授权校验通过，操作已执行', 'ok');
        } catch (err) {
          toast(err.message + '，请等待调度台人工确认', 'err');
          await refresh();
        }
        break;
      }
      case 'escalate-ok':
        await api.post(`/api/escalations/${b.dataset.eid}/resolve`,
          { approve: true, by: '调度台-王主管', note: '已电话与家属确认' });
        toast('已批准并追加授权', 'ok'); await refresh(); break;
      case 'escalate-no':
        await api.post(`/api/escalations/${b.dataset.eid}/resolve`,
          { approve: false, by: '调度台-王主管', note: '联系不上家属' });
        toast('已驳回', 'ok'); await refresh(); break;
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

init().catch(e => toast('初始化失败：' + e.message, 'err'));
