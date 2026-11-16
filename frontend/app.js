const $ = (s) => document.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
const JSON_H = { "content-type": "application/json" };
const api = async (url, opt) => {
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};
const post = (url, body, method = "POST") => api(url, { method, headers: JSON_H, body: JSON.stringify(body ?? {}) });
const LABEL = { correct: "正确", wrong: "错误", partial: "部分正确", review: "待复核" };
const STEPS = [
  ["preprocess", "预处理", "PDF / 图片转为逐页图片"],
  ["classify", "识别材料用途", "先区分试卷、标准答案与学生答卷"],
  ["ocr", "DeepSeek OCR", "仅识别学生答卷，文字块与坐标随原图传入"],
  ["process", "理解、解题与批改", "一个 Agent 根据完整材料自主理解题目、独立解题并对照学生作答"],
  ["parse", "理解材料", "主 Agent 阅读材料，区分试卷、参考答案和学生作答"],
  ["paper", "试卷库匹配", "判断是否为已批改过的同一套试卷"],
  ["solve", "独立解题", "主 Agent 根据原题生成答案草稿"],
  ["answer_review", "标准答案审核", "审核子 Agent 独立验算，争议题等待人工确认"],
  ["grade", "学生作答批改", "批改子 Agent 读取学生答卷，对照已审核答案判定"],
  ["locate", "答案定位", "程序校验答案文字块与坐标，无法定位时保留判分"],
  ["review", "复核 Agent", "逐题放大核对抄录与位置"],
  ["annotate", "标注", "在卷面绘制对错号"],
];
let cur = null, page = 1, kind = "annotated", es = null, backTo = null, taskTimer = null;
let manualPosition = null;
let manualReviewQueue = Promise.resolve();
let classes = [], allStudents = [], allTasks = [];
let activeJob = null, viewGeneration = 0, stageState = {}, seenEvents = new Set(), lastEventId = 0;
let stageFolds = {}, jobsCache = [], jobsLoading = false, settingsCache = {};
const taskOcrModes = new Map();
let homeOcrMode = null;
const activeStatus = (j) => ["queued", "running"].includes(j.status) || (!j.status && !j.done);
const jobStatus = (j) => j.status || (j.error ? "failed" : j.done ? "done" : "running");
const JOB_LABEL = {queued:"排队中",running:"批改中",done:"已完成",failed:"失败",interrupted:"已中断"};
const seconds = (v) => v == null ? "—" : Number(v).toFixed(1) + "s";
function stepName(key, job = activeJob) {
  return key === "review" && job?.position_method === "ocr_blocks" ? "OCR 位置检查" : (STEPS.find(([k]) => k === key) || [0,key])[1];
}
function gradingRoute(hash) {
  const m = /^#grading\/([^/]+)$/.exec(hash);
  try { return m ? decodeURIComponent(m[1]) : null; } catch { return null; }
}
function stopWatching() {
  cancelManualPosition();
  ++viewGeneration;
  if (es) es.close(); es = null;
}
function clearGradingRoute() {
  if (gradingRoute(location.hash)) history.replaceState(null, "", location.pathname + location.search);
}
function defaultOcrMode() { return "vision"; }
function syncOcrSelectors() { homeOcrMode = "vision"; }


