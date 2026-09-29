/* 墨蓝陪诊演示平台前端（原生 JS，无框架） */
"use strict";

const state = {
  role: "coordinator",
  escortId: null,
  patientId: null,
  meta: null,
  escorts: [],
  patients: [],
  currentApptId: null,
  calendarDate: null,
};

const STATUS_LABELS = {
  open: "待接单", assigned: "进行中", completed: "已完成",
  cancelled: "已取消", refused: "拒绝陪同",
};
const DEPT_DEST = { "放射科": "CT 3号机房", "超声科": "超声 B 超 2 室" };
const STAGE_LOCATION = {
  arrived: "门诊大厅", waiting: "科室登记处", exam_done: "检查机房", departed: "门诊出口",
};

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", "X-Role": state.role };
  if (state.escortId) headers["X-Escort-Id"] = state.escortId;
  if (state.patientId) headers["X-Patient-Id"] = state.patientId;
  const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const err = new Error(data.message || "请求失败");
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dt(s) { return s ? s.replace("T", " ").slice(5, 16) : "—"; }
function dlong(s) { return s ? s.replace("T", " ").slice(0, 16) : "—"; }

/* ---------------- 初始化与角色切换 ---------------- */
async function init() {
  state.meta = await api("/api/meta");
  const [es, ps] = await Promise.all([api("/api/escorts"), safePatients()]);
  state.escorts = es.items;
  state.patients = ps.items || [];
  fillSelect("escortSelect", state.escorts.map(e => ({ value: e.id, label: e.name })));
  fillSelect("patientSelect", state.patients.map(p => ({ value: p.id, label: `${p.anon_code}（${p.demo_name}）` })));
  state.escortId = state.escorts[0]?.id ?? null;
  state.patientId = state.patients[0]?.id ?? null;

  document.getElementById("roleSwitch").addEventListener("click", e => {
    const btn = e.target.closest("button[data-role]");
    if (!btn) return;
    setRole(btn.dataset.role);
  });
  document.getElementById("escortSelect").addEventListener("change", e => {
    state.escortId = Number(e.target.value); renderActiveTab();
  });
  document.getElementById("patientSelect").addEventListener("change", e => {
    state.patientId = Number(e.target.value); renderActiveTab();
  });
  document.getElementById("tabs").addEventListener("click", e => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b === btn));
    renderActiveTab();
  });
  document.getElementById("taskStatusFilter").addEventListener("change", renderTasks);
  document.getElementById("drawerMask").addEventListener("click", closeDrawer);
  initCalendarControls();

  document.getElementById("calendarMonth").value = new Date().toISOString().slice(0, 7);
  await renderActiveTab();
}

async function safePatients() {
  try { return await api("/api/patients"); } catch { return { items: [] }; }
}

function fillSelect(id, opts) {
  const sel = document.getElementById(id);
  sel.innerHTML = opts.map(o => `<option value="${o.value}">${esc(o.label)}</option>`).join("");
}

function setRole(role) {
  state.role = role;
  document.querySelectorAll("#roleSwitch button").forEach(b =>
    b.classList.toggle("active", b.dataset.role === role));
  document.getElementById("escortSelect").classList.toggle("hidden", role !== "escort");
  document.getElementById("patientSelect").classList.toggle("hidden", role !== "patient");
  closeDrawer();
  renderActiveTab();
}

function renderActiveTab() {
  const tab = document.querySelector("#tabs button.active").dataset.tab;
  const panel = document.getElementById("tab-" + tab);
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
  panel.classList.remove("hidden");
  ({
    calendar: renderCalendar, tasks: renderTasks, timeline: renderTimelineTab,
    authz: renderAuthzTab, handoffs: renderHandoffTab, reviews: renderReviews,
  })[tab]();
}

/* ---------------- 预约日历 ---------------- */
function initCalendarControls() {
  document.getElementById("calendarMonth").addEventListener("change", renderCalendar);
  document.getElementById("calendarGrid").addEventListener("click", e => {
    const cell = e.target.closest(".cal-cell[data-date]");
    if (!cell) return;
    state.calendarDate = cell.dataset.date;
    renderCalendar();
  });
}

