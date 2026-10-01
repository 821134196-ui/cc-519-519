/* 灯箱运营台前端（无框架） */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const KIND_TAG = {
  normal: "ok", strip_partial: "warn", power_outage: "bad",
  timer_misconfig: "bad", re_outage: "bad", artwork_removed: "info",
  daytime_false_positive: "muted",
};
const KIND_LABEL = {
  normal: "亮灯正常", strip_partial: "部分灯带不亮", power_outage: "供电中断",
  timer_misconfig: "定时器配置错", re_outage: "维修后再次熄灭",
  artwork_removed: "画面已撤换", daytime_false_positive: "白天误判（非故障）",
};
const STATUS_LABEL = {
  open: "未关闭", repaired: "待复验", rejected: "复验不通过·再次熄灭", verified_closed: "已复验关闭",
};
const WO_STATUS = { open: "派修中", done: "维修完成", closed: "已闭环" };
const CREDIT_STATUS = { proposed: "待审批", approved: "已批准", rejected: "已驳回" };

const state = { boxes: [], inspectors: [], faults: [], now: null, currentFault: null };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function tag(cls, text) { return `<span class="tag ${cls}">${esc(text)}</span>`; }
function statusTag(f) {
  if (!f) return tag("muted", "—");
  if (f.fault_status === "repaired") return tag("warn", "维修完成待复验");
  if (f.fault_status === "rejected") return tag("bad", "复验不通过·维修后再次熄灭");
  if (f.fault_status) {
    if (f.fault_status === "verified_closed") return tag("ok", "已复验关闭");
    // 未关闭：按故障类别着色（画面撤换是信息态，不是亮灯故障）
    return tag(KIND_TAG[f.kind] || "bad", f.conclusion);
  }
  return tag(KIND_TAG[f.kind] || "muted", f.conclusion);
}
function evidenceText(st) {
  if (st.last_lit_ts != null) return `最近亮灯：${st.last_lit_ts_label} · ${esc(st.last_lit_inspector || "")}`;
  if (st.last_ts == null) return "暂无巡检证据";
  return `无亮灯证据（最近巡检 ${st.last_ts_label}，亮度 ${Math.round(st.last_brightness * 100)}%）`;
}
function nowLocalInput() {
  const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}

async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}
let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3200);
}

// --------------------------------------------------------------- 拉取
async function refreshAll() {
  const [ov, ins, fl] = await Promise.all([
    api("/api/overview"), api("/api/inspectors"), api("/api/faults"),
  ]);
  state.boxes = ov.boxes;
  state.inspectors = ins.inspectors;
  state.faults = fl.faults;
  state.now = ov.now;
  $("#clock").textContent = `当前 ${ov.now_label}（UTC+8）`;
  renderMap();
  renderBoxTable();
  renderFaultTable();
  fillSelects();
  const openN = state.faults.filter(f => ["open", "rejected", "repaired"].includes(f.status)).length;
  $("#faultBadge").textContent = openN || "";
  if (state.currentFault) await renderFaultDetail(state.currentFault);
}

