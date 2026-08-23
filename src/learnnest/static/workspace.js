const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const error = new Error(payload.detail || "暂时无法完成操作。");
    error.status = response.status;
    throw error;
  }
  return payload;
};

const notice = document.querySelector("#notice");
const sourceJobs = document.querySelector("#source-jobs");
const uploadForm = document.querySelector("#upload-form");
const urlForm = document.querySelector("#url-form");
const lists = {
  inbox: document.querySelector("#inbox-list"),
  processing: document.querySelector("#processing-list"),
  library: document.querySelector("#library-list"),
};
const favoriteList = document.querySelector("#douyin-favorites-list");
const favoriteStatus = document.querySelector("#favorites-status");
const syncFavoritesButton = document.querySelector("#sync-favorites");
const connectDouyinButton = document.querySelector("#connect-douyin");
const loginPanel = document.querySelector("#douyin-login-panel");
const loginQr = document.querySelector("#douyin-qr");
const loginState = document.querySelector("#douyin-connection-state");
const loginMessage = document.querySelector("#douyin-login-message");
const loginCountdown = document.querySelector("#douyin-countdown");
const refreshDouyinButton = document.querySelector("#refresh-douyin");
const cancelDouyinButton = document.querySelector("#cancel-douyin");
const providerForm = document.querySelector("#provider-connection-form");
const providerKeyField = document.querySelector("#provider-key-field");
const providerList = document.querySelector("#provider-connection-list");
const providerState = document.querySelector("#provider-settings-state");
const providerFeedback = document.querySelector("#provider-feedback");
const setupReadiness = document.querySelector("#setup-readiness");
const windowsVoiceField = document.querySelector("#windows-voice-field");
const windowsVoice = document.querySelector("#windows-voice");
const automationForm = document.querySelector("#automation-form");
const automationState = document.querySelector("#automation-state");
const automationSummary = document.querySelector("#automation-summary");
const automationAccess = document.querySelector("#automation-access");
const automationAccessTitle = document.querySelector("#automation-access-title");
const automationAccessCopy = document.querySelector("#automation-access-copy");
const authorizeAutomationButton = document.querySelector("#authorize-automation");
const disableAutomationButton = document.querySelector("#disable-automation");
const automationAuthorizationDialog = document.querySelector("#automation-authorization-dialog");
const confirmAutomationAuthorization = document.querySelector("#confirm-automation-authorization");
const authorizationOutput = document.querySelector("#authorization-output");
const authorizationInterval = document.querySelector("#authorization-interval");
const addSelectedFavoritesButton = document.querySelector("#add-selected-favorites");
const favoriteSelection = document.querySelector("#favorite-selection");
const taskList = document.querySelector("#task-list");
const taskDetail = document.querySelector("#task-detail");
const taskWorkbench = document.querySelector("#task-workbench");
const taskDetailView = document.querySelector("#task-detail-view");
const taskConsole = document.querySelector("#task-console");
const taskFocus = document.querySelector("#task-focus");
const workbenchNote = document.querySelector("#workbench-note");
const taskSummary = document.querySelector("#task-summary");
const taskCount = document.querySelector("#task-count");
const taskListTitle = document.querySelector("#task-list-title");
const defaultOutputLabel = document.querySelector("#default-output-label");
const singleVideoOutput = document.querySelector("#single-video-output");
const singleVideoReadiness = document.querySelector("#single-video-readiness");
const singleVideoDialog = document.querySelector("#single-video-dialog");
const deleteTaskDialog = document.querySelector("#delete-task-dialog");
const deleteTaskForm = document.querySelector("#delete-task-form");
const deleteTaskTitle = document.querySelector("#delete-task-title");
const selectedVideoName = document.querySelector("#selected-video-name");
const connectionDialog = document.querySelector("#connection-dialog");
const providerAdapterList = document.querySelector("#provider-adapter-list");
const providerLimitsForm = document.querySelector("#provider-limits-form");
const providerLimitsFeedback = document.querySelector("#provider-limits-feedback");
const storageForm = document.querySelector("#storage-form");
const outputRoot = document.querySelector("#output-root");
const currentOutputRoot = document.querySelector("#current-output-root");
const storageFeedback = document.querySelector("#storage-feedback");
const runtimeState = document.querySelector(".runtime-state");
const runtimeTitle = document.querySelector("#runtime-title");
const runtimeCopy = document.querySelector("#runtime-copy");
const settingsReadiness = document.querySelector(".settings-readiness");
const settingsReadinessTitle = document.querySelector("#settings-readiness-title");
const settingsReadinessCopy = document.querySelector("#settings-readiness-copy");
let windowsVoicesLoaded = false;
let pendingDeleteItemRef = null;
const stateLabel = {
  materials_ready: "材料已准备",
  waiting_setup: "等待设置",
  waiting_authorization: "等待授权",
  queued: "等待整理",
  organizing: "正在整理",
  partial_ready: "笔记已就绪，音频待完成",
  needs_action: "需要你处理",
  ready: "可阅读",
};
const learningActionKinds = new Set(["open_note", "open_settings", "open_automation", "open_single_video", "continue", "start_automation", "retry_automation"]);
const loginLabel = {
  disconnected: "未连接",
  starting: "准备中",
  browser_ready: "短信 + 扫码",
  qr_ready: "请扫码",
  scanned: "已扫码",
  confirmed: "待确认",
  verification_required: "需验证",
  validating: "正在校验",
  connected: "已连接",
  expired: "需重连",
  failed: "需重连",
  cancelled: "需重连",
};

let revision = null;
let timer = null;
let douyinSessionId = null;
let douyinLoginStatus = null;
let loginPollTimer = null;
let loginCountdownTimer = null;
let loginRefreshInFlight = false;
let syncFavoritesAfterLogin = false;
let favoriteStatusProtected = false;
let selectedFavoriteIdsState = new Set();
let suggestedProviderConnectionName = "";
const dirtySettingsForms = new Set();
let currentSnapshot = { inbox: [], processing: [], library: [] };
let selectedItemRef = null;
let activeTaskFilter = "all";
let noticeTimer = null;
let lastAutomationStatus = { configured: false, enabled: false };
const providerConnectionNameDefaults = {
  "windows-tts": "windows-tts",
  "local-asr": "local-asr",
  "local-ocr": "local-ocr",
};

function say(message) {
  window.clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.hidden = false;
  noticeTimer = window.setTimeout(() => { notice.hidden = true; }, 4200);
}
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

