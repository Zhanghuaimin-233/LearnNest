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
const disconnectDouyinButton = document.querySelector("#disconnect-douyin");
const providerForm = document.querySelector("#provider-connection-form");
const providerKeyField = document.querySelector("#provider-key-field");
const providerCapabilityList = document.querySelector("#provider-capability-list");
const providerRoleList = document.querySelector("#provider-role-list");
const providerRoleFeedback = document.querySelector("#provider-role-feedback");
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
const taskSelectVisible = document.querySelector("#task-select-visible");
const taskSelectionBar = document.querySelector("#task-selection-bar");
const taskSelectionCount = document.querySelector("#task-selection-count");
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
const batchTaskDialog = document.querySelector("#batch-task-dialog");
const batchTaskForm = document.querySelector("#batch-task-form");
const batchTaskKicker = document.querySelector("#batch-task-kicker");
const batchTaskTitle = document.querySelector("#batch-task-title");
const batchTaskCopy = document.querySelector("#batch-task-copy");
const batchTaskNote = document.querySelector("#batch-task-note");
const confirmBatchTask = document.querySelector("#confirm-batch-task");
const selectedVideoName = document.querySelector("#selected-video-name");
const connectionDialog = document.querySelector("#connection-dialog");
const connectionDialogTitle = document.querySelector("#connection-dialog-title");
const connectionDialogCopy = document.querySelector("#connection-dialog-copy");
const providerServiceHint = document.querySelector("#provider-service-hint");
const checkConnectionDialog = document.querySelector("#check-connection-dialog");
const checkConnectionName = document.querySelector("#check-connection-name");
const confirmCheckConnection = document.querySelector("#confirm-check-connection");
const modelSelectionDialog = document.querySelector("#model-selection-dialog");
const providerModelForm = document.querySelector("#provider-model-form");
const modelSelectionCurrent = document.querySelector("#model-selection-current");
const fetchProviderModelsButton = document.querySelector("#fetch-provider-models");
const providerModelSearch = document.querySelector("#provider-model-search");
const providerModelFeedback = document.querySelector("#provider-model-feedback");
const providerModelList = document.querySelector("#provider-model-list");
const providerModelEmpty = document.querySelector("#provider-model-empty");
const saveProviderModelButton = document.querySelector("#save-provider-model");
const providerKeyEntry = document.querySelector("#provider-key-entry");
const providerKeyEntryLink = document.querySelector("#provider-key-entry-link");
const newProviderModelSection = document.querySelector("#new-provider-model-section");
const fetchNewProviderModelsButton = document.querySelector("#fetch-new-provider-models");
const newProviderModelFeedback = document.querySelector("#new-provider-model-feedback");
const newProviderModelList = document.querySelector("#new-provider-model-list");
const newProviderModelEmpty = document.querySelector("#new-provider-model-empty");
const deleteConnectionDialog = document.querySelector("#delete-connection-dialog");
const deleteConnectionForm = document.querySelector("#delete-connection-form");
const deleteConnectionName = document.querySelector("#delete-connection-name");
const providerLimitsForm = document.querySelector("#provider-limits-form");
const providerLimitsFeedback = document.querySelector("#provider-limits-feedback");
const storageForm = document.querySelector("#storage-form");
const outputRoot = document.querySelector("#output-root");
const currentOutputRoot = document.querySelector("#current-output-root");
const storageFeedback = document.querySelector("#storage-feedback");
const localModelRootForm = document.querySelector("#local-model-root-form");
const runtimeState = document.querySelector(".runtime-state");
const runtimeTitle = document.querySelector("#runtime-title");
const runtimeCopy = document.querySelector("#runtime-copy");
let windowsVoicesLoaded = false;
let pendingDeleteItemRef = null;
let pendingBatchTask = null;
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
let pendingModelConnection = null;
let providerModelRequestToken = 0;
let providerModelCandidates = [];
let selectedProviderModel = null;
let newProviderModelRequestToken = 0;
let newProviderModelCandidates = [];
let selectedNewProviderModel = null;
let newProviderModelPreset = null;
let providerSettingsMutationRevision = 0;
const dirtySettingsForms = new Set();
let currentSnapshot = { inbox: [], processing: [], library: [] };
let currentTrash = [];
let activeTaskFilter = "all";
let activeTaskQuery = "";
let selectedTaskKeys = new Set();
let noticeTimer = null;
let lastAutomationStatus = { configured: false, enabled: false };
const providerConnectionNameDefaults = {
  "windows-tts": "windows-tts",
  "local-asr": "local-asr",
  "local-ocr": "local-ocr",
};
const providerCapabilityDefinitions = {
  asr: { icon: "ASR", title: "ASR · 语音识别", copy: "维护语音识别服务连接；具体职责请在职责配置中选择。", empty: "当前使用内置 faster-whisper large-v3；添加服务后可在职责配置中选择。" },
  ocr: { icon: "OCR", title: "OCR · 画面文字", copy: "维护画面文字识别服务连接；具体职责请在职责配置中选择。", empty: "当前使用内置 PaddleOCR；添加服务后可在职责配置中选择。" },
  llm: { icon: "LLM", title: "LLM · 内容生成", copy: "维护可供职责配置使用的文本模型连接。", empty: "还没有 LLM 连接。添加 MiMo 或 DeepSeek 后再到职责配置中选择。" },
  tts: { icon: "TTS", title: "TTS · 语音合成", copy: "维护语音合成服务连接；具体职责请在职责配置中选择。", empty: "还没有语音连接。可以添加 Windows 系统语音或 MiMo TTS。" },
};
const providerRoleOrder = [
  ["asr", "语音识别（ASR）", "ASR"],
  ["ocr", "画面文字（OCR）", "OCR"],
  ["llm", "笔记 Writer", "W"],
  ["llm", "笔记 Reviewer", "R"],
  ["llm", "播客", "P"],
  ["tts", "TTS", "TTS"],
];
const providerRolePresentation = {
  "语音识别（ASR）": { title: "语音识别", copy: "从视频中提取语音内容", local: "Faster Whisper · large-v3" },
  "画面文字（OCR）": { title: "画面文字识别", copy: "识别视频画面中的字幕与文字", local: "PaddleOCR · PP-OCRv6 medium" },
  "笔记 Writer": { title: "笔记生成", copy: "Writer · 生成最终学习笔记" },
  "笔记 Reviewer": { title: "内容校验", copy: "Reviewer · 检查结构与生成结果" },
  播客: { title: "播客稿生成", copy: "Podcast · 将学习笔记改写为口播内容" },
  TTS: { title: "语音合成", copy: "将播客稿转换为语音" },
};

// The native settings page owns the markup for this panel.  Keep the selectors
// deliberately small and optional so the existing workspace can load while a
// page variant is being rolled out, and so reading status never creates a
// model download as a side effect.
const localModelPanelNames = new Set(["local", "local-models", "local_models"]);
const localModelStateLabels = {
  not_installed: "未安装",
  external_ready: "外部可用",
  queued: "准备下载",
  downloading: "下载中",
  verifying: "校验中",
  ready: "已就绪",
  failed: "下载失败",
  interrupted: "已中断",
  cancelled: "已中断",
};
const localModelCapabilityLabels = { asr: "语音识别", ocr: "画面文字识别" };
const localModelNextSteps = {
  not_installed: "点击“下载”后，才会从官方模型源获取文件。",
  external_ready: "可以直接使用外部缓存，也可以下载一份由语栖管理的副本。",
  queued: "下载即将开始；页面会继续显示最新状态。",
  downloading: "下载进行中，完成校验前不会被任务使用。你可以随时取消。",
  verifying: "文件正在校验，校验通过后才会发布为可用模型。",
  ready: "模型已通过校验，相关任务可以使用。",
  failed: "请检查网络或磁盘空间，然后点击“重新下载”。",
  interrupted: "上次下载没有完成，点击“重新下载”即可继续尝试。",
  cancelled: "下载已停止，确认后可以重新下载。",
};
const localModelPollDelay = 1200;
const localModelPollLimit = 300;
let localModelsSnapshot = null;
let localModelPollTimer = null;
let localModelPollCount = 0;
let localModelRequestToken = 0;
let localModelDomWarningShown = false;
const localModelActionsInFlight = new Set();

function findLocalModelPanel() {
  return document.querySelector(
    '#local-model-panel, #local-models-panel, #local-models, #local-model, #local, [data-local-model-panel], [data-local-models-panel], [data-settings-panel="local-models"], [data-settings-panel="local"]',
  );
}