// --------------------------------------------------------------- 地图
function renderMap() {
  const svg = $("#map");
  const roads = `
    <path class="map-water" d="M820,0 C760,120 880,260 780,420 C720,520 800,580 760,620 L1000,620 L1000,0 Z"/>
    <path class="map-road" d="M60,180 C260,150 430,260 620,210 C780,170 880,240 960,210"/>
    <path class="map-road" d="M120,520 C300,430 470,470 620,420 C720,386 820,430 940,400"/>
    <path class="map-road-sub" d="M300,40 L300,580"/>
    <path class="map-road-sub" d="M560,60 C520,220 640,380 560,600"/>
    <text class="map-label" x="70" y="160">中山路</text>
    <text class="map-label" y="40" x="130">滨河路</text>
    <text class="map-label" x="850" y="330">滨河</text>
    <text class="map-label" x="430" y="560">老城广场</text>`;
  const pins = state.boxes.map((b, i) => {
    const x = b.map_x * 1000, y = b.map_y * 620;
    const st = b.state;
    const colorMap = {
      ok: "#2f7d5b", warn: "#c08a2e", bad: "#b4433a", info: "#34597e", muted: "#9aa0a8",
    };
    let colorKey;
    if (st.fault_status === "repaired") colorKey = "warn";
    else if (st.fault_status === "rejected") colorKey = "bad";
    else if (st.fault_status === "verified_closed") colorKey = "ok";
    else colorKey = KIND_TAG[st.kind] || "muted";
    const color = colorMap[colorKey];
    const ev = st.last_lit_ts != null
      ? `亮灯 ${st.last_lit_ts_label.slice(5)}`
      : (st.last_ts != null
        ? `无亮灯证据 ${st.last_ts_label.slice(5)}`
        : "暂无证据");
    // 卡片错开，避免重叠
    const up = i % 2 === 0;
    const cy = up ? y - 64 : y + 18;
    return `
    <g class="pin" data-box="${b.id}" transform="translate(${x},${y})">
      <line x1="0" y1="0" x2="0" y2="${up ? -24 : 16}" stroke="#c9c5bb" stroke-width="1.5"/>
      <g transform="translate(-64,${up ? -64 : 16})">
        <rect class="pin-card" width="128" height="40" rx="7"/>
        <circle class="pin-dot" cx="14" cy="14" r="6" fill="${color}"/>
        <text class="pin-code" x="26" y="18">${esc(b.code)}</text>
        <text class="pin-ev" x="10" y="32">${esc(ev)}</text>
      </g>
      <circle class="pin-dot" r="7" fill="${color}"/>
    </g>`;
  }).join("");
  svg.innerHTML = roads + pins;
  $$(".pin", svg).forEach(g => g.addEventListener("click", () => {
    const b = state.boxes.find(x => x.id == g.dataset.box);
    if (b.state.fault_id) { switchTab("faults"); selectFault(b.state.fault_id); }
    else showBoxModal(b);
  }));
  $("#mapLegend").innerHTML = [
    ["#2f7d5b", "亮灯正常 / 已闭环"], ["#c08a2e", "部分灯带不亮 / 待复验"],
    ["#b4433a", "未关闭故障（供电/定时器/再熄灭）"], ["#34597e", "画面已撤换"],
    ["#9aa0a8", "白天误判 / 暂无证据"],
  ].map(([c, t]) => `<span><i style="background:${c}"></i>${t}</span>`).join("");
}

function showBoxModal(b) {
  const c = b.contract;
  $("#modalContent").innerHTML = `
    <h2 style="margin:0 0 4px">${esc(b.code)} · ${esc(b.name)}</h2>
    <p class="empty" style="margin:0 0 12px">${esc(b.address || "")}${b.circuit ? "　回路：" + esc(b.circuit.name) : ""}</p>
    <div class="kv">
      <dt>亮灯计划</dt><dd>${esc(b.schedule_text)}</dd>
      <dt>当前画面</dt><dd>${c
        ? `<span class="art"><i style="background:${esc(c.preview_color)}"></i>${esc(c.artwork_name)}
             <small>${esc(c.advertiser_name)}</small></span>`
        : "无在档合同（画面空档）"}</dd>
      <dt>最近证据</dt><dd>${evidenceText(b.state)}</dd>
      <dt>状态结论</dt><dd>${statusTag(b.state)}</dd>
    </div>`;
  $("#lightboxModal").classList.remove("hidden");
}

// --------------------------------------------------------------- 列表
function renderBoxTable() {
  $("#boxTable tbody").innerHTML = state.boxes.map(b => {
    const c = b.contract;
    return `<tr class="clickable" data-box="${b.id}">
      <td class="code">${esc(b.code)}</td>
      <td>${esc(b.name)}<br><small class="ev">${esc(b.address || "")}</small></td>
      <td>${b.circuit ? esc(b.circuit.name) : '<span class="ev">独立供电</span>'}</td>
      <td>${c ? `<span class="art"><i style="background:${esc(c.preview_color)}"></i>
          <span>${esc(c.artwork_name)}<small>${esc(c.advertiser_name)}</small></span></span>`
        : '<span class="ev">画面空档</span>'}</td>
      <td class="ev">${esc(b.schedule_text)}</td>
      <td class="ev">${b.state.last_lit_ts != null
        ? `<span class="ts">${esc(b.state.last_lit_ts_label)}</span><br>满亮 · ${esc(b.state.last_lit_inspector)} 拍照`
        : (b.state.last_ts != null
          ? `无亮灯证据<br><span class="ev">最近巡检 ${esc(b.state.last_ts_label)}，亮度 ${Math.round(b.state.last_brightness * 100)}%</span>`
          : "暂无巡检证据")}</td>
      <td>${statusTag(b.state)}</td>
    </tr>`;
  }).join("");
  $$("#boxTable tbody tr").forEach(tr => tr.addEventListener("click", () => {
    const b = state.boxes.find(x => x.id == tr.dataset.box);
    if (b.state.fault_id) { switchTab("faults"); selectFault(b.state.fault_id); }
    else showBoxModal(b);
  }));
}

