const api = async (path, options = {}) => {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
  } catch (networkError) {
    const error = new Error("无法连接语栖服务。请确认语栖正在运行，然后刷新页面。");
    error.status = 0;
    throw error;
  }
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
const favoriteFolders = document.querySelector("#favorite-folders");
const favoriteFolderTitle = document.querySelector("#favorite-folder-title");
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
const providerCapabilityList = document.querySelector("#provider-capability-list");
const providerState = document.querySelector("#provider-settings-state");
const providerFeedback = document.querySelector("#provider-feedback");
const providerReadyCount = document.querySelector("#provider-ready-count");
const providerReadyBadges = document.querySelector("#provider-ready-badges");
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
const trashList = document.querySelector("#trash-list");
const taskSearch = document.querySelector("#task-search");
const taskTableCount = document.querySelector("#task-table-count");
const taskFilterEmpty = document.querySelector("#task-filter-empty");
const recentActivity = document.querySelector("#recent-activity");
const taskSystemStatus = document.querySelector("#task-system-status");
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
const connectionDialogTitle = document.querySelector("#connection-dialog-title");
const connectionDialogCopy = document.querySelector("#connection-dialog-copy");
const providerServiceHint = document.querySelector("#provider-service-hint");
const checkConnectionDialog = document.querySelector("#check-connection-dialog");
const checkConnectionName = document.querySelector("#check-connection-name");
const confirmCheckConnection = document.querySelector("#confirm-check-connection");
const deleteConnectionDialog = document.querySelector("#delete-connection-dialog");
const deleteConnectionForm = document.querySelector("#delete-connection-form");
const deleteConnectionName = document.querySelector("#delete-connection-name");
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
const settingsSummaryOutput = document.querySelector("#settings-summary-output");
const settingsSummaryLicense = document.querySelector("#settings-summary-license");
const settingsSummaryFavorites = document.querySelector("#settings-summary-favorites");
const settingsSummaryInterval = document.querySelector("#settings-summary-interval");
let windowsVoicesLoaded = false;
let pendingDeleteItemRef = null;
const stateLabel = {
  materials_ready: "材料已准备",
  waiting_setup: "等待设置",
  waiting_authorization: "等待付费许可",
  queued: "等待整理",
  organizing: "正在整理",
  partial_ready: "笔记已就绪，音频待完成",
  needs_action: "需要你处理",
  ready: "可阅读",
};
const learningActionKinds = new Set(["open_note", "open_settings", "open_automation", "open_sources", "open_single_video", "continue", "start_automation", "retry_automation", "resume_task"]);
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
let activeFavoriteFolderId = "all";
let latestFavoritesSnapshot = { synced_at: null, folders: [], items: [] };
let suggestedProviderConnectionName = "";
let latestProviderSettings = { connections: [], adapters: [], roles: {}, readiness: null };
let pendingCheckConnection = null;
let pendingDeleteConnection = null;
let providerSettingsMutationRevision = 0;
const dirtySettingsForms = new Set();
let currentSnapshot = { inbox: [], processing: [], library: [] };
let currentTrash = [];
let selectedItemRef = null;
let activeTaskFilter = "all";
let activeTaskQuery = "";
let noticeTimer = null;
let lastAutomationStatus = { configured: false, enabled: false };
const providerConnectionNameDefaults = {
  "windows-tts": "windows-tts",
  "local-asr": "local-asr",
  "local-ocr": "local-ocr",
};
const providerCapabilityDefinitions = {
  asr: { icon: "ASR", title: "ASR · 语音识别", copy: "从视频中提取语音内容，新任务只使用一个当前连接。", empty: "当前使用内置 faster-whisper large-v3；添加服务后可以显式绑定。" },
  ocr: { icon: "OCR", title: "OCR · 画面文字", copy: "识别视频画面中的文字内容，新任务只使用一个当前连接。", empty: "当前使用内置 PaddleOCR；添加服务后可以显式绑定。" },
  llm: { icon: "LLM", title: "LLM · 内容生成", copy: "连接是通用资源；Writer、Reviewer、Podcast 可以分别选择兼容连接。", empty: "还没有 LLM 连接。添加 MiMo 或 DeepSeek 后再分配职责。" },
  tts: { icon: "TTS", title: "TTS · 语音合成", copy: "将播客稿转换为音频，新任务只使用一个当前语音连接。", empty: "还没有语音连接。可以添加 Windows 系统语音或 MiMo TTS。" },
};
const providerRoleCopy = {
  "笔记 Writer": "生成完整笔记初稿",
  "笔记 Reviewer": "复核内容和证据约束",
  "播客": "生成播客稿与 speech.txt",
};

function say(message) {
  window.clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.hidden = false;
  noticeTimer = window.setTimeout(() => { notice.hidden = true; }, 4200);
}
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

function renderProviderSettings(settings) {
  latestProviderSettings = settings;
  const configured = settings.connections.length;
  const capabilities = Object.keys(providerCapabilityDefinitions);
  const readyCapabilities = capabilities.filter((capability) => providerCapabilityIsReady(capability, settings.roles?.[capability] || []));
  providerState.textContent = readyCapabilities.length === capabilities.length ? "全部已准备" : `已配置 ${configured} 个连接`;
  providerState.className = `status-pill ${readyCapabilities.length === capabilities.length ? "connected" : ""}`;
  providerReadyCount.textContent = `${readyCapabilities.length} / ${capabilities.length}`;
  document.querySelector(".capability-overview").classList.toggle("is-ready", readyCapabilities.length === capabilities.length);
  providerReadyBadges.innerHTML = capabilities.map((capability) => {
    const ready = readyCapabilities.includes(capability);
    return `<span>${capability.toUpperCase()} ${ready ? "已配置" : "待配置"}</span>`;
  }).join("");
  providerCapabilityList.innerHTML = capabilities.map((capability) => renderProviderCapabilityCard(capability, settings)).join("");
  providerCapabilityList.querySelectorAll("button[data-check-connection]").forEach((button) => button.addEventListener("click", () => requestProviderConnectionCheck(button)));
  providerCapabilityList.querySelectorAll("button[data-delete-connection]:not(:disabled)").forEach((button) => button.addEventListener("click", () => openDeleteProviderConnection(button)));
  providerCapabilityList.querySelectorAll("button[data-use-connection]").forEach((button) => button.addEventListener("click", () => setCurrentProviderConnection(button)));
  providerCapabilityList.querySelectorAll("button[data-add-capability]").forEach((button) => button.addEventListener("click", () => openConnectionDialog(button.dataset.addCapability)));
  providerCapabilityList.querySelectorAll("select[data-setup-role-select]").forEach((select) => select.addEventListener("change", () => saveProviderRoleSelection(select)));
  renderProviderLimits(settings.limits);
  renderSetupReadiness(settings.readiness);
}