/* ---------- 导航 ---------- */
$$("nav button").forEach((b) => (b.onclick = () => show(b.dataset.view)));
async function show(v) {
  stopWatching(); clearGradingRoute();
  const generation = viewGeneration;
  clearInterval(taskTimer);
  $$("nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("on", s.id === "view-" + v));
  if (v === "work") { cur = null; activeJob = null; $("#desk").hidden = true; $("#home").hidden = false; }
  if (v === "jobs") renderJobs();
  if (v === "papers") loadPapers();
  if (v === "settings") loadSettings();
  if (v === "work" && !cur) { await loadRoster(); if (generation !== viewGeneration) return; fillUploadPickers(); loadHistory(); loadUploadPapers(); }
  if (v === "tasks") { await loadRoster(); if (generation !== viewGeneration) return; $("#taskDetail").hidden = true; $("#taskHome").hidden = false; loadTasks(); }
  if (v === "classes") { $("#studentPage").hidden = true; $("#classHome").hidden = false; loadClasses(); }
  if (v === "reports") { await loadRoster(); if (generation !== viewGeneration) return; initReports(); }
}
async function loadRoster() {
  [classes, allStudents, allTasks] = await Promise.all([api("/api/classes"), api("/api/students"), api("/api/tasks")]);
}
const opts = (items, label, first) => (first != null ? `<option value="">${first}</option>` : "") +
  items.map((i) => `<option value="${i.id}">${esc(label(i))}</option>`).join("");
const stuLabel = (s) => (s.no ? s.no + " " : "") + s.name;

/* ---------- 设置 ---------- */
async function loadSettings() {
  const s = await api("/api/settings"); settingsCache = s; syncOcrSelectors();
  $("#sModel").value = s.model; $("#sUrl").value = s.base_url;
  $("#sKey").value = ""; $("#sKey").placeholder = s.has_key ? `已保存（${s.api_key_masked}），留空则不修改` : "sk-…";
  badge(s);
}
function badge(s) {
  $("#modelBadge").innerHTML = `<span class="dot ${s.has_key ? "ok" : ""}"></span><b>${esc(s.model)}</b><br>${s.has_key ? esc(s.base_url) : "尚未填写 API Key"}`;
}
async function saveSettings() {
  await post("/api/settings", { model: $("#sModel").value, base_url: $("#sUrl").value, api_key: $("#sKey").value }, "PUT");
  await loadSettings();
}
$("#setForm").onsubmit = async (e) => { e.preventDefault(); await saveSettings(); msg("已保存", true); };
$("#testBtn").onclick = async () => {
  if (!$("#setForm").reportValidity()) return;
  msg("正在连接…");
  try {
    await saveSettings();
    const r = await api("/api/settings/test", { method: "POST" });
    msg(r.ok ? `连接成功（${r.elapsed_s}s）：${r.reply}` : r.error, r.ok);
  } catch (e) { msg(e.message, false); }
};
function msg(t, ok) { const m = $("#setMsg"); m.textContent = t; m.style.color = ok === undefined ? "var(--muted)" : ok ? "var(--ok)" : "var(--bad)"; }

/* ---------- 上传 ---------- */
function fillUploadPickers() {
  const c = $("#uClass").value;
  $("#uClass").innerHTML = opts(classes, (x) => x.name, "不归档");
  $("#uClass").value = classes.some((x) => x.id === c) ? c : "";
  fillTaskStudent();
}
function fillTaskStudent() {
  const c = $("#uClass").value, keepT = $("#uTask").value, keepS = $("#uStudent").value;
  $("#uTask").innerHTML = opts(allTasks.filter((t) => t.class_id === c), (t) => t.title, c ? "不属于任何任务" : "先选班级");
  $("#uStudent").innerHTML = opts(allStudents.filter((s) => s.class_id === c), stuLabel, c ? "按卷面姓名自动识别" : "先选班级");
  // 返回上传页时保留之前选的任务和学生（只要它们还属于当前班级）
  if (allTasks.some((t) => t.id === keepT && t.class_id === c)) $("#uTask").value = keepT;
  if (allStudents.some((x) => x.id === keepS && x.class_id === c)) $("#uStudent").value = keepS;
  $("#uTask").disabled = $("#uStudent").disabled = !c;
}
$("#uClass").onchange = () => { fillTaskStudent(); saveUploadDraft(); renderUploadDrafts(); };
$("#uTask").onchange = $("#uStudent").onchange = $("#answerKey").oninput = () => { saveUploadDraft(); renderUploadDrafts(); };

const MATERIAL_ROLES = { auto: "自动识别", question_paper: "试卷", answer_key: "标准答案", student_work: "学生作答" };
let stagedFiles = [], nextMaterialId = 0, uploadInFlight = false;
let uploadMode = "grade", selectedPaperId = "";
const uploadDrafts = new Map();
let activeDraftId = 1, nextDraftId = 1;
function snapshotUploadDraft() {
  return { files: stagedFiles, answer_key: $("#answerKey").value, class_id: $("#uClass").value,
    task_id: $("#uTask").value, student_id: $("#uStudent").value, ocr_mode: homeOcrMode,
    mode: uploadMode, paper_id: selectedPaperId, error: $("#uploadErr").textContent };
}
function saveUploadDraft() { uploadDrafts.set(activeDraftId, snapshotUploadDraft()); }
function renderUploadDrafts() {
  const drafts = [...uploadDrafts].filter(([id,draft]) => id === activeDraftId || draft.files.length || draft.answer_key);
  $("#uploadDrafts").hidden = drafts.length < 2;
  $("#uploadDrafts").innerHTML = drafts.map(([id,draft]) => {
    const task = allTasks.find((t) => t.id === draft.task_id), cls = classes.find((c) => c.id === draft.class_id);
    const label = draft.mode === "prepare" ? "试卷建库" : task?.title || cls?.name || "未归档";
    return `<button type="button" class="ghost ${id === activeDraftId ? "on" : ""}" data-upload-draft="${id}" aria-pressed="${id === activeDraftId}">${esc(label)} · ${draft.files.length} 个文件${draft.answer_key ? " · 含答案文本" : ""}</button>`;
  }).join("");
}
function restoreUploadDraft(id) {
  const draft = uploadDrafts.get(id); if (!draft) return;
  activeDraftId = id; stagedFiles = draft.files;
  fillUploadPickers(); $("#uClass").value = draft.class_id; fillTaskStudent();
  $("#uTask").value = draft.task_id; $("#uStudent").value = draft.student_id;
  $("#answerKey").value = draft.answer_key; homeOcrMode = draft.ocr_mode; syncOcrSelectors();
  uploadMode = draft.mode || "grade"; selectedPaperId = draft.paper_id || ""; updateUploadMode();
  $("#uploadErr").textContent = draft.error || ""; renderMaterials();
}
function switchUploadDraft(id) {
  if (!uploadDrafts.has(Number(id))) return;
  saveUploadDraft(); show_("work"); cur = null; activeJob = null;
  $("#home").hidden = false; $("#desk").hidden = true; restoreUploadDraft(Number(id));
}
$("#uploadDrafts").onclick = (e) => { if (e.target.dataset.uploadDraft) switchUploadDraft(e.target.dataset.uploadDraft); };
function materialError(files) {
  if (files.length > 20) return "一次批改最多添加 20 个文件。";
  if (files.some((x) => x.file.size > 40 * 1024 * 1024)) return "单个文件不能超过 40MB。";
  if (files.reduce((n, x) => n + (x.file.size || 0), 0) > 200 * 1024 * 1024) return "同一次批改的文件合计不能超过 200MB。";
  return "";
}
function renderMaterials() {
  $("#materialList").innerHTML = stagedFiles.map((x) => `<div class="material-row"><div class="grow"><b>${esc(x.file.name)}</b><small>${((x.file.size || 0) / 1024 / 1024).toFixed(1)} MB</small></div><label>用途 <select data-material-role="${x.id}" aria-label="${esc(x.file.name)} 的用途">${Object.entries(MATERIAL_ROLES).map(([v,l]) => `<option value="${v}" ${x.role === v ? "selected" : ""}>${l}</option>`).join("")}</select></label><button class="ghost" data-material-remove="${x.id}" aria-label="移除 ${esc(x.file.name)}">移除</button></div>`).join("");
  $("#materialSummary").textContent = stagedFiles.length ? `已添加 ${stagedFiles.length} 个文件 · ${uploadMode === "prepare" ? "试卷与参考答案" : "同一次批改，属于一名学生"}` : "还未添加材料";
  $("#startUpload").disabled = uploadInFlight || !stagedFiles.length;
  $("#startUpload").textContent = uploadInFlight ? "正在提交…" : uploadMode === "prepare" ? "开始建库" : "开始批改";
  saveUploadDraft(); renderUploadDrafts();
}
function updateUploadMode() {
  $("#uMode").value = uploadMode; $("#uPaper").value = selectedPaperId;
  for (const id of ["uClassWrap", "uTaskWrap", "uStudentWrap", "uPaperWrap"]) $("#" + id).hidden = uploadMode === "prepare";
  $("#uploadTitle").textContent = uploadMode === "prepare" ? "先备好这套试卷" : "今天批改哪一份？";
  $("#uploadLead").textContent = uploadMode === "prepare"
    ? "上传试卷和可选参考答案。主 Agent 独立解题，审核子 Agent 核对后存入试卷库，争议题可人工确认。"
    : "添加试卷、可选参考答案和一位学生的作答。一个 Agent 直接看图，按需裁剪放大、独立解题、批改和复核，生成红勾、红叉及黄问号。";
}
$("#uMode").onchange = (e) => { uploadMode = e.target.value === "prepare" ? "prepare" : "grade"; updateUploadMode(); renderMaterials(); };
$("#uPaper").onchange = (e) => { selectedPaperId = e.target.value; saveUploadDraft(); };
async function loadUploadPapers() {
  const generation = viewGeneration;
  try {
    const papers = await api("/api/papers");
    if (generation !== viewGeneration) return;
    $("#uPaper").innerHTML = opts(papers, (p) => p.title, "从本次材料识别试卷");
    $("#uPaper").value = selectedPaperId;
  } catch (e) { if (generation === viewGeneration) $("#uploadErr").textContent = "读取试卷库失败：" + e.message; }
}
$("#preparePaper").onclick = () => {
  saveUploadDraft();
  const id = ++nextDraftId;
  uploadDrafts.set(id, {files:[], answer_key:"", class_id:"", task_id:"", student_id:"", mode:"prepare", paper_id:"", error:""});
  switchUploadDraft(id);
};
function stageFiles(files, role = "auto") {
  const additions = Array.from(files, (file) => ({ id: ++nextMaterialId, file, role }));
  const error = materialError([...stagedFiles, ...additions]);
  $("#uploadErr").textContent = error;
  if (error) return false;
  stagedFiles.push(...additions); renderMaterials(); return true;
}
function removeMaterial(id) { stagedFiles = stagedFiles.filter((x) => x.id !== Number(id)); renderMaterials(); }
function setMaterialRole(id, role) {
  const entry = stagedFiles.find((x) => x.id === Number(id));
  if (entry && Object.hasOwn(MATERIAL_ROLES, role)) entry.role = role;
}
$("#materialList").onchange = (e) => { if (e.target.dataset.materialRole) setMaterialRole(e.target.dataset.materialRole, e.target.value); };
$("#materialList").onclick = (e) => { if (e.target.dataset.materialRemove) removeMaterial(e.target.dataset.materialRemove); };
$("#startUpload").onclick = () => uploadFromHome();
$("#keyFile").onchange = (e) => {
  stageFiles([...e.target.files], "answer_key"); e.target.value = "";
};
const drop = $("#drop");
["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files.length) stageFiles([...e.dataTransfer.files]); });
$("#file").onchange = (e) => { if (e.target.files.length) stageFiles([...e.target.files]); e.target.value = ""; };

async function sendMaterials(materials, extra) {
  const fd = new FormData();
  for (const x of materials) fd.append("files", x.file);
  fd.append("file_roles", JSON.stringify(materials.map((x) => x.role)));
  for (const [k, v] of Object.entries(extra)) if (v) fd.append(k, v);
  return api("/api/submissions", { method: "POST", body: fd });
}
async function uploadFromHome(files) {
  if (uploadInFlight) return;
  if (files && !stageFiles(files)) return;
  if (!stagedFiles.length) return;
  const error = materialError(stagedFiles);
  $("#uploadErr").textContent = error;
  if (error) return;
  saveUploadDraft();
  const snapshot = stagedFiles.map((x) => ({...x})), uploadGeneration = viewGeneration, submittedDraftId = activeDraftId;
  const extra = {mode: uploadMode, ocr_mode: "vision", answer_key: $("#answerKey").value,
    ...(uploadMode === "grade" ? {paper_id:selectedPaperId, class_id:$("#uClass").value, task_id:$("#uTask").value, student_id:$("#uStudent").value} : {})};
  const from = extra.task_id ? () => openTask(extra.task_id) : null;
  const name = snapshot.map((x) => x.file.name).join("、");
  const row = document.createElement("button");
  row.className = "item"; row.innerHTML = `<div class="grow"><b>${esc(name)}</b><small>上传中…</small></div>`;
  $("#queue").prepend(row);
  const note = (t) => { row.querySelector("small").textContent = t; };
  uploadInFlight = true; renderMaterials();
  try {
    const r = await sendMaterials(snapshot, extra);
    const submitted = new Set(snapshot.map((x) => x.id));
    saveUploadDraft();
    const draft = uploadDrafts.get(submittedDraftId);
    draft.files = draft.files.filter((x) => !submitted.has(x.id));
    if (draft.answer_key === extra.answer_key) draft.answer_key = "";
    draft.error = "";
    if (activeDraftId === submittedDraftId) { stagedFiles = draft.files; $("#answerKey").value = draft.answer_key; }
    if (r.cached) {
      note("同一材料，直接读取上次结果，点击查看"); row.onclick = () => openResult(r.id, true, from);
      refreshJobs();
      if (uploadGeneration === viewGeneration && activeDraftId === submittedDraftId) openResult(r.id, true, from);
    } else {
      note("已进入队列"); row.onclick = () => openJob(r.id, name, from);
      pending.set(r.id, {row, note, res() {}}); pollQueue();
      if (uploadGeneration === viewGeneration && activeDraftId === submittedDraftId) watch(r.id, name, from);
    }
  } catch (e) {
    note("失败：" + e.message);
    uploadDrafts.get(submittedDraftId).error = e.message;
    if (uploadGeneration === viewGeneration && activeDraftId === submittedDraftId) $("#uploadErr").textContent = e.message;
  } finally { uploadInFlight = false; renderMaterials(); }
}
renderMaterials();
const pending = new Map();
function pollQueue() { refreshJobs(); }
function jobRows(jobs) {
  return jobs.length ? jobs.map((j) => `<button class="item" data-job="${esc(j.id)}"><div class="grow"><b>${esc(j.filename)}</b><small>${esc(j.message || j.error || JOB_LABEL[jobStatus(j)])} · ${seconds(j.elapsed_s)}${j.stage ? " · " + esc(stepName(j.stage,j)) : ""}</small></div><span class="chip ${j.error ? "wrong" : activeStatus(j) ? "accent" : "correct"}">${JOB_LABEL[jobStatus(j)] || esc(j.status)}</span></button>`).join("") : `<div class="empty">当前没有批改任务。</div>`;
}
function bindJobs(root, from) { $$("[data-job]", root).forEach((b) => b.onclick = () => openJob(b.dataset.job, null, from)); }
function renderJobs() {
  const status = $("#jobFilter").value || "all";
  $("#jobList").innerHTML = jobRows(jobsCache.filter((j) => status === "all" || jobStatus(j) === status)); bindJobs($("#jobList"), () => show("jobs"));
}
async function refreshJobs() {
  if (jobsLoading) return; jobsLoading = true;
  try {
    jobsCache = await api("/api/jobs?status=all");
    const active = jobsCache.filter(activeStatus);
    $("#queueCount").textContent = active.length; $("#activeJobs").innerHTML = jobRows(active); bindJobs($("#activeJobs"), null);
    $("#activeJobsSection").hidden = !active.length; renderJobs();
    const by = Object.fromEntries(jobsCache.map((j) => [j.id,j]));
    for (const [id,x] of [...pending]) {
      const j = by[id]; if (!j) continue;
      x.note(j.message || JOB_LABEL[jobStatus(j)]); x.row.onclick = () => openJob(id, null, null);
      if (!activeStatus(j)) { pending.delete(id); x.res(); if (!cur && !activeJob) loadHistory(); }
    }
  } catch {} finally { jobsLoading = false; }
}
$("#jobFilter").onchange = renderJobs;

/* ---------- 单份：实时进度 ---------- */
function renderSteps(state, reset = false) {
  if (reset || !$("#steps").children.length) {
    $("#steps").innerHTML = STEPS.map(([k,name,desc]) => `<li data-stage="${k}"><span class="ico"></span><details class="stage-detail"><summary><b>${name}</b><span class="stage-time"></span><small class="stage-summary">${desc}</small>${["parse","solve","answer_review","grade","locate","review"].includes(k) ? '<small class="effort">推理强度 high</small>' : ""}</summary><pre class="stage-log"></pre><details class="raw-output"><summary>模型原始输出</summary></details></details></li>`).join("");
    $$("#steps [data-stage]").forEach((node) => {
      const detail = node.querySelector(".stage-detail");
      detail.addEventListener("toggle", () => { if (detail.open !== detail._defaultOpen) stageFolds[node.dataset.stage] = detail.open; });
    });
  }
  for (const [k,,desc] of STEPS) {
    const node = $(`#steps [data-stage="${k}"]`); if (!node) continue;
    const st = state[k] || {}, cls = {start:"run",info:"run",done:"done",skip:"skip",error:"fail",interrupted:"fail"}[st.status] || "";
    node.className = cls;
    node.hidden = ["single_agent", "vision_agent"].includes(activeJob?.execution_mode)
      ? ["parse", "solve", "answer_review", "grade", "review"].includes(k) ||
        (activeJob?.execution_mode === "vision_agent" && ["classify", "ocr"].includes(k)) ||
        (k === "paper" && !state.paper) || (k === "locate" && activeJob?.mode === "prepare")
      : k === "process" || (k === "review" ? activeJob?.workflow_version === 2 || !state.review
        : ["answer_review", "locate"].includes(k) && activeJob?.workflow_version === 1);
    const ocrPosition = k === "review" && activeJob?.position_method === "ocr_blocks";
    node.querySelector("b").textContent = st.title || stepName(k);
    const effort = node.querySelector(".effort"); if (effort) effort.hidden = ocrPosition;
    node.querySelector(".ico").textContent = {done:"✓",skip:"↷",fail:"!"}[cls] || "";
    node.querySelector(".stage-summary").textContent = st.message || (ocrPosition ? "使用 OCR 答案文字块坐标，无需调用复核 Agent" : desc);
    const elapsed = st.started_at != null && cls === "run"
      ? (st.elapsed_s || 0) + Math.max(0, Date.now()/1000 - st.started_at) : st.elapsed_s;
    node.querySelector(".stage-time").textContent = elapsed == null ? "" : seconds(elapsed);
    const detail = node.querySelector(".stage-detail"), open = stageFolds[k] ?? cls === "run";
    detail._defaultOpen = open; if (detail.open !== open) detail.open = open;
    node.querySelector(".stage-log").textContent = (st.logs || []).join("\n");
    const raw = node.querySelector(".raw-output");
    const panes = raw._outputPanes ||= new Map();
    for (const [id, output] of st.outputs || []) {
      let pane = panes.get(id);
      if (!pane) {
        const detail = document.createElement("details"), label = document.createElement("summary"), text = document.createElement("pre");
        detail.className = "call-output"; detail.append(label, text); raw.append(detail);
        pane = { detail, label, text }; panes.set(id, pane);
      }
      pane.label.textContent = output.label;
      if (pane.text.textContent !== output.text) pane.text.textContent = output.text;
    }
    raw.hidden = !st.outputs?.size;
  }
}
function applyJobEvent(e) {
  const id = Number(e.id || 0); if (id && seenEvents.has(id)) return false;
  if (id) { seenEvents.add(id); lastEventId = Math.max(lastEventId,id); }
  if (!STEPS.some(([k]) => k === e.stage)) return true;
  const old = stageState[e.stage] || {logs:[]};
  const st = stageState[e.stage] = {...old, logs:old.logs || [], outputs:old.outputs || new Map()};
  if (e.kind === "output" || e.status === "output") {
    const callId = e.call_id == null ? "" : String(e.call_id);
    const output = st.outputs.get(callId) || {label: e.label || "模型调用", text: ""};
    if (e.label) output.label = e.label;
    output.text += e.text || ""; st.outputs.set(callId, output);
  }
  else {
    if (e.message) st.logs.push(e.message);
    if (e.kind === "metric" && e.metric) st.logs.push(`${e.metric.label || "模型调用"} · ${e.metric.model || ""} · ${e.metric.effort || "high"} · ${modelMetricText(e.metric)}`);
    if ((!e.kind || e.kind === "status") && (!old.t || !e.t || e.t >= old.t)) {
      const running = old.started_at != null && ["start","info"].includes(old.status);
      Object.assign(st,e);
      if (e.status === "start") {
        st.started_at = e.started_at ?? (running ? old.started_at : e.t);
        st.elapsed_s = e.elapsed_s ?? old.elapsed_s ?? 0;
      }
      if (["done","skip","error"].includes(e.status)) {
        st.elapsed_s = e.elapsed_s ?? ((old.elapsed_s || 0) + (running && e.t != null ? Math.max(0, e.t - old.started_at) : 0));
      }
    }
  }
  return true;
}
function openDesk(title) {
  show_("work");
  $("#home").hidden = true; $("#desk").hidden = false;
  $("#dTitle").textContent = title; $("#dBadges").innerHTML = ""; $("#summary").hidden = true;
  $("#qlist").innerHTML = ""; $("#pageTabs").innerHTML = "";
  $("#regrade").hidden = $("#pdfBtn").hidden = $("#dStudentWrap").hidden = true;
  $("#retryJob").hidden = true; $("#jobNotice").textContent = ""; $("#jobMetrics").hidden = true;
  $("#pageImg").removeAttribute("src"); $("#pageImg").hidden = true; $("#paperWait").hidden = false; $("#paperWait").textContent = "等待页面预处理…";
}
function show_(v) {
  stopWatching(); clearGradingRoute(); clearInterval(taskTimer);
  $$("nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("on", s.id === "view-" + v));
}
function openJob(id, name, from) { return watch(id, name, from); }
async function watch(id, name, from) {
  cur = null; activeJob = {id,page_count:0,status:"queued"}; page = 1; kind = "raw";
  if (from !== undefined) backTo = from;
  openDesk(name || "正在读取批改任务…");
  history.replaceState(null, "", "#grading/" + encodeURIComponent(id));
  const generation = viewGeneration, valid = () => generation === viewGeneration && activeJob?.id === id;
  stageState = {}; stageFolds = {}; seenEvents = new Set(); lastEventId = 0; renderSteps(stageState, true); drawPage();
  let snapshot;
  try {
    snapshot = await api(`/api/submissions/${id}/status`); if (!valid()) return;
    activeJob = {...activeJob,...snapshot}; $("#dTitle").textContent = snapshot.filename || name || id;
    stageState = snapshot.stages || {}; renderSteps(stageState); updateJobPreview();
    $("#dBadges").innerHTML = `<span class="chip accent">${JOB_LABEL[jobStatus(snapshot)] || "批改中"}</span><span class="chip">${esc(snapshot.model || "")} · 推理强度 high</span><span class="chip">${snapshot.ocr_mode === "deepseek_ocr" ? "DeepSeek OCR 辅助" : "视觉识别"}</span>`;
    if (snapshot.error) showJobFailure(snapshot.error);
    if (jobStatus(snapshot) === "done" && !snapshot.last_event_id) { await openResult(id,false,undefined,generation); return; }
  } catch(e) { if(valid()) { $("#jobNotice").textContent = "无法读取任务：" + e.message; $("#retryJob").hidden = false; } return; }
  if (!valid()) return;
  const source = new EventSource(`/api/submissions/${id}/events?after=0`); es = source;
  source.onmessage = async (m) => {
    if (!valid() || es !== source) return;
    let e; try { e = JSON.parse(m.data); if (!e.id && m.lastEventId) e.id = +m.lastEventId; } catch { return; }
    if (!applyJobEvent(e)) return;
    $("#jobNotice").textContent = "";
    if (e.page_count) { activeJob.page_count = e.page_count; updateJobPreview(); }
    if (e.stage === "preprocess" && e.status === "done" && !activeJob.page_count) {
      api(`/api/submissions/${id}/status`).then((s) => {if (valid()) {activeJob = {...activeJob,...s};updateJobPreview();}}).catch(()=>{});
    }
    if (e.kind === "status" || !e.kind) {
      if (STEPS.some(([k]) => k === e.stage)) activeJob.status = "running";
      $("#dBadges").innerHTML = `<span class="chip accent">${JOB_LABEL[jobStatus(activeJob)] || "批改中"}</span><span class="chip">${esc(activeJob.model || "")} · 推理强度 high</span><span class="chip">${activeJob.ocr_mode === "deepseek_ocr" ? "DeepSeek OCR 辅助" : "视觉识别"}</span>`;
    }
    renderSteps(stageState);
    if (e.stage === "done" || (snapshot.status === "done" && lastEventId >= snapshot.last_event_id)) {
      source.close(); if (es === source) es = null;
      try { await openResult(id,false,undefined,generation); } catch(err) { if (valid()) $("#jobNotice").textContent = "结果暂时无法读取：" + err.message; }
      refreshJobs();
    } else if (["error","interrupted"].includes(e.stage)) {
      source.close(); if (es === source) es = null; activeJob.status = e.stage === "error" ? "failed" : "interrupted"; showJobFailure(e.message); refreshJobs();
    }
  };
  source.onerror = () => {
    if (!valid() || es !== source) return;
    $("#jobNotice").textContent = "连接暂时中断，正在自动重连；服务端继续批改。";
    // EventSource sends Last-Event-ID when reconnecting; seenEvents also filters replayed IDs.
  };
}
function showJobFailure(message) {
  for (const st of Object.values(stageState)) if (["start","info"].includes(st.status)) {
    st.status = "error"; st.message = message || "批改已中断";
    if (st.started_at != null) st.elapsed_s = (st.elapsed_s || 0) + Math.max(0, (activeJob?.finished_at || Date.now()/1000) - st.started_at);
  }
  renderSteps(stageState);
  $("#jobNotice").textContent = "批改未完成：" + (message || "服务已中断，可重试"); $("#retryJob").hidden = false;
}
function firstStudentPage(job) {
  return job.graded_pages?.[0] || job.materials?.find((m) => m.role === "student_work")?.page || 1;
}
function pageTabs(job) {
  return Array.from({length:job.page_count || 0}, (_,i) => {
    const m = job.materials?.find((x) => x.page === i + 1);
    const label = m ? `${MATERIAL_ROLES[m.role] || "材料"} · ${m.filename || "文件"} 第 ${m.source_page || 1} 页` : `第 ${i+1} 页`;
    return `<button data-p="${i+1}">${esc(label)}</button>`;
  }).join("");
}
function updateJobPreview() {
  const count = activeJob?.page_count || 0;
  if ($$("#pageTabs button").length !== count) {
    $("#pageTabs").innerHTML = pageTabs(activeJob);
    $$("#pageTabs button").forEach((b)=>b.onclick=()=>{page=+b.dataset.p;drawPage();});
  }
  drawPage();
}
$("#retryJob").onclick = async () => {
  const id = activeJob?.id, name = activeJob?.filename, generation = viewGeneration; if (!id) return;
  try { const r = await post(`/api/submissions/${id}/retry`); if (generation === viewGeneration) openJob(r.id,name); refreshJobs(); }
  catch(e) { if (generation === viewGeneration && activeJob?.id === id) $("#jobNotice").textContent = e.message; }
};

/* ---------- 单份：结果 ---------- */
async function openResult(id, fileCached, from, generation) {
  if (generation === undefined) {
    const opening = openJob(id, null, from); activeJob.fileCached = !!fileCached; return opening;
  }
  const r = await api(`/api/submissions/${id}`);
  if (generation !== viewGeneration || activeJob?.id !== id) return;
  if (r.mode === "prepare") {
    show_("papers"); activeJob = null; cur = null;
    await openPaper(r.paper_id);
    return;
  }
  if (!allStudents.length) await loadRoster();
  if (generation !== viewGeneration || activeJob?.id !== id) return;
  if (from !== undefined) backTo = from;
  cur = r; page = firstStudentPage(r); kind = "annotated";
  $("#dTitle").textContent = r.filename; activeJob = {...activeJob, ...r, status:"done"};
  const state = {...stageState};
  STEPS.forEach(([k]) => (state[k] = {...state[k], status: state[k]?.status || "done", elapsed_s:r.timings?.[k] ?? state[k]?.elapsed_s}));
  if (r.paper_cache_hit) state.solve = {...state.solve, status: "skip", message: "命中试卷库，直接复用已保存的标准答案" };
  state.paper = {...state.paper, status: "done", message: r.paper_cache_hit ? `命中「${r.paper_title}」（相似度 ${Math.round(r.paper_similarity * 100)}%）` : "新试卷，已存入试卷库" };
  if (!r.ocr_used && r.execution_mode !== "vision_agent") state.ocr = {...state.ocr, status: "skip", message: "本次未使用（非 DeepSeek 接口、已关闭或未返回坐标）" };
  renderSteps(state);
  const b = [];
  if (fileCached || activeJob.fileCached) b.push(`<span class="chip accent">同一文件 · 直接读取上次结果</span>`);
  b.push(r.paper_cache_hit ? `<span class="chip accent">命中试卷库 · 未重新计算答案</span>` : `<span class="chip">新试卷 · 答案已入库</span>`);
  if (r.student_match === "auto") b.push(`<span class="chip accent">按卷面姓名自动归档</span>`);
  else if (!r.student_id && r.detected_name) b.push(`<span class="chip review">卷面姓名「${esc(r.detected_name)}」未匹配到学生</span>`);
  if (r.execution_mode === "vision_agent") b.push(`<span class="chip accent">纯视觉 · 动态放大复核</span>`);
  else if (r.ocr_used) b.push(`<span class="chip accent">${r.vision_fallback_used ? "OCR＋模型辅助定位" : r.position_method === "ocr_blocks" ? "OCR 坐标直接标注" : "DeepSeek OCR 辅助定位"}</span>`);
  b.push(`<span class="chip">推理强度 ${esc(r.effort || "high")}</span>`);
  b.push(`<span class="chip">答案来源：${r.answer_source === "teacher" ? "老师" : "AI"}</span>`, `<span class="chip">${esc(r.model)}</span>`, `<span class="chip">${r.elapsed_s}s</span>`);
  $("#dBadges").innerHTML = b.join("");
  $("#pageTabs").innerHTML = pageTabs(r);
  $$("#pageTabs button").forEach((x) => (x.onclick = () => { page = +x.dataset.p; drawPage(); }));
  $("#regrade").hidden = false;
  $("#pdfBtn").hidden = false; $("#pdfBtn").href = `/api/submissions/${id}/pdf`;
  const pool = allStudents.filter((s) => !r.class_id || s.class_id === r.class_id);
  $("#dStudentWrap").hidden = !pool.length;
  $("#dStudent").innerHTML = opts(pool, (s) => (r.class_id ? "" : s.class_name + " · ") + stuLabel(s), "未归档");
  $("#dStudent").value = r.student_id || "";
  $("#jobNotice").textContent = ""; $("#retryJob").hidden = true; renderMetrics(r);
  drawPage(); drawResult();
}
$("#dStudent").onchange = async (e) => {
  const id = cur?.id, generation = viewGeneration; if (!id) return;
  const r = await post(`/api/submissions/${id}/assign`, { student_id: e.target.value || null });
  if (generation === viewGeneration && cur?.id === id) cur = r;
};
function drawPage() {
  if (manualPosition && (manualPosition.sid !== cur?.id || manualPosition.page !== page || kind !== "raw")) cancelManualPosition();
  const job = cur || activeJob, img = $("#pageImg"), wait = $("#paperWait");
  const material = job?.materials?.find((m) => m.page === page);
  const studentPage = !material || material.role === "student_work";
  $("#showAnn").disabled = !cur || !studentPage;
  if (!cur || !studentPage) kind = "raw";
  $("#showAnn").classList.toggle("on", kind === "annotated"); $("#showRaw").classList.toggle("on", kind === "raw");
  if (!job?.id || !job.page_count) { img.hidden = true; wait.hidden = false; wait.textContent = "等待页面预处理…"; return; }
  const generation = viewGeneration, id = job.id, requestPage = page, requestKind = kind;
  const url = `/api/submissions/${id}/pages/${page}?kind=${kind}&t=${Date.now()}`;
  const valid = () => generation === viewGeneration && (cur || activeJob)?.id === id && page === requestPage && kind === requestKind && img.src.endsWith(url);
  img.hidden = true; wait.hidden = false; wait.textContent = "正在加载页面…"; $("#retryPage").hidden = true;
  img.onload = () => { if (valid()) { img.hidden = false; wait.hidden = true; } };
  img.onerror = () => { if (valid()) { wait.textContent = "页面加载失败，请重试"; $("#retryPage").hidden = false; } };
  img.src = url;
  $$("#pageTabs button").forEach((x) => x.classList.toggle("on", +x.dataset.p === page));
}
$("#showAnn").onclick = () => { if (!cur) return; kind = "annotated"; drawPage(); };
$("#showRaw").onclick = () => { kind = "raw"; drawPage(); };
$("#retryPage").onclick = drawPage;
function cancelManualPosition() {
  manualPosition = null;
  $("#manualPositionTools").hidden = true;
  $("#positionBox").hidden = true;
  $("#pageImg").classList.remove("positioning");
}
function beginManualPosition(qid) {
  const q = cur?.questions.find((q) => q.id === qid);
  if (!q) return;
  cancelManualPosition(); page = q.page; kind = "raw";
  manualPosition = {sid:cur.id, qid, page, generation:viewGeneration};
  $("#positionHint").textContent = `${q.position_note && !q.draw ? q.position_note + "。" : ""}请在${questionPageLabel(q)}原图上拖动框选第 ${questionLabel(q)} 题的学生答案，松开后保存位置`;
  $("#manualPositionTools").hidden = false;
  $("#pageImg").classList.add("positioning");
  drawPage();
}
$("#cancelPosition").onclick = cancelManualPosition;
function positionPoint(e) {
  const rect = $("#pageImg").getBoundingClientRect();
  return {x:Math.max(0,Math.min(1000,(e.clientX-rect.left)/rect.width*1000)),
    y:Math.max(0,Math.min(1000,(e.clientY-rect.top)/rect.height*1000))};
}
function showPositionBox(a, b) {
  const img = $("#pageImg"), box = $("#positionBox");
  box.hidden = false;
  Object.assign(box.style, {left:`${img.offsetLeft+Math.min(a.x,b.x)/1000*img.clientWidth}px`,
    top:`${img.offsetTop+Math.min(a.y,b.y)/1000*img.clientHeight}px`,
    width:`${Math.abs(a.x-b.x)/1000*img.clientWidth}px`,height:`${Math.abs(a.y-b.y)/1000*img.clientHeight}px`});
}
$("#pageImg").addEventListener("pointerdown", (e) => {
  if (!manualPosition || manualPosition.saving || (e.button != null && e.button !== 0)) return;
  e.preventDefault();
  manualPosition.start = positionPoint(e);
  manualPosition.pointer = e.pointerId;
  $("#pageImg").setPointerCapture(e.pointerId);
  showPositionBox(manualPosition.start, manualPosition.start);
});
$("#pageImg").addEventListener("pointermove", (e) => {
  if (manualPosition?.start && manualPosition.pointer === e.pointerId) showPositionBox(manualPosition.start, positionPoint(e));
});
$("#pageImg").addEventListener("pointercancel", () => {
  if (manualPosition) manualPosition.start = null;
  $("#positionBox").hidden = true;
});
$("#pageImg").addEventListener("pointerup", async (e) => {
  const target = manualPosition;
  if (!target?.start || target.pointer !== e.pointerId) return;
  const a = target.start, b = positionPoint(e); target.start = null;
  const bbox = [Math.min(a.x,b.x),Math.min(a.y,b.y),Math.max(a.x,b.x),Math.max(a.y,b.y)].map(Math.round);
  if (bbox[2]-bbox[0]<2 || bbox[3]-bbox[1]<2) { $("#positionBox").hidden = true; return; }
  target.saving = true; $("#positionHint").textContent = "正在保存答案位置…";
  try {
    const r = await post(`/api/submissions/${target.sid}/position`, {qid:target.qid,bbox});
    if (manualPosition !== target || cur?.id !== target.sid || viewGeneration !== target.generation) return;
    cur = r; cancelManualPosition(); kind = "annotated"; drawResult(); drawPage();
  } catch (error) {
    if (manualPosition === target) { target.saving = false; $("#positionHint").textContent = `保存失败：${error.message}，请重新框选`; }
  }
});
function modelMetricText(m) {
  const parts = [`调用 ${seconds(m.elapsed_s ?? m.duration_s)}`];
  if (m.queue_s != null) parts.push(`排队 ${seconds(m.queue_s)}`);
  if (m.first_output_s != null) parts.push(`首字 ${seconds(m.first_output_s)}`);
  if (m.usage?.input_tokens != null) parts.push(`输入 ${m.usage.input_tokens} tokens`);
  if (m.usage?.output_tokens != null) parts.push(`输出 ${m.usage.output_tokens} tokens`);
  return parts.join(" · ");
}
function renderMetrics(r) {
  const box = $("#jobMetrics");
  const timings = Object.entries(r.timings || {});
  box.hidden = !timings.length && !(r.model_calls || []).length;
  box.innerHTML = `<summary>耗时与模型调用</summary><p class="sub">总耗时 ${seconds(r.elapsed_s)} · 排队 ${seconds(r.queue_s)}</p>${timings.map(([k,v])=>`<div class="metric-row"><span>${esc(stepName(k,r))}</span><b>${seconds(v)}</b></div>`).join("")}${(r.model_calls||[]).map((m)=>`<div class="metric-row"><span>${esc(m.label || stepName(m.stage,r) || m.name || "模型调用")} · ${esc(m.model || r.model)} · ${esc(m.effort || "high")}<small>${esc(modelMetricText(m))}</small></span></div>`).join("")}`;
}

function questionLabel(q) { return q.question_label || q.id; }
function questionPageLabel(q) {
  const material = cur?.materials?.find((m) => m.page === q.page);
  return material ? `${material.filename || "文件"} 第 ${material.source_page || 1} 页` : `第 ${q.page} 页`;
}
function applyManualOverride(qid, status) {
  const id = cur?.id, generation = viewGeneration;
  const valid = () => generation === viewGeneration && cur?.id === id;
  if (!id) return Promise.resolve();
  // Each response contains the whole result. Keep updates in click order so a
  // slower response cannot replace a later review with an older snapshot.
  const edit = async () => {
    if (!valid()) return;
    try {
      const r = await post(`/api/submissions/${id}/override`, {qid, status});
      if (!valid()) return;
      cur = r;
      const q = cur.questions.find((q) => q.id === qid);
      cancelManualPosition();
      if (q) page = q.page;
      kind = "annotated";
      $("#jobNotice").textContent = "";
      drawResult();
      if (q && (!q.bbox_ok || !q.bbox_px || !q.draw)) beginManualPosition(q.id);
      else drawPage();
    } catch (error) {
      if (valid()) $("#jobNotice").textContent = `人工改判未保存：${error.message}`;
    }
  };
  manualReviewQueue = manualReviewQueue.then(edit, edit);
  return manualReviewQueue;
}
function drawResult() {
  const s = cur.summary;
  $("#summary").hidden = false;
  $("#summary").innerHTML = `
    <div class="tile"><b>${pct(s.accuracy)}</b><span>正确率（已判定）</span></div>
    <div class="tile ok"><b>${s.correct}</b><span>正确</span></div>
    <div class="tile bad"><b>${s.wrong + s.partial}</b><span>错误 / 部分</span></div>
    <div class="tile warn"><b>${s.review}</b><span>待人工复核</span></div>`;
  const order = { review: 0, wrong: 1, partial: 2, correct: 3 };
  const qs = [...cur.questions].sort((a, b) => order[a.status] - order[b.status]);
  $("#qlist").innerHTML = qs.map((q) => {
    const ans = q.answer_state === "blank" ? "（未作答）" : q.answer_state === "unreadable" ? "（无法辨认）" : esc(q.student_answer);
    const notes = [];
    if (q.status === "review") notes.push(`<p class="q-note">${esc(q.reasons.join("；") || "未能得出结论")}${q.verdict ? ` · AI 倾向：${LABEL[q.verdict]}` : ""}</p>`);
    else if (q.reason && q.status !== "correct") notes.push(`<p class="q-note plain">${esc(q.reason)}${q.error_type ? ` · 错因：${esc(q.error_type)}` : ""}</p>`);
    if (q.position_source === "vision_fallback") notes.push(`<p class="q-note plain">位置由模型看图推断，可用「框选答案位置」修正。</p>`);
    if (q.position_note && !q.draw) notes.push(`<p class="q-note">${esc(q.position_note)}</p>`);
    const btn = (st, t) => `<button title="${LABEL[st]}" class="${q.status === st ? "on" : ""}" data-q="${esc(q.id)}" data-s="${st}">${t}</button>`;
    const metadata = [];
    if (cur.page_count > 1) metadata.push(`<span title="${esc(questionPageLabel(q))}">第 ${esc(q.page)} 页</span>`);
    if (q.knowledge) metadata.push(`<span>${esc(q.knowledge)}</span>`);
    if (q.ocr_confirmed) metadata.push(`<span>OCR 印证</span>`);
    return `<div class="q ${q.status}">
      <div class="q-top"><span class="q-id">${esc(questionLabel(q))}</span>
        <span class="q-ans">学生：<b>${ans}</b> <s>· 标准答案：${esc(q.standard_answer ?? "缺失")}</s></span>
        <span class="chip ${q.status}">${LABEL[q.status]}${q.overridden ? " · 人工" : ""}</span></div>
      <p class="q-stem">${esc(q.question)}</p>${notes.join("")}
      <div class="q-act"><div class="q-verdict"><span>人工改判</span>${btn("correct", "✓")}${btn("partial", "½")}${btn("wrong", "✗")}</div>${!q.bbox_ok || !q.bbox_px || ["vision", "vision_fallback"].includes(q.position_source) || (!q.draw && q.status !== "review") ? `<button class="q-position" data-position="${esc(q.id)}">框选答案位置</button>` : ""}</div>
      ${metadata.length ? `<div class="q-meta">${metadata.join('<span aria-hidden="true">·</span>')}</div>` : ""}
    </div>`;
  }).join("");
  $$("#qlist button[data-position]").forEach((b) => (b.onclick = () => beginManualPosition(b.dataset.position)));
  $$("#qlist button[data-q]").forEach((b) => (b.onclick = () => applyManualOverride(b.dataset.q, b.dataset.s)));
}
$("#back").onclick = () => {
  stopWatching(); clearGradingRoute(); activeJob = null; cur = null; $("#desk").hidden = true; $("#home").hidden = false;
  const b = backTo; backTo = null;
  if (b) b(); else show("work");
};
$("#regrade").onclick = async () => {
  const id = cur?.id, name = cur?.filename, generation = viewGeneration; if (!id) return;
  try { const r = await api(`/api/submissions/${id}/regrade`, { method: "POST" }); if (generation === viewGeneration) watch(r.id, name); refreshJobs(); }
  catch (e) { alert(e.message); }
};

/* ---------- 最近批改 ---------- */
async function loadHistory() {
  const list = await api("/api/submissions");
  $("#history").innerHTML = list.length ? list.map((r) => `
    <button class="item" data-id="${r.id}"><div class="grow"><b>${esc(r.filename)}</b><small>${r.student_name ? esc(r.student_name) + " · " : ""}${esc(r.paper_title)} · ${r.created_at}</small></div>
      <span class="chip correct">${r.summary.correct} 对</span><span class="chip wrong">${r.summary.wrong + r.summary.partial} 错</span>
      ${r.summary.review ? `<span class="chip review">${r.summary.review} 待复核</span>` : ""}</button>`).join("")
    : `<div class="empty">还没有批改记录。</div>`;
  $$("#history .item").forEach((b) => (b.onclick = () => openResult(b.dataset.id, false, null)));
}

/* ---------- 图表小件 ---------- */
const hbar = (name, ratio, val, tip) => `<div class="hbar" title="${esc(tip || "")}"><span class="nm">${esc(name)}</span>
  <span class="track"><span class="fill" style="width:${Math.max(0, Math.min(1, ratio || 0)) * 100}%;display:block"></span></span><span class="val">${val}</span></div>`;
const tiles = (arr) => `<div class="tiles">${arr.map(([v, l, c]) => `<div class="tile ${c || ""}"><b>${v}</b><span>${l}</span></div>`).join("")}</div>`;
function trendChart(points) {
  const pts = points.filter((p) => p.accuracy != null);
  if (pts.length < 2) return `<p class="sub">至少批改两次后显示趋势。</p>`;
  const W = 640, H = 200, L = 40, R = 46, T = 14, B = 30, n = pts.length;
  const x = (i) => L + (i * (W - L - R)) / (n - 1), y = (v) => T + (1 - v) * (H - T - B);
  const grid = [0, 0.5, 1].map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e8e2d4"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${v * 100}%</text>`).join("");
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.accuracy)}`).join("");
  const dots = pts.map((p, i) => `<circle class="pt" cx="${x(i)}" cy="${y(p.accuracy)}" r="5"/>
    <circle class="hit" cx="${x(i)}" cy="${y(p.accuracy)}" r="16" data-tip="${esc(p.date + " · " + p.title + " · 正确率 " + pct(p.accuracy))}"/>`).join("");
  const last = pts[n - 1];
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="历次正确率趋势">${grid}
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>${dots}
    <text x="${x(n - 1) + 9}" y="${y(last.accuracy) + 4}" style="fill:var(--ink);font-weight:600">${pct(last.accuracy)}</text>
    <text x="${L}" y="${H - 8}">${esc(pts[0].date)}</text><text x="${W - R}" y="${H - 8}" text-anchor="end">${esc(last.date)}</text></svg>`;
}
const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true; document.body.append(tip);
document.addEventListener("mousemove", (e) => {
  const t = e.target.closest && e.target.closest("[data-tip]");
  tip.hidden = !t; if (!t) return;
  tip.textContent = t.dataset.tip; tip.style.left = Math.min(e.clientX + 14, innerWidth - 270) + "px"; tip.style.top = e.clientY + 14 + "px";
});
const knowledgeBox = (k) => k.length ? k.map((x) => hbar(x.name, x.mastery, `${pct(x.mastery)} <small>/${x.total}题</small>`, `${x.name}：掌握率 ${pct(x.mastery)}，共 ${x.total} 题`)).join("") : `<p class="sub">暂无数据。</p>`;
const errBox = (e) => { const m = Math.max(1, ...e.map((x) => x.count)); return e.length ? e.map((x) => hbar(x.name, x.count / m, x.count + " 次")).join("") : `<p class="sub">暂无错题。</p>`; };
const ul = (a) => (a && a.length ? `<ul>${a.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");

/* ---------- 班级管理 ---------- */
let curClass = null;
async function loadClasses(sel) {
  classes = await api("/api/classes");
  $("#classList").innerHTML = classes.length ? classes.map((c) => `<button class="item ${c.id === sel ? "on" : ""}" data-id="${c.id}">
    <div class="grow"><b>${esc(c.name)}</b><small>${c.student_count} 名学生 · ${c.task_count} 个任务</small></div></button>`).join("")
    : `<div class="empty">还没有班级，先在上面添加一个。</div>`;
  $$("#classList .item").forEach((b) => (b.onclick = () => openClass(b.dataset.id)));
  if (!sel) $("#classDetail").hidden = true;
}
$("#classForm").onsubmit = async (e) => {
  e.preventDefault();
  const c = await post("/api/classes", { name: $("#cName").value }); $("#cName").value = ""; openClass(c.id);
};
async function openClass(id) {
  curClass = id;
  const c = await api(`/api/classes/${id}`);
  const d = $("#classDetail"); d.hidden = false;
  d.innerHTML = `<div class="page-head"><div class="grow"><h2>${esc(c.name)}</h2><p class="sub">${c.students.length} 名学生 · 点击姓名查看学生档案</p></div>
      <button class="ghost" id="clsReport">学情报告</button><button class="ghost danger" id="clsDel">删除班级</button></div>
    ${c.students.length ? `<table class="t"><thead><tr><th>学号</th><th>姓名</th><th class="num">批改次数</th><th>平均正确率</th><th class="num">最近一次</th><th></th></tr></thead><tbody>
      ${c.students.map((s) => `<tr><td>${esc(s.no)}</td><td><span class="link" data-stu="${s.id}">${esc(s.name)}</span></td><td class="num">${s.count}</td>
        <td><span class="mini"><i style="width:${(s.accuracy || 0) * 100}%"></i></span>${pct(s.accuracy)}</td>
        <td class="num">${pct(s.last)}${s.delta == null ? "" : s.delta >= 0 ? ` ↑${Math.round(s.delta * 100)}` : ` ↓${Math.round(-s.delta * 100)}`}</td>
        <td class="num"><span class="link danger" data-del="${s.id}">移除</span></td></tr>`).join("")}</tbody></table>` : `<p class="empty">花名册是空的。</p>`}
    <h3 style="font:500 16px var(--serif);margin:22px 0 6px">添加学生</h3>
    <textarea class="bulk" id="bulk" placeholder="一行一个：&#10;张小明&#10;2024001 李华"></textarea>
    <div class="row" style="margin-top:10px"><button class="primary" id="bulkBtn">添加到花名册</button><span class="sub" id="bulkMsg"></span></div>`;
  $("#bulkBtn").onclick = async () => {
    const r = await post(`/api/classes/${id}/students`, { text: $("#bulk").value });
    await openClass(id); $("#bulkMsg").textContent = `已添加 ${r.added} 人`;
  };
  $("#clsDel").onclick = async () => { if (confirm(`删除「${c.name}」及其花名册和任务？已批改的试卷不会删除。`)) { await api(`/api/classes/${id}`, { method: "DELETE" }); loadClasses(); } };
  $("#clsReport").onclick = async () => { await show("reports"); $("#rClass").value = id; $("#rClass").onchange(); };
  $$("[data-stu]", d).forEach((x) => (x.onclick = () => openStudent(x.dataset.stu)));
  $$("[data-del]", d).forEach((x) => (x.onclick = async () => { if (confirm("从花名册移除这名学生？")) { await api(`/api/students/${x.dataset.del}`, { method: "DELETE" }); openClass(id); } }));
  loadClasses(id);
}

/* ---------- 学生档案 / 成长档案 ---------- */
async function openStudent(id) {
  show_("classes");
  const r = await api(`/api/students/${id}`);
  $("#classHome").hidden = true;
  const p = $("#studentPage"); p.hidden = false;
  const s = r.student;
  p.innerHTML = `<div class="page-head"><button class="ghost no-print" id="stuBack">← 返回班级</button>
      <div class="grow"><h1>${esc(s.name)}</h1><p class="sub">${esc(r.class_name)}${s.no ? " · 学号 " + esc(s.no) : ""} · 建档于 ${s.created_at.slice(0, 10)}</p></div>
      <button class="ghost no-print" onclick="print()">打印 / 存为 PDF</button></div>
    ${tiles([[r.count, "批改次数"], [pct(r.accuracy), "平均正确率"], [pct(r.last), "最近一次"],
      [r.delta == null ? "—" : (r.delta >= 0 ? "+" : "−") + Math.abs(Math.round(r.delta * 100)) + "%", "较上一次", r.delta == null ? "" : r.delta >= 0 ? "ok" : "bad"], [r.review, "待复核题数", r.review ? "warn" : ""]])}
    <div class="grid2">
      <div class="box full"><h3>成长曲线</h3><p class="sub">历次批改的正确率（待复核的题不计入）</p>${trendChart(r.trend)}
        ${r.trend.length ? `<table class="t" style="margin-top:10px"><thead><tr><th>日期</th><th>作业 / 试卷</th><th class="num">正确率</th><th class="num">对 / 错 / 待复核</th><th></th></tr></thead><tbody>
          ${[...r.trend].reverse().map((t) => `<tr><td>${t.date}</td><td>${esc(t.title)}</td><td class="num">${pct(t.accuracy)}</td><td class="num">${t.correct} / ${t.wrong + t.partial} / ${t.review}</td>
            <td class="num"><span class="link no-print" data-sub="${t.sid}">查看试卷</span></td></tr>`).join("")}</tbody></table>` : `<p class="sub">还没有归到这名学生名下的批改记录。</p>`}</div>
      <div class="box"><h3>知识点掌握</h3><p class="sub">从薄弱到熟练排序</p>${knowledgeBox(r.knowledge)}</div>
      <div class="box"><h3>错因分布</h3><p class="sub">由批改 Agent 归类</p>${errBox(r.error_types)}</div>
      <div class="box full ai" id="stuAi"></div>
      <div class="box full"><h3>错题本</h3><p class="sub">共 ${r.wrong_book.length} 道，最近的在前</p>
        ${r.wrong_book.map((w) => `<div class="wb"><div><b>${esc(w.question)}</b></div>
          <div>学生答案：${w.answer_state === "blank" ? "（未作答）" : esc(w.student_answer)}　标准答案：${esc(w.standard_answer ?? "—")}</div>
          <div class="meta">${w.date} · ${esc(w.paper_title)} 第 ${esc(w.qid)} 题 · ${esc(w.knowledge)} · ${esc(w.error_type)}${w.reason ? " · " + esc(w.reason) : ""}</div></div>`).join("") || `<p class="sub">暂无错题。</p>`}</div>
    </div>`;
  $("#stuBack").onclick = () => { p.hidden = true; $("#classHome").hidden = false; openClass(s.class_id); };
  $$("[data-sub]", p).forEach((x) => (x.onclick = () => openResult(x.dataset.sub, false, () => openStudent(id))));
  drawStudentAi(id, r.ai, r.judged);
}
function drawStudentAi(id, ai, judged) {
  const box = $("#stuAi");
  box.innerHTML = ai ? `<h3>成长评语与提升建议</h3><p class="sub">学情分析 Agent 生成于 ${ai.generated_at} · 仅供参考</p>
      <p>${esc(ai.comment)}</p><h4>优势</h4>${ul(ai.strengths)}<h4>待加强</h4>${ul(ai.weaknesses)}<h4>建议</h4>${ul(ai.suggestions)}
      ${(ai.practice || []).length ? `<h4>错一练三 · 同类练习</h4><table class="t"><tbody>${ai.practice.map((q, i) => `<tr><td>${i + 1}</td><td>${esc(q.question)}<div class="sub">${esc(q.knowledge)}</div></td><td>答案：${esc(q.answer)}</td></tr>`).join("")}</tbody></table>` : ""}`
    : `<h3>成长评语与提升建议</h3><p class="sub">让学情分析 Agent 根据上面的数据写评语，并生成针对薄弱点的同类练习题。批改数据有变化后可以重新生成。</p>
       <button class="primary no-print" id="stuAiBtn" ${judged ? "" : "disabled"}>生成评语</button> <span class="sub" id="stuAiMsg">${judged ? "" : "还没有可分析的数据"}</span>`;
  const b = $("#stuAiBtn");
  if (b) b.onclick = async () => {
    b.disabled = true; $("#stuAiMsg").textContent = "正在生成…";
    try { drawStudentAi(id, await post("/api/reports/ai", { scope: "student", student_id: id }), judged); }
    catch (e) { b.disabled = false; $("#stuAiMsg").textContent = e.message; }
  };
}

/* ---------- 任务中心 ---------- */
async function loadTasks() {
  $("#tClass").innerHTML = opts(classes, (c) => c.name, classes.length ? null : "请先到「班级管理」建班");
  allTasks = await api("/api/tasks");
  $("#taskList").innerHTML = allTasks.length ? allTasks.map((t) => `<button class="item" data-id="${t.id}">
      <div class="grow"><b>${esc(t.title)}</b><small>${esc(t.class_name)} · ${t.paper_title ? esc(t.paper_title) : "尚未上传试卷"} · ${t.created_at.slice(0, 16)}</small></div>
      <span class="chip ${t.submitted >= t.student_count && t.student_count ? "correct" : ""}">已交 ${t.submitted} / ${t.student_count}</span>
      ${t.unassigned ? `<span class="chip review">${t.unassigned} 份未归档</span>` : ""}${t.review ? `<span class="chip review">${t.review} 题待复核</span>` : ""}</button>`).join("")
    : `<div class="empty">还没有任务。</div>`;
  $$("#taskList .item").forEach((b) => (b.onclick = () => openTask(b.dataset.id)));
}
$("#taskForm").onsubmit = async (e) => {
  e.preventDefault();
  if (!$("#tClass").value) return;
  const generation = viewGeneration;
  const t = await post("/api/tasks", { class_id: $("#tClass").value, title: $("#tTitle").value });
  if (generation !== viewGeneration) return;
  $("#tTitle").value = ""; openTask(t.id);
};
async function openTask(id) {
  clearInterval(taskTimer);
  show_("tasks");
  const taskGeneration = viewGeneration, taskValid = () => taskGeneration === viewGeneration;
  const [t, jobs] = await Promise.all([api(`/api/tasks/${id}`), api(`/api/jobs?task_id=${id}`)]);
  if (taskGeneration !== viewGeneration) return;
  $("#taskHome").hidden = true;
  const d = $("#taskDetail"); d.hidden = false;
  const done = t.rows.filter((r) => r.sid).length, running = jobs.filter((j) => !j.done), failed = jobs.filter((j) => j.error);
  const stuOpts = opts(t.rows, stuLabel, "选择学生…");
  d.innerHTML = `<div class="page-head"><button class="ghost" id="tBack">← 返回</button>
      <div class="grow"><h1>${esc(t.title)}</h1><p class="sub">${esc(t.class_name)} · ${t.paper_title ? "试卷：" + esc(t.paper_title) : "上传第一份试卷后自动绑定试卷"}</p></div>
      <label class="ghost" style="cursor:pointer">添加本次批改材料（可多选）<input type="file" id="tFile" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden></label>
      <button class="ghost" id="tReport">学情报告</button><button class="ghost danger" id="tDel">删除任务</button></div>
    ${tiles([[`${done} / ${t.rows.length}`, "已交"], [t.rows.length - done, "未交", t.rows.length - done ? "warn" : ""], [running.length, "批改中"],
      [t.rows.reduce((a, r) => a + (r.summary ? r.summary.review : 0), 0), "待复核题数"]])}
    <p class="err" id="tErr"></p>
    ${running.length || failed.length ? `<div class="box" style="max-width:980px;margin-bottom:18px"><h3>批改队列</h3>
      ${running.map((j) => `<button class="wb job-link" data-job="${esc(j.id)}">${esc(j.filename)} <span class="meta">· ${esc(j.message)}</span></button>`).join("")}
      ${failed.map((j) => `<button class="wb job-link" data-job="${esc(j.id)}">${esc(j.filename)} <span class="meta danger">· 失败：${esc(j.error)}</span></button>`).join("")}</div>` : ""}
    ${t.unassigned.length ? `<div class="box" style="max-width:980px;margin-bottom:18px"><h3>未归档的试卷</h3><p class="sub">卷面姓名没能匹配到花名册，请手动指定学生。</p>
      <table class="t"><tbody>${t.unassigned.map((u) => `<tr><td><span class="link" data-sub="${u.sid}">${esc(u.filename)}</span></td><td>卷面姓名：${esc(u.detected_name || "未识别")}</td>
        <td class="num">${pct(u.summary.accuracy)}</td><td class="num"><select data-assign="${u.sid}">${stuOpts}</select></td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="box" style="max-width:980px"><h3>交卷情况</h3>
      ${t.rows.length ? `<table class="t"><thead><tr><th>学号</th><th>姓名</th><th>状态</th><th>正确率</th><th class="num">对 / 错 / 待复核</th><th></th></tr></thead><tbody>
      ${t.rows.map((r) => `<tr><td>${esc(r.no)}</td><td>${esc(r.name)}</td>
        <td>${r.sid ? (r.summary.review ? `<span class="chip review">待复核 ${r.summary.review}</span>` : `<span class="chip correct">已批改</span>`) : `<span class="chip">未交</span>`}</td>
        <td>${r.sid ? `<span class="mini"><i style="width:${(r.summary.accuracy || 0) * 100}%"></i></span>${pct(r.summary.accuracy)}` : "—"}</td>
        <td class="num">${r.sid ? `${r.summary.correct} / ${r.summary.wrong + r.summary.partial} / ${r.summary.review}` : ""}</td>
        <td class="num">${r.sid ? `<span class="link" data-sub="${r.sid}">查看</span> · <a class="link" href="/api/submissions/${r.sid}/pdf">PDF</a>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<p class="sub">这个班级还没有学生，请先到「班级管理」添加花名册。</p>`}</div>`;
  bindJobs(d, () => openTask(id));
  $("#tBack").onclick = () => show("tasks");
  $("#tDel").onclick = async () => { if (confirm("删除这个任务？已批改的试卷不会删除。")) { await api(`/api/tasks/${id}`, { method: "DELETE" }); if (taskValid()) show("tasks"); } };
  $("#tReport").onclick = async () => { const showing = show("reports"), reportGeneration = viewGeneration; await showing; if (reportGeneration !== viewGeneration) return; $("#rClass").value = t.class_id; fillReportTasks(); $("#rTask").value = id; loadReport(); };
  $$("[data-sub]", d).forEach((x) => (x.onclick = () => openResult(x.dataset.sub, false, () => openTask(id))));
  $$("[data-assign]", d).forEach((x) => (x.onchange = async () => { if (x.value) { await post(`/api/submissions/${x.dataset.assign}/assign`, { student_id: x.value }); if (taskValid()) openTask(id); } }));
  $("#tFile").onchange = (e) => {
    const files = [...e.target.files], ocrMode = defaultOcrMode(); e.target.value = "";
    if (!files.length || !taskValid()) return;
    const current = snapshotUploadDraft();
    const saved = current.task_id === id ? [activeDraftId,current]
      : [...uploadDrafts].find(([key,draft]) => key !== activeDraftId && draft.task_id === id);
    const target = saved?.[1] || { files: [], answer_key: "", class_id: t.class_id, task_id: id, student_id: "", ocr_mode: ocrMode, error: "" };
    const error = materialError([...target.files, ...files.map((file) => ({file}))]);
    if (error) { $("#tErr").textContent = error; return; }
    // Validate the destination draft before changing navigation or the current archive fields.
    saveUploadDraft();
    const draftId = saved?.[0] || ++nextDraftId;
    const additions = files.map((file) => ({id:++nextMaterialId,file,role:"auto"}));
    uploadDrafts.set(draftId, {...target,files:[...target.files,...additions],mode:"grade",paper_id:t.paper_id || target.paper_id || "",ocr_mode:"vision",error:""});
    if (!classes.some((c) => c.id === t.class_id)) classes.push({id:t.class_id, name:t.class_name});
    if (!allTasks.some((task) => task.id === id)) allTasks.push({...t,id});
    show_("work"); cur = null; activeJob = null;
    $("#home").hidden = false; $("#desk").hidden = true; restoreUploadDraft(draftId);
  };
  if (running.length) taskTimer = setInterval(() => { if (taskValid() && $("#view-tasks").classList.contains("on") && !d.hidden && !d.contains(document.activeElement)) openTask(id); }, 3000);
}

/* ---------- 学情报告 ---------- */
function initReports() {
  const keep = $("#rClass").value;
  $("#rClass").innerHTML = opts(classes, (c) => c.name, classes.length ? null : "请先到「班级管理」建班");
  if (classes.some((c) => c.id === keep)) $("#rClass").value = keep;
  fillReportTasks(); loadReport();
}
function fillReportTasks() {
  $("#rTask").innerHTML = opts(allTasks.filter((t) => t.class_id === $("#rClass").value), (t) => t.title, "全部任务（累计）");
}
$("#rClass").onchange = () => { fillReportTasks(); loadReport(); };
$("#rTask").onchange = loadReport;
$("#rPrint").onclick = () => print();
let reportSeq = 0;
async function loadReport() {
  const seq = ++reportSeq;
  const cid = $("#rClass").value, tid = $("#rTask").value, body = $("#reportBody");
  $("#rCsv").hidden = $("#rPrint").hidden = !cid;
  if (!cid) { body.innerHTML = `<div class="empty">还没有班级。</div>`; return; }
  const q = `class_id=${cid}${tid ? "&task_id=" + tid : ""}`;
  $("#rCsv").href = `/api/reports/class.csv?${q}`;
  const r = await api(`/api/reports/class?${q}`);
  if (seq !== reportSeq) return;  // 期间选择器又变了，丢弃过期结果
  if (!r.submission_count) { body.innerHTML = `<div class="empty">这个范围内还没有批改记录。到「任务中心」上传试卷后，这里会自动生成报告。</div>`; return; }
  const dmax = Math.max(1, ...r.distribution.map((d) => d.count));
  body.innerHTML = `
    ${tiles([[pct(r.accuracy), "平均正确率"], [`${r.submitted} / ${r.student_count}`, tid ? "已交人数" : "有记录的学生"], [r.attention.length, "需要关注", r.attention.length ? "bad" : ""],
      [r.review, "待复核题数", r.review ? "warn" : ""], [r.unassigned, "未归档试卷", r.unassigned ? "warn" : ""]])}
    ${r.review ? `<p class="sub" style="margin:-8px 0 18px">有 ${r.review} 道题还在等人工复核，它们暂不计入正确率。</p>` : ""}
    <div class="grid2">
      <div class="box full ai" id="clsAi"></div>
      <div class="box full"><h3>讲评清单</h3><p class="sub">按正确率从低到高排列，先讲错得最多的题</p>
        <table class="t"><thead><tr><th>题号</th><th>题目</th><th>知识点</th><th>正确率</th><th>典型错误答案</th><th>答错的学生</th></tr></thead><tbody>
        ${r.questions.slice(0, 30).map((x) => `<tr><td>${esc(x.qid)}</td><td>${esc(x.question)}<div class="sub">答案：${esc(x.standard_answer ?? "—")}${tid ? "" : " · " + esc(x.paper_title)}</div></td><td>${esc(x.knowledge)}</td>
          <td style="white-space:nowrap"><span class="mini"><i style="width:${x.rate * 100}%"></i></span>${pct(x.rate)} <small class="sub">/${x.total}次</small></td>
          <td>${x.wrong_answers.map((w) => `${esc(w.answer)} ×${w.count}`).join("<br>") || "—"}</td><td>${esc(x.wrong_students.join("、")) || "—"}</td></tr>`).join("")}</tbody></table></div>
      <div class="box"><h3>知识点掌握</h3><p class="sub">全班合计，从薄弱到熟练</p>${knowledgeBox(r.knowledge)}</div>
      <div class="box"><h3>正确率分布</h3><p class="sub">按学生平均正确率分段</p>${r.distribution.map((d) => hbar(d.name, d.count / dmax, d.count + " 人")).join("")}
        <h3 style="margin-top:18px">错因分布</h3>${errBox(r.error_types)}</div>
      <div class="box"><h3>分层名单</h3><p class="sub">用于分层作业和分组辅导（≥85% / 60–85% / &lt;60%）</p>
        <div class="tiers">${r.tiers.map((t) => `<div class="tier"><b>${t.name} · ${t.students.length} 人</b>${esc(t.students.join("、")) || "—"}</div>`).join("")}</div></div>
      <div class="box"><h3>需要关注</h3><p class="sub">未交、正确率低于 60% 或明显下滑</p>
        ${r.attention.map((a) => `<div class="wb"><span class="link" data-stu="${a.id}">${esc(a.name)}</span> <span class="meta">· ${esc(a.why)}</span></div>`).join("") || `<p class="sub">暂时没有。</p>`}</div>
      <div class="box full"><h3>学生明细</h3><table class="t"><thead><tr><th>学号</th><th>姓名</th><th class="num">批改次数</th><th>平均正确率</th><th class="num">最近一次</th><th class="num">待复核</th></tr></thead><tbody>
        ${r.students.map((s) => `<tr><td>${esc(s.no)}</td><td><span class="link" data-stu="${s.id}">${esc(s.name)}</span></td><td class="num">${s.count}</td>
          <td><span class="mini"><i style="width:${(s.accuracy || 0) * 100}%"></i></span>${pct(s.accuracy)}</td><td class="num">${pct(s.last)}</td><td class="num">${s.review}</td></tr>`).join("")}</tbody></table></div>
    </div>`;
  $$("[data-stu]", body).forEach((x) => (x.onclick = () => openStudent(x.dataset.stu)));
  drawClassAi(cid, tid, r.ai, r.judged);
}
function drawClassAi(cid, tid, ai, judged) {
  const box = $("#clsAi");
  box.innerHTML = ai ? `<h3>讲评与教学建议</h3><p class="sub">学情分析 Agent 生成于 ${ai.generated_at} · 仅供参考</p><p>${esc(ai.summary)}</p>
      <h4>掌握较好</h4>${ul(ai.strengths)}<h4>共性薄弱点</h4>${ul(ai.weaknesses)}
      ${(ai.lecture_plan || []).length ? `<h4>讲评安排</h4><ul>${ai.lecture_plan.map((l) => `<li><b>${esc(l.focus)}</b>：${esc(l.how)}</li>`).join("")}</ul>` : ""}
      ${ai.tier_suggestions ? `<h4>分层建议</h4><ul>${Object.entries(ai.tier_suggestions).map(([k, v]) => `<li><b>${esc(k)}</b>：${esc(v)}</li>`).join("")}</ul>` : ""}
      <h4>后续作业</h4>${ul(ai.follow_up)}`
    : `<h3>讲评与教学建议</h3><p class="sub">让学情分析 Agent 根据下面的统计写出讲评重点、分层建议和后续作业安排。批改数据有变化后可以重新生成。</p>
       <button class="primary no-print" id="clsAiBtn" ${judged ? "" : "disabled"}>生成建议</button> <span class="sub" id="clsAiMsg"></span>`;
  const b = $("#clsAiBtn");
  if (b) b.onclick = async () => {
    b.disabled = true; $("#clsAiMsg").textContent = "正在生成…";
    try { drawClassAi(cid, tid, await post("/api/reports/ai", { scope: "class", class_id: cid, task_id: tid || null }), judged); }
    catch (e) { b.disabled = false; $("#clsAiMsg").textContent = e.message; }
  };
}

/* ---------- 试卷库 ---------- */
let openedPaperId = "";
async function loadPapers(sel) {
  const generation = viewGeneration;
  const list = await api("/api/papers");
  if (generation !== viewGeneration) return;
  $("#paperList").innerHTML = list.length ? list.map((p) => `
    <button class="item ${p.id === sel ? "on" : ""}" data-id="${p.id}"><div class="grow"><b>${esc(p.title)}</b>
      <small>${p.question_count} 道题 · 已使用 ${p.used_count} 次 · ${p.created_at}</small></div>
      <span class="chip ${p.review_summary?.review ? "review" : ""}">${p.review_summary?.review ? `待确认 ${p.review_summary.review} 题` : p.workflow_version === 2 ? "已审核" : "历史试卷"}</span></button>`).join("")
    : `<div class="empty">试卷库是空的。点击「上传试卷建库」，或先批改一份学生作答。</div>`;
  $$("#paperList .item").forEach((b) => (b.onclick = () => openPaper(b.dataset.id)));
  if (!list.length && !sel) $("#paperDetail").hidden = true;
}
async function openPaper(id) {
  openedPaperId = id;
  const generation = viewGeneration, valid = () => generation === viewGeneration && openedPaperId === id;
  const p = await api(`/api/papers/${id}`);
  if (!valid()) return;
  const amap = Object.fromEntries(p.answers.map((a) => [a.id, a]));
  const d = $("#paperDetail"); d.hidden = false;
  d.innerHTML = `<h2>${esc(p.title)}</h2>
    <p class="sub">${esc(p.id)} · 答案版本 ${p.rev || 0} · 修改或确认后，打开批改记录点「重新批改」即可生效</p>
    ${(p.reference_pages || []).length ? `<details class="paper-sources"><summary>查看试卷原图</summary>${p.reference_pages.map((ref) => `<a class="link" href="/api/papers/${encodeURIComponent(id)}/pages/${ref.page}" target="_blank" rel="noopener">原图第 ${ref.page} 页</a>`).join(" · ")}</details>` : ""}
    <table><thead><tr><th>题号</th><th>题目</th><th style="width:26%">标准答案</th><th style="width:22%">知识点</th></tr></thead><tbody>
    ${p.questions.map((q) => { const a = amap[q.id] || {}; return `<tr><td>${esc(questionLabel(q))}</td><td>${esc(q.question)}</td>
      <td><input aria-label="第 ${esc(questionLabel(q))} 题标准答案" data-id="${esc(q.id)}" data-f="answer" value="${esc(a.answer ?? "")}">
        <small class="answer-review ${a.review_status === "review" ? "pending" : ""}">${({approved:"审核通过",ai_solved:"AI 独立解题",review:"待人工确认",manual_confirmed:"已人工确认"})[a.review_status] || "历史答案，使用前需审核"}</small>
        ${a.teacher_answer ? `<small class="answer-evidence">老师答案：${esc(a.teacher_answer)}</small>` : ""}
        ${a.review_answer || a.independent_answer ? `<small class="answer-evidence">独立验算：${esc(a.review_answer || a.independent_answer)}</small>` : ""}
        ${a.review_reason ? `<small class="answer-evidence">${esc(a.review_reason)}</small>` : ""}
        ${a.review_status !== "manual_confirmed" ? `<button class="link" type="button" data-confirm="${esc(q.id)}">确认当前答案</button>` : ""}</td>
      <td><input data-id="${esc(q.id)}" data-f="knowledge" value="${esc(a.knowledge ?? "")}"></td></tr>`; }).join("")}
    </tbody></table>
    <div class="row" style="margin-top:16px"><button class="primary" id="saveAns">保存</button><span id="ansMsg" style="color:var(--ok);font-size:13.5px"></span></div>`;
  function answerInputs() {
    const by = {};
    $$("input[data-id]", d).forEach((i) => ((by[i.dataset.id] ||= { id: i.dataset.id })[i.dataset.f] = i.value.trim()));
    return by;
  }
  async function saveAnswers(answers) {
    try {
      await post(`/api/papers/${id}/answers`, {answers}, "PUT");
      if (!valid()) return;
      await openPaper(id); if (valid()) $("#ansMsg").textContent = "已保存；重新批改后使用新答案";
    } catch(e) { if (valid()) $("#ansMsg").textContent = "保存失败：" + e.message; }
  }
  $("#saveAns").onclick = () => saveAnswers(Object.values(answerInputs()));
  $$('[data-confirm]', d).forEach((button) => button.onclick = () => {
    const qid = button.dataset.confirm;
    return saveAnswers([{...(answerInputs()[qid] || {id:qid}),confirmed:true}]);
  });
  if (p.questions.length) {
    const use = document.createElement("button"); use.className = "ghost"; use.textContent = "使用这套试卷批改";
    use.onclick = () => {
      saveUploadDraft(); const draft = ++nextDraftId;
      uploadDrafts.set(draft,{files:[],answer_key:"",class_id:"",task_id:"",student_id:"",mode:"grade",paper_id:id,error:""});
      switchUploadDraft(draft); loadUploadPapers();
    };
    d.append(use);
  };
  loadPapers(id);
}

/* ---------- 启动 ---------- */
(async () => {
  const s = await api("/api/settings"); settingsCache = s; badge(s); syncOcrSelectors();
  const route = gradingRoute(location.hash);
  if (route) await openJob(route); else await show(s.has_key ? "work" : "settings");
  refreshJobs(); setInterval(() => { refreshJobs(); if (activeJob && !cur && $("#view-work").classList.contains("on") && !$("#desk").hidden) renderSteps(stageState); }, 3000);
})();

window.addEventListener("hashchange", () => { const id = gradingRoute(location.hash); if (id) openJob(id); else if (activeJob) show("work"); });