function findLocalModelList(panel = findLocalModelPanel()) {
  return panel?.querySelector(
    '#local-model-list, #local-models-list, #local-model-items, [data-local-model-list], .local-model-list',
  ) || document.querySelector(
    '#local-model-list, #local-models-list, #local-model-items, [data-local-model-list]',
  );
}

function findLocalModelHome(panel = findLocalModelPanel()) {
  return panel?.querySelector(
    '#local-model-home, #local-model-directory, #local-model-path, #model-home, [data-local-model-home], [data-local-model-directory]',
  ) || document.querySelector(
    '#local-model-home, #local-model-directory, #local-model-path, #model-home, [data-local-model-home], [data-local-model-directory]',
  );
}

function findLocalModelFeedback(panel = findLocalModelPanel()) {
  return panel?.querySelector(
    '#local-model-feedback, #local-models-feedback, #local-model-status, [data-local-model-feedback]',
  ) || document.querySelector(
    '#local-model-feedback, #local-models-feedback, #local-model-status, [data-local-model-feedback]',
  );
}

function localModelDom() {
  const panel = findLocalModelPanel();
  const list = findLocalModelList(panel);
  const home = findLocalModelHome(panel);
  const feedback = findLocalModelFeedback(panel);
  if (list) localModelDomWarningShown = false;
  return { panel, list, home, feedback };
}

function reportMissingLocalModelDom() {
  if (localModelDomWarningShown) return;
  localModelDomWarningShown = true;
  console.warn("[LearnNest] 本地模型页面尚未提供模型列表容器（建议使用 #local-model-list），跳过模型状态读取。");
}

function localModelsPanelVisible(panel) {
  if (!panel) return true;
  return !panel.hidden && !panel.closest("[hidden]");
}

function localModelIsActive(model) {
  return ["queued", "downloading", "verifying"].includes(String(model?.state));
}

function localModelsAreActive(snapshot = localModelsSnapshot) {
  return Array.isArray(snapshot?.models) && snapshot.models.some(localModelIsActive);
}

function stopLocalModelPolling() {
  window.clearTimeout(localModelPollTimer);
  localModelPollTimer = null;
  localModelPollCount = 0;
}

function scheduleLocalModelPolling(snapshot = localModelsSnapshot) {
  const { panel } = localModelDom();
  if (!localModelsAreActive(snapshot) || document.hidden || !localModelsPanelVisible(panel)) {
    stopLocalModelPolling();
    return;
  }
  if (localModelPollTimer || localModelPollCount >= localModelPollLimit) return;
  localModelPollTimer = window.setTimeout(() => {
    localModelPollTimer = null;
    localModelPollCount += 1;
    loadLocalModels({ fromPoll: true });
  }, localModelPollDelay);
}

function localModelProgress(model) {
  const declared = model?.progress_percent;
  const declaredNumber = Number(declared);
  if (declared !== null && declared !== undefined && declared !== "" && Number.isFinite(declaredNumber)) {
    return Math.max(0, Math.min(100, Math.round(declaredNumber)));
  }
  const downloaded = Number(model?.downloaded_bytes);
  const expected = Number(model?.expected_bytes);
  if (Number.isFinite(downloaded) && Number.isFinite(expected) && expected > 0) {
    return Math.max(0, Math.min(100, Math.round(downloaded * 100 / expected)));
  }
  return null;
}

function localModelState(model) {
  const state = String(model?.state || "not_installed");
  if (state === "downloading" && /校验/.test(String(model?.message || ""))) return "verifying";
  return state === "cancelled" ? "interrupted" : state;
}

function localModelId(model) {
  return String(model?.asset_id || model?.package_id || "");
}

function localModelActionMarkup(model, state) {
  const assetId = escapeHtml(localModelId(model));
  if (!assetId) return "";
  if (model.can_cancel === true || ["queued", "downloading", "verifying"].includes(state)) {
    return `<button class="btn secondary-button local-model-action" type="button" data-local-model-action="cancel" data-asset-id="${assetId}">取消下载</button>`;
  }
  if (model.can_download === true || ["not_installed", "external_ready", "failed", "interrupted"].includes(state)) {
    const label = ["failed", "interrupted"].includes(state) ? "重新下载" : "下载";
    return `<button class="btn primary local-model-action" type="button" data-local-model-action="download" data-asset-id="${assetId}">${label}</button>`;
  }
  if (state === "ready") return '<button class="btn local-model-action" type="button" disabled>已就绪</button>';
  return '<button class="btn local-model-action" type="button" disabled>暂不可用</button>';
}

function renderLocalModel(model) {
  const state = localModelState(model);
  const stateLabel = localModelStateLabels[state] || "状态未知";
  const capability = localModelCapabilityLabels[model.capability] || "本地能力";
  const progress = localModelProgress(model);
  const active = localModelIsActive({ ...model, state });
  const message = model.message || model.error || localModelNextSteps[state] || "请刷新后重试。";
  const nextStep = localModelNextSteps[state] || "请刷新后查看最新状态。";
  const assetId = localModelId(model);
  const progressMarkup = active
    ? `<div class="local-model-progress" aria-label="下载进度">
        <div class="local-model-progress-label"><span>下载与校验进度</span><strong>${progress === null ? "进行中" : `${progress}%`}</strong></div>
        <progress class="local-model-progress-bar" max="100"${progress === null ? "" : ` value="${progress}"`}>${progress === null ? "" : progress}</progress>
      </div>`
    : "";
  return `<article class="model-card local-model-card" data-local-model="${escapeHtml(assetId)}" data-asset-id="${escapeHtml(assetId)}" data-state="${escapeHtml(state)}">
    <div class="model-main local-model-main">
      <div class="provider local-model-logo">${model.capability === "ocr" ? "OCR" : "FW"}</div>
      <div class="local-model-copy"><div class="local-model-title"><b>${escapeHtml(model.name || "本地模型")} · ${escapeHtml(model.version || "")}</b><span class="tag local-model-state local-model-state-${escapeHtml(state)}">${escapeHtml(stateLabel)}</span></div>
        <span>${escapeHtml(capability)} · ${escapeHtml(model.size_label || "大小未知")}</span>
        <p class="local-model-message">${escapeHtml(message)}</p>
        <p class="local-model-next">下一步：${escapeHtml(nextStep)}</p>${progressMarkup}
      </div>
    </div>
    <div class="model-actions local-model-actions">${localModelActionMarkup(model, state)}</div>
  </article>`;
}

function renderLocalModels(snapshot) {
  const { list, home, feedback } = localModelDom();
  if (!list) {
    reportMissingLocalModelDom();
    stopLocalModelPolling();
    return;
  }
  if (home) {
    const value = snapshot.model_home || "未设置";
    if ("value" in home) {
      if (!localModelRootForm || !settingsFormNeedsProtection(localModelRootForm)) {
        home.value = value;
      }
    } else home.textContent = value;
  }
  const models = Array.isArray(snapshot.models) ? snapshot.models : [];
  list.innerHTML = models.length
    ? models.map(renderLocalModel).join("")
    : '<p class="empty">当前没有可管理的本地模型。</p>';
  if (feedback) {
    feedback.textContent = localModelsAreActive(snapshot)
      ? "模型下载状态会自动更新；只有你点击下载按钮后才会联网。"
      : "只有你点击下载按钮后才会联网；完成校验前的半成品不会被任务使用。";
  }
  scheduleLocalModelPolling(snapshot);
}

function applyLocalModelResponse(payload) {
  if (Array.isArray(payload?.models)) {
    localModelsSnapshot = payload;
    renderLocalModels(payload);
    return payload;
  }
  const payloadId = payload?.asset_id || payload?.package_id;
  if (payloadId && (!localModelsSnapshot || !Array.isArray(localModelsSnapshot.models))) return null;
  if (payloadId && Array.isArray(localModelsSnapshot.models)) {
    const models = localModelsSnapshot.models.map((model) => (
      localModelId(model) === payloadId ? { ...model, ...payload } : model
    ));
    localModelsSnapshot = { ...localModelsSnapshot, models };
    renderLocalModels(localModelsSnapshot);
    return localModelsSnapshot;
  }
  throw new Error("本地模型状态响应格式无效。");
}