function providerCapabilityIsReady(capability, roles) {
  if (capability === "llm") return roles.length === 3 && roles.every((role) => role.connection && ["连接配置可读取", "本地配置可读取"].includes(role.state));
  const role = roles[0];
  if (!role) return false;
  return ["连接配置可读取", "本地配置可读取"].includes(role.state)
    || (["asr", "ocr"].includes(capability) && role.state === "使用内置本地能力");
}

function providerLogo(item) {
  if (item.provider === "MiMo" || item.provider === "MiMo TTS") return "MiMo";
  if (item.provider === "DeepSeek") return "DS";
  if (item.provider === "Windows 系统语音") return "WIN";
  if (item.capability === "asr") return "FW";
  if (item.capability === "ocr") return "OCR";
  return item.capability.toUpperCase();
}

function renderProviderConnectionRow(item, capability, role) {
  const current = item.bound_roles.length > 0;
  const model = item.voice || item.model || item.provider;
  const roleBadges = item.bound_roles.map((label) => `<span class="provider-badge is-role">${escapeHtml(label.replace("笔记 ", ""))}</span>`).join("");
  const localBadge = item.local ? '<span class="provider-badge is-local">本地</span>' : "";
  const useButton = capability === "llm"
    ? ""
    : `<button class="provider-use-button" type="button" data-use-connection="${escapeHtml(item.name)}" data-use-role="${escapeHtml(role?.name || "")}"${current ? " disabled" : ""}>${current ? "使用中" : "设为当前"}</button>`;
  const deleteTitle = item.deletable ? `删除连接 ${item.name}` : item.delete_reason;
  return `<section class="provider-row${current ? " is-current" : ""}" data-connection="${escapeHtml(item.name)}">
    <span class="provider-logo">${escapeHtml(providerLogo(item))}</span>
    <span class="provider-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.provider)} · ${escapeHtml(item.state)}</span></span>
    <span class="provider-model"><strong>${escapeHtml(model)}</strong><span class="provider-badges">${roleBadges}${localBadge}</span></span>
    <span class="provider-actions"><button type="button" data-check-connection="${escapeHtml(item.name)}">检查连接</button>${useButton}<button class="provider-delete-button" type="button" data-delete-connection="${escapeHtml(item.name)}" aria-label="${escapeHtml(deleteTitle)}" title="${escapeHtml(deleteTitle)}"${item.deletable ? "" : " disabled"}>删除</button></span>
  </section>`;
}

function renderLlmRoleAssignment(roles) {
  const bound = roles.filter((role) => role.connection).length;
  const rows = roles.map((role) => {
    const options = role.options.map((item) => `<option value="${escapeHtml(item.name)}"${item.name === role.connection ? " selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.provider)}</option>`).join("");
    const attention = !role.connection || !["连接配置可读取", "本地配置可读取"].includes(role.state);
    return `<label class="llm-role-row"><span class="llm-role-copy"><strong>${escapeHtml(role.name)}</strong><small>${escapeHtml(providerRoleCopy[role.name] || "选择新任务使用的连接")}</small></span><select data-setup-role-select="${escapeHtml(role.name)}" data-current-connection="${escapeHtml(role.connection || "")}" aria-label="为${escapeHtml(role.name)}选择连接"><option value="">暂不绑定</option>${options}</select><span class="llm-role-state${attention ? " is-attention" : ""}"><strong>${attention ? "等待绑定" : "已绑定"}</strong>${escapeHtml(role.hint || role.state)}</span></label>`;
  }).join("");
  return `<section class="llm-role-assignment" id="setup-readiness" aria-label="LLM 职责分配"><div class="llm-role-heading"><div><h4>职责分配</h4><p>这是 LLM 独有的子项；保存后只影响尚未开始的新任务。</p></div><span>${bound} / ${roles.length} 已绑定</span></div>${rows}</section>`;
}

function renderProviderCapabilityCard(capability, settings) {
  const definition = providerCapabilityDefinitions[capability];
  const roles = settings.roles?.[capability] || [];
  const connections = settings.connections.filter((item) => item.capability === capability);
  const ready = providerCapabilityIsReady(capability, roles);
  const state = capability === "llm" ? `${roles.filter((role) => role.connection).length} 个职责已绑定` : ready ? "已就绪" : "等待选择";
  const rows = connections.length
    ? connections.map((item) => renderProviderConnectionRow(item, capability, roles[0])).join("")
    : `<p class="provider-capability-empty">${escapeHtml(definition.empty)}</p>`;
  const assignment = capability === "llm" ? renderLlmRoleAssignment(roles) : "";
  const footer = capability === "llm" ? `${connections.length} 个 LLM 连接` : roles[0]?.connection ? `当前：${roles[0].connection}` : roles[0]?.state || "尚未选择连接";
  return `<article class="provider-capability-card" data-capability="${capability}"><header class="provider-capability-header"><div class="provider-capability-identity"><span class="provider-capability-icon">${definition.icon}</span><div><div class="provider-capability-title"><h3>${definition.title}</h3><span class="provider-card-state${ready ? "" : " is-attention"}">${escapeHtml(state)}</span></div><p class="provider-capability-copy">${definition.copy}</p></div></div><button class="add-capability-button" type="button" data-add-capability="${capability}" aria-label="添加 ${capability.toUpperCase()} 服务"><strong aria-hidden="true">＋</strong><span>添加服务</span></button></header><p class="provider-list-label">服务连接</p><div class="provider-connection-list">${rows}</div>${assignment}<footer class="capability-card-footer">${escapeHtml(footer)}</footer></article>`;
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
  const ready = readiness.state === "可以开始整理";
  settingsReadiness.classList.toggle("is-ready", ready);
  settingsReadinessTitle.textContent = ready ? "当前可以完整产出" : readiness.state;
  settingsReadinessCopy.textContent = readiness.message;
  singleVideoReadiness.textContent = readiness.message;
}

