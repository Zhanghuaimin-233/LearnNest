const api = (path, options) => fetch(path, { headers: { "Content-Type": "application/json" }, ...options }).then(async (response) => {
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "暂时无法完成操作。");
  return payload;
});

const notice = document.querySelector("#notice");
const lists = {
  inbox: document.querySelector("#inbox-list"),
  processing: document.querySelector("#processing-list"),
  library: document.querySelector("#library-list"),
};
const stateLabel = {
  queued: "等待整理",
  organizing: "正在整理",
  materials_ready: "材料已就绪",
  needs_action: "需要继续",
  ready: "可以阅读",
};
let revision = null;
let timer = null;

function say(message) { notice.textContent = message; }
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

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
      ${item.action ? `<button type="button" data-item-ref="${escapeHtml(item.item_ref)}" data-action="${item.state === "ready" ? "open" : "continue"}">${escapeHtml(item.action)}</button>` : ""}
    </article>`).join("");
  target.querySelectorAll("button[data-item-ref]").forEach((button) => button.addEventListener("click", () => actOnItem(button.dataset.itemRef, button.dataset.action)));
}

function render(snapshot) {
  renderList(lists.library, snapshot.library, "还没有可以打开的笔记。");
  renderList(lists.processing, snapshot.processing, "现在没有正在整理的内容。");
  renderList(lists.inbox, snapshot.inbox, "添加内容后，它会在这里等待你。 ");
}

async function refresh(force = false) {
  try {
    const query = !force && revision ? `?revision=${encodeURIComponent(revision)}` : "";
    const snapshot = await api(`/api/learning/snapshot${query}`);
    if (!snapshot.unchanged) {
      revision = snapshot.revision;
      render(snapshot);
    }
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
    if (action === "open") {
      window.open(`/api/learning/items/${encodeURIComponent(itemRef)}/note`, "_blank", "noopener");
      return;
    }
    const result = await api(`/api/learning/items/${encodeURIComponent(itemRef)}/continue`, { method: "POST" });
    say(result.outcome === "needs_setup" ? "需要完成设置后才能继续。" : "已继续整理内容。");
    await refresh(true);
  } catch (error) {
    say(error.message);
  }
}

document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    say("正在添加内容，页面会自动更新。");
    const result = await api("/api/learning/items", {
      method: "POST",
      body: JSON.stringify({ source: form.get("source"), desired_output: form.get("desired-output") }),
    });
    say(result.item.message);
    event.currentTarget.reset();
    await refresh(true);
  } catch (error) {
    say(error.message);
  }
});

document.addEventListener("visibilitychange", () => refresh(true));
refresh(true);