// --------------------------------------------------------------- 故障
function renderFaultTable() {
  $("#faultTable tbody").innerHTML = state.faults.map(f => {
    const cls = f.status === "verified_closed" ? "ok"
      : f.status === "repaired" ? "warn"
      : f.status === "rejected" ? "bad"
      : (KIND_TAG[f.kind] || "bad");
    return `<tr class="clickable ${state.currentFault === f.id ? "sel" : ""}" data-fid="${f.id}">
      <td class="code">#${f.id}</td>
      <td>${esc(f.box_code)}</td>
      <td>${tag(KIND_TAG[f.kind] || "muted", f.kind_text)}<div class="ev">${esc(f.start_label)} 起</div></td>
      <td>${tag(cls, f.status_text)}</td>
      <td class="ev">${esc(f.detected_label)}</td>
    </tr>`;
  }).join("");
  $$("#faultTable tbody tr").forEach(tr =>
    tr.addEventListener("click", () => selectFault(+tr.dataset.fid)));
}

async function selectFault(fid) {
  state.currentFault = fid;
  renderFaultTable();
  await renderFaultDetail(fid);
}

function inspectorOptions(skipId) {
  return state.inspectors
    .filter(p => !skipId || p.id !== skipId)
    .map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
}

async function renderFaultDetail(fid) {
  let d;
  try { d = await api(`/api/faults/${fid}`); }
  catch (e) { $("#faultDetail").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  const f = d.fault, bd = d.breakdown;
  const isArtwork = f.kind === "artwork_removed";
  const open = ["open", "rejected"].includes(f.status) && !isArtwork;
  const reporter = f.reporter_name;
  const hasOpenWO = d.work_orders.some(w => w.status === "open");

  const advHtml = d.advertisers.length
    ? d.advertisers.map(a => `<span class="art"><i style="background:#3a3f47"></i>
        ${esc(a.name)}<small>${esc(a.box_code)}${a.contact ? " · " + esc(a.contact) : ""}</small></span>`)
      .join("　")
    : '<span class="ev">无在档受影响广告主（画面空档）</span>';

  const compare = `
    <div class="compare">
      <div class="box"><h4>计划亮灯时段（故障窗口内）</h4>
        <div class="num">${bd.planned_minutes} <small>分钟</small></div>
        <ul class="timeline">${bd.planned_segments.map(s =>
          `<li class="hit"><b>${esc(s.start_label)}</b> → <b>${esc(s.end_label)}</b>（${s.minutes} 分钟）</li>`
        ).join("") || '<li>窗口内无计划时段</li>'}</ul>
      </div>
      <div class="box"><h4>实际黑屏 / 缺额（应补时长依据）</h4>
        <div class="num">${bd.effective_blackout_minutes} <small>分钟</small></div>
        <p class="ev" style="margin:6px 0 0">窗口 ${esc(bd.window_start_label)} → ${esc(bd.window_end_label)}<br>
           缺额比例 ${Math.round(bd.deficit * 100)}%${f.kind === "artwork_removed" ? "（画面撤换不计补播）" : ""}</p>
      </div>
    </div>`;

  const photos = d.inspections.filter(i => i.photo_path).slice(0, 6).map(i => `
    <figure class="photo" data-photo="${encodeURIComponent(i.photo_path)}">
      <img src="/photos/${encodeURIComponent(i.photo_path)}" alt="巡检照片">
      <figcaption>${esc(i.ts_label)} · ${esc(i.inspector_name)} · ${Math.round(i.brightness * 100)}%</figcaption>
    </figure>`).join("");

  const woHtml = d.work_orders.map(w => `
    <div class="wo">
      <div class="row1"><b>维修单 #${w.id}</b>
        <span class="tag ${w.status === "open" ? "bad" : w.status === "done" ? "warn" : "ok"}">${WO_STATUS[w.status]}</span></div>
      <div class="ev">派工：${esc(w.assignee)} · 派单 ${esc(w.created_label)} · 预计恢复 ${esc(w.eta_label)}
        ${w.done_min ? `<br>完工 ${esc(w.done_label)}：${esc(w.repair_note || "")}` : ""}</div>
      ${w.status === "open" ? `
      <div class="inline-form" data-wo="${w.id}">
        <label>完工时间<input type="datetime-local" class="wo-done" step="60" value="${nowLocalInput()}"></label>
        <label>维修说明<input class="wo-note" placeholder="如：更换定时器模块"></label>
        <button class="btn small primary btn-wo-done">维修完成</button>
      </div>` : ""}
    </div>`).join("") || '<p class="ev">尚未派单。</p>';

  const dispatch = (open && !hasOpenWO) ? `
    <div class="inline-form" id="dispatchForm">
      <label>维修人员<input id="woAssignee" placeholder="如：赵师傅"></label>
      <label>预计恢复<input id="woEta" type="datetime-local" step="60" value="${nowLocalInput()}"></label>
      <label>说明<input id="woNote" placeholder="故障判断/备件"></label>
      <button class="btn small primary">派单</button>
    </div>` : (hasOpenWO ? `<p class="ev">已有未关闭维修单，不得平行创建第二张。</p>`
      : isArtwork && f.status !== "verified_closed"
        ? `<p class="ev">画面撤换空档不属于亮灯故障，无需派修；新画面上线并在计划时段满亮后自动关闭。</p>`
        : "");

  const reverify = f.status === "repaired" ? `
    <h3>复验关单</h3>
    <p class="ev">最初上报：<b>${esc(reporter || "—")}</b>。复验人不得为上报人本人。</p>
    <div class="inline-form" id="reverifyForm">
      <label>复验人<select id="rvInspector">${inspectorOptions()}</select></label>
      <label>复验时间<input id="rvTs" type="datetime-local" step="60" value="${nowLocalInput()}"></label>
      <label>备注<input id="rvNote" placeholder="亮灯恢复 / 仍异常"></label>
      <button class="btn small primary">提交复验</button>
    </div>` : "";

  const credits = d.credits.length ? `<h3>补播核算（按合同版本）</h3>` + d.credits.map(c => `
    <div class="credit">
      <div class="row1">
        <span class="art"><i style="background:${esc(c.preview_color)}"></i>
          <span>${esc(c.artwork_name)} · ${esc(c.advertiser_name)}<small>${esc(c.box_code)}</small></span></span>
        <span class="tag ${c.status === "approved" ? "ok" : c.status === "rejected" ? "bad" : "warn"}">${CREDIT_STATUS[c.status]}</span>
      </div>
      <div class="ev" style="margin-top:4px">应补 <b>${c.owed_minutes}</b> 分钟 · 核算金额
        <span class="amount">¥${c.amount.toFixed(2)}</span> · 由 ${esc(proposerName(c.proposed_by))} 提出</div>
    </div>`).join("") : "";

  const approve = d.credits.some(c => c.status === "proposed") ? `
    <div class="inline-form" id="approveForm">
      <label>审批人（不得为提出人本人）<select id="apInspector">${inspectorOptions()}</select></label>
      <label>审批时间<input id="apTs" type="datetime-local" step="60" value="${nowLocalInput()}"></label>
      <button class="btn small primary" data-v="1">批准补播</button>
      <button class="btn small" data-v="0">驳回</button>
    </div>` : "";

  const revLog = d.reverifications.length ? `<h3>复验记录</h3><ul class="timeline">` +
    d.reverifications.map(r => `<li class="${r.passed ? "" : "hit"}">
      ${esc(r.ts_label)} · ${esc(r.inspector_name)} · 亮度 ${Math.round(r.brightness * 100)}% ·
      <b>${r.passed ? "通过，故障关闭" : "不通过，维修后再次熄灭"}</b>${r.note ? " · " + esc(r.note) : ""}</li>`).join("")
    + "</ul>" : "";

  $("#faultDetail").innerHTML = `
    <h3 style="margin-top:0">故障单 #${f.id}
      <span style="margin-left:8px">${tag(KIND_TAG[f.kind] || "muted", f.kind_text)}</span>
      ${tag(f.status === "verified_closed" ? "ok" : f.status === "repaired" ? "warn" : "bad", f.status_text)}</h3>
    <dl class="kv">
      <dt>灯箱</dt><dd class="code">${esc(d.box.code)} · ${esc(d.box.name)}</dd>
      <dt>供电回路</dt><dd>${d.box.circuit ? esc(d.box.circuit.name) : "独立供电"}</dd>
      <dt>受影响广告主</dt><dd>${advHtml}</dd>
      <dt>发现时间</dt><dd>${esc(f.detected_label)}（上报人：${esc(reporter || "—")}）</dd>
      <dt>计划时段</dt><dd>${esc(d.schedule_text)}</dd>
      ${f.closed_min ? `<dt>恢复关闭</dt><dd>${esc(f.closed_label)}</dd>` : ""}
      ${f.cause_note ? `<dt>备注</dt><dd>${esc(f.cause_note)}</dd>` : ""}
    </dl>
    <h3>计划时段 × 实际黑屏对照</h3>${compare}
    ${photos ? `<h3>巡检照片</h3><div class="photos">${photos}</div>` : ""}
    <h3>维修派单</h3>${woHtml}${dispatch}
    ${reverify}
    ${credits}${approve}
    ${revLog}`;

  bindDetailEvents(d);
}

function proposerName(id) {
  return state.inspectors.find(p => p.id === id)?.name || `#${id}`;
}

function bindDetailEvents(d) {
  const fid = d.fault.id;
  $$(".photo", $("#faultDetail")).forEach(fig => fig.addEventListener("click", () => {
    const p = decodeURIComponent(fig.dataset.photo);
    $("#modalContent").innerHTML = `<img src="/photos/${encodeURIComponent(p)}" alt="巡检照片">`;
    $("#lightboxModal").classList.remove("hidden");
  }));

  $$(".btn-wo-done", $("#faultDetail")).forEach(btn => btn.addEventListener("click", async () => {
    const form = btn.closest(".inline-form");
    try {
      await api(`/api/workorders/${form.dataset.wo}/complete`, {
        ts: $(".wo-done", form).value, note: $(".wo-note", form).value,
      });
      toast("已登记维修完成，等待复验");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  }));

  const df = $("#dispatchForm");
  if (df) df.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/workorders", {
        fault_id: fid, assignee: $("#woAssignee").value.trim() || "值班维修员",
        eta: $("#woEta").value, note: $("#woNote").value,
      });
      toast("维修单已派出");
      await refreshAll();
    } catch (err) { toast(err.message, true); }
  });

  const rf = $("#reverifyForm");
  if (rf) rf.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api(`/api/faults/${fid}/reverify`, {
        inspector_id: +$("#rvInspector").value, ts: $("#rvTs").value, note: $("#rvNote").value,
      });
      toast(r.passed ? "复验通过，故障已关闭，补播已生成待审批" : "复验不通过：维修后再次熄灭，已退回维修");
      await refreshAll();
    } catch (err) { toast(err.message, true); }
  });

  const af = $("#approveForm");
  if (af) $$("button[data-v]", af).forEach(btn => btn.addEventListener("click", async () => {
    try {
      await api(`/api/makeup/${fid}/approve`, {
        inspector_id: +$("#apInspector").value, ts: $("#apTs").value,
        approve: btn.dataset.v === "1",
      });
      toast(btn.dataset.v === "1" ? "补播结论已批准" : "补播结论已驳回");
      await refreshAll();
    } catch (err) { toast(err.message, true); }
  }));
}