async function renderCalendar() {
  const month = document.getElementById("calendarMonth").value;
  const data = await api(`/api/calendar?month=${month}`);
  const byDate = Object.fromEntries(data.days.map(d => [d.date, d.statuses]));
  const [y, m] = month.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const days = new Date(y, m, 0).getDate();
  const lead = (first.getDay() + 6) % 7; // 周一开头
  const heads = ["一", "二", "三", "四", "五", "六", "日"]
    .map(h => `<div class="cal-weekhead">周${h}</div>`).join("");
  let cells = "";
  for (let i = 0; i < lead; i++) cells += `<div></div>`;
  for (let d = 1; d <= days; d++) {
    const ds = `${month}-${String(d).padStart(2, "0")}`;
    const st = byDate[ds] || {};
    const dots = Object.entries(st).map(([s, c]) =>
      `<div class="cal-dot ${s}">${STATUS_LABELS[s] || s} ${c}</div>`).join("");
    cells += `<div class="cal-cell ${state.calendarDate === ds ? "selected" : ""}" data-date="${ds}">
      <div class="d">${d}</div><div class="cal-dots">${dots}</div></div>`;
  }
  document.getElementById("calendarGrid").innerHTML = heads + cells;

  const qs = state.calendarDate ? `&date=${state.calendarDate}` : "";
  document.getElementById("calDayTitle").textContent =
    state.calendarDate ? `${state.calendarDate} 当日预约` : "全部预约（点击日历日期筛选）";
  const list = await api(`/api/appointments?${qs}`);
  document.getElementById("calendarDayList").innerHTML = list.items.map(apptCard).join("") || emptyHint("暂无预约");
  bindCards();
}

/* ---------------- 陪诊任务 ---------------- */
async function renderTasks() {
  const hero = document.getElementById("escortMobileCard");
  if (state.role === "escort") {
    const dash = await api("/api/dashboard");
    hero.innerHTML = (dash.my_tasks || []).length
      ? await mobileHero(dash.my_tasks[0])
      : `<div class="mobile-empty">当前没有进行中的任务，可在下方接开放单。</div>`;
    bindHero();
  } else {
    hero.innerHTML = "";
  }
  const f = document.getElementById("taskStatusFilter").value;
  const items = await api(`/api/appointments${f ? `?status=${f}` : ""}`);
  document.getElementById("taskList").innerHTML = items.items.map(apptCard).join("") || emptyHint("暂无任务");
  bindCards();
}

async function mobileHero(a) {
  const dest = DEPT_DEST[a.department] || "医技楼三层";
  const lastLoc = a.timeline.length ? a.timeline[a.timeline.length - 1].location : "门诊大厅";
  let guide = { done: false, next: "加载路线…" };
  try {
    const r = await api(`/api/route?origin=${encodeURIComponent("门诊大厅")}&destination=${encodeURIComponent(dest)}&location=${encodeURIComponent(lastLoc || "门诊大厅")}`);
    guide = r.guide;
  } catch { /* 路线仅辅助 */ }
  const doneStages = new Set(a.timeline.map(t => t.stage));
  const nextStage = ["arrived", "waiting", "exam_done", "departed"].find(s => !doneStages.has(s));
  const btns = ["arrived", "waiting", "exam_done", "departed"].map(s => {
    const labels = state.meta.stages;
    const label = labels.find(x => x.key === s).label;
    const cls = doneStages.has(s) ? "done" : s === nextStage ? "ready" : "";
    return `<button data-checkin="${s}" data-location="${esc(STAGE_LOCATION[s])}"
      class="${cls}" ${doneStages.has(s) || s !== nextStage ? "disabled" : ""}>
      ${doneStages.has(s) ? "✓ " : ""}${label}</button>`;
  }).join("");
  const p = a.patient?.demo_name ? a.patient.demo_name : a.patient?.anon_code;
  return `<div class="mobile-hero" data-appt="${a.id}">
    <div class="mh-top">
      <div>
        <div class="muted" style="color:#cdd9e5">${esc(a.code)} · ${esc(a.department)} · ${esc(a.exam_item)}</div>
        <div style="font-size:17px;font-weight:700;margin-top:2px">患者 ${esc(p)} · ${esc(a.mobility_label)}</div>
      </div>
      <span class="badge badge-assigned" style="background:rgba(255,255,255,.15);color:#fff;border:none">进行中</span>
    </div>
    <div class="place">
      <div class="label">当前地点</div>
      <div class="big">${esc(lastLoc || "门诊大厅")}</div>
    </div>
    <div class="next">➜ ${esc(guide.next || "按时间线推进")}${guide.remaining ? `（还剩 ${guide.remaining} 段）` : ""}</div>
    <div class="checkin-row">${btns}</div>
    <div class="muted" style="color:#cdd9e5;margin-top:8px">签到须按 到达→候诊→检查完成→离院 顺序；离院后服务不可重新开启。</div>
  </div>`;
}