function renderProviderSettings(settings) {
  const configured = settings.connections.length;
  providerState.textContent = configured ? `已配置 ${configured} 个` : "未配置";
  providerState.className = `status-pill ${configured ? "connected" : ""}`;
  providerList.innerHTML = settings.connections.length
    ? settings.connections.map((item) => {
      return `<div class="provider-row"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.provider)} · ${escapeHtml(item.state)}</span><div class="provider-actions"><button type="button" data-check-connection="${escapeHtml(item.name)}">检查连接</button><button type="button" data-delete-connection="${escapeHtml(item.name)}">删除</button></div></div>`;
    }).join("")
    : '<p class="empty">还没有学习连接。点击“添加连接”选择一个当前支持的服务。</p>';
  providerList.querySelectorAll("button[data-check-connection]").forEach((button) => button.addEventListener("click", async () => {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      const result = await api(`/api/providers/connections/${encodeURIComponent(button.dataset.checkConnection)}/check`, { method: "POST" });
      providerFeedback.textContent = result.message;
      say(result.message);
    } catch (error) { providerFeedback.textContent = error.message; say(error.message); } finally { button.disabled = false; button.textContent = label; }
  }));
  providerList.querySelectorAll("button[data-delete-connection]").forEach((button) => button.addEventListener("click", () => deleteProviderConnection(button)));
  renderProviderAdapters(settings.adapters || []);
  renderProviderLimits(settings.limits);
  renderSetupReadiness(settings.readiness);
}

function renderProviderAdapters(adapters) {
  providerAdapterList.innerHTML = adapters.length
    ? adapters.map((adapter) => `<article class="adapter-card"><span>${escapeHtml(adapter.capability)}${adapter.local ? " · 本地" : ""}</span><strong>${escapeHtml(adapter.name)}</strong><button type="button" data-add-adapter="${escapeHtml(adapter.preset)}">添加连接</button></article>`).join("")
    : '<p class="empty">当前没有可添加的模型或语音服务。</p>';
  providerAdapterList.querySelectorAll("button[data-add-adapter]").forEach((button) => button.addEventListener("click", () => openConnectionDialog(button.dataset.addAdapter)));
}

function renderProviderLimits(limits) {
  if (!limits || settingsFormNeedsProtection(providerLimitsForm)) return;
  providerLimitsForm.elements.retries_per_role.value = limits.retries_per_role;
  providerLimitsForm.elements.global_calls_per_day.value = limits.global_calls_per_day;
  const groups = limits.budget_group_calls_per_day || {};
  for (const name of ["note", "podcast", "tts", "asr", "ocr"]) {
    providerLimitsForm.elements[`limit_${name}`].value = groups[name] ?? 0;
  }
}

function suggestProviderConnectionName() {
  const name = providerForm.elements.name;
  if (name.value.trim() && name.value !== suggestedProviderConnectionName) return;
  suggestedProviderConnectionName = providerConnectionNameDefaults[providerForm.elements.preset.value] || "";
  name.value = suggestedProviderConnectionName;
}

function startProviderSave(event) {
  const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "保存中…";
  providerState.textContent = "保存中";
  return () => {
    button.disabled = false;
    button.textContent = label;
  };
}

function showProviderUpdateFailure(error) {
  providerState.textContent = "操作失败";
  providerState.className = "status-pill";
  providerFeedback.textContent = error.message;
  say(error.message);
}

function renderSetupReadiness(readiness) {
  if (!readiness) return;
  const ready = readiness.state === "可以自动整理";
  settingsReadiness.classList.toggle("is-ready", ready);
  settingsReadinessTitle.textContent = ready ? "当前可以完整产出" : readiness.state;
  settingsReadinessCopy.textContent = readiness.message;
  singleVideoReadiness.textContent = readiness.message;
  const roleRows = (roles) => roles.map((role) => {
    const options = role.options.map((item) => `<option value="${escapeHtml(item.name)}"${item.name === role.connection ? " selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.provider)}</option>`).join("");
    const control = role.connection
      ? `<button type="button" data-unbind-setup-role="${escapeHtml(role.name)}">解绑</button>`
      : role.options.length
        ? `<select aria-label="为${escapeHtml(role.name)}选择连接" data-setup-role-select="${escapeHtml(role.name)}"><option value="">选择连接</option>${options}</select><button type="button" data-bind-setup-role="${escapeHtml(role.name)}">绑定</button>`
        : `<span class="field-hint">${escapeHtml(role.hint || "请先添加兼容连接。")}</span>`;
    const connection = role.connection || (role.state === "使用内置本地能力" ? "未显式绑定" : "等待选择");
    return `<div class="provider-role-summary"><span>${escapeHtml(role.name)}</span><span>${escapeHtml(connection)}</span><div class="provider-role-actions"><strong>${escapeHtml(role.state)}</strong>${control}</div></div>`;
  }).join("");
  setupReadiness.innerHTML = `<p class="panel-kicker">所选结果：${escapeHtml(readiness.default_output)}</p>`
    + `<p>${escapeHtml(readiness.message)}</p>`
    + `<section class="provider-role-group"><div class="role-group-heading"><h4>材料提取</h4><p>ASR 与 OCR 会冻结到新任务；未显式绑定时继续使用内置本地能力。</p></div><div class="provider-role-list">${roleRows(readiness.material_roles || [])}</div></section>`
    + `<section class="provider-role-group"><div class="role-group-heading"><h4>成品生成</h4><p>这些职责由当前默认成品决定。</p></div><div class="provider-role-list">${roleRows(readiness.required_roles)}</div></section>`
    + `<p>自动处理：${escapeHtml(readiness.authorization.state)}。${escapeHtml(readiness.authorization.message)}</p>`;
  setupReadiness.querySelectorAll("button[data-bind-setup-role]").forEach((button) => button.addEventListener("click", () => bindSetupRole(button)));
  setupReadiness.querySelectorAll("button[data-unbind-setup-role]").forEach((button) => button.addEventListener("click", () => clearSetupRole(button)));
  setupReadiness.querySelectorAll("select[data-setup-role-select]").forEach((select) => select.addEventListener("change", () => dirtySettingsForms.add(setupReadiness)));
}

async function bindSetupRole(button) {
  const label = button.dataset.bindSetupRole;
  const select = button.previousElementSibling;
  if (!select?.value) { providerFeedback.textContent = `请先为${label}选择连接。`; return; }
  const buttonLabel = button.textContent;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const settings = await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "POST", body: JSON.stringify({ connection_name: select.value }) });
    dirtySettingsForms.delete(setupReadiness);
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = `已绑定：${label}。自动整理需要重新确认。`;
    say(providerFeedback.textContent);
  } catch (error) {
    showProviderUpdateFailure(error);
  } finally {
    button.disabled = false;
    button.textContent = buttonLabel;
  }
}

async function clearSetupRole(button) {
  const label = button.dataset.unbindSetupRole;
  const buttonLabel = button.textContent;
  button.disabled = true;
  button.textContent = "解绑中…";
  try {
    const settings = await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "DELETE" });
    dirtySettingsForms.delete(setupReadiness);
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = `已解绑：${label}。自动整理需要重新确认。`;
    say(providerFeedback.textContent);
  } catch (error) { showProviderUpdateFailure(error); } finally { button.disabled = false; button.textContent = buttonLabel; }
}