async function loadLocalModels({ fromPoll = false } = {}) {
  const dom = localModelDom();
  if (!dom.list) {
    reportMissingLocalModelDom();
    stopLocalModelPolling();
    return null;
  }
  const token = ++localModelRequestToken;
  try {
    const snapshot = await api("/api/local-models");
    if (token !== localModelRequestToken) return snapshot;
    applyLocalModelResponse(snapshot);
    return snapshot;
  } catch (error) {
    if (token === localModelRequestToken) {
      if (dom.feedback) dom.feedback.textContent = error.message;
      if (!fromPoll) notifyLocalModel(error.message);
      if (fromPoll && localModelsAreActive()) scheduleLocalModelPolling();
    }
    return null;
  }
}

function notifyLocalModel(message) {
  if (notice) say(message);
}

function localModelActionFromButton(button) {
  const actionValue = button.dataset.localModelAction || button.dataset.modelAction || "";
  const action = button.hasAttribute("data-local-model-download")
    ? "download"
    : button.hasAttribute("data-local-model-cancel")
      ? "cancel"
      : actionValue.includes("cancel")
        ? "cancel"
        : actionValue.includes("download")
          ? "download"
          : actionValue;
  const assetId = button.dataset.assetId
    || button.dataset.packageId
    || button.dataset.modelAssetId
    || button.dataset.modelId
    || button.dataset.localModelDownload
    || button.dataset.localModelCancel
    || button.dataset.localModelId
    || button.closest("[data-asset-id]")?.dataset.assetId
    || button.closest("[data-package-id]")?.dataset.packageId
    || "";
  return { action, assetId };
}

async function actOnLocalModel(button, action, assetId) {
  if (!assetId || !["download", "cancel"].includes(action) || localModelActionsInFlight.has(assetId)) return;
  localModelActionsInFlight.add(assetId);
  const label = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = action === "download" ? "正在开始…" : "正在取消…";
  try {
    const result = await api(`/api/local-models/${encodeURIComponent(assetId)}/${action}`, { method: "POST" });
    if (result?.models || result?.asset_id || result?.package_id) applyLocalModelResponse(result);
    await loadLocalModels();
    notifyLocalModel(action === "download" ? "模型下载已开始；页面会显示真实进度。" : "正在取消模型下载。 ");
  } catch (error) {
    notifyLocalModel(error.message);
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = label;
    }
  } finally {
    localModelActionsInFlight.delete(assetId);
  }
}

function say(message) {
  window.clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.hidden = false;
  noticeTimer = window.setTimeout(() => { notice.hidden = true; }, 4200);
}
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

function providerConnectionFingerprint(item) {
  return JSON.stringify({
    name: item.name,
    provider: item.provider,
    capability: item.capability,
    preset: item.preset,
    model: item.model,
    voice: item.voice,
    local: item.local,
  });
}

function invalidateProviderModelRequest(message = "") {
  providerModelRequestToken += 1;
  providerModelCandidates = [];
  selectedProviderModel = null;
  providerModelList.replaceChildren();
  providerModelEmpty.hidden = false;
  providerModelEmpty.textContent = message || "连接设置已变化，请重新获取模型目录。";
  fetchProviderModelsButton.disabled = false;
  fetchProviderModelsButton.textContent = "获取模型";
  saveProviderModelButton.disabled = true;
  if (message) providerModelFeedback.textContent = message;
}