function bindHero() {
  document.querySelectorAll("#escortMobileCard button[data-checkin]").forEach(b =>
    b.addEventListener("click", async () => {
      const apptId = b.closest(".mobile-hero").dataset.appt;
      try {
        await api(`/api/appointments/${apptId}/checkin`, {
          method: "POST",
          body: JSON.stringify({ stage: b.dataset.checkin, location: b.dataset.location }),
        });
        toast("签到成功");
        renderTasks();
      } catch (e) { toast(e.message, "error"); }
    }));
}

/* ---------------- 卡片 ---------------- */
function apptCard(a) {
  const st = a.status;
  const pinfo = a.patient?.demo_name
    ? `患者 ${esc(a.patient.demo_name)}`
    : `档案 ${esc(a.patient?.anon_code || "?")}`;
  let right = "";
  if (st === "open" && state.role === "escort") right = `<button class="btn btn-amber btn-sm" data-accept="${a.id}">立即接单</button>`;
  const authz = a.authorization_labels
    ? a.authorization_labels.map(x => `<span class="chip">${esc(x)}</span>`).join("")
    : a.authorization_summary
      ? `<span class="chip">授权 ${a.authorization_summary.count} 项${a.authorization_summary.has_sensitive ? ' <span class="chip sensitive">含敏感</span>' : ""}</span>`
      : "";
  return `<div class="card" data-appt="${a.id}">
    <div class="code">${esc(a.code)}</div>
    <div class="dept">${esc(a.department)} · ${esc(a.exam_item || "检查")}</div>
    <div class="time">🕒 ${dlong(a.scheduled_at)}（${esc(a.mobility_label)}）</div>
    <div class="muted">${pinfo}${a.escort ? " · " + esc(a.escort.name) : ""}</div>
    <div style="margin-top:6px">${authz}</div>
    <div class="row">
      <span class="badge badge-${st}">${STATUS_LABELS[st] || st}</span>
      ${right}
    </div>
  </div>`;
}

function bindCards() {
  document.querySelectorAll("[data-accept]").forEach(b =>
    b.addEventListener("click", async e => {
      e.stopPropagation();
      b.disabled = true;
      try {
        await api(`/api/appointments/${b.dataset.accept}/accept`, { method: "POST", body: "{}" });
        toast("接单成功：必要字段已对你开放");
        renderActiveTab();
      } catch (err) {
        toast(err.message, "error");
        b.disabled = false;
      }
    }));
  document.querySelectorAll(".card[data-appt]").forEach(c =>
    c.addEventListener("click", () => openDrawer(Number(c.dataset.appt))));
}

function emptyHint(t) { return `<div class="mobile-empty">${t}</div>`; }

/* ---------------- 选择器（时间线/授权/交接页） ---------------- */
async function apptOptions() {
  const data = await api("/api/appointments");
  return data.items;
}
async function mountApptSelect(selectId, viewFn) {
  const items = await apptOptions();
  const sel = document.getElementById(selectId);
  sel.innerHTML = items.map(a =>
    `<option value="${a.id}">${esc(a.code)}｜${esc(a.department)}｜${STATUS_LABELS[a.status]}</option>`).join("");
  if (!items.length) { document.getElementById(sel.id.replace("Select", "View")).innerHTML = emptyHint("暂无预约"); return; }
  sel.onchange = () => viewFn(Number(sel.value));
  await viewFn(Number(sel.value));
}

async function renderTimelineTab() { await mountApptSelect("timelineApptSelect", renderTimelineView); }
async function renderAuthzTab() { await mountApptSelect("authzApptSelect", renderAuthzView); }
async function renderHandoffTab() { await mountApptSelect("handoffApptSelect", renderHandoffView); }