async function deleteProviderConnection(button) {
  const name = button.dataset.deleteConnection;
  if (!window.confirm(`确定删除连接“${name}”吗？此操作无法恢复。`)) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "删除中…";
  providerState.textContent = "删除中";
  try {
    const settings = await api(`/api/providers/connections/${encodeURIComponent(name)}`, { method: "DELETE" });
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = `已删除连接：${name}。如自动整理此前已授权，请重新确认。`;
    say(providerFeedback.textContent);
  } catch (error) {
    showProviderUpdateFailure(error);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function refreshWindowsVoices() {
  if (providerForm.elements.preset.value !== "windows-tts") {
    windowsVoiceField.hidden = true;
    return;
  }
  windowsVoiceField.hidden = false;
  if (windowsVoicesLoaded) return;
  const payload = await api("/api/providers/windows-tts/voices");
  windowsVoice.innerHTML = payload.voices.map((voice) => `<option value="${escapeHtml(voice.name)}" ${voice.name === payload.default_voice ? "selected" : ""}>${escapeHtml(voice.name)} · ${escapeHtml(voice.culture)}</option>`).join("");
  windowsVoicesLoaded = true;
}

async function refreshProviderConnectionFields() {
  const preset = providerForm.elements.preset.value;
  const local = ["windows-tts", "local-asr", "local-ocr"].includes(preset);
  providerKeyField.hidden = local;
  if (local) providerForm.elements.api_key.value = "";
  await refreshWindowsVoices();
}

async function loadProviderSettings(defaultOutput = null, protectDirty = false) {
  try {
    const settings = await api(`/api/providers/settings${defaultOutput ? `?default_output=${encodeURIComponent(defaultOutput)}` : ""}`);
    if (protectDirty && (settingsFormNeedsProtection(providerForm) || settingsFormNeedsProtection(setupReadiness))) return;
    if (protectDirty && settingsFormNeedsProtection(providerLimitsForm)) return;
    renderProviderSettings(settings);
    await refreshProviderConnectionFields();
  } catch (error) { providerState.textContent = "无法读取"; }
}

function settingsFormNeedsProtection(form) {
  const active = document.activeElement;
  return form.contains(active) || dirtySettingsForms.has(form);
}

async function refreshProviderSettingsWhenIdle() {
  if (!settingsFormNeedsProtection(providerForm) && !settingsFormNeedsProtection(setupReadiness) && !settingsFormNeedsProtection(providerLimitsForm)) await loadProviderSettings(automationForm.elements.default_output.value, true);
}

function renderList(target, items, empty) {
  if (!items.length) {
    target.innerHTML = empty ? `<p class="empty">${escapeHtml(empty)}</p>` : "";
    return;
  }
  target.innerHTML = items.map((item) => `
    <article class="learning-row" data-item-ref="${escapeHtml(item.item_ref)}" data-state="${escapeHtml(item.state)}" data-filter="${taskFilterFor(item)}" tabindex="0">
      <div class="learning-copy">
        <h3>${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.source)}</p>
      </div>
      <span class="learning-state">${escapeHtml(stateLabel[item.state] || "需要检查")}</span>
      <span class="task-progress" aria-hidden="true" style="--task-progress:${taskProgress(item)}%"><i></i></span>
      <span class="row-next">${escapeHtml(item.message)}</span>
      <button class="open-task-detail" type="button" data-open-item-ref="${escapeHtml(item.item_ref)}">查看任务 <span aria-hidden="true">→</span></button>
    </article>`).join("");
  target.querySelectorAll("article[data-item-ref]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      selectTask(row.dataset.itemRef);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectTask(row.dataset.itemRef); }
    });
  });
  target.querySelectorAll("button[data-open-item-ref]").forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.openItemRef)));
  updateSelectedTaskRows();
}

function render(snapshot) {
  currentSnapshot = snapshot;
  renderList(lists.library, snapshot.library, "");
  renderList(lists.processing, snapshot.processing, "");
  renderList(lists.inbox, snapshot.inbox, "");
  const items = allLearningItems();
  if (!items.length) lists.processing.innerHTML = '<p class="empty">还没有任务。请从来源页添加内容。</p>';
  if (selectedItemRef && !items.some((item) => item.item_ref === selectedItemRef)) showTaskWorkbench();
  const counts = {
    all: items.length,
    attention: items.filter((item) => taskFilterFor(item) === "attention").length,
    processing: items.filter((item) => taskFilterFor(item) === "processing").length,
    queued: items.filter((item) => taskFilterFor(item) === "queued").length,
    completed: items.filter((item) => taskFilterFor(item) === "completed").length,
  };
  for (const [name, count] of Object.entries(counts)) document.querySelector(`[data-filter-count="${name}"]`).textContent = count;
  taskCount.textContent = counts.all;
  taskSummary.innerHTML = `<strong>${counts.processing} 项正在处理</strong>，${counts.attention} 项需要你处理，${counts.completed} 项已完成。`;
  workbenchNote.hidden = !items.length;
  renderTaskFocus(items);
  applyTaskFilter();
  if (selectedItemRef) renderTaskDetail(items.find((item) => item.item_ref === selectedItemRef));
}

function renderTaskFocus(items) {
  const priority = { attention: 0, processing: 1, queued: 2, completed: 3 };
  const item = [...items].sort((left, right) => priority[taskFilterFor(left)] - priority[taskFilterFor(right)])[0];
  if (!item) {
    taskFocus.className = "task-focus is-empty";
    taskFocus.innerHTML = `<div class="focus-copy"><p class="panel-kicker">工作台已准备好</p><h2>从“来源”添加第一项内容</h2><p>本地视频、公开链接和收藏进入任务后，进度与问题会持续保留在这里。</p></div><button class="focus-open" type="button">打开来源 <span aria-hidden="true">→</span></button>`;
    taskFocus.querySelector("button").addEventListener("click", () => showView("sources"));
    return;
  }
  const filter = taskFilterFor(item);
  const heading = filter === "attention" ? "这项任务需要你处理" : filter === "processing" ? "这项任务正在向前推进" : filter === "queued" ? "下一项等待整理的内容" : "最近完成的内容";
  taskFocus.className = `task-focus ${filter}`;
  taskFocus.innerHTML = `<div class="focus-signal" aria-hidden="true"><span>${taskProgress(item)}</span><small>%</small></div><div class="focus-copy"><p class="panel-kicker">${heading}</p><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.message)}</p></div><button class="focus-open" type="button">查看任务 <span aria-hidden="true">→</span></button>`;
  taskFocus.querySelector("button").addEventListener("click", () => selectTask(item.item_ref));
}

function allLearningItems() {
  return [...currentSnapshot.processing, ...currentSnapshot.inbox, ...currentSnapshot.library];
}