function renderProviderSettings(settings) {
  const nextModelConnection = pendingModelConnection && settings.connections.find((item) => item.name === pendingModelConnection.name);
  if (pendingModelConnection && (!nextModelConnection || providerConnectionFingerprint(nextModelConnection) !== pendingModelConnection.fingerprint)) {
    pendingModelConnection = null;
    invalidateProviderModelRequest();
  }
  latestProviderSettings = settings;
  const configured = settings.connections.length;
  const capabilities = Object.keys(providerCapabilityDefinitions);
  const readyCapabilities = capabilities.filter((capability) => providerCapabilityIsReady(capability, settings.roles?.[capability] || []));
  const allReady = readyCapabilities.length === capabilities.length;
  providerState.textContent = allReady ? "全部已准备" : configured ? `已配置 ${configured} 个连接` : "尚未配置连接";
  providerState.className = `status-pill ${allReady ? "connected" : ""}`;
  providerReadyCount.textContent = `${readyCapabilities.length} / ${capabilities.length}`;
  const overview = document.querySelector(".capability-overview");
  overview.classList.toggle("is-ready", allReady);
  overview.classList.toggle("is-partial", !allReady);
  overview.querySelector(".capability-overview-mark").textContent = allReady ? "✓" : "!";
  const pendingCapabilities = capabilities.filter((capability) => !readyCapabilities.includes(capability));
  providerReadyBadges.innerHTML = (pendingCapabilities.length ? pendingCapabilities : ["all"]).map((capability) => {
    if (capability === "all") return '<span class="is-ready">全部能力可用</span>';
    const ready = readyCapabilities.includes(capability);
    return `<span class="${ready ? "is-ready" : "is-pending"}">${capability.toUpperCase()} ${ready ? "已配置" : "待配置"}</span>`;
  }).join("");
  if (providerCapabilityList) {
    providerCapabilityList.innerHTML = capabilities.map((capability) => renderProviderCapabilityCard(capability, settings)).join("");
    providerCapabilityList.querySelectorAll("button[data-check-connection]").forEach((button) => button.addEventListener("click", () => requestProviderConnectionCheck(button)));
    providerCapabilityList.querySelectorAll("button[data-delete-connection]:not(:disabled)").forEach((button) => button.addEventListener("click", () => openDeleteProviderConnection(button)));
    providerCapabilityList.querySelectorAll("button[data-select-model]").forEach((button) => button.addEventListener("click", () => openProviderModelDialog(button)));
    providerCapabilityList.querySelectorAll("button[data-add-capability]").forEach((button) => button.addEventListener("click", () => openConnectionDialog(button.dataset.addCapability)));
  }
  renderProviderRoleList(settings);
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

function renderProviderConnectionRow(item, capability) {
  const current = item.bound_roles.length > 0;
  const model = item.voice || item.model || item.provider;
  const boundBadge = current ? '<span class="provider-badge is-role">已分配职责</span>' : "";
  const localBadge = item.local ? '<span class="provider-badge is-local">本地</span>' : "";
  const selectModelButton = capability === "llm" && !item.local
    ? `<button type="button" data-select-model="${escapeHtml(item.name)}">选择模型</button>`
    : "";
  const deleteTitle = item.deletable
    ? `删除连接 ${item.name}`
    : "连接正在被职责使用，请先在职责配置中更换连接。";
  return `<section class="provider-row${current ? " is-current" : ""}" data-connection="${escapeHtml(item.name)}">
    <span class="provider-logo">${escapeHtml(providerLogo(item))}</span>
    <span class="provider-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.provider)} · ${escapeHtml(item.state)}</span></span>
    <span class="provider-model"><strong>${escapeHtml(model)}</strong><span class="provider-badges">${boundBadge}${localBadge}</span></span>
    <span class="provider-actions"><button type="button" data-check-connection="${escapeHtml(item.name)}">检查连接</button>${selectModelButton}<button class="provider-delete-button" type="button" data-delete-connection="${escapeHtml(item.name)}" aria-label="${escapeHtml(deleteTitle)}" title="${escapeHtml(deleteTitle)}"${item.deletable ? "" : " disabled"}>删除</button></span>
  </section>`;
}

function providerRoleOptionLabel(option, settings) {
  const connection = (settings.connections || []).find((item) => item.name === option.name);
  const model = connection?.voice || connection?.model || "";
  return [option.name, model].filter(Boolean).join(" · ");
}

function renderProviderRoleRow(role, capability, icon, settings) {
  const presentation = providerRolePresentation[role.name] || { title: role.name, copy: "选择新任务使用的模型或服务" };
  const options = Array.isArray(role.options) ? role.options : [];
  const currentConnection = role.connection || "";
  const currentConnectionItem = (settings.connections || []).find((item) => item.name === currentConnection);
  const renderedOptions = options.map((item) => `<option value="${escapeHtml(item.name)}"${item.name === currentConnection ? " selected" : ""}>${escapeHtml(providerRoleOptionLabel(item, settings))}</option>`).join("");
  const implicitLocal = !currentConnection && role.state === "使用内置本地能力";
  const ready = implicitLocal || (currentConnection && ["连接配置可读取", "本地配置可读取"].includes(role.state));
  const attention = !ready;
  const currentLabel = implicitLocal
    ? presentation.local
    : currentConnection
      ? providerRoleOptionLabel({ name: currentConnection }, settings)
      : "暂无可选模型";
  const select = options.length
    ? `<select data-setup-role-select="${escapeHtml(role.name)}" data-current-connection="${escapeHtml(currentConnection)}" aria-label="为${escapeHtml(presentation.title)}选择模型或连接"><option value="">暂不绑定</option>${renderedOptions}</select>`
    : `<select data-setup-role-select="${escapeHtml(role.name)}" data-current-connection="${escapeHtml(currentConnection)}" aria-label="${escapeHtml(presentation.title)}当前模型或连接" disabled><option value="${escapeHtml(currentConnection)}">${escapeHtml(currentLabel)}</option></select>`;
  const iconClass = capability === "llm"
    ? ({ "笔记 Writer": "i-writer", "笔记 Reviewer": "i-review", 播客: "i-podcast" }[role.name] || "i-llm")
    : `i-${capability}`;
  const roleClass = capability === "llm" ? "provider-role-llm" : `provider-role-${capability}`;
  const statusLabel = implicitLocal ? "设备可用" : ready ? "已绑定" : "等待连接";
  const statusCopy = implicitLocal
    ? "添加连接后可以切换"
    : ready
      ? "新任务将使用此模型"
      : role.name === "播客"
        ? "需要单独的播客连接"
        : "先到 API 连接添加模型";
  const modelMark = implicitLocal ? (capability === "asr" ? "FW" : "OCR") : currentConnectionItem ? providerLogo(currentConnectionItem) : "—";
  return `<label class="role-row provider-role-row ${roleClass}" data-provider-role="${escapeHtml(role.name)}"><span class="role-meta provider-role-meta"><span class="role-icon provider-role-icon ${iconClass}" aria-hidden="true">${escapeHtml(icon)}</span><span class="role-copy"><strong>${escapeHtml(presentation.title)}</strong><small>${escapeHtml(presentation.copy)}</small></span></span><span class="role-picker${attention ? " is-attention" : ""}"><span class="role-model-mark" aria-hidden="true">${escapeHtml(modelMark)}</span>${select}<span class="role-state provider-role-state${attention ? " is-attention" : ""}"><strong>${statusLabel}</strong><small>${escapeHtml(statusCopy)}</small></span></span></label>`;
}

function renderProviderRoleList(settings) {
  if (!providerRoleList) return;
  const roleGroups = settings.roles || {};
  const rolesByName = new Map(
    Object.entries(roleGroups).flatMap(([capability, roles]) => (
      Array.isArray(roles) ? roles.map((role) => [role.name, { role, capability }]) : []
    )),
  );
  const rows = providerRoleOrder.map(([capability, name, icon], index) => {
    const entry = rolesByName.get(name);
    const divider = index === 2 ? '<div class="role-section-divider"><span>内容生成职责</span><small>可以分别指定不同模型</small></div>' : "";
    return entry ? divider + renderProviderRoleRow(entry.role, capability, icon, settings) : "";
  }).join("");
  providerRoleList.innerHTML = rows || '<p class="empty">正在读取职责配置。</p>';
  providerRoleList.querySelectorAll("select[data-setup-role-select]").forEach((select) => select.addEventListener("change", () => saveProviderRoleSelection(select)));
  if (providerRoleFeedback) {
    const roleRows = [...providerRoleList.querySelectorAll("[data-provider-role]")];
    const readyCount = roleRows.filter((row) => !row.querySelector(".role-state.is-attention")).length;
    providerRoleFeedback.textContent = roleRows.length ? `${readyCount} / ${roleRows.length} 项可用` : "暂无职责";
    providerRoleFeedback.classList.toggle("connected", roleRows.length > 0 && readyCount === roleRows.length);
  }
}

function renderProviderCapabilityCard(capability, settings) {
  const definition = providerCapabilityDefinitions[capability];
  const roles = settings.roles?.[capability] || [];
  const connections = settings.connections.filter((item) => item.capability === capability);
  const ready = providerCapabilityIsReady(capability, roles);
  const state = ready ? "已就绪" : connections.length ? `${connections.length} 个连接` : "尚未配置";
  const rows = connections.length
    ? connections.map((item) => renderProviderConnectionRow(item, capability)).join("")
    : `<p class="provider-capability-empty">${escapeHtml(definition.empty)}</p>`;
  return `<article class="provider-capability-card" data-capability="${capability}"><header class="provider-capability-header"><div class="provider-capability-identity"><span class="provider-capability-icon">${definition.icon}</span><div><div class="provider-capability-title"><h3>${definition.title}</h3><span class="provider-card-state${ready ? "" : " is-attention"}">${escapeHtml(state)}</span></div><p class="provider-capability-copy">${definition.copy}</p></div></div><button class="add-capability-button" type="button" data-add-capability="${capability}" aria-label="添加 ${capability.toUpperCase()} 服务"><strong aria-hidden="true">＋</strong><span>添加服务</span></button></header><p class="provider-list-label">服务连接</p><div class="provider-connection-list">${rows}</div></article>`;
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

function renderProviderModelCandidates() {
  const query = providerModelSearch.value.trim().toLocaleLowerCase("zh-CN");
  const candidates = providerModelCandidates.filter((item) => {
    if (!query) return true;
    return `${item.id} ${item.owned_by || ""}`.toLocaleLowerCase("zh-CN").includes(query);
  });
  providerModelList.innerHTML = candidates.map((item) => {
    const selected = item.id === selectedProviderModel;
    const owner = item.owned_by ? `<small>${escapeHtml(item.owned_by)}</small>` : "";
    return `<button class="provider-model-option${selected ? " is-selected" : ""}" type="button" data-provider-model="${escapeHtml(item.id)}" aria-pressed="${selected}"><strong>${escapeHtml(item.id)}</strong>${owner}</button>`;
  }).join("");
  providerModelList.querySelectorAll("button[data-provider-model]").forEach((button) => button.addEventListener("click", () => {
    selectedProviderModel = button.dataset.providerModel;
    saveProviderModelButton.disabled = false;
    renderProviderModelCandidates();
  }));
  providerModelEmpty.hidden = candidates.length > 0;
  if (!candidates.length) providerModelEmpty.textContent = query ? "没有符合搜索条件的模型。" : "官方目录中没有可选择的兼容模型。";
}

function openProviderModelDialog(button) {
  const connection = latestProviderSettings.connections.find((item) => item.name === button.dataset.selectModel);
  if (!connection || connection.local || connection.capability !== "llm") return;
  providerModelRequestToken += 1;
  pendingModelConnection = {
    name: connection.name,
    model: connection.model,
    fingerprint: providerConnectionFingerprint(connection),
  };
  providerModelCandidates = [];
  selectedProviderModel = null;
  modelSelectionCurrent.textContent = connection.model;
  providerModelSearch.value = "";
  providerModelFeedback.textContent = "尚未获取模型目录。";
  providerModelEmpty.hidden = false;
  providerModelEmpty.textContent = "点击“获取模型”读取当前连接提供的兼容模型。";
  providerModelList.replaceChildren();
  saveProviderModelButton.disabled = true;
  fetchProviderModelsButton.disabled = false;
  fetchProviderModelsButton.textContent = "获取模型";
  modelSelectionDialog.showModal();
  window.requestAnimationFrame(() => fetchProviderModelsButton.focus({ preventScroll: true }));
}

function providerModelRequestIsCurrent(token, mutationRevision, request) {
  const current = latestProviderSettings.connections.find((item) => item.name === request.name);
  return token === providerModelRequestToken
    && mutationRevision === providerSettingsMutationRevision
    && modelSelectionDialog.open
    && pendingModelConnection === request
    && current
    && providerConnectionFingerprint(current) === request.fingerprint;
}

async function fetchProviderModels() {
  if (!pendingModelConnection) return;
  const request = pendingModelConnection;
  const token = ++providerModelRequestToken;
  const mutationRevision = providerSettingsMutationRevision;
  fetchProviderModelsButton.disabled = true;
  fetchProviderModelsButton.textContent = "获取中…";
  providerModelFeedback.textContent = "正在从官方目录读取兼容模型…";
  providerModelCandidates = [];
  selectedProviderModel = null;
  renderProviderModelCandidates();
  try {
    const result = await api(`/api/providers/connections/${encodeURIComponent(request.name)}/models`, { method: "POST" });
    if (!providerModelRequestIsCurrent(token, mutationRevision, request)) return;
    providerModelCandidates = Array.isArray(result.models) ? result.models : [];
    renderProviderModelCandidates();
    if (!providerModelCandidates.length) {
      providerModelFeedback.textContent = "官方目录中没有可选择的兼容模型。";
    } else if (!providerModelCandidates.some((item) => item.id === request.model)) {
      providerModelFeedback.textContent = `警告：当前已保存模型 ${request.model} 不在本次目录中，仍保留当前模型；请选择其他模型后再保存。`;
    } else {
      providerModelFeedback.textContent = `已获取 ${providerModelCandidates.length} 个兼容模型；请选择后保存。`;
    }
  } catch (error) {
    if (!providerModelRequestIsCurrent(token, mutationRevision, request)) return;
    providerModelCandidates = [];
    renderProviderModelCandidates();
    providerModelFeedback.textContent = error.message;
    say(error.message);
  } finally {
    if (token === providerModelRequestToken) {
      fetchProviderModelsButton.disabled = false;
      fetchProviderModelsButton.textContent = "获取模型";
    }
  }
}

async function saveProviderModel(event) {
  event.preventDefault();
  if (!pendingModelConnection || !selectedProviderModel) return;
  const request = pendingModelConnection;
  const connection = latestProviderSettings.connections.find((item) => item.name === request.name);
  if (!connection || providerConnectionFingerprint(connection) !== request.fingerprint) {
    pendingModelConnection = null;
    invalidateProviderModelRequest();
    providerModelFeedback.textContent = "连接设置已变化，请关闭后重新打开模型选择。";
    return;
  }
  const model = selectedProviderModel;
  const label = saveProviderModelButton.textContent;
  providerModelRequestToken += 1;
  saveProviderModelButton.disabled = true;
  saveProviderModelButton.textContent = "保存中…";
  try {
    const settings = await api(`/api/providers/connections/${encodeURIComponent(request.name)}/model`, {
      method: "PUT",
      body: JSON.stringify({ model }),
    });
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    await loadAutomationStatus();
    const updated = settings.connections.find((item) => item.name === request.name);
    const roles = updated?.bound_roles?.length ? updated.bound_roles.join("、") : "尚未绑定职责";
    providerFeedback.textContent = `模型已保存：${model}。受影响职责：${roles}；付费许可需重新确认。`;
    say(providerFeedback.textContent);
    modelSelectionDialog.close();
  } catch (error) {
    providerModelFeedback.textContent = error.message;
    say(error.message);
  } finally {
    saveProviderModelButton.disabled = false;
    saveProviderModelButton.textContent = label;
  }
}

function renderSetupReadiness(readiness) {
  if (!readiness) return;
  singleVideoReadiness.textContent = readiness.message;
}

async function saveProviderRoleSelection(select) {
  const label = select.dataset.setupRoleSelect;
  const previous = select.dataset.currentConnection || "";
  const next = select.value;
  const roleContainer = providerRoleList || providerCapabilityList;
  if (roleContainer) dirtySettingsForms.add(roleContainer);
  select.disabled = true;
  try {
    const settings = next
      ? await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "POST", body: JSON.stringify({ connection_name: next }) })
      : await api(`/api/providers/setup-roles/${encodeURIComponent(label)}`, { method: "DELETE" });
    if (roleContainer) dirtySettingsForms.delete(roleContainer);
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    await loadAutomationStatus();
    const message = next ? `已更新${label}使用的连接；付费整理许可需要重新确认。` : `已解除${label}的连接；补齐职责后才能完整产出。`;
    providerFeedback.textContent = message;
    say(message);
  } catch (error) {
    select.value = previous;
    if (roleContainer) dirtySettingsForms.delete(roleContainer);
    if (providerRoleFeedback) {
      providerRoleFeedback.textContent = "保存失败";
      providerRoleFeedback.classList.remove("connected");
    }
    showProviderUpdateFailure(error);
  } finally {
    select.disabled = false;
  }
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

function resetNewProviderModelState() {
  newProviderModelRequestToken += 1;
  newProviderModelCandidates = [];
  selectedNewProviderModel = null;
  newProviderModelPreset = providerForm.elements.preset.value;
  newProviderModelList.replaceChildren();
  newProviderModelEmpty.hidden = true;
  fetchNewProviderModelsButton.disabled = false;
  fetchNewProviderModelsButton.textContent = "获取模型";
}

function currentProviderAdapter() {
  const preset = providerForm.elements.preset.value;
  return (latestProviderSettings.adapters || []).find((adapter) => adapter.preset === preset) || null;
}

async function refreshProviderConnectionFields() {
  const preset = providerForm.elements.preset.value;
  const adapter = currentProviderAdapter();
  const local = ["windows-tts", "local-asr", "local-ocr"].includes(preset);
  providerKeyField.hidden = local;
  if (local) providerForm.elements.api_key.value = "";
  const keyEntry = !local && adapter?.key_entry ? adapter.key_entry : null;
  providerKeyEntry.hidden = !keyEntry;
  if (keyEntry) providerKeyEntryLink.href = keyEntry;
  const requiresModel = Boolean(adapter?.requires_model);
  newProviderModelSection.hidden = !requiresModel;
  if (!requiresModel || newProviderModelPreset !== preset || !connectionDialog.open) {
    resetNewProviderModelState();
    if (requiresModel) {
      newProviderModelFeedback.textContent = adapter.catalog_mode === "curated"
        ? "这个服务使用语栖内置支持列表；获取后仍需明确选择一个模型。"
        : "填写 API Key 后获取模型，再明确选择一个模型；不会自动使用默认模型。";
    }
  }
  if (requiresModel) {
    providerForm.querySelector('button[type="submit"]').disabled = !selectedNewProviderModel;
  } else {
    providerForm.querySelector('button[type="submit"]').disabled = !preset;
  }
  await refreshWindowsVoices();
}

function newProviderModelRequestIsCurrent(token) {
  return token === newProviderModelRequestToken && connectionDialog.open;
}

function renderNewProviderModels() {
  newProviderModelList.innerHTML = newProviderModelCandidates.map((item) => {
    const selected = item.id === selectedNewProviderModel;
    const owner = item.owned_by ? `<small>${escapeHtml(item.owned_by)}</small>` : "";
    return `<button class="provider-model-option${selected ? " is-selected" : ""}" type="button" data-new-provider-model="${escapeHtml(item.id)}" aria-pressed="${selected}"><strong>${escapeHtml(item.id)}</strong>${owner}</button>`;
  }).join("");
  newProviderModelList.querySelectorAll("button[data-new-provider-model]").forEach((button) => button.addEventListener("click", () => {
    selectedNewProviderModel = button.dataset.newProviderModel;
    providerForm.querySelector('button[type="submit"]').disabled = false;
    renderNewProviderModels();
  }));
  newProviderModelEmpty.hidden = newProviderModelCandidates.length > 0;
}

async function fetchNewProviderModels() {
  const preset = providerForm.elements.preset.value;
  const adapter = currentProviderAdapter();
  if (!adapter?.requires_model || !preset) return;
  const key = providerForm.elements.api_key.value.trim();
  if (adapter.catalog_mode !== "curated" && !key) {
    newProviderModelFeedback.textContent = "请先填写 API Key，再获取模型。";
    say(newProviderModelFeedback.textContent);
    return;
  }
  const token = ++newProviderModelRequestToken;
  fetchNewProviderModelsButton.disabled = true;
  fetchNewProviderModelsButton.textContent = "获取中…";
  newProviderModelFeedback.textContent = "正在读取模型目录…";
  selectedNewProviderModel = null;
  newProviderModelCandidates = [];
  renderNewProviderModels();
  try {
    const result = await api(`/api/providers/presets/${encodeURIComponent(preset)}/models`, {
      method: "POST",
      body: JSON.stringify({ api_key: key }),
    });
    if (!newProviderModelRequestIsCurrent(token)) return;
    newProviderModelCandidates = Array.isArray(result.models) ? result.models : [];
    renderNewProviderModels();
    const sourceLabel = result.source === "curated" ? "内置支持列表" : "实时目录";
    if (!newProviderModelCandidates.length) {
      newProviderModelFeedback.textContent = "官方目录中没有可选择的兼容模型。";
      newProviderModelEmpty.textContent = "官方目录中没有可选择的兼容模型。";
    } else {
      newProviderModelFeedback.textContent = `已获取 ${newProviderModelCandidates.length} 个模型（${sourceLabel}）：${result.note || ""}请明确选择一个模型后再保存。`;
      newProviderModelEmpty.hidden = true;
    }
  } catch (error) {
    if (!newProviderModelRequestIsCurrent(token)) return;
    newProviderModelCandidates = [];
    renderNewProviderModels();
    newProviderModelFeedback.textContent = error.message;
    say(error.message);
  } finally {
    if (token === newProviderModelRequestToken) {
      fetchNewProviderModelsButton.disabled = false;
      fetchNewProviderModelsButton.textContent = "获取模型";
    }
  }
}

async function loadProviderSettings(defaultOutput = null, protectDirty = false) {
  try {
    const mutationRevision = providerSettingsMutationRevision;
    const settings = await api(`/api/providers/settings${defaultOutput ? `?default_output=${encodeURIComponent(defaultOutput)}` : ""}`);
    if (protectDirty && mutationRevision !== providerSettingsMutationRevision) return;
    if (!protectDirty && mutationRevision !== providerSettingsMutationRevision) return;
    if (protectDirty && (settingsFormNeedsProtection(providerForm) || settingsFormNeedsProtection(providerCapabilityList) || settingsFormNeedsProtection(providerRoleList))) return;
    if (protectDirty && settingsFormNeedsProtection(providerLimitsForm)) return;
    renderProviderSettings(settings);
    await refreshProviderConnectionFields();
  } catch (error) { providerState.textContent = "无法读取"; }
}

function settingsFormNeedsProtection(form) {
  if (!form) return false;
  const active = document.activeElement;
  return form.contains(active) || dirtySettingsForms.has(form);
}

async function refreshProviderSettingsWhenIdle() {
  if (!settingsFormNeedsProtection(providerForm) && !settingsFormNeedsProtection(providerCapabilityList) && !settingsFormNeedsProtection(providerRoleList) && !settingsFormNeedsProtection(providerLimitsForm)) await loadProviderSettings(automationForm.elements.default_output.value, true);
}

function renderList(target, items, empty) {
  if (!items.length) {
    target.innerHTML = empty ? `<p class="empty">${escapeHtml(empty)}</p>` : "";
    return;
  }
  target.innerHTML = items.map((item) => {
    const progress = taskProgress(item);
    const output = item.output_goal === "complete_note_with_audio" ? "笔记 + 音频" : "完整笔记";
    const failure = item.failure_reason
      ? `<span class="row-problem"><strong>失败阶段 · ${escapeHtml(item.failure_stage || "处理内容")}</strong><span>${escapeHtml(item.failure_reason)}</span></span>`
      : "";
    const contextualAction = item.action && item.action_kind !== "open_note" && learningActionKinds.has(item.action_kind)
      ? `<button class="task-row-context" type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="${escapeHtml(item.action_kind)}">${escapeHtml(item.action)}</button>`
      : "";
    const pauseControl = item.can_pause
      ? `<button class="task-row-pause" type="button" data-pause-item-ref="${escapeHtml(item.item_ref)}" title="只暂停后续调度；已经开始的处理会继续完成">暂停</button>`
      : "";
    const deleteDisabled = item.state === "organizing" && !item.manually_paused;
    const viewDisabled = !item.note_href;
    const selected = selectedTaskKeys.has(item.item_ref);
    return `
    <article class="learning-row${selected ? " is-selected" : ""}" data-item-ref="${escapeHtml(item.item_ref)}" data-state="${escapeHtml(item.state)}" data-filter="${taskFilterFor(item)}">
      <div class="learning-copy">
        <input class="task-row-check" data-task-select="${escapeHtml(item.item_ref)}" type="checkbox" aria-label="选择任务 ${escapeHtml(item.title)}"${selected ? " checked" : ""} />
        <div class="learning-copy-text"><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.source)}</p><small>ID · ${escapeHtml(item.item_ref)}</small></div>
      </div>
      <div class="row-progress">
        <span class="progress-ring" aria-label="进度 ${progress}%" style="--task-progress:${progress}%"><strong>${progress}</strong><small>%</small></span>
        <div class="progress-copy"><span class="learning-state">${escapeHtml(publicStateLabel(item))}</span><span class="task-progress" aria-hidden="true" style="--task-progress:${progress}%"><i></i></span>${failure}</div>
      </div>
      <span class="row-output">${output}</span>
      <span class="row-next">${escapeHtml(item.message)}</span>
      <div class="row-actions">${contextualAction}${pauseControl}<div class="row-action-pair"><button class="task-row-delete" type="button" data-delete-item-ref="${escapeHtml(item.item_ref)}"${deleteDisabled ? ' disabled title="当前步骤仍在完成，结束后可删除"' : ""}>删除</button><button class="task-row-view" type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="open_note"${viewDisabled ? ' disabled title="笔记生成后即可查看"' : ` aria-label="查看 ${escapeHtml(item.title)}的笔记"`}>查看</button></div></div>
    </article>`;
  }).join("");
  target.querySelectorAll("button[data-item-ref][data-action]:not(:disabled)").forEach((button) => button.addEventListener("click", () => actOnItem(button.dataset.itemRef, button.dataset.action)));
  target.querySelectorAll("button[data-pause-item-ref]").forEach((button) => button.addEventListener("click", () => pauseItem(button.dataset.pauseItemRef)));
  const itemsByRef = new Map(items.map((item) => [item.item_ref, item]));
  target.querySelectorAll("button[data-delete-item-ref]:not(:disabled)").forEach((button) => button.addEventListener("click", () => openDeleteTask(itemsByRef.get(button.dataset.deleteItemRef))));
  bindTaskSelectionInputs(target);
}

function publicStateLabel(item) {
  if (item.manually_paused) return "已暂停";
  if (item.failure_reason) return "处理已停止";
  return stateLabel[item.state] || "需要检查";
}

function renderTrash(items) {
  currentTrash = items;
  document.querySelector('[data-filter-count="trash"]').textContent = items.length;
  if (!items.length) {
    trashList.innerHTML = '<p class="empty">回收站为空。移入这里的任务会保留，直到恢复。</p>';
    return;
  }
  trashList.innerHTML = items.map((item) => {
    const selected = selectedTaskKeys.has(item.bundle_id);
    return `
    <article class="learning-row trash-row${selected ? " is-selected" : ""}" data-trash-bundle="${escapeHtml(item.bundle_id)}" data-filter="trash">
      <div class="learning-copy"><input class="task-row-check" data-task-select="${escapeHtml(item.bundle_id)}" type="checkbox" aria-label="选择回收任务 ${escapeHtml(item.title)}"${selected ? " checked" : ""} /><div class="learning-copy-text"><h3>${escapeHtml(item.title)}</h3><p>任务已从当前列表移出</p></div></div>
      <div class="row-progress"><span class="progress-ring" aria-hidden="true" style="--task-progress:0%"><strong>0</strong><small>%</small></span><div class="progress-copy"><span class="learning-state">回收区</span><span class="task-progress" aria-hidden="true"><i></i></span></div></div>
      <span class="row-output">任务记录</span>
      <span class="row-next">移入时间：${escapeHtml(formatSyncTime(item.trashed_at))}</span>
      <div class="row-actions"><div class="row-action-pair"><button class="task-row-delete" type="button" data-purge-bundle="${escapeHtml(item.bundle_id)}">永久删除</button><button class="task-row-view" type="button" data-restore-bundle="${escapeHtml(item.bundle_id)}">恢复任务</button></div></div>
    </article>`;
  }).join("");
  trashList.querySelectorAll("button[data-restore-bundle]").forEach((button) => button.addEventListener("click", () => restoreTrashItem(button.dataset.restoreBundle)));
  trashList.querySelectorAll("button[data-purge-bundle]").forEach((button) => button.addEventListener("click", () => openBatchTaskDialog("purge", [button.dataset.purgeBundle])));
  bindTaskSelectionInputs(trashList);
}

function render(snapshot) {
  currentSnapshot = snapshot;
  renderList(lists.library, snapshot.library, "");
  renderList(lists.processing, snapshot.processing, "");
  renderList(lists.inbox, snapshot.inbox, "");
  const items = allLearningItems();
  if (!items.length) lists.processing.innerHTML = '<p class="empty">还没有任务。请从来源页添加内容。</p>';
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
  applyTaskFilter();
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

function bindTaskSelectionInputs(target) {
  target.querySelectorAll("input[data-task-select]").forEach((input) => input.addEventListener("change", () => {
    if (input.checked) selectedTaskKeys.add(input.dataset.taskSelect);
    else selectedTaskKeys.delete(input.dataset.taskSelect);
    input.closest(".learning-row")?.classList.toggle("is-selected", input.checked);
    updateTaskSelectionUI();
  }));
}

function selectedTaskEntries() {
  if (activeTaskFilter === "trash") return currentTrash.filter((item) => selectedTaskKeys.has(item.bundle_id));
  return allLearningItems().filter((item) => selectedTaskKeys.has(item.item_ref));
}

function batchActionApplies(action, entries) {
  if (!entries.length) return false;
  if (activeTaskFilter === "trash") return ["restore", "purge"].includes(action);
  if (action === "pause") return entries.every((item) => item.can_pause);
  if (action === "resume") return entries.every((item) => item.manually_paused);
  if (action === "continue") return entries.every((item) => item.action_kind === "continue");
  if (action === "start") return entries.every((item) => item.action_kind === "start_automation");
  if (action === "retry") return entries.every((item) => item.action_kind === "retry_automation");
  if (action === "trash") return entries.every((item) => item.state !== "organizing" || item.manually_paused);
  return false;
}

function updateTaskSelectionUI() {
  const availableKeys = new Set(activeTaskFilter === "trash"
    ? currentTrash.map((item) => item.bundle_id)
    : allLearningItems().map((item) => item.item_ref));
  selectedTaskKeys = new Set([...selectedTaskKeys].filter((key) => availableKeys.has(key)));
  const visibleInputs = [...taskList.querySelectorAll("article[data-filter]:not([hidden]) input[data-task-select]")];
  const selectedVisible = visibleInputs.filter((input) => selectedTaskKeys.has(input.dataset.taskSelect));
  taskSelectVisible.disabled = visibleInputs.length === 0;
  taskSelectVisible.checked = visibleInputs.length > 0 && selectedVisible.length === visibleInputs.length;
  taskSelectVisible.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleInputs.length;
  visibleInputs.forEach((input) => {
    input.checked = selectedTaskKeys.has(input.dataset.taskSelect);
    input.closest(".learning-row")?.classList.toggle("is-selected", input.checked);
  });
  const entries = selectedTaskEntries();
  taskSelectionBar.hidden = entries.length === 0;
  taskSelectionCount.textContent = `已选 ${entries.length} 项`;
  taskSelectionBar.querySelectorAll("button[data-batch-task-action]").forEach((button) => {
    button.hidden = !batchActionApplies(button.dataset.batchTaskAction, entries);
  });
}

function clearTaskSelection() {
  selectedTaskKeys.clear();
  taskList.querySelectorAll("input[data-task-select]").forEach((input) => { input.checked = false; });
  updateTaskSelectionUI();
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
  workbenchNote.hidden = showingTrash || visibleCount === 0;
  updateTaskSelectionUI();
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

function openBatchTaskDialog(action, keys = [...selectedTaskKeys]) {
  if (!keys.length || !["trash", "purge"].includes(action)) return;
  const count = keys.length;
  pendingBatchTask = { action, keys: [...keys] };
  if (action === "purge") {
    batchTaskKicker.textContent = "永久删除";
    batchTaskTitle.textContent = `永久删除 ${count} 项任务？`;
    batchTaskCopy.textContent = "任务记录和已生成内容会从回收站永久删除，无法恢复。";
    batchTaskNote.textContent = "不会删除你原来的视频。删除开始后若遇到文件占用，会停止并报告已完成范围。";
    confirmBatchTask.textContent = "永久删除";
  } else {
    batchTaskKicker.textContent = "移入语栖回收区";
    batchTaskTitle.textContent = `移入 ${count} 项任务？`;
    batchTaskCopy.textContent = "任务记录和已生成内容会移入回收区，之后仍可恢复。";
    batchTaskNote.textContent = "不会删除你原来的视频。状态变化中的任务会停止批量操作并保留未处理项。";
    confirmBatchTask.textContent = "移入回收区";
  }
  batchTaskDialog.showModal();
  window.requestAnimationFrame(() => confirmBatchTask.focus({ preventScroll: true }));
}

function closeBatchTaskDialog() {
  pendingBatchTask = null;
  if (batchTaskDialog.open) batchTaskDialog.close();
}

async function requestTaskBatchAction(action, key) {
  if (action === "pause") return api(`/api/learning/items/${encodeURIComponent(key)}/pause`, { method: "POST" });
  if (action === "resume") return api(`/api/learning/items/${encodeURIComponent(key)}/resume`, { method: "POST" });
  if (action === "continue") return api(`/api/learning/items/${encodeURIComponent(key)}/continue`, { method: "POST" });
  if (action === "start") return api(`/api/learning/items/${encodeURIComponent(key)}/start-automation`, { method: "POST" });
  if (action === "retry") return api(`/api/learning/items/${encodeURIComponent(key)}/retry-automation`, { method: "POST" });
  if (action === "trash") return api(`/api/learning/items/${encodeURIComponent(key)}`, { method: "DELETE" });
  if (action === "restore") return api(`/api/learning/trash/${encodeURIComponent(key)}/restore`, { method: "POST" });
  if (action === "purge") return api(`/api/learning/trash/${encodeURIComponent(key)}`, { method: "DELETE" });
  throw new Error("当前批量操作不可用。请刷新后重试。");
}

function taskBatchSuccessMessage(action, count) {
  if (action === "pause") return `已暂停 ${count} 项任务的后续调度。`;
  if (action === "resume") return `已恢复 ${count} 项任务的后续调度。`;
  if (action === "continue") return `已继续处理 ${count} 项任务。`;
  if (action === "start") return `${count} 项任务已加入整理队列。`;
  if (action === "retry") return `${count} 项任务已重新加入整理队列。`;
  if (action === "trash") return `${count} 项任务已移入回收区。`;
  if (action === "restore") return `已恢复 ${count} 项任务。`;
  return `已永久删除 ${count} 项任务。`;
}

async function executeTaskBatch(action, keys = [...selectedTaskKeys]) {
  const entries = selectedTaskEntries();
  if (!keys.length || (keys.length === selectedTaskKeys.size && !batchActionApplies(action, entries))) {
    say("所选任务不能共同执行这项操作。请刷新或重新选择。");
    return;
  }
  const controls = [...taskSelectionBar.querySelectorAll("button[data-batch-task-action]")];
  controls.forEach((button) => { button.disabled = true; });
  confirmBatchTask.disabled = true;
  const originalConfirmText = confirmBatchTask.textContent;
  if (batchTaskDialog.open) confirmBatchTask.textContent = "正在处理…";
  let completed = 0;
  let failure = null;
  for (const key of keys) {
    try {
      await requestTaskBatchAction(action, key);
      completed += 1;
    } catch (error) {
      failure = error;
      break;
    }
  }
  if (batchTaskDialog.open) batchTaskDialog.close();
  pendingBatchTask = null;
  selectedTaskKeys.clear();
  await refresh(true);
  if (failure) say(`${completed} 项已完成，剩余 ${keys.length - completed} 项未处理：${failure.message}`);
  else say(taskBatchSuccessMessage(action, completed));
  controls.forEach((button) => { button.disabled = false; });
  confirmBatchTask.disabled = false;
  confirmBatchTask.textContent = originalConfirmText;
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
  const paidAuthorized = status.paid_authorized ?? status.enabled;
  const autoNewFavoritesEnabled = Boolean(status.auto_new_favorites_enabled ?? status.auto_organize_new_favorites);
  const autoNewFavoritesActive = Boolean(status.auto_new_favorites_active);
  automationState.textContent = paidAuthorized ? "付费许可已开启" : status.needs_authorization ? "需要重新确认付费许可" : status.configured ? "等待付费许可" : "等待设置";
  automationState.className = `status-pill ${paidAuthorized ? "connected" : ""}`;
  const output = status.default_output || "complete_note_with_audio";
  const outputLabel = output === "complete_note" ? "完整笔记" : "笔记 + 音频";
  defaultOutputLabel.textContent = outputLabel;
  singleVideoOutput.textContent = outputLabel;
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
  if (view !== "settings") stopLocalModelPolling();
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    const visible = panel === target;
    panel.hidden = !visible;
    panel.classList.toggle("is-visible", visible);
  });
  document.querySelectorAll(".bookmark[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
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
  if (localModelPanelNames.has(panelName)) loadLocalModels();
  else stopLocalModelPolling();
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
  connectDouyinButton.disabled = requestContractBlocked;
  connectDouyinButton.textContent = requestContractBlocked
    ? "暂不需要重新扫码"
    : (state.status === "connected" ? "重新验证" : "打开抖音验证窗口");
  const reconnect = ["expired", "failed", "cancelled"].includes(state.status);
  const connected = state.status === "connected";
  const inProgress = !connected && !reconnect && !requestContractBlocked;
  loginPanel.hidden = !inProgress;
  cancelDouyinButton.hidden = !inProgress;
  disconnectDouyinButton.hidden = !connected;
  if (connected) {
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

batchTaskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!pendingBatchTask) return;
  const { action, keys } = pendingBatchTask;
  await executeTaskBatch(action, keys);
});

localModelRootForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = localModelRootForm.querySelector("button[type=submit]");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const snapshot = await api("/api/local-models/root", {
      method: "PUT",
      body: JSON.stringify({ model_root: localModelRootForm.elements.model_root.value.trim() }),
    });
    dirtySettingsForms.delete(localModelRootForm);
    renderLocalModels(snapshot);
    notifyLocalModel("模型保存位置已修改；旧目录内容保持不变。");
  } catch (error) {
    const feedback = findLocalModelFeedback();
    if (feedback) feedback.textContent = error.message;
    notifyLocalModel(error.message);
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
  const adapter = (latestProviderSettings.adapters || []).find((item) => item.preset === preset) || null;
  if (adapter?.requires_model && !selectedNewProviderModel) {
    providerFeedback.textContent = "请先获取并选择具体模型，再保存连接。";
    say(providerFeedback.textContent);
    return;
  }
  const finishSaving = startProviderSave(event);
  try {
    const settings = await api("/api/providers/connections", {
      method: "POST",
      body: JSON.stringify({
        name: form.get("name"), preset,
        ...(key ? { api_key: key } : {}),
        ...(adapter?.requires_model ? { model: selectedNewProviderModel } : {}),
        ...(preset === "windows-tts" ? { voice: form.get("voice") } : {}),
      }),
    });
    submittedForm.reset();
    dirtySettingsForms.delete(providerForm);
    suggestProviderConnectionName();
    providerSettingsMutationRevision += 1;
    renderProviderSettings(settings);
    providerFeedback.textContent = adapter?.requires_model
      ? `连接已保存：${selectedNewProviderModel}；密钥不会显示在页面中。`
      : "连接已保存；密钥不会显示在页面中。";
    say(providerFeedback.textContent);
    connectionDialog.close();
    await refreshProviderConnectionFields().catch(() => {});
  } catch (error) { showProviderUpdateFailure(error); } finally { finishSaving(); }
});