/* ---------------- 检查时间线 ---------------- */
async function renderTimelineView(id) {
  const a = await api(`/api/appointments/${id}`);
  const done = new Set(a.timeline.map(t => t.stage));
  const byStage = Object.fromEntries(a.timeline.map(t => [t.stage, t]));
  const nodes = state.meta.stages.map((s, i) => {
    const ev = byStage[s.key];
    const cls = ev ? "done" : (i === a.timeline.length ? "current" : "pending");
    return `<div class="tl-node ${cls}">
      <div class="dot"></div>
      <div><span class="stage-name">${s.label}</span>${ev ? `<span class="stage-time">${dlong(ev.created_at)}</span>` : ""}</div>
      ${ev ? `<div class="stage-meta">📍 ${esc(ev.location || "—")}${ev.note ? "｜" + esc(ev.note) : ""}</div>`
        : `<div class="stage-meta muted">${i === a.timeline.length ? "下一节点" : "未到节点"}</div>`}
    </div>`;
  }).join("");
  const ex = a.exceptions.length ? `<div class="panel"><h3>异常与改期留痕</h3><div class="stack">
    ${a.exceptions.map(x => `<div>🔸 ${esc(exceptionLabel(x))} <span class="muted">${dlong(x.created_at)}</span>${x.requires_manual ? ` <span class="chip sensitive">人工：${manualCn(x.manual_status)}</span>` : ""}<div class="muted">${esc(x.detail || "")}</div></div>`).join("")}
  </div></div>` : "";
  document.getElementById("timelineView").innerHTML =
    `<div class="panel"><div class="code muted">${esc(a.code)}｜${esc(a.department)}｜${dlong(a.scheduled_at)}</div>
     <div class="timeline" style="margin-top:12px">${nodes}</div></div>${ex}`;
}

function exceptionLabel(x) {
  const base = state.meta.exceptions.find(e => e.key === x.type)?.label || x.type;
  if (x.type === "reschedule") return `${base}：${dt(x.old_time)} → ${dt(x.new_time)}`;
  if (x.type === "escort_late") return `${base} ${x.late_minutes} 分钟`;
  if (x.type === "route_changed") return `${base}：${esc(x.new_route)}`;
  return base;
}
function manualCn(s) { return { none: "—", pending: "待确认", approved: "已批准", rejected: "已驳回" }[s] || s; }