async function saveProviderRoleSelection(select) {
  const label = select.dataset.setupRoleSelect;
  const previous = select.dataset.currentConnection || "";
  const next = select.value;
  dirtySettingsForms.add(providerCapabilityList);
  select.disabled = true;
  try {
    const settings = next
      ? await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "POST", body: JSON.stringify({ connection_name: next }) })
      : await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "DELETE" });
    dirtySettingsForms.delete(providerCapabilityList);
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = next ? `已更新${label}使用的连接；付费整理许可需要重新确认。` : `已解除${label}的连接；补齐职责后才能完整产出。`;
    say(providerFeedback.textContent);
  } catch (error) {
    select.value = previous;
    dirtySettingsForms.delete(providerCapabilityList);
    showProviderUpdateFailure(error);
  } finally {
    select.disabled = false;
  }
}

async function setCurrentProviderConnection(button) {
  const label = button.dataset.useRole;
  const connectionName = button.dataset.useConnection;
  const buttonLabel = button.textContent;
  button.disabled = true;
  button.textContent = "切换中…";
  try {
    const settings = await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "POST", body: JSON.stringify({ connection_name: connectionName }) });
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = `已将${connectionName}设为${label}的当前连接；付费整理许可需要重新确认。`;
    say(providerFeedback.textContent);
  } catch (error) { showProviderUpdateFailure(error); } finally { button.disabled = false; button.textContent = buttonLabel; }
}

function requestProviderConnectionCheck(button) {
  const name = button.dataset.checkConnection;
  const connection = latestProviderSettings.connections.find((item) => item.name === name);
  if (!connection) return;
  if (connection.local) {
    runProviderConnectionCheck(button);
    return;
  }
  pendingCheckConnection = name;
  checkConnectionName.textContent = `${connection.name}（${connection.provider}）`;
  checkConnectionDialog.showModal();
  window.requestAnimationFrame(() => confirmCheckConnection.focus({ preventScroll: true }));
}