// --------------------------------------------------------------- 巡检/信号
function fillSelects() {
  const boxOpts = state.boxes.map(b =>
    `<option value="${b.id}">${esc(b.code)} · ${esc(b.name)}</option>`).join("");
  const inspOpts = state.inspectors.map(p =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $$("select.f-box").forEach(s => {
    const prev = s.value;
    s.innerHTML = boxOpts;
    if (prev) s.value = prev;
  });
  $$("select.f-inspector").forEach(s => s.innerHTML = inspOpts);
}

async function previewSignal() {
  const form = $("#inspectForm");
  const lb = form.elements["lightbox_id"].value;
  const ts = form.elements["ts"].value;
  if (!lb) return;
  try {
    const s = await api(`/api/signal/${lb}?at=${encodeURIComponent(ts)}`);
    const pct = Math.round(s.brightness * 100);
    const cls = s.brightness >= 0.999 ? "ok" : s.brightness > 0 ? "warn" : "bad";
    $("#signalPreview").innerHTML =
      `适配器读数：${tag(cls, `亮度 ${pct}%`)} 灯带 ${s.lit_bars}/${s.total_bars}
       <span class="ev">（${s.source === "simulated_override" ? "录制的故障信号" : "按计划推断"}）</span>`;
  } catch (e) { $("#signalPreview").textContent = e.message; }
}

function bindInspectTab() {
  const form = $("#inspectForm");
  form.elements["ts"].value = nowLocalInput();
  $("#signalForm").elements["set_at"].value = nowLocalInput();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("#inspectResult");
    try {
      const r = await api("/api/inspect", {
        lightbox_id: +form.elements["lightbox_id"].value,
        inspector_id: +form.elements["inspector_id"].value,
        ts: form.elements["ts"].value, note: form.elements["note"].value,
      });
      out.className = "form-result ok";
      out.textContent = `判定：${r.kind_text}${r.fault_id ? `（故障单 #${r.fault_id}）` : "，未立故障单"}`;
      toast("巡检已登记并生成照片");
      await refreshAll();
    } catch (err) {
      out.className = "form-result err";
      out.textContent = err.message;
    }
  });
  ["lightbox_id", "ts"].forEach(n =>
    form.elements[n].addEventListener("change", previewSignal));

  const sf = $("#signalForm");
  const br = sf.elements["brightness"], bars = sf.elements["lit_bars"];
  function sync() {
    $("#brVal").textContent = `${Math.round(br.value * 100)}%`;
    $("#barVal").textContent = `${bars.value} / 4`;
  }
  br.addEventListener("input", () => {
    bars.value = Math.round(br.value * 4);
    sync();
  });
  bars.addEventListener("input", () => {
    br.value = bars.value / 4;
    sync();
  });
  sync();

  sf.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("#signalResult");
    try {
      await api("/api/signals", {
        lightbox_id: +sf.elements["lightbox_id"].value,
        brightness: +br.value, lit_bars: +bars.value,
        set_at: sf.elements["set_at"].value, note: sf.elements["note"].value,
      });
      out.className = "form-result ok";
      out.textContent = "故障信号已录制，之后该时刻起的巡检将读到该信号";
      toast("信号已录制");
      previewSignal();
    } catch (err) {
      out.className = "form-result err";
      out.textContent = err.message;
    }
  });
  $("#clearSignal").addEventListener("click", async () => {
    try {
      await api("/api/signals", { lightbox_id: +sf.elements["lightbox_id"].value, clear: true });
      $("#signalResult").className = "form-result ok";
      $("#signalResult").textContent = "已恢复为计划信号";
      previewSignal();
    } catch (e) { toast(e.message, true); }
  });
}

// --------------------------------------------------------------- 框架
function switchTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "inspect") previewSignal();
}

$("#modalClose").addEventListener("click", () => $("#lightboxModal").classList.add("hidden"));
$("#lightboxModal").addEventListener("click", (e) => {
  if (e.target.id === "lightboxModal") e.currentTarget.classList.add("hidden");
});
$$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

bindInspectTab();
refreshAll().catch(e => toast(e.message, true));
setInterval(() => refreshAll().catch(() => {}), 30000);
