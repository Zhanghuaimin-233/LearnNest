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
const sourceJobs = document.createElement("section");
sourceJobs.className = "source-jobs";
sourceJobs.hidden = true;
notice?.insertAdjacentElement("afterend", sourceJobs);
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
const providerList = document.querySelector("#provider-connection-list");
const providerState = document.querySelector("#provider-settings-state");
const providerFeedback = document.querySelector("#provider-feedback");
const setupReadiness = document.querySelector("#setup-readiness");
const windowsVoiceField = document.querySelector("#windows-voice-field");
const windowsVoice = document.querySelector("#windows-voice");
const automationForm = document.querySelector("#automation-form");
const automationState = document.querySelector("#automation-state");
const automationSummary = document.querySelector("#automation-summary");
const confirmPaid = document.querySelector("#confirm-paid");
const authorizeAutomationButton = document.querySelector("#authorize-automation");
const disableAutomationButton = document.querySelector("#disable-automation");
const addSelectedFavoritesButton = document.querySelector("#add-selected-favorites");
const favoriteSelection = document.querySelector("#favorite-selection");
let windowsVoicesLoaded = false;
const stateLabel = {
  materials_ready: "材料已准备",
  waiting_setup: "等待设置",
  waiting_authorization: "等待授权",
  queued: "等待整理",
  organizing: "正在整理",
  partial_ready: "笔记已就绪，音频待完成",
  needs_action: "需要处理",
  ready: "可阅读",
};
const learningActionKinds = new Set(["open_note", "open_settings", "open_automation", "continue", "start_automation", "retry_automation"]);
const loginLabel = {
  disconnected: "未连接",
  starting: "准备中",
  browser_ready: "短信 + 扫码",
  qr_ready: "请扫码",
  scanned: "已扫码",
  confirmed: "待确认",
  verification_required: "需验证",
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
let selectedFavoriteIdsState = new Set();
let suggestedProviderConnectionName = "";
const dirtySettingsForms = new Set();
const providerConnectionNameDefaults = {
  "windows-tts": "windows-tts",
  "local-asr": "local-asr",
  "local-ocr": "local-ocr",
};

function say(message) { notice.textContent = message; }
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

function renderProviderSettings(settings) {
  const configured = settings.connections.length;
  providerState.textContent = configured ? `已配置 ${configured} 个` : "未配置";
  providerState.className = `status-pill ${configured ? "connected" : ""}`;
  providerList.innerHTML = settings.connections.length
    ? settings.connections.map((item) => {
      return `<div class="provider-row"><span>${escapeHtml(item.name)}</span><span>${escapeHtml(item.provider)}</span><span>${escapeHtml(item.state)}</span><div class="provider-actions"><button type="button" data-check-connection="${escapeHtml(item.name)}">检查连接</button><button type="button" data-delete-connection="${escapeHtml(item.name)}">删除</button></div></div>`;
    }).join("")
    : '<p class="empty">还没有学习连接。</p>';
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
  renderSetupReadiness(settings.readiness);
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
  setupReadiness.innerHTML = `<p class="chapter-label">所选结果：${escapeHtml(readiness.default_output)}</p>`
    + `<p class="section-note">${escapeHtml(readiness.message)}</p>`
    + `<div class="provider-role-list">${readiness.required_roles.map((role) => {
      const options = role.options.map((item) => `<option value="${escapeHtml(item.name)}"${item.name === role.connection ? " selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.provider)}</option>`).join("");
      const control = role.connection
        ? `<button type="button" data-unbind-setup-role="${escapeHtml(role.name)}">解绑</button>`
        : `<select aria-label="为${escapeHtml(role.name)}选择连接" data-setup-role-select="${escapeHtml(role.name)}"><option value="">选择连接</option>${options}</select><button type="button" data-bind-setup-role="${escapeHtml(role.name)}">绑定</button>`;
      return `<div class="provider-role-summary"><span>${escapeHtml(role.name)}</span><span>${escapeHtml(role.connection || role.state)}</span><div class="provider-role-actions"><strong>${escapeHtml(role.state)}</strong>${control}</div></div>`;
    }).join("")}</div><p class="section-note">自动整理：${escapeHtml(readiness.authorization.state)}。${escapeHtml(readiness.authorization.message)}</p>`;
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

async function loadProviderSettings(defaultOutput = null) {
  try { renderProviderSettings(await api(`/api/providers/settings${defaultOutput ? `?default_output=${encodeURIComponent(defaultOutput)}` : ""}`)); await refreshWindowsVoices(); } catch (error) { providerState.textContent = "无法读取"; }
}

function settingsFormNeedsProtection(form) {
  const active = document.activeElement;
  return form.contains(active) || dirtySettingsForms.has(form);
}

async function refreshProviderSettingsWhenIdle() {
  if (!settingsFormNeedsProtection(providerForm) && !settingsFormNeedsProtection(setupReadiness)) await loadProviderSettings(automationForm.elements.default_output.value);
}

function renderList(target, items, empty) {
  if (!items.length) {
    target.innerHTML = `<p class="empty">${escapeHtml(empty)}</p>`;
    return;
  }
  target.innerHTML = items.map((item) => `
    <article class="learning-row">
      <span class="status-bookmark ${escapeHtml(item.state)}" aria-label="${escapeHtml(stateLabel[item.state])}"></span>
      <div class="learning-copy">
        <h3>${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.source)} · ${escapeHtml(item.message)}</p>
      </div>
      ${item.action && learningActionKinds.has(item.action_kind) ? `<button type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="${escapeHtml(item.action_kind)}">${escapeHtml(item.action)}</button>` : ""}
      ${item.audio_href ? `<audio controls preload="metadata" src="${escapeHtml(item.audio_href)}">音频暂时不能播放。</audio>` : ""}
    </article>`).join("");
  target.querySelectorAll("button[data-item-ref]").forEach((button) => button.addEventListener("click", () => actOnItem(button.dataset.itemRef, button.dataset.action)));
}

function render(snapshot) {
  renderList(lists.library, snapshot.library, "还没有可以打开的笔记。");
  renderList(lists.processing, snapshot.processing, "现在没有正在整理的内容。");
  renderList(lists.inbox, snapshot.inbox, "添加内容后，它会在这里等待你。 ");
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
    favoriteStatus.textContent = "连接抖音后，收藏会出现在这里。";
    updateFavoriteSelection();
    return;
  }
  favoriteStatus.textContent = `最近同步：${formatSyncTime(snapshot.synced_at)}`;
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
  const enabled = status.enabled;
  automationState.textContent = enabled ? "自动整理已开启" : status.needs_authorization ? "需要重新确认" : status.configured ? "等待确认" : "等待设置";
  automationState.className = `status-pill ${enabled ? "connected" : ""}`;
  automationSummary.textContent = enabled
    ? `会按 ${status.check_interval_seconds} 秒检查收件箱；默认生成${status.default_output === "complete_note" ? "完整笔记" : "完整笔记和播客音频"}。`
    : "保存设置后，需要勾选付费确认才会开始自动整理。";
  if (status.configured && !settingsFormNeedsProtection(automationForm)) {
    automationForm.elements.default_output.value = status.default_output;
    automationForm.elements.check_interval_seconds.value = status.check_interval_seconds;
    automationForm.elements.auto_organize_new_favorites.checked = status.auto_organize_new_favorites;
    automationForm.elements.max_items_per_tick.value = status.max_items_per_tick;
  }
}

async function loadAutomationStatus() { try { renderAutomationStatus(await api("/api/automation/status")); } catch (error) { automationState.textContent = "无法读取"; } }

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
  loginState.textContent = loginLabel[state.status] || "需重连";
  loginState.className = `status-pill ${state.status}`;
  loginPanel.hidden = false;
  connectDouyinButton.textContent = state.status === "connected" ? "重新验证" : "打开抖音验证窗口";
  const reconnect = ["expired", "failed", "cancelled"].includes(state.status);
  if (state.status === "connected") {
    loginMessage.textContent = "官方短信与扫码验证已完成，可以同步收藏。";
  } else if (state.status === "browser_ready") {
    loginMessage.textContent = "请在抖音官方窗口完成手机号验证码，并按提示使用抖音 App 扫码确认；两步完成后才会连接。";
  } else if (state.status === "scanned") {
    loginMessage.textContent = "已扫码，请在手机上确认登录。";
  } else if (state.status === "confirmed") {
    loginMessage.textContent = "已确认，正在完成连接。";
  } else if (state.status === "verification_required") {
    loginMessage.textContent = "请在原抖音官方窗口继续完成短信、扫码或页面要求的额外验证。";
  } else if (reconnect) {
    loginMessage.textContent = "需要重新连接抖音。";
  } else {
    loginMessage.textContent = "正在打开抖音官方短信与扫码验证窗口。";
  }
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
    if (!state.session_id || state.status !== "connected") return;
    douyinSessionId = state.session_id;
    showLoginState(state);
  } catch (_) {
    /* A missing or temporarily unverifiable local session stays disconnected. */
  }
}

async function syncFavorites() {
  if (!douyinSessionId || douyinLoginStatus !== "connected") {
    favoriteStatus.textContent = "请先在设置中连接抖音。";
    return;
  }
  syncFavoritesButton.disabled = true;
  favoriteStatus.textContent = "正在同步收藏…";
  try {
    const snapshot = await api("/api/douyin/favorites", {
      method: "POST",
      body: JSON.stringify({ session_id: douyinSessionId }),
    });
    renderFavorites(snapshot);
    say("收藏已同步。");
  } catch (error) {
    if (error.status === 401) showLoginFailure();
    favoriteStatus.textContent = error.message;
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
    await Promise.all([loadFavorites(), refreshProviderSettingsWhenIdle()]);
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
      document.querySelector("#settings")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (action === "open_automation") {
      document.querySelector("#settings")?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.requestAnimationFrame(() => confirmPaid?.focus({ preventScroll: true }));
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
    await refresh(true);
  } catch (error) {
    say(error.message);
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
    confirmPaid.checked = false;
    say("自动整理设置已保存；请阅读提示并明确确认后开启。");
  } catch (error) { say(error.message); }
});

authorizeAutomationButton.addEventListener("click", async () => {
  if (!confirmPaid.checked) { say("请先勾选付费确认。 "); return; }
  try { renderAutomationStatus(await api("/api/automation/authorize", { method: "POST", body: JSON.stringify({ confirm_paid: true }) })); say("自动整理已开启。"); } catch (error) { say(error.message); }
});

disableAutomationButton.addEventListener("click", async () => {
  try { renderAutomationStatus(await api("/api/automation/disable", { method: "POST" })); say("自动整理已关闭。 "); } catch (error) { say(error.message); }
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
  } catch (error) { showProviderUpdateFailure(error); } finally { finishSaving(); }
});

providerForm.elements.preset.addEventListener("change", () => {
  suggestProviderConnectionName();
  windowsVoicesLoaded = false;
  refreshWindowsVoices().catch((error) => say(error.message));
});

for (const form of [providerForm, automationForm]) {
  form.addEventListener("input", () => dirtySettingsForms.add(form));
  form.addEventListener("change", () => dirtySettingsForms.add(form));
  form.addEventListener("reset", () => {
    dirtySettingsForms.delete(form);
    window.setTimeout(() => {
      if (form === providerForm) loadProviderSettings(automationForm.elements.default_output.value);
      else loadAutomationStatus();
    });
  });
}

document.querySelectorAll(".bookmark").forEach((link) => link.addEventListener("click", () => {
  document.querySelectorAll(".bookmark").forEach((item) => item.classList.remove("active"));
  link.classList.add("active");
}));

window.addEventListener("hashchange", () => {
  const current = document.querySelector(`.bookmark[href="${CSS.escape(window.location.hash)}"]`);
  if (!current) return;
  document.querySelectorAll(".bookmark").forEach((item) => item.classList.toggle("active", item === current));
});

connectDouyinButton.addEventListener("click", connectDouyin);
refreshDouyinButton.addEventListener("click", refreshDouyinQr);
cancelDouyinButton.addEventListener("click", cancelDouyin);
syncFavoritesButton.addEventListener("click", syncFavorites);
document.addEventListener("visibilitychange", () => refresh(true));
restoreDouyinLogin();
suggestProviderConnectionName();
loadProviderSettings();
refresh(true);