async function runProviderConnectionCheck(button, confirmPaid = false) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "检测中…";
  try {
    const request = { method: "POST" };
    if (confirmPaid) request.body = JSON.stringify({ confirm_paid: true });
    const result = await api(`/api/providers/connections/${encodeURIComponent(button.dataset.checkConnection)}/check`, request);
    providerFeedback.textContent = result.message;
    say(result.message);
  } catch (error) {
    providerFeedback.textContent = error.message;
    say(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function confirmProviderConnectionCheck() {
  if (!pendingCheckConnection) return;
  const name = pendingCheckConnection;
  const button = providerCapabilityList.querySelector(`button[data-check-connection="${CSS.escape(name)}"]`);
  checkConnectionDialog.close();
  if (button) await runProviderConnectionCheck(button, true);
}

function openDeleteProviderConnection(button) {
  pendingDeleteConnection = button.dataset.deleteConnection;
  deleteConnectionName.textContent = pendingDeleteConnection;
  deleteConnectionDialog.showModal();
  window.requestAnimationFrame(() => document.querySelector("[data-close-delete-connection]")?.focus({ preventScroll: true }));
}

async function deleteProviderConnection(event) {
  event.preventDefault();
  if (!pendingDeleteConnection) return;
  const name = pendingDeleteConnection;
  const button = event.submitter || document.querySelector("#confirm-delete-connection");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "删除中…";
  providerState.textContent = "删除中";
  try {
    const settings = await api(`/api/providers/connections/${encodeURIComponent(name)}`, { method: "DELETE" });
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    await loadAutomationStatus();
    providerFeedback.textContent = `已删除连接：${name}。如付费整理许可此前有效，请重新确认。`;
    say(providerFeedback.textContent);
    deleteConnectionDialog.close();
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
    const mutationRevision = providerSettingsMutationRevision;
    const settings = await api(`/api/providers/settings${defaultOutput ? `?default_output=${encodeURIComponent(defaultOutput)}` : ""}`);
    if (protectDirty && mutationRevision !== providerSettingsMutationRevision) return;
    if (protectDirty && (settingsFormNeedsProtection(providerForm) || settingsFormNeedsProtection(providerCapabilityList))) return;
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
  if (!settingsFormNeedsProtection(providerForm) && !settingsFormNeedsProtection(providerCapabilityList) && !settingsFormNeedsProtection(providerLimitsForm)) await loadProviderSettings(automationForm.elements.default_output.value, true);
}

function renderList(target, items, empty) {
  if (!items.length) {
    target.innerHTML = empty ? `<p class="empty">${escapeHtml(empty)}</p>` : "";
    return;
  }
  target.innerHTML = items.map((item) => {
    const progress = taskProgress(item);
    const output = item.output_goal === "complete_note_with_audio" ? "笔记 + 音频" : "完整笔记";
    return `
    <article class="learning-row" data-item-ref="${escapeHtml(item.item_ref)}" data-state="${escapeHtml(item.state)}" data-filter="${taskFilterFor(item)}" tabindex="0">
      <div class="learning-copy">
        <h3>${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.source)}</p>
        <small>ID · ${escapeHtml(item.item_ref)}</small>
      </div>
      <div class="row-progress">
        <span class="progress-ring" aria-label="进度 ${progress}%" style="--task-progress:${progress}%"><strong>${progress}</strong><small>%</small></span>
        <div class="progress-copy"><span class="learning-state">${escapeHtml(publicStateLabel(item))}</span><span class="task-progress" aria-hidden="true" style="--task-progress:${progress}%"><i></i></span></div>
      </div>
      <span class="row-output">${output}</span>
      <span class="row-next">${escapeHtml(item.message)}</span>
      <button class="open-task-detail" type="button" data-open-item-ref="${escapeHtml(item.item_ref)}" aria-label="查看 ${escapeHtml(item.title)}">查看 <span aria-hidden="true">→</span></button>
    </article>`;
  }).join("");
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

function publicStateLabel(item) {
  if (item.manually_paused) return "已暂停";
  if (item.failure_reason) return "处理已停止";
  return stateLabel[item.state] || "需要检查";
}

function renderRecentActivity(items) {
  const recent = items.slice(0, 3);
  recentActivity.innerHTML = recent.length
    ? recent.map((item) => `
      <div class="activity-item" data-filter="${taskFilterFor(item)}">
        <i aria-hidden="true"></i>
        <div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(publicStateLabel(item))} · ${escapeHtml(item.message)}</span></div>
      </div>`).join("")
    : '<p class="empty">还没有任务动态。</p>';
}

function renderTaskSystemStatus(status) {
  const paidAuthorized = Boolean(status.paid_authorized ?? status.enabled);
  const autoFavorites = Boolean(status.auto_new_favorites_active);
  const interval = Number(status.check_interval_minutes || 30);
  const output = status.default_output === "complete_note" ? "完整笔记" : "笔记 + 音频";
  const rows = [
    ["默认成品", output, Boolean(status.configured)],
    ["付费整理", paidAuthorized ? "许可有效" : "尚未许可", paidAuthorized],
    ["新收藏", autoFavorites ? "自动加入" : "仅手动加入", autoFavorites],
    ["自动检查", `每 ${interval} 分钟`, Boolean(status.configured)],
  ];
  taskSystemStatus.innerHTML = rows.map(([title, copy, ready]) => `
    <div class="system-status-row ${ready ? "is-ready" : ""}"><i aria-hidden="true"></i><div><strong>${title}</strong><span>${copy}</span></div></div>`).join("");
}

function renderTrash(items) {
  currentTrash = items;
  document.querySelector('[data-filter-count="trash"]').textContent = items.length;
  if (!items.length) {
    trashList.innerHTML = '<p class="empty">回收站为空。移入这里的任务会保留，直到恢复。</p>';
    return;
  }
  trashList.innerHTML = items.map((item) => `
    <article class="learning-row trash-row" data-trash-bundle="${escapeHtml(item.bundle_id)}" data-filter="trash">
      <div class="learning-copy"><h3>${escapeHtml(item.title)}</h3><p>任务已从当前列表移出</p></div>
      <div class="row-progress"><span class="progress-ring" aria-hidden="true" style="--task-progress:0%"><strong>0</strong><small>%</small></span><div class="progress-copy"><span class="learning-state">回收区</span><span class="task-progress" aria-hidden="true"><i></i></span></div></div>
      <span class="row-output">任务记录</span>
      <span class="row-next">移入时间：${escapeHtml(formatSyncTime(item.trashed_at))}</span>
      <button class="open-task-detail" type="button" data-restore-bundle="${escapeHtml(item.bundle_id)}">恢复任务</button>
    </article>`).join("");
  trashList.querySelectorAll("button[data-restore-bundle]").forEach((button) => button.addEventListener("click", () => restoreTrashItem(button.dataset.restoreBundle)));
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
    paused: items.filter((item) => taskFilterFor(item) === "paused").length,
    queued: items.filter((item) => taskFilterFor(item) === "queued").length,
    completed: items.filter((item) => taskFilterFor(item) === "completed").length,
    trash: currentTrash.length,
  };
  for (const [name, count] of Object.entries(counts)) document.querySelector(`[data-filter-count="${name}"]`).textContent = count;
  taskCount.textContent = counts.all;
  taskSummary.innerHTML = `<strong>${counts.processing} 项正在处理</strong>，${counts.paused} 项已暂停，${counts.attention} 项需要你处理，${counts.completed} 项已完成。`;
  taskTableCount.textContent = `共 ${counts.all} 项`;
  renderRecentActivity(items);
  renderActiveTaskFocus(items);
  applyTaskFilter();
  if (selectedItemRef) renderTaskDetail(items.find((item) => item.item_ref === selectedItemRef));
}

function renderActiveTaskFocus(items = allLearningItems()) {
  if (activeTaskFilter === "trash") {
    taskFocus.className = `task-focus ${currentTrash.length ? "attention" : "is-empty"}`;
    taskFocus.innerHTML = currentTrash.length
      ? `<div class="focus-copy"><p class="panel-kicker">任务生命周期</p><h2>回收站有 ${currentTrash.length} 项任务</h2><p>恢复会把原任务事实和已生成内容放回当前任务列表，不覆盖已有任务。</p></div>`
      : '<div class="focus-copy"><p class="panel-kicker">任务生命周期</p><h2>回收站为空</h2><p>移入回收区的任务会显示在这里，并可安全恢复。</p></div>';
    workbenchNote.hidden = true;
    return;
  }
  workbenchNote.hidden = !items.length;
  renderTaskFocus(items);
}

function renderTaskFocus(items) {
  const priority = { attention: 0, paused: 1, processing: 2, queued: 3, completed: 4 };
  const item = [...items].sort((left, right) => priority[taskFilterFor(left)] - priority[taskFilterFor(right)])[0];
  if (!item) {
    taskFocus.className = "task-focus is-empty";
    const filtering = activeTaskFilter !== "all" || Boolean(activeTaskQuery);
    taskFocus.innerHTML = filtering
      ? '<div class="focus-copy"><p class="panel-kicker">当前视图</p><h2>没有匹配的任务</h2><p>清除搜索或切换任务状态，可以查看其他任务。</p></div>'
      : `<div class="focus-copy"><p class="panel-kicker">工作台已准备好</p><h2>从“来源”添加第一项内容</h2><p>本地视频、公开链接和收藏进入任务后，进度与问题会持续保留在这里。</p></div><button class="focus-open" type="button">打开来源 <span aria-hidden="true">→</span></button>`;
    taskFocus.querySelector("button")?.addEventListener("click", () => showView("sources"));
    return;
  }
  const filter = taskFilterFor(item);
  const heading = filter === "attention" ? "这项任务需要你处理" : filter === "paused" ? "这项任务已暂停" : filter === "processing" ? "这项任务正在向前推进" : filter === "queued" ? "下一项等待整理的内容" : "最近完成的内容";
  taskFocus.className = `task-focus ${filter}`;
  taskFocus.innerHTML = `<div class="focus-signal" aria-hidden="true"><span>${taskProgress(item)}</span><small>%</small></div><div class="focus-copy"><p class="panel-kicker">${heading}</p><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.message)}</p></div><button class="focus-open" type="button">查看任务 <span aria-hidden="true">→</span></button>`;
  taskFocus.querySelector("button").addEventListener("click", () => selectTask(item.item_ref));
}

function allLearningItems() {
  return [...currentSnapshot.processing, ...currentSnapshot.inbox, ...currentSnapshot.library];
}

function taskFilterFor(item) {
  if (item.manually_paused) return "paused";
  if (["needs_action", "waiting_setup", "waiting_authorization"].includes(item.state)) return "attention";
  if (["organizing", "partial_ready"].includes(item.state)) return "processing";
  if (item.state === "ready") return "completed";
  return "queued";
}

function taskProgress(item) {
  const failureProgress = {
    source: 0,
    transcript: 24,
    frames: 24,
    ocr: 24,
    evidence: 24,
    content_pack: 24,
    note: 58,
    publish: 58,
    podcast_script: 78,
    tts: 78,
  };
  if (item.failure_stage_code && item.failure_stage_code in failureProgress) return failureProgress[item.failure_stage_code];
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
  const showingTrash = activeTaskFilter === "trash";
  Object.values(lists).forEach((list) => { list.hidden = showingTrash; });
  trashList.hidden = !showingTrash;
  const rows = [...taskList.querySelectorAll("article[data-filter]")];
  rows.forEach((row) => {
    const isTrash = row.dataset.filter === "trash";
    const filterMatches = showingTrash
      ? isTrash
      : !isTrash && (activeTaskFilter === "all" || row.dataset.filter === activeTaskFilter);
    const queryMatches = !activeTaskQuery || row.textContent.toLocaleLowerCase("zh-CN").includes(activeTaskQuery);
    row.hidden = !filterMatches || !queryMatches;
  });
  const label = document.querySelector(`[data-filter="${activeTaskFilter}"] span`)?.textContent?.trim() || "全部任务";
  taskListTitle.textContent = label;
  const visibleCount = rows.filter((row) => !row.hidden).length;
  const total = showingTrash ? currentTrash.length : allLearningItems().length;
  taskFilterEmpty.hidden = visibleCount !== 0 || total === 0;
  taskTableCount.textContent = activeTaskQuery || activeTaskFilter !== "all" ? `显示 ${visibleCount} / ${total} 项` : `共 ${total} 项`;
  const focusItems = allLearningItems().filter((item) => {
    if (showingTrash) return false;
    const filterMatches = activeTaskFilter === "all" || taskFilterFor(item) === activeTaskFilter;
    const searchable = `${item.title} ${item.source} ${item.message} ${publicStateLabel(item)}`.toLocaleLowerCase("zh-CN");
    return filterMatches && (!activeTaskQuery || searchable.includes(activeTaskQuery));
  });
  renderActiveTaskFocus(focusItems);
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
  const failedCompletedSteps = {
    source: 0,
    transcript: 1,
    frames: 1,
    ocr: 1,
    evidence: 1,
    content_pack: 1,
    note: 2,
    publish: 2,
    podcast_script: 3,
    tts: 3,
  };
  if (item.failure_stage_code && item.failure_stage_code in failedCompletedSteps) completed = failedCompletedSteps[item.failure_stage_code];
  else {
    if (["materials_ready", "waiting_setup", "waiting_authorization", "queued", "organizing", "partial_ready", "needs_action", "ready"].includes(item.state)) completed = 2;
    if (["partial_ready", "ready"].includes(item.state) || item.note_href) completed = 3;
    if (item.state === "ready") completed = labels.length;
  }
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
  const currentStateLabel = publicStateLabel(item);
  const pauseControl = item.can_pause
    ? `<button class="pause-link" type="button" data-pause-item-ref="${escapeHtml(item.item_ref)}" title="只暂停后续调度；已经开始的处理会继续完成">暂停任务</button>`
    : "";
  const failureExplanation = item.failure_reason
    ? `<section class="failure-explanation" aria-label="失败详情"><p>失败阶段</p><strong>${escapeHtml(item.failure_stage || "处理内容")}</strong><p>具体原因</p><strong>${escapeHtml(item.failure_reason)}</strong></section>`
    : "";
  taskDetail.innerHTML = `
    <div class="detail-layout">
      <div class="detail-main">
        <div class="detail-heading"><div><p class="panel-kicker">当前任务</p><h2>${escapeHtml(item.title)}</h2><p class="detail-source">${escapeHtml(item.source)}</p></div><span class="detail-status ${escapeHtml(item.manually_paused ? "paused" : item.state)}">${escapeHtml(currentStateLabel)}</span></div>
        <p class="detail-message">${escapeHtml(item.message)}</p>
        ${failureExplanation}
        <section class="production-section"><div class="production-heading"><h3>产出轨道</h3><span>${percent}%</span></div><div class="production-track" style="--track-steps:${steps.length}">${steps.map((step) => `<span class="track-step${step.done ? " is-done" : ""}${step.current ? " is-current" : ""}"><i>${step.done ? "✓" : ""}</i><strong>${step.label}</strong></span>`).join("")}</div></section>
      </div>
      <aside class="detail-action"><p class="panel-kicker">${actionHeading}</p><strong>${escapeHtml(currentStateLabel)}</strong><p>${escapeHtml(actionCopy)}</p><div class="detail-action-controls">${item.action && learningActionKinds.has(item.action_kind) ? `<button class="detail-primary-action" type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="${escapeHtml(item.action_kind)}">${escapeHtml(item.action)}</button>` : ""}${pauseControl}<button class="danger-link" type="button" data-delete-item-ref="${escapeHtml(item.item_ref)}"${item.state === "organizing" && !item.manually_paused ? ' disabled title="当前步骤仍在完成，结束后可删除"' : ""}>删除任务</button></div>${item.audio_href ? `<audio controls preload="metadata" src="${escapeHtml(item.audio_href)}">音频暂时不能播放。</audio>` : ""}</aside>
    </div>`;
  taskDetail.querySelectorAll("button[data-item-ref]").forEach((button) => button.addEventListener("click", () => actOnItem(button.dataset.itemRef, button.dataset.action)));
  taskDetail.querySelector("button[data-pause-item-ref]")?.addEventListener("click", () => pauseItem(item.item_ref));
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
  latestFavoritesSnapshot = snapshot;
  const availableIds = new Set(snapshot.items.map((item) => item.aweme_id));
  selectedFavoriteIdsState = new Set(
    [...selectedFavoriteIdsState].filter((itemId) => availableIds.has(itemId)),
  );
  const folders = Array.isArray(snapshot.folders) && snapshot.folders.length
    ? snapshot.folders
    : [{ folder_id: "default", name: "默认收藏夹", item_count: snapshot.items.length }];
  if (activeFavoriteFolderId !== "all" && !folders.some((folder) => folder.folder_id === activeFavoriteFolderId)) {
    activeFavoriteFolderId = "all";
  }
  const folderOptions = [
    { folder_id: "all", name: "全部收藏", item_count: snapshot.items.length },
    ...folders,
  ];
  favoriteFolders.innerHTML = folderOptions.map((folder) => `
    <button type="button" data-favorite-folder="${escapeHtml(folder.folder_id)}" class="${folder.folder_id === activeFavoriteFolderId ? "is-active" : ""}">
      <span>${escapeHtml(folder.name)}</span><small>${escapeHtml(String(folder.item_count))}</small>
    </button>`).join("");
  favoriteFolders.querySelectorAll("button[data-favorite-folder]").forEach((button) => button.addEventListener("click", () => {
    activeFavoriteFolderId = button.dataset.favoriteFolder;
    renderFavorites(latestFavoritesSnapshot);
  }));
  const selectedFolder = folderOptions.find((folder) => folder.folder_id === activeFavoriteFolderId);
  favoriteFolderTitle.textContent = selectedFolder?.name || "全部收藏";
  const visibleItems = activeFavoriteFolderId === "all"
    ? snapshot.items
    : snapshot.items.filter((item) => (item.folder_ids || ["default"]).includes(activeFavoriteFolderId));
  if (!snapshot.items.length) {
    favoriteList.innerHTML = `<p class="empty">还没有同步的抖音收藏。</p>`;
    if (!favoriteStatusProtected) favoriteStatus.textContent = "连接抖音后，收藏会出现在这里。";
    updateFavoriteSelection();
    return;
  }
  if (!favoriteStatusProtected) favoriteStatus.textContent = `最近同步：${formatSyncTime(snapshot.synced_at)}`;
  if (!visibleItems.length) {
    favoriteList.innerHTML = `<p class="empty">这个收藏夹还没有作品。</p>`;
    updateFavoriteSelection();
    return;
  }
  favoriteList.innerHTML = visibleItems.map((item) => {
    const thumbnail = item.thumbnail_path
      ? `<img src="${thumbnailHref(item.thumbnail_path)}" alt="" loading="lazy" />`
      : `<div class="thumbnail-placeholder" aria-label="封面暂不可用">封面暂不可用</div>`;
    return `
      <article class="favorite-card">
        <div class="favorite-media">${thumbnail}<label class="favorite-select"><input type="checkbox" value="${escapeHtml(item.aweme_id)}"${selectedFavoriteIdsState.has(item.aweme_id) ? " checked" : ""} /> 选择</label></div>
        <div class="favorite-card-copy">
          <h3 class="favorite-title">${escapeHtml(item.title)}</h3>
          <div class="favorite-card-meta"><p class="favorite-time">${escapeHtml(formatSyncTime(item.synced_at))}</p><a class="favorite-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener">打开作品</a></div>
        </div>
      </article>`;
  }).join("");
  favoriteList.querySelectorAll("input[type=checkbox]").forEach((checkbox) => checkbox.addEventListener("change", () => {
    if (checkbox.checked) selectedFavoriteIdsState.add(checkbox.value);
    else selectedFavoriteIdsState.delete(checkbox.value);
    updateFavoriteSelection();
  }));
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
  const selected = selectedFavoriteIds();
  addSelectedFavoritesButton.disabled = selected.length === 0;
  favoriteSelection.textContent = selected.length ? `已选 ${selected.length} 项，切换收藏夹不会丢失选择。` : "可跨收藏夹多选；首次同步不会自动处理。";
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
  renderTaskSystemStatus(status);
  const paidAuthorized = status.paid_authorized ?? status.enabled;
  const autoNewFavoritesEnabled = Boolean(status.auto_new_favorites_enabled ?? status.auto_organize_new_favorites);
  const autoNewFavoritesActive = Boolean(status.auto_new_favorites_active);
  automationState.textContent = paidAuthorized ? "付费许可已开启" : status.needs_authorization ? "需要重新确认付费许可" : status.configured ? "等待付费许可" : "等待设置";
  automationState.className = `status-pill ${paidAuthorized ? "connected" : ""}`;
  const output = status.default_output || "complete_note_with_audio";
  const outputLabel = output === "complete_note" ? "完整笔记" : "笔记 + 音频";
  defaultOutputLabel.textContent = outputLabel;
  singleVideoOutput.textContent = outputLabel;
  settingsSummaryOutput.textContent = outputLabel;
  settingsSummaryLicense.textContent = paidAuthorized ? "许可有效" : status.needs_authorization ? "需要重新确认" : "尚未许可";
  settingsSummaryFavorites.textContent = autoNewFavoritesEnabled ? "自动加入" : "仅手动加入";
  settingsSummaryInterval.textContent = `每 ${Number(status.check_interval_minutes || 30)} 分钟`;
  runtimeState.classList.toggle("is-on", paidAuthorized);
  runtimeTitle.textContent = paidAuthorized ? "付费整理许可已开启" : "付费整理许可未开启";
  runtimeCopy.textContent = paidAuthorized
    ? autoNewFavoritesActive ? "手动任务可继续；新收藏也会自动加入" : "手动任务可继续；新收藏不会自动加入"
    : "任务可以先完成材料，付费阶段会等待许可";
  automationSummary.textContent = paidAuthorized
    ? autoNewFavoritesActive
      ? `手动任务会复用当前付费许可；自动加入新收藏已开启。默认生成${status.default_output === "complete_note" ? "完整笔记" : "完整笔记和播客音频"}。`
      : `手动任务会复用当前付费许可；自动加入新收藏当前关闭。默认生成${status.default_output === "complete_note" ? "完整笔记" : "完整笔记和播客音频"}。`
    : status.needs_authorization
      ? "模型、职责、额度或产出设置已经变化。检查后重新确认付费许可，手动任务才会继续。"
      : status.configured
        ? "设置已保存。开启付费许可后，手动任务可以继续；自动加入新收藏由独立开关控制。"
        : "先保存默认产出并完成所需连接，再决定是否允许付费整理。";
  const accessState = paidAuthorized ? "enabled" : status.needs_authorization ? "attention" : status.configured ? "ready" : "setup";
  automationAccess.dataset.state = accessState;
  automationAccessTitle.textContent = paidAuthorized ? "付费整理许可有效" : status.needs_authorization ? "设置已变化，需要重新确认" : status.configured ? "设置已保存，付费许可未开启" : "先完成设置";
  automationAccessCopy.textContent = paidAuthorized
    ? `这项许可由语栖持久保存；手动任务无需重复确认。${autoNewFavoritesEnabled ? "自动加入新收藏开关已开启。" : "自动加入新收藏仍是关闭状态。"}实质设置变化时许可会自动失效。`
    : status.needs_authorization
      ? "已有任务和材料不会丢失。确认当前设置后，可以重新开启付费整理许可。"
      : status.configured
        ? "只有明确允许付费整理后，语栖才会推进需要 Provider 的阶段；自动加入新收藏由独立开关控制。"
        : "保存默认产出并完成模型职责绑定后，这里会提供明确的开启操作。";
  authorizeAutomationButton.hidden = paidAuthorized;
  authorizeAutomationButton.disabled = !status.configured || paidAuthorized;
  authorizeAutomationButton.textContent = status.needs_authorization ? "查看变化并重新确认" : "允许付费整理";
  disableAutomationButton.hidden = !paidAuthorized;
  if (status.configured && !settingsFormNeedsProtection(automationForm)) {
    automationForm.elements.default_output.value = status.default_output;
    automationForm.elements.check_interval_minutes.value = status.check_interval_minutes;
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

function populateProviderPresetOptions(capability, preset = null) {
  const adapters = (latestProviderSettings.adapters || []).filter((adapter) => adapter.capability_key === capability);
  const existingPresets = new Set((latestProviderSettings.connections || []).filter((item) => item.capability === capability).map((item) => item.preset));
  const options = adapters.map((adapter) => {
    const alreadyAdded = capability !== "llm" && existingPresets.has(adapter.preset);
    return `<option value="${escapeHtml(adapter.preset)}"${alreadyAdded ? " disabled" : ""}>${escapeHtml(adapter.name)}${adapter.local ? "（本地）" : ""}${alreadyAdded ? " · 已添加" : ""}</option>`;
  });
  providerForm.elements.preset.innerHTML = options.length ? options.join("") : '<option value="">当前没有可添加的服务</option>';
  const selectable = adapters.find((adapter) => capability === "llm" || !existingPresets.has(adapter.preset));
  const requested = adapters.find((adapter) => adapter.preset === preset && (capability === "llm" || !existingPresets.has(adapter.preset)));
  providerForm.elements.preset.value = (requested || selectable)?.preset || "";
  providerForm.querySelector('button[type="submit"]').disabled = !providerForm.elements.preset.value;
  providerServiceHint.textContent = selectable ? "保存后，这个连接会出现在当前能力卡中。" : "当前支持的服务都已添加；请先使用现有连接。";
}

function openConnectionDialog(capability = "llm", preset = null) {
  providerForm.reset();
  suggestedProviderConnectionName = "";
  connectionDialogTitle.textContent = `添加 ${capability.toUpperCase()} 服务`;
  connectionDialogCopy.textContent = capability === "llm" ? "创建连接后，再把它分配给 Writer、Reviewer 或 Podcast。" : "这里只显示当前能力真正支持的服务。";
  populateProviderPresetOptions(capability, preset);
  suggestProviderConnectionName();
  windowsVoicesLoaded = false;
  refreshProviderConnectionFields().catch((error) => say(error.message));
  connectionDialog.showModal();
  window.requestAnimationFrame(() => providerForm.elements.name.focus());
}

async function loadAutomationStatus() { try { renderAutomationStatus(await api("/api/automation/status")); } catch (error) { automationState.textContent = "无法读取"; } }

function openAutomationAuthorization() {
  const paidAuthorized = lastAutomationStatus.paid_authorized ?? lastAutomationStatus.enabled;
  if (!lastAutomationStatus.configured || paidAuthorized) return;
  const output = lastAutomationStatus.default_output === "complete_note" ? "完整笔记" : "完整笔记 + 播客音频";
  authorizationOutput.textContent = output;
  authorizationInterval.textContent = lastAutomationStatus.auto_new_favorites_enabled
    ? "手动加入的任务；并允许自动加入新收藏"
    : "手动加入的任务；不会自动加入新收藏";
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
    const [snapshot, trash] = await Promise.all([
      api(`/api/learning/snapshot${query}`),
      api("/api/learning/trash"),
    ]);
    renderTrash(trash.items);
    if (!snapshot.unchanged) {
      revision = snapshot.revision;
      render(snapshot);
    } else applyTaskFilter();
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
    if (action === "open_sources") {
      showView("sources");
      document.querySelector("#sources")?.scrollIntoView({ behavior: "smooth", block: "start" });
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
    if (action === "resume_task") {
      await api(`/api/learning/items/${encodeURIComponent(itemRef)}/resume`, { method: "POST" });
      say("已恢复后续调度；现有任务事实没有改写。");
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
    showTaskWorkbench();
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
    renderAutomationStatus(await api("/api/automation/configure", { method: "POST", body: JSON.stringify({ default_output: form.get("default_output"), auto_organize_new_favorites: form.has("auto_organize_new_favorites"), check_interval_minutes: Number(form.get("check_interval_minutes")), max_items_per_tick: Number(form.get("max_items_per_tick")) }) }));
    dirtySettingsForms.delete(automationForm);
    await loadProviderSettings(String(form.get("default_output")));
    say("产出与来源设置已保存。付费整理许可与自动加入新收藏是两个独立状态。");
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
    say("付费整理许可已开启；设置未变化时，手动任务无需重复确认。");
  } catch (error) { say(error.message); }
  finally {
    confirmAutomationAuthorization.disabled = false;
    confirmAutomationAuthorization.textContent = label;
  }
});

disableAutomationButton.addEventListener("click", async () => {
  try { renderAutomationStatus(await api("/api/automation/disable", { method: "POST" })); say("付费整理许可已关闭。"); } catch (error) { say(error.message); }
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
    providerSettingsMutationRevision += 1;
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
    providerSettingsMutationRevision += 1;
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

async function restoreTrashItem(bundleId) {
  try {
    await api(`/api/learning/trash/${encodeURIComponent(bundleId)}/restore`, { method: "POST" });
    say("任务已恢复到当前任务列表。原有内容和状态均已保留。");
    await Promise.all([refresh(true), loadSourceJobs()]);
  } catch (error) {
    say(error.message);
  }
}

async function pauseItem(itemRef) {
  try {
    await api(`/api/learning/items/${encodeURIComponent(itemRef)}/pause`, { method: "POST" });
    say("已暂停后续调度；已经开始的处理会继续完成。");
    await refresh(true);
  } catch (error) {
    say(error.message);
  }
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
  if (activeTaskFilter === "trash") showTaskWorkbench();
  applyTaskFilter();
}));
taskSearch.addEventListener("input", () => {
  activeTaskQuery = taskSearch.value.trim().toLocaleLowerCase("zh-CN");
  applyTaskFilter();
});
document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => showSettingsPanel(button.dataset.settingsTab)));
document.querySelectorAll("[data-open-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.showModal()));
document.querySelectorAll("[data-close-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.close()));
document.querySelectorAll("[data-close-automation-authorization]").forEach((button) => button.addEventListener("click", closeAutomationAuthorization));
document.querySelectorAll("[data-close-delete-task]").forEach((button) => button.addEventListener("click", closeDeleteTask));
deleteTaskDialog.addEventListener("close", () => { pendingDeleteItemRef = null; });
confirmCheckConnection.addEventListener("click", confirmProviderConnectionCheck);
document.querySelectorAll("[data-close-check-connection]").forEach((button) => button.addEventListener("click", () => checkConnectionDialog.close()));
checkConnectionDialog.addEventListener("close", () => { pendingCheckConnection = null; });
deleteConnectionForm.addEventListener("submit", deleteProviderConnection);
document.querySelectorAll("[data-close-delete-connection]").forEach((button) => button.addEventListener("click", () => deleteConnectionDialog.close()));
deleteConnectionDialog.addEventListener("close", () => { pendingDeleteConnection = null; });
document.querySelectorAll("[data-go-settings]").forEach((button) => button.addEventListener("click", () => {
  if (singleVideoDialog.open) singleVideoDialog.close();
  showView("settings");
  showSettingsPanel("output");
}));
document.querySelectorAll("[data-go-sources]").forEach((button) => button.addEventListener("click", () => showView("sources")));
document.querySelectorAll("[data-go-tasks]").forEach((button) => button.addEventListener("click", () => showView("tasks")));
document.querySelectorAll("[data-source-target]").forEach((button) => button.addEventListener("click", () => {
  document.querySelector(`#${CSS.escape(button.dataset.sourceTarget)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}));
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
if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
window.addEventListener("load", () => window.scrollTo({ top: 0, behavior: "auto" }));
restoreDouyinLogin();
suggestProviderConnectionName();
loadProviderSettings();
loadStorageStatus();
showView(["tasks", "sources", "settings"].includes(window.location.hash.slice(1)) ? window.location.hash.slice(1) : "tasks", false);
refresh(true);