function taskFilterFor(item) {
  if (["needs_action", "waiting_setup", "waiting_authorization"].includes(item.state)) return "attention";
  if (["organizing", "partial_ready"].includes(item.state)) return "processing";
  if (item.state === "ready") return "completed";
  return "queued";
}

function taskProgress(item) {
  if (item.state === "ready") return 100;
  if (item.state === "partial_ready") return 78;
  if (item.state === "organizing") return 58;
  if (item.state === "needs_action") return item.output_goal === "complete_note_with_audio" && item.note_href ? 78 : 52;
  return 34;
}

function updateSelectedTaskRows() {
  taskList.querySelectorAll("article[data-item-ref]").forEach((row) => row.classList.toggle("is-selected", row.dataset.itemRef === selectedItemRef));
}

function applyTaskFilter() {
  taskList.querySelectorAll("article[data-filter]").forEach((row) => {
    row.hidden = activeTaskFilter !== "all" && row.dataset.filter !== activeTaskFilter;
  });
  const label = document.querySelector(`[data-filter="${activeTaskFilter}"] span`)?.textContent?.trim() || "全部任务";
  taskListTitle.textContent = label;
}

function selectTask(itemRef) {
  selectedItemRef = itemRef;
  renderTaskDetail(allLearningItems().find((item) => item.item_ref === itemRef));
  taskWorkbench.hidden = true;
  taskDetailView.hidden = false;
  taskConsole.classList.add("has-detail");
  updateSelectedTaskRows();
  window.scrollTo({ top: 0, behavior: "auto" });
  document.querySelector("#back-to-tasks")?.focus({ preventScroll: true });
}

function showTaskWorkbench() {
  selectedItemRef = null;
  taskDetailView.hidden = true;
  taskWorkbench.hidden = false;
  taskConsole.classList.remove("has-detail");
  updateSelectedTaskRows();
  window.scrollTo({ top: 0, behavior: "auto" });
}

function trackSteps(item) {
  const labels = item.output_goal === "complete_note_with_audio"
    ? ["获取内容", "准备材料", "生成笔记", "生成音频"]
    : ["获取内容", "准备材料", "生成笔记"];
  let completed = 1;
  if (["materials_ready", "waiting_setup", "waiting_authorization", "queued", "organizing", "partial_ready", "needs_action", "ready"].includes(item.state)) completed = 2;
  if (["partial_ready", "ready"].includes(item.state) || item.note_href) completed = 3;
  if (item.state === "ready") completed = labels.length;
  return labels.map((label, index) => ({ label, done: index < completed, current: index === completed && completed < labels.length }));
}

function renderTaskDetail(item) {
  if (!item) {
    taskDetail.classList.remove("has-selection");
    taskDetail.innerHTML = '<div class="empty-detail"><span aria-hidden="true">⌁</span><h2>还没有任务</h2><p>请从来源页添加第一项内容。</p></div>';
    return;
  }
  taskDetail.classList.add("has-selection");
  const steps = trackSteps(item);
  const percent = taskProgress(item);
  const needsUserAction = ["needs_action", "waiting_setup", "waiting_authorization"].includes(item.state);
  const actionHeading = item.action ? "下一步" : needsUserAction ? "当前需要你处理" : "当前不需要操作";
  const actionCopy = item.action || needsUserAction
    ? item.message
    : "语栖会根据当前事实更新这里；遇到需要确认的问题时会给出明确操作。";
  const failureExplanation = item.failure_reason
    ? `<section class="failure-explanation" aria-label="停止原因"><p>停止原因</p><strong>${escapeHtml(item.failure_reason)}</strong></section>`
    : "";
  taskDetail.innerHTML = `
    <div class="detail-layout">
      <div class="detail-main">
        <div class="detail-heading"><div><p class="panel-kicker">当前任务</p><h2>${escapeHtml(item.title)}</h2><p class="detail-source">${escapeHtml(item.source)}</p></div><span class="detail-status ${escapeHtml(item.state)}">${escapeHtml(stateLabel[item.state] || "需要检查")}</span></div>
        <p class="detail-message">${escapeHtml(item.message)}</p>
        ${failureExplanation}
        <section class="production-section"><div class="production-heading"><h3>产出轨道</h3><span>${percent}%</span></div><div class="production-track" style="--track-steps:${steps.length}">${steps.map((step) => `<span class="track-step${step.done ? " is-done" : ""}${step.current ? " is-current" : ""}"><i>${step.done ? "✓" : ""}</i><strong>${step.label}</strong></span>`).join("")}</div></section>
      </div>
      <aside class="detail-action"><p class="panel-kicker">${actionHeading}</p><strong>${escapeHtml(stateLabel[item.state] || "需要检查")}</strong><p>${escapeHtml(actionCopy)}</p><div class="detail-action-controls">${item.action && learningActionKinds.has(item.action_kind) ? `<button class="detail-primary-action" type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="${escapeHtml(item.action_kind)}">${escapeHtml(item.action)}</button>` : ""}<button class="danger-link" type="button" data-delete-item-ref="${escapeHtml(item.item_ref)}"${item.state === "organizing" ? ' disabled title="正在处理，暂时不能删除"' : ""}>删除任务</button></div>${item.audio_href ? `<audio controls preload="metadata" src="${escapeHtml(item.audio_href)}">音频暂时不能播放。</audio>` : ""}</aside>
    </div>`;
  taskDetail.querySelectorAll("button[data-item-ref]").forEach((button) => button.addEventListener("click", () => actOnItem(button.dataset.itemRef, button.dataset.action)));
  taskDetail.querySelector("button[data-delete-item-ref]:not(:disabled)")?.addEventListener("click", () => openDeleteTask(item));
}

function openDeleteTask(item) {
  pendingDeleteItemRef = item.item_ref;
  deleteTaskTitle.textContent = item.title;
  deleteTaskDialog.showModal();
  window.requestAnimationFrame(() => document.querySelector("#confirm-delete-task")?.focus({ preventScroll: true }));
}

function closeDeleteTask() {
  pendingDeleteItemRef = null;
  if (deleteTaskDialog.open) deleteTaskDialog.close();
}

function thumbnailHref(relativePath) {
  return `/api/douyin/favorites/thumbnails/${relativePath.split("/").map(encodeURIComponent).join("/")}`;
}