/* ---------------- 授权事项 ---------------- */
async function renderAuthzView(id) {
  const a = await api(`/api/appointments/${id}`);
  const granted = new Set(a.authorization_scope || []);
  const items = state.meta.authz_items.map(s => {
    const has = granted.has(s.key);
    return `<div class="authz-item ${has ? "granted" : "denied"}">
      <div><div>${s.label}</div>${s.sensitive ? '<div class="tag-s">敏感事项</div>' : ""}</div>
      <div>${has
        ? (state.role === "escort" && a.mine
            ? `<button class="btn btn-ghost btn-sm" data-authz="${s.key}">执行校验</button>`
            : '<span class="chip">已授权</span>')
        : '<span class="chip locked">未授权</span>'}</div>
    </div>`;
  }).join("");
  let sensitiveBox = "";
  if (state.role === "escort") {
    sensitiveBox = `<div class="notice-demo">未在授权范围内的操作（如代取报告、查看检查结果、协助缴费）将被拦截并转人工确认，不会直接执行。</div>`;
  }
  const atts = (a.attachments || []).map(attView).join("") || '<p class="muted">暂无附件</p>';
  document.getElementById("authzView").innerHTML =
    `${sensitiveBox}
     <div class="panel"><h3>本单授权范围（共 ${granted.size} 项）</h3>
       <div class="authz-grid" style="margin-top:8px">${items}</div></div>
     <div class="panel"><h3>附件与检查结果（最小权限展示）</h3><div class="stack">${atts}</div></div>`;
  document.querySelectorAll("[data-authz]").forEach(b =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/api/appointments/${id}/authorize`, {
          method: "POST", body: JSON.stringify({ scope: b.dataset.authz }) });
        toast(`校验通过：${r.allowed ? "在授权范围内" : ""}`);
      } catch (e) { toast(e.message, "warn"); renderAuthzView(id); }
    }));
  bindAttachmentDownloads(id);
}

function attView(att) {
  const tag = att.sensitivity === "sensitive" ? '<span class="chip sensitive">敏感·检查结果</span>' : '<span class="chip">普通</span>';
  if (att.visible) {
    return `<div class="row-flex"><span>📎 ${esc(att.label || att.filename)}</span>${tag}
      <a class="btn btn-ghost btn-sm" href="/api/attachments/${att.id}" data-att="${att.id}">查看</a></div>`;
  }
  return `<div class="row-flex"><span class="eye-off">🔒 ${esc(att.label || att.filename)}（${esc(att.reason || "不可见")}）</span>${tag}</div>`;
}

function bindAttachmentDownloads(apptId) {
  document.querySelectorAll("a[data-att]").forEach(a =>
    a.addEventListener("click", async e => {
      e.preventDefault();
      try {
        const res = await fetch(`/api/attachments/${a.dataset.att}`, {
          headers: { "X-Role": state.role, "X-Escort-Id": state.escortId || "" },
        });
        if (!res.ok) {
          const d = await res.json();
          throw new Error(d.message);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank");
      } catch (err) { toast(err.message, "warn"); renderAuthzView(apptId); }
    }));
}

/* ---------------- 交接记录 ---------------- */
async function renderHandoffView(id) {
  const a = await api(`/api/appointments/${id}`);
  const rows = a.handoffs.map(h => `
    <div class="handoff-row">
      <div><div class="party">${esc(h.from_party)}</div><div class="meta">${dlong(h.created_at)}｜📍${esc(h.location || "—")}</div></div>
      <div class="arrow">→</div>
      <div><div class="party">${esc(h.to_party)}</div>
        <div class="meta">物料：${(h.materials || []).map(m => `<span class="chip">${esc(m)}</span>`).join("") || "无"}
        ${h.note ? "<br>" + esc(h.note) : ""}</div></div>
    </div>`).join("");
  const canAdd = state.role === "escort" && a.mine && !["completed", "cancelled", "refused"].includes(a.status);
  document.getElementById("handoffView").innerHTML =
    `<div class="panel"><h3>交接链条（对象与时间全程留痕）</h3>
       <div style="margin-top:10px">${rows || '<p class="muted">暂无交接记录</p>'}</div>
       ${canAdd ? handoffForm(id) : ""}
     </div>`;
  const form = document.getElementById("handoffForm");
  if (form) form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      await api(`/api/appointments/${id}/handoffs`, {
        method: "POST",
        body: JSON.stringify({
          from_party: fd.get("from_party"), to_party: fd.get("to_party"),
          location: fd.get("location"),
          materials: fd.get("materials").split(/[,，]/).map(s => s.trim()).filter(Boolean),
          note: fd.get("note"),
        }),
      });
      toast("交接已留痕"); renderHandoffView(id);
    } catch (err) { toast(err.message, "error"); }
  });
}

function handoffForm(id) {
  return `<div class="divider"></div>
  <form id="handoffForm" class="form-grid">
    <label>交出方<input name="from_party" required placeholder="如：CT 3号机房技师"></label>
    <label>接收方<input name="to_party" required placeholder="如：陪诊员林岚 / 家属"></label>
    <label>地点<input name="location" placeholder="如：CT 3号机房"></label>
    <label>交接物料（逗号分隔）<input name="materials" placeholder="如：演示报告, 随身包"></label>
    <label class="full">备注<input name="note"></label>
    <div class="full right"><button class="btn btn-primary" type="submit">保存交接记录</button></div>
  </form>`;
}

/* ---------------- 评价汇总 ---------------- */
async function renderReviews() {
  const sum = await api("/api/reviews/summary");
  const dist = [5, 4, 3, 2, 1].map(n => {
    const c = sum.distribution[n] || 0;
    const pct = sum.count ? Math.round(c / sum.count * 100) : 0;
    return `<div class="dist-row"><span>${n}星</span><div class="dist-bar"><i style="width:${pct}%"></i></div><span>${c}</span></div>`;
  }).join("");
  const tags = sum.tags.length
    ? sum.tags.map(([t, c]) => `<span class="chip">${esc(t)} ×${c}</span>`).join("")
    : '<span class="muted">暂无标签</span>';
  document.getElementById("reviewView").innerHTML =
    `<div class="panel"><div class="review-hero">
       <div><div class="score-big">${sum.avg || "—"}</div><div class="stars">★★★★★</div>
       <div class="muted">共 ${sum.count} 条评价${state.role === "escort" ? "（仅我的服务）" : ""}</div></div>
       <div style="flex:1;min-width:220px" class="stack">${dist}</div>
     </div><div class="divider"></div><h3>高频标签</h3><div style="margin-top:6px">${tags}</div></div>`;

  if (state.role !== "escort") {
    const items = await api("/api/appointments?status=completed");
    const cards = items.items.map(a =>
      `<div class="card" data-appt="${a.id}"><div class="code">${esc(a.code)}</div>
        <div class="dept">${esc(a.department)}</div>
        <div class="muted">${dlong(a.scheduled_at)} · ${esc(a.escort?.name || "")}</div>
        <div class="row"><span class="badge badge-completed">已完成</span></div></div>`).join("");
    document.getElementById("reviewView").innerHTML +=
      `<h3 class="subhead">已完成服务（点击可评价或查看详情）</h3><div class="card-grid">${cards || emptyHint("暂无已完成服务")}</div>`;
    bindCards();
  }
}

/* ---------------- 详情抽屉 ---------------- */
async function openDrawer(id) {
  state.currentApptId = id;
  const a = await api(`/api/appointments/${id}`);
  document.getElementById("drawerMask").classList.remove("hidden");
  document.getElementById("drawer").classList.remove("hidden");
  const p = a.patient || {};
  const isEscortMine = state.role === "escort" && a.mine;

  const patientBlock = p.demo_name ? `
    <div class="notice-demo">演示数据，非真实患者信息</div>
    <dl class="kv">
      <dt>匿名编号</dt><dd>${esc(p.anon_code)}</dd>
      <dt>姓名</dt><dd>${esc(p.demo_name)}</dd>
      <dt>证件(脱敏)</dt><dd>${esc(p.demo_id_masked)}</dd>
      <dt>联系(脱敏)</dt><dd>${esc(p.contact_masked)}</dd>
      <dt>行动辅助</dt><dd>${esc(a.mobility_label)}</dd>
    </dl>` : `<dl class="kv"><dt>匿名编号</dt><dd>${esc(p.anon_code || "未开放")}</dd>
      <dt>患者信息</dt><dd class="eye-off">接单成功后开放必要字段</dd></dl>`;

  // 内部备注：患者端恒隐藏；未接单陪诊员不可见
  const noteBlock = state.role === "patient"
    ? ""
    : (isEscortMine || state.role === "coordinator") && a.internal_note
      ? `<h3>内部备注 <span class="chip locked">患者端不可见</span></h3><p class="kv"><dd class="internal">${esc(a.internal_note)}</dd></p>`
      : "";

  const authzChips = (a.authorization_labels || []).map(x =>
    `<span class="chip">${esc(x)}</span>`).join("") ||
    (a.authorization_summary ? `<span class="muted">授权 ${a.authorization_summary.count} 项，接单后可见明细</span>` : "");

  document.getElementById("drawer").innerHTML = `
    <div class="close-row">
      <div><div class="code muted">${esc(a.code)}</div>
        <h2 style="font-size:17px">${esc(a.department)} · ${esc(a.exam_item || "检查")}</h2></div>
      <button class="btn btn-ghost btn-sm" id="drawerClose">关闭</button>
    </div>
    <span class="badge badge-${a.status}">${STATUS_LABELS[a.status]}</span>
    <span class="muted" style="margin-left:8px">🕒 ${dlong(a.scheduled_at)}</span>

    <h3>患者档案（匿名）</h3>${patientBlock}
    ${noteBlock}

    <h3>授权范围</h3><div>${authzChips}</div>
    ${a.sensitive_scopes?.length ? `<div style="margin-top:6px">${a.sensitive_scopes.map(s => `<span class="chip sensitive">敏感：${esc(state.meta.authz_items.find(x=>x.key===s)?.label||s)}</span>`).join("")}</div>` : ""}

    <h3>进度签到</h3><div id="drawerTimeline">${miniTimeline(a)}</div>
    ${drawerActions(a)}
    ${drawerException(a)}
    ${drawerAttachments(a)}
    ${drawerHandoffs(a)}
    ${drawerReview(a)}
    ${await coordinatorQueue()}
  `;
  document.getElementById("drawerClose").onclick = closeDrawer;
  bindDrawerActions(id, a);
}

function miniTimeline(a) {
  const done = new Set(a.timeline.map(t => t.stage));
  return `<div class="row-flex">${state.meta.stages.map((s, i) => {
    const on = done.has(s.key);
    const cur = i === a.timeline.length && !["completed", "cancelled", "refused"].includes(a.status);
    return `<span class="chip" style="padding:4px 10px;${on ? "background:var(--green-soft);color:var(--green)" : cur ? "background:var(--amber-soft);color:#8a5d12" : ""}">${on ? "✓ " : ""}${s.label}</span>`;
  }).join("")}</div>`;
}

function drawerActions(a) {
  if (state.role !== "escort") return "";
  if (a.status === "open" && !a.mine) {
    return `<div style="margin-top:12px"><button class="btn btn-amber" id="actAccept">接单（成功后开放必要字段）</button>
      <p class="muted" style="margin-top:6px">同一时段多人并发接单，仅一人成功。</p></div>`;
  }
  if (!a.mine) return "";
  if (["completed", "cancelled", "refused"].includes(a.status))
    return `<div style="margin-top:10px" class="notice-demo">服务已结束，不可重新开启或补签到。</div>`;
  const done = new Set(a.timeline.map(t => t.stage));
  const next = state.meta.stages.find(s => !done.has(s.key));
  return `<div style="margin-top:12px" class="stack">
    <div class="row-flex">
      <select id="checkinStage">
        ${state.meta.stages.map(s => `<option value="${s.key}" ${s.key !== next.key ? "disabled" : ""}>${s.label}${done.has(s.key) ? "（已签到）" : s.key === next.key ? "（下一步）" : "（未到顺序）"}</option>`).join("")}
      </select>
      <input id="checkinLocation" placeholder="地点" value="${esc(STAGE_LOCATION[next.key])}" style="flex:1;min-width:120px">
      <input id="checkinNote" placeholder="备注（可选）" style="flex:1;min-width:120px">
      <button class="btn btn-primary" id="actCheckin">签到</button>
    </div>
    <p class="muted">必须按 到达 → 候诊 → 检查完成 → 离院 顺序，每步仅一次。</p>
  </div>`;
}

function drawerException(a) {
  if (state.role !== "escort" || !a.mine || ["completed", "cancelled", "refused"].includes(a.status)) return "";
  return `<h3>异常处理</h3><div class="panel" style="box-shadow:none">
    <div class="row-flex">
      <select id="excType">${state.meta.exceptions.map(e => `<option value="${e.key}">${e.label}</option>`).join("")}</select>
      <input id="excNewTime" type="datetime-local" class="hidden">
      <input id="excLate" type="number" min="1" placeholder="迟到分钟" class="hidden" style="width:110px">
      <input id="excRoute" placeholder="新路线（含“病区”将转人工）" class="hidden" style="flex:1;min-width:160px">
    </div>
    <textarea id="excDetail" placeholder="情况说明" style="width:100%;margin-top:8px" rows="2"></textarea>
    <div class="right" style="margin-top:8px"><button class="btn btn-amber" id="actException">登记异常</button></div>
  </div>`;
}

function drawerAttachments(a) {
  if (state.role === "escort" && !a.mine)
    return `<h3>附件</h3><p class="eye-off">接单前附件不可见。</p>`;
  const list = (a.attachments || []);
  return `<h3>附件与检查结果</h3><div class="stack">${list.map(attView).join("") || '<p class="muted">暂无附件</p>'}</div>`;
}

function drawerHandoffs(a) {
  if (!a.handoffs.length) return "";
  return `<h3>交接记录</h3><div class="stack">${a.handoffs.map(h =>
    `<div class="panel" style="box-shadow:none;padding:10px 12px">
      <b>${esc(h.from_party)}</b> <span style="color:var(--amber)">→</span> <b>${esc(h.to_party)}</b>
      <div class="muted">${dlong(h.created_at)}｜${esc(h.location || "")}｜${(h.materials || []).map(esc).join("、")}</div>
    </div>`).join("")}</div>`;
}

function drawerReview(a) {
  if (state.role === "escort" || a.status !== "completed") return "";
  return `<h3>服务评价</h3>
  <div class="panel" style="box-shadow:none">
    <div class="row-flex">星级
      <select id="reviewStars">${[5,4,3,2,1].map(n => `<option value="${n}">${"★".repeat(n)}${"☆".repeat(5-n)}</option>`).join("")}</select>
    </div>
    <input id="reviewTags" placeholder="标签，逗号分隔（如：准时,耐心）" style="width:100%;margin-top:8px">
    <textarea id="reviewComment" rows="2" placeholder="评价内容（可选）" style="width:100%;margin-top:8px"></textarea>
    <div class="right" style="margin-top:8px"><button class="btn btn-primary" id="actReview">提交评价</button></div>
  </div>`;
}

async function coordinatorQueue() {
  if (state.role !== "coordinator") return "";
  const dash = await api("/api/dashboard");
  const pend = dash.manual_pending || [];
  if (!pend.length) return `<h3>人工确认</h3><p class="muted">暂无待处理事项。</p>`;
  return `<h3>人工确认队列（超授权/通知失败/异常）</h3><div class="stack">${pend.map(m =>
    `<div class="panel" style="box-shadow:none;padding:10px 12px">
      <div><b>#${m.id}</b> ${esc(m.reason)}</div>
      <div class="muted">${esc(m.requested_action)} · ${dlong(m.created_at)}</div>
      <div class="row-flex" style="margin-top:6px">
        <button class="btn btn-primary btn-sm" data-manual="${m.id}" data-ok="1">批准</button>
        <button class="btn btn-ghost btn-sm" data-manual="${m.id}" data-ok="0">驳回</button>
      </div></div>`).join("")}</div>`;
}

function bindDrawerActions(id, a) {
  const accept = document.getElementById("actAccept");
  if (accept) accept.onclick = async () => {
    try { await api(`/api/appointments/${id}/accept`, { method: "POST", body: "{}" });
      toast("接单成功"); openDrawer(id); renderActiveTab();
    } catch (e) { toast(e.message, "error"); }
  };
  const checkin = document.getElementById("actCheckin");
  if (checkin) checkin.onclick = async () => {
    try {
      await api(`/api/appointments/${id}/checkin`, {
        method: "POST",
        body: JSON.stringify({
          stage: document.getElementById("checkinStage").value,
          location: document.getElementById("checkinLocation").value,
          note: document.getElementById("checkinNote").value,
        }),
      });
      toast("签到成功"); openDrawer(id); renderActiveTab();
    } catch (e) { toast(e.message, "error"); }
  };
  const excType = document.getElementById("excType");
  if (excType) excType.onchange = () => {
    document.getElementById("excNewTime").classList.toggle("hidden", excType.value !== "reschedule");
    document.getElementById("excLate").classList.toggle("hidden", excType.value !== "escort_late");
    document.getElementById("excRoute").classList.toggle("hidden", excType.value !== "route_changed");
  };
  const excBtn = document.getElementById("actException");
  if (excBtn) excBtn.onclick = async () => {
    const payload = { type: excType.value, detail: document.getElementById("excDetail").value };
    if (payload.type === "reschedule") {
      const v = document.getElementById("excNewTime").value;
      if (!v) return toast("请选择新时间", "warn");
      payload.new_time = v;
    }
    if (payload.type === "escort_late") payload.late_minutes = Number(document.getElementById("excLate").value);
    if (payload.type === "route_changed") payload.new_route = document.getElementById("excRoute").value;
    try {
      const r = await api(`/api/appointments/${id}/exceptions`, {
        method: "POST", body: JSON.stringify(payload) });
      toast(r.requires_manual ? "已登记，并转人工确认" : "异常已登记", r.requires_manual ? "warn" : "");
      openDrawer(id); renderActiveTab();
    } catch (e) { toast(e.message, "error"); }
  };
  const reviewBtn = document.getElementById("actReview");
  if (reviewBtn) reviewBtn.onclick = async () => {
    try {
      await api(`/api/appointments/${id}/reviews`, {
        method: "POST",
        body: JSON.stringify({
          stars: Number(document.getElementById("reviewStars").value),
          tags: document.getElementById("reviewTags").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
          comment: document.getElementById("reviewComment").value,
        }),
      });
      toast("评价已提交"); openDrawer(id);
    } catch (e) { toast(e.message, "error"); }
  };
  document.querySelectorAll("[data-manual]").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/manual-reviews/${b.dataset.manual}/decide`, {
        method: "POST", body: JSON.stringify({ approve: b.dataset.ok === "1" }) });
      toast("已处理"); openDrawer(id); renderActiveTab();
    } catch (e) { toast(e.message, "error"); }
  });
  bindAttachmentDownloads(id);
}

function closeDrawer() {
  document.getElementById("drawerMask").classList.add("hidden");
  document.getElementById("drawer").classList.add("hidden");
  state.currentApptId = null;
}

init().catch(e => toast("初始化失败：" + e.message, "error"));