providerForm.elements.preset.addEventListener("change", () => {
  suggestProviderConnectionName();
  windowsVoicesLoaded = false;
  refreshProviderConnectionFields().catch((error) => say(error.message));
});

fetchNewProviderModelsButton.addEventListener("click", fetchNewProviderModels);
connectionDialog.addEventListener("close", () => {
  newProviderModelRequestToken += 1;
  newProviderModelCandidates = [];
  selectedNewProviderModel = null;
  newProviderModelPreset = null;
});

providerModelForm.addEventListener("submit", saveProviderModel);
fetchProviderModelsButton.addEventListener("click", fetchProviderModels);
providerModelSearch.addEventListener("input", renderProviderModelCandidates);

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

for (const form of [providerForm, automationForm, providerLimitsForm, storageForm, localModelRootForm]) {
  form.addEventListener("input", () => dirtySettingsForms.add(form));
  form.addEventListener("change", () => dirtySettingsForms.add(form));
  form.addEventListener("reset", () => {
    dirtySettingsForms.delete(form);
    window.setTimeout(() => {
      if (form === providerForm) loadProviderSettings(automationForm.elements.default_output.value);
      else if (form === automationForm) loadAutomationStatus();
      else if (form === providerLimitsForm) loadProviderSettings(automationForm.elements.default_output.value);
      else if (form === storageForm) loadStorageStatus();
      else loadLocalModels();
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
  clearTaskSelection();
  activeTaskFilter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
  applyTaskFilter();
}));
taskSearch.addEventListener("input", () => {
  clearTaskSelection();
  activeTaskQuery = taskSearch.value.trim().toLocaleLowerCase("zh-CN");
  applyTaskFilter();
});
taskSelectVisible.addEventListener("change", () => {
  const visibleInputs = [...taskList.querySelectorAll("article[data-filter]:not([hidden]) input[data-task-select]")];
  visibleInputs.forEach((input) => {
    if (taskSelectVisible.checked) selectedTaskKeys.add(input.dataset.taskSelect);
    else selectedTaskKeys.delete(input.dataset.taskSelect);
  });
  updateTaskSelectionUI();
});
document.querySelectorAll("button[data-batch-task-action]").forEach((button) => button.addEventListener("click", () => {
  const action = button.dataset.batchTaskAction;
  if (["trash", "purge"].includes(action)) openBatchTaskDialog(action);
  else executeTaskBatch(action);
}));
document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => showSettingsPanel(button.dataset.settingsTab)));
document.querySelectorAll("[data-open-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.showModal()));
document.querySelectorAll("[data-close-single-video]").forEach((button) => button.addEventListener("click", () => singleVideoDialog.close()));
document.querySelectorAll("[data-close-automation-authorization]").forEach((button) => button.addEventListener("click", closeAutomationAuthorization));
document.querySelectorAll("[data-close-delete-task]").forEach((button) => button.addEventListener("click", closeDeleteTask));
deleteTaskDialog.addEventListener("close", () => { pendingDeleteItemRef = null; });
document.querySelectorAll("[data-close-batch-task]").forEach((button) => button.addEventListener("click", closeBatchTaskDialog));
batchTaskDialog.addEventListener("close", () => { pendingBatchTask = null; });
confirmCheckConnection.addEventListener("click", confirmProviderConnectionCheck);
document.querySelectorAll("[data-close-check-connection]").forEach((button) => button.addEventListener("click", () => checkConnectionDialog.close()));
checkConnectionDialog.addEventListener("close", () => { pendingCheckConnection = null; });
document.querySelectorAll("[data-close-model-selection]").forEach((button) => button.addEventListener("click", () => modelSelectionDialog.close()));
modelSelectionDialog.addEventListener("close", () => {
  providerModelRequestToken += 1;
  pendingModelConnection = null;
  providerModelCandidates = [];
  selectedProviderModel = null;
  providerModelList.replaceChildren();
  saveProviderModelButton.disabled = true;
});
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
document.addEventListener("click", (event) => {
  const element = event.target instanceof Element ? event.target : null;
  const button = element?.closest("[data-local-model-action], [data-local-model-download], [data-local-model-cancel], [data-model-action]");
  if (!button) return;
  const { action, assetId } = localModelActionFromButton(button);
  if (!["download", "cancel"].includes(action) || !assetId) return;
  event.preventDefault();
  void actOnLocalModel(button, action, assetId);
});
document.querySelectorAll("[data-close-connection]").forEach((button) => button.addEventListener("click", () => connectionDialog.close()));
document.querySelector("#local-video").addEventListener("change", (event) => {
  selectedVideoName.textContent = event.currentTarget.files[0]?.name || "MP4、MOV、MKV、AVI、MPEG 或 WebM";
});
document.querySelector("#refresh-tasks").addEventListener("click", () => refresh(true));

window.fetchProviderModels = fetchProviderModels;
connectDouyinButton.addEventListener("click", connectDouyin);
refreshDouyinButton.addEventListener("click", refreshDouyinQr);
cancelDouyinButton.addEventListener("click", cancelDouyin);
disconnectDouyinButton.addEventListener("click", cancelDouyin);
syncFavoritesButton.addEventListener("click", syncFavorites);
document.addEventListener("visibilitychange", () => refresh(true));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopLocalModelPolling();
  else if (localModelsAreActive()) scheduleLocalModelPolling();
});
if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
window.addEventListener("load", () => window.scrollTo({ top: 0, behavior: "auto" }));
restoreDouyinLogin();
suggestProviderConnectionName();
loadProviderSettings();
loadStorageStatus();
showView(["tasks", "sources", "settings"].includes(window.location.hash.slice(1)) ? window.location.hash.slice(1) : "tasks", false);
refresh(true);