function formatSyncTime(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

function renderFavorites(snapshot) {
  const availableIds = new Set(snapshot.items.map((item) => item.aweme_id));
  selectedFavoriteIdsState = new Set(
    [...selectedFavoriteIdsState].filter((itemId) => availableIds.has(itemId)),
  );
  if (!snapshot.items.length) {
    favoriteList.innerHTML = `<p class="empty">还没有同步的抖音收藏。</p>`;
    if (!favoriteStatusProtected) favoriteStatus.textContent = "连接抖音后，收藏会出现在这里。";
    updateFavoriteSelection();
    return;
  }
  if (!favoriteStatusProtected) favoriteStatus.textContent = `最近同步：${formatSyncTime(snapshot.synced_at)}`;
  favoriteList.innerHTML = snapshot.items.map((item) => {
    const thumbnail = item.thumbnail_path
      ? `<img src="${thumbnailHref(item.thumbnail_path)}" alt="" loading="lazy" />`
      : `<div class="thumbnail-placeholder" aria-label="封面暂不可用">封面暂不可用</div>`;
    return `
      <article class="favorite-card">
        <div class="favorite-media">${thumbnail}</div>
        <div class="favorite-card-copy">
          <h3 class="favorite-title">${escapeHtml(item.title)}</h3>
          <p class="favorite-time">同步于 ${escapeHtml(formatSyncTime(item.synced_at))}</p>
          <label class="favorite-select"><input type="checkbox" value="${escapeHtml(item.aweme_id)}"${selectedFavoriteIdsState.has(item.aweme_id) ? " checked" : ""} /> 加入收件箱</label>
          <a class="favorite-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener">打开作品页</a>
        </div>
      </article>`;
  }).join("");
  favoriteList.querySelectorAll("input[type=checkbox]").forEach((checkbox) => checkbox.addEventListener("change", updateFavoriteSelection));
  updateFavoriteSelection();
}

function renderSourceJobs(jobs) {
  const visible = jobs.slice(0, 8);
  sourceJobs.hidden = visible.length === 0;
  if (!visible.length) return;
  sourceJobs.innerHTML = `<h2>来源处理</h2>${visible.map((job) => `
    <article class="source-job">
      <span>${escapeHtml(job.source_kind === "douyin_favorite" ? "抖音收藏" : job.source_kind === "public_url" ? "公开链接" : "本地视频")}</span>
      <span>${escapeHtml(job.status === "queued" ? "等待开始" : job.status === "running" ? "正在处理材料" : job.status === "completed" ? "材料已加入收件箱" : job.status === "interrupted" ? "处理已中断" : "处理失败")}</span>
      ${["failed", "interrupted"].includes(job.status) ? `<button type="button" data-retry-job="${escapeHtml(job.job_id)}">重试</button>` : ""}
    </article>`).join("")}`;
  sourceJobs.querySelectorAll("button[data-retry-job]").forEach((button) => button.addEventListener("click", () => retrySourceJob(button.dataset.retryJob)));
}

async function loadSourceJobs() {
  try { renderSourceJobs((await api("/api/learning/jobs")).jobs); } catch (error) { sourceJobs.hidden = true; }
}

async function retrySourceJob(jobId) {
  try {
    await api(`/api/learning/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
    say("已重新加入来源处理。");
    await refresh(true);
  } catch (error) { say(error.message); }
}

function selectedFavoriteIds() { return [...selectedFavoriteIdsState]; }
function updateFavoriteSelection() {
  selectedFavoriteIdsState = new Set(
    [...favoriteList.querySelectorAll("input[type=checkbox]:checked")].map((item) => item.value),
  );
  const selected = selectedFavoriteIds();
  addSelectedFavoritesButton.disabled = selected.length === 0;
  favoriteSelection.textContent = selected.length ? `已选 ${selected.length} 项，加入后会使用当前默认结果。` : "可多选历史收藏；首次同步不会自动处理。";
}

async function loadFavorites() {
  try {
    renderFavorites(await api("/api/douyin/favorites"));
  } catch (error) {
    favoriteStatus.textContent = error.message;
  }
}

function renderAutomationStatus(status) {
  lastAutomationStatus = status;
  const enabled = status.enabled;
  automationState.textContent = enabled ? "自动整理已开启" : status.needs_authorization ? "需要重新确认" : status.configured ? "等待确认" : "等待设置";
  automationState.className = `status-pill ${enabled ? "connected" : ""}`;
  const output = status.default_output || "complete_note_with_audio";
  const outputLabel = output === "complete_note" ? "完整笔记" : "笔记 + 音频";
  defaultOutputLabel.textContent = outputLabel;
  singleVideoOutput.textContent = outputLabel;
  runtimeState.classList.toggle("is-on", enabled);
  runtimeTitle.textContent = enabled ? "自动处理已开启" : "自动处理已关闭";
  runtimeCopy.textContent = enabled ? "仅在语栖运行时工作" : "单个任务仍可加入并等待设置";
  automationSummary.textContent = enabled
    ? `会按 ${status.check_interval_seconds} 秒检查收件箱；默认生成${status.default_output === "complete_note" ? "完整笔记" : "完整笔记和播客音频"}。设置未变化时授权会持续有效。`
    : status.needs_authorization
      ? "模型、职责、额度或产出设置已经变化。检查后重新确认，自动整理才会继续。"
      : status.configured
        ? "设置已保存。自动整理当前关闭，手动开启时会显示本次调用与费用边界。"
        : "先保存默认产出并完成所需连接，再决定是否开启自动整理。";
  const accessState = enabled ? "enabled" : status.needs_authorization ? "attention" : status.configured ? "ready" : "setup";
  automationAccess.dataset.state = accessState;
  automationAccessTitle.textContent = enabled ? "已授权并运行" : status.needs_authorization ? "设置已变化，需要重新确认" : status.configured ? "设置已保存，自动整理未开启" : "先完成设置";
  automationAccessCopy.textContent = enabled
    ? "这项授权由语栖持久保存；再次进入页面无需重复确认。实质设置变化时会自动失效。"
    : status.needs_authorization
      ? "已有任务和材料不会丢失。确认当前设置后，可以重新开启自动整理。"
      : status.configured
        ? "只有点击开启并确认调用说明后，语栖才会自动推进需要 Provider 的阶段。"
        : "保存默认产出并完成模型职责绑定后，这里会提供明确的开启操作。";
  authorizeAutomationButton.hidden = enabled;
  authorizeAutomationButton.disabled = !status.configured || enabled;
  authorizeAutomationButton.textContent = status.needs_authorization ? "查看变化并重新确认" : "开启自动整理";
  disableAutomationButton.hidden = !enabled;
  if (status.configured && !settingsFormNeedsProtection(automationForm)) {
    automationForm.elements.default_output.value = status.default_output;
    automationForm.elements.check_interval_seconds.value = status.check_interval_seconds;
    automationForm.elements.auto_organize_new_favorites.checked = status.auto_organize_new_favorites;
    automationForm.elements.max_items_per_tick.value = status.max_items_per_tick;
  }
}

function renderStorageStatus(status, protectDirty = false) {
  currentOutputRoot.textContent = `当前正在使用：${status.current_output_root}`;
  if (!protectDirty || !settingsFormNeedsProtection(storageForm)) outputRoot.value = status.next_output_root;
  storageFeedback.textContent = status.restart_required
    ? `已保存新的启动位置：${status.next_output_root}。关闭并重新启动语栖后生效；当前任务仍使用 ${status.current_output_root}。`
    : `当前与下次启动都使用：${status.current_output_root}`;
}

async function loadStorageStatus(protectDirty = false) {
  try { renderStorageStatus(await api("/api/storage"), protectDirty); }
  catch (error) { storageFeedback.textContent = error.message; }
}

function showView(view, updateHash = true) {
  const target = document.querySelector(`[data-view-panel="${view}"]`);
  if (!target) return;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    const visible = panel === target;
    panel.hidden = !visible;
    panel.classList.toggle("is-visible", visible);
  });
  document.querySelectorAll(".bookmark[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  if (view === "tasks") showTaskWorkbench();
  if (updateHash && window.location.hash !== `#${view}`) window.history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "auto" });
}

function showSettingsPanel(panelName) {
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => {
    const visible = panel.dataset.settingsPanel === panelName;
    panel.hidden = !visible;
    panel.classList.toggle("is-visible", visible);
  });
  document.querySelectorAll("[data-settings-tab]").forEach((button) => button.classList.toggle("is-active", button.dataset.settingsTab === panelName));
}

function openConnectionDialog(preset = null) {
  if (preset) providerForm.elements.preset.value = preset;
  suggestProviderConnectionName();
  refreshProviderConnectionFields().catch((error) => say(error.message));
  connectionDialog.showModal();
  window.requestAnimationFrame(() => providerForm.elements.name.focus());
}

async function loadAutomationStatus() { try { renderAutomationStatus(await api("/api/automation/status")); } catch (error) { automationState.textContent = "无法读取"; } }

function openAutomationAuthorization() {
  if (!lastAutomationStatus.configured || lastAutomationStatus.enabled) return;
  const output = lastAutomationStatus.default_output === "complete_note" ? "完整笔记" : "完整笔记 + 播客音频";
  authorizationOutput.textContent = output;
  authorizationInterval.textContent = `每 ${lastAutomationStatus.check_interval_seconds} 秒检查一次，每次最多 ${lastAutomationStatus.max_items_per_tick} 项`;
  automationAuthorizationDialog.showModal();
  window.requestAnimationFrame(() => document.querySelector("#cancel-automation-authorization")?.focus({ preventScroll: true }));
}

function closeAutomationAuthorization() {
  automationAuthorizationDialog.close();
}

function stopLoginPolling() {
  window.clearTimeout(loginPollTimer);
  loginPollTimer = null;
}

function stopLoginCountdown() {
  window.clearInterval(loginCountdownTimer);
  loginCountdownTimer = null;
}

function setLoginCountdown(seconds) {
  stopLoginCountdown();
  let remaining = Math.max(0, Number(seconds) || 0);
  const renderCountdown = () => {
    loginCountdown.textContent = remaining > 0
      ? `二维码将在 ${remaining} 秒后更新。`
      : "二维码已过期，正在刷新。";
  };
  renderCountdown();
  loginCountdownTimer = window.setInterval(() => {
    remaining -= 1;
    renderCountdown();
    if (remaining <= 0) {
      stopLoginCountdown();
      refreshDouyinQr();
    }
  }, 1000);
}

function showLoginState(state) {
  douyinLoginStatus = state.status;
  const requestContractBlocked = state.failure_kind === "request_rejected";
  loginState.textContent = requestContractBlocked ? "接口变化" : (loginLabel[state.status] || "需重连");
  loginState.className = `status-pill ${state.status}`;
  loginPanel.hidden = false;
  connectDouyinButton.disabled = requestContractBlocked;
  connectDouyinButton.textContent = requestContractBlocked
    ? "暂不需要重新扫码"
    : (state.status === "connected" ? "重新验证" : "打开抖音验证窗口");
  const reconnect = ["expired", "failed", "cancelled"].includes(state.status);
  if (state.status === "connected") {
    loginMessage.textContent = "官方短信与扫码验证已完成，可以同步收藏。";
  } else if (state.status === "browser_ready") {
    loginMessage.textContent = "请在抖音官方窗口完成手机号验证码，并按提示使用抖音 App 扫码确认；随后官方页面会自动验证收藏访问。";
  } else if (state.status === "scanned") {
    loginMessage.textContent = "已扫码，请在手机上确认登录。";
  } else if (state.status === "confirmed") {
    loginMessage.textContent = "已确认，正在等待官方页面验证收藏访问。";
  } else if (state.status === "verification_required") {
    loginMessage.textContent = "请在原抖音官方窗口继续完成短信、扫码或页面要求的额外验证。";
  } else if (reconnect) {
    loginMessage.textContent = "需要重新连接抖音。";
  } else {
    loginMessage.textContent = "正在打开抖音官方短信与扫码验证窗口。";
  }
  if (state.message) loginMessage.textContent = state.message;
  loginQr.hidden = !state.qr_available;
  loginCountdown.hidden = !state.qr_available;
  refreshDouyinButton.hidden = !state.qr_available;
  if (state.qr_available && douyinSessionId) {
    loginQr.src = `/api/douyin/login/qr/${encodeURIComponent(douyinSessionId)}/image?v=${Date.now()}`;
    setLoginCountdown(state.expires_in);
  } else {
    stopLoginCountdown();
  }
  syncFavoritesButton.disabled = state.status !== "connected";
  if (["connected", "expired", "failed", "cancelled"].includes(state.status)) {
    stopLoginPolling();
  }
}

function showLoginFailure() {
  showLoginState({ status: "failed", expires_in: 0, qr_available: false });
  favoriteStatus.textContent = "登录已失效，请重新连接抖音。";
}

function scheduleLoginPoll() {
  stopLoginPolling();
  if (!douyinSessionId || ["connected", "expired", "failed", "cancelled"].includes(douyinLoginStatus)) return;
  loginPollTimer = window.setTimeout(pollDouyinLogin, 1000);
}

async function pollDouyinLogin() {
  if (!douyinSessionId) return;
  try {
    const state = await api(`/api/douyin/login/${encodeURIComponent(douyinSessionId)}`);
    showLoginState(state);
    if (state.status === "connected" && syncFavoritesAfterLogin) {
      syncFavoritesAfterLogin = false;
      loginMessage.textContent = "登录验证完成，正在同步全部收藏。";
      await syncFavorites({ automatic: true });
    }
    scheduleLoginPoll();
  } catch (error) {
    showLoginFailure();
    say(error.message);
  }
}

async function refreshDouyinQr() {
  if (!douyinSessionId || loginRefreshInFlight) return;
  loginRefreshInFlight = true;
  try {
    const state = await api(`/api/douyin/login/qr/${encodeURIComponent(douyinSessionId)}/refresh`, { method: "POST" });
    showLoginState(state);
    scheduleLoginPoll();
  } catch (error) {
    showLoginFailure();
    say(error.message);
  } finally {
    loginRefreshInFlight = false;
  }
}

async function connectDouyin() {
  stopLoginPolling();
  stopLoginCountdown();
  syncFavoritesAfterLogin = true;
  favoriteStatusProtected = false;
  if (douyinSessionId) {
    try { await api(`/api/douyin/login/${encodeURIComponent(douyinSessionId)}`, { method: "DELETE" }); } catch (_) { /* the old local session may already be gone */ }
  }
  try {
    const state = await api("/api/douyin/login/browser", { method: "POST" });
    douyinSessionId = state.session_id;
    showLoginState(state);
    scheduleLoginPoll();
    say("抖音官方验证窗口已打开。");
  } catch (error) {
    syncFavoritesAfterLogin = false;
    showLoginFailure();
    say(error.message);
  }
}

async function cancelDouyin() {
  if (!douyinSessionId) return;
  try {
    const state = await api(`/api/douyin/login/${encodeURIComponent(douyinSessionId)}`, { method: "DELETE" });
    showLoginState(state);
    say("已取消连接。");
  } catch (error) {
    showLoginFailure();
    say(error.message);
  }
}

async function restoreDouyinLogin() {
  try {
    const state = await api("/api/douyin/login/current");
    if (!state.session_id || state.status === "disconnected") return;
    douyinSessionId = state.session_id;
    showLoginState(state);
    scheduleLoginPoll();
  } catch (_) {
    /* A missing or temporarily unverifiable local session stays disconnected. */
  }
}

async function syncFavorites(options = {}) {
  if (!douyinSessionId || douyinLoginStatus !== "connected") {
    favoriteStatus.textContent = "请先在设置中连接抖音。";
    return;
  }
  const automatic = options?.automatic === true;
  favoriteStatusProtected = true;
  syncFavoritesButton.disabled = true;
  favoriteStatus.textContent = "正在获取抖音当前请求校验，并由后端同步全部收藏…";
  try {
    const snapshot = await api("/api/douyin/favorites", {
      method: "POST",
      body: JSON.stringify({ session_id: douyinSessionId }),
    });
    favoriteStatusProtected = false;
    renderFavorites(snapshot);
    if (automatic) loginMessage.textContent = "登录和收藏同步均已完成。";
    say("收藏已同步。");
  } catch (error) {
    if (error.status === 401) showLoginFailure();
    favoriteStatus.textContent = error.message;
    if (automatic) loginMessage.textContent = `登录有效，但收藏同步未完成：${error.message}`;
    say(error.message);
  } finally {
    syncFavoritesButton.disabled = douyinLoginStatus !== "connected";
  }
}

async function refresh(force = false) {
  try {
    const query = !force && revision ? `?revision=${encodeURIComponent(revision)}` : "";
    const snapshot = await api(`/api/learning/snapshot${query}`);
    if (!snapshot.unchanged) {
      revision = snapshot.revision;
      render(snapshot);
    }
    await Promise.all([loadFavorites(), refreshProviderSettingsWhenIdle(), loadStorageStatus(true)]);
    await loadSourceJobs();
    await loadAutomationStatus();
  } catch (error) {
    say(error.message);
  } finally {
    scheduleRefresh();
  }
}

function scheduleRefresh() {
  window.clearTimeout(timer);
  timer = window.setTimeout(() => refresh(), document.hidden ? 5000 : 2000);
}

async function actOnItem(itemRef, action) {
  try {
    if (action === "open_note") {
      window.open(`/api/learning/items/${encodeURIComponent(itemRef)}/note`, "_blank", "noopener");
      return;
    }
    if (action === "open_settings") {
      showView("settings");
      showSettingsPanel("connections");
      document.querySelector("#settings")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (action === "open_automation") {
      showView("settings");
      showSettingsPanel("output");
      document.querySelector("#settings")?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.requestAnimationFrame(() => authorizeAutomationButton?.focus({ preventScroll: true }));
      return;
    }
    if (action === "open_single_video") {
      singleVideoDialog.showModal();
      window.requestAnimationFrame(() => document.querySelector("#local-video")?.focus({ preventScroll: true }));
      return;
    }
    if (action === "start_automation") {
      await api(`/api/learning/items/${encodeURIComponent(itemRef)}/start-automation`, { method: "POST" });
      say("已加入整理队列。");
      await refresh(true);
      return;
    }
    if (action === "retry_automation") {
      await api(`/api/learning/items/${encodeURIComponent(itemRef)}/retry-automation`, { method: "POST" });
      say("已重新加入整理队列。");
      await refresh(true);
      return;
    }
    if (action !== "continue") return;
    const result = await api(`/api/learning/items/${encodeURIComponent(itemRef)}/continue`, { method: "POST" });
    say(result.outcome === "needs_setup" ? "需要完成设置后才能继续。" : "已继续整理内容。");
    await refresh(true);
  } catch (error) {
    say(error.message);
  }
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  const form = new FormData(submittedForm);
  const file = form.get("video");
  if (!(file instanceof File) || !file.name) return;
  try {
    say("正在保存视频到学习收件箱。");
    const result = await api(`/api/learning/uploads?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: file,
    });
    say("已加入收件箱，正在整理材料。");
    submittedForm.reset();
    selectedVideoName.textContent = "MP4、MOV、MKV、AVI、MPEG 或 WebM";
    singleVideoDialog.close();
    showView("tasks");
    await refresh(true);
  } catch (error) {
    say(error.message);
  }
});

deleteTaskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!pendingDeleteItemRef) return;
  const taskId = pendingDeleteItemRef;
  const button = document.querySelector("#confirm-delete-task");
  button.disabled = true;
  button.textContent = "正在移入回收区…";
  try {
    await api(`/api/learning/items/${encodeURIComponent(taskId)}`, { method: "DELETE" });
    closeDeleteTask();
    selectedItemRef = null;
    say("任务已移入回收区。");
    await Promise.all([refresh(true), loadSourceJobs()]);
  } catch (error) {
    say(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "移入回收区";
  }
});

urlForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  const form = new FormData(submittedForm);
  try {
    await api("/api/learning/submit", { method: "POST", body: JSON.stringify({ source: form.get("source") }) });
    say("已加入收件箱，正在整理材料。");
    submittedForm.reset();
    showView("tasks");
    await refresh(true);
  } catch (error) { say(error.message); }
});

addSelectedFavoritesButton.addEventListener("click", async () => {
  const awemeIds = selectedFavoriteIds();
  if (!awemeIds.length) return;
  try {
    await api("/api/douyin/favorites/select", { method: "POST", body: JSON.stringify({ aweme_ids: awemeIds }) });
    say("已加入收件箱，正在整理材料。");
    selectedFavoriteIdsState.clear();
    showView("tasks");
    await refresh(true);
  } catch (error) { say(error.message); }
});

automationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    renderAutomationStatus(await api("/api/automation/configure", { method: "POST", body: JSON.stringify({ default_output: form.get("default_output"), auto_organize_new_favorites: form.has("auto_organize_new_favorites"), check_interval_seconds: Number(form.get("check_interval_seconds")), max_items_per_tick: Number(form.get("max_items_per_tick")) }) }));
    dirtySettingsForms.delete(automationForm);
    await loadProviderSettings(String(form.get("default_output")));
    say("自动整理设置已保存。需要自动执行时，请从权限状态卡明确开启。");
  } catch (error) { say(error.message); }
});

authorizeAutomationButton.addEventListener("click", openAutomationAuthorization);

confirmAutomationAuthorization.addEventListener("click", async () => {
  const label = confirmAutomationAuthorization.textContent;
  confirmAutomationAuthorization.disabled = true;
  confirmAutomationAuthorization.textContent = "正在开启…";
  try {
    renderAutomationStatus(await api("/api/automation/authorize", { method: "POST", body: JSON.stringify({ confirm_paid: true }) }));
    closeAutomationAuthorization();
    say("自动整理已开启；设置未变化时无需重复确认。");
  } catch (error) { say(error.message); }
  finally {
    confirmAutomationAuthorization.disabled = false;
    confirmAutomationAuthorization.textContent = label;
  }
});

disableAutomationButton.addEventListener("click", async () => {
  try { renderAutomationStatus(await api("/api/automation/disable", { method: "POST" })); say("自动整理已关闭。 "); } catch (error) { say(error.message); }
});

storageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = storageForm.querySelector("button[type=submit]");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const status = await api("/api/storage/output-root", { method: "PUT", body: JSON.stringify({ output_root: outputRoot.value.trim() }) });
    dirtySettingsForms.delete(storageForm);
    renderStorageStatus(status);
    say(status.restart_required ? "新的保存位置已保存，重启语栖后生效。" : "保存位置未变化。");
  } catch (error) {
    storageFeedback.textContent = error.message;
    say(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

providerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  const form = new FormData(submittedForm);
  const preset = String(form.get("preset"));
  const key = String(form.get("api_key") || "").trim();
  const finishSaving = startProviderSave(event);
  try {
    const settings = await api("/api/providers/connections", {
      method: "POST",
      body: JSON.stringify({
        name: form.get("name"), preset,
        ...(key ? { api_key: key } : {}),
        ...(preset === "windows-tts" ? { voice: form.get("voice") } : {}),
      }),
    });
    submittedForm.reset();
    dirtySettingsForms.delete(providerForm);
    suggestProviderConnectionName();
    renderProviderSettings(settings);
    providerFeedback.textContent = "连接已保存；密钥不会显示在页面中。";
    say(providerFeedback.textContent);
    connectionDialog.close();
  } catch (error) { showProviderUpdateFailure(error); } finally { finishSaving(); }
});

providerForm.elements.preset.addEventListener("change", () => {
  suggestProviderConnectionName();
  windowsVoicesLoaded = false;
  refreshProviderConnectionFields().catch((error) => say(error.message));
});

providerLimitsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = event.submitter;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const settings = await api("/api/providers/limits", {
      method: "PUT",
      body: JSON.stringify({
        retries_per_role: Number(form.get("retries_per_role")),
        global_calls_per_day: Number(form.get("global_calls_per_day")),
        budget_group_calls_per_day: Object.fromEntries(["note", "podcast", "tts", "asr", "ocr"].map((name) => [name, Number(form.get(`limit_${name}`))])),
      }),
    });
    dirtySettingsForms.delete(providerLimitsForm);
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerLimitsFeedback.textContent = "调用限制已保存；自动处理需要重新确认。";
    say(providerLimitsFeedback.textContent);
  } catch (error) {
    providerLimitsFeedback.textContent = error.message;
    say(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

for (const form of [providerForm, automationForm, providerLimitsForm, storageForm]) {
  form.addEventListener("input", () => dirtySettingsForms.add(form));
  form.addEventListener("change", () => dirtySettingsForms.add(form));
  form.addEventListener("reset", () => {
    dirtySettingsForms.delete(form);
    window.setTimeout(() => {
      if (form === providerForm) loadProviderSettings(automationForm.elements.default_output.value);
      else if (form === automationForm) loadAutomationStatus();
      else if (form === providerLimitsForm) loadProviderSettings(automationForm.elements.default_output.value);
      else loadStorageStatus();
    });
  });
}

document.querySelectorAll(".bookmark[data-view]").forEach((link) => link.addEventListener("click", (event) => {
  event.preventDefault();
  showView(link.dataset.view);
}));

window.addEventListener("hashchange", () => {
  const current = document.querySelector(`.bookmark[href="${CSS.escape(window.location.hash)}"]`);
  if (!current) return;
  showView(current.dataset.view, false);
});

document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  activeTaskFilter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
  applyTaskFilter();
}));
document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => showSettingsPanel(button.dataset.settingsTab)));
document.querySelectorAll("[data-open-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.showModal()));
document.querySelectorAll("[data-close-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.close()));
document.querySelectorAll("[data-close-automation-authorization]").forEach((button) => button.addEventListener("click", closeAutomationAuthorization));
document.querySelectorAll("[data-close-delete-task]").forEach((button) => button.addEventListener("click", closeDeleteTask));
deleteTaskDialog.addEventListener("close", () => { pendingDeleteItemRef = null; });
document.querySelectorAll("[data-go-settings]").forEach((button) => button.addEventListener("click", () => {
  if (singleVideoDialog.open) singleVideoDialog.close();
  showView("settings");
  showSettingsPanel("output");
}));
document.querySelectorAll("[data-go-sources]").forEach((button) => button.addEventListener("click", () => showView("sources")));
document.querySelector("#open-connection-dialog").addEventListener("click", () => openConnectionDialog());
document.querySelectorAll("[data-close-connection]").forEach((button) => button.addEventListener("click", () => connectionDialog.close()));
document.querySelector("#local-video").addEventListener("change", (event) => {
  selectedVideoName.textContent = event.currentTarget.files[0]?.name || "MP4、MOV、MKV、AVI、MPEG 或 WebM";
});
document.querySelector("#refresh-tasks").addEventListener("click", () => refresh(true));
document.querySelector("#back-to-tasks").addEventListener("click", showTaskWorkbench);

connectDouyinButton.addEventListener("click", connectDouyin);
refreshDouyinButton.addEventListener("click", refreshDouyinQr);
cancelDouyinButton.addEventListener("click", cancelDouyin);
syncFavoritesButton.addEventListener("click", syncFavorites);
document.addEventListener("visibilitychange", () => refresh(true));
restoreDouyinLogin();
suggestProviderConnectionName();
loadProviderSettings();
loadStorageStatus();
showView(["tasks", "sources", "settings"].includes(window.location.hash.slice(1)) ? window.location.hash.slice(1) : "tasks", false);
refresh(true);
