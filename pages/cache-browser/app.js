const bridge = window.AstrBotPluginPage;

const state = {
  items: [],
  selectedId: null,
  selectedDetail: null,
  activeView: "summary",
  query: "",
};

const nodes = {
  cacheCount: document.getElementById("cacheCount"),
  refreshButton: document.getElementById("refreshButton"),
  searchInput: document.getElementById("searchInput"),
  list: document.getElementById("list"),
  emptyState: document.getElementById("emptyState"),
  detailPanel: document.getElementById("detailPanel"),
  statusBadge: document.getElementById("statusBadge"),
  detailTitle: document.getElementById("detailTitle"),
  detailUrl: document.getElementById("detailUrl"),
  detailUpdated: document.getElementById("detailUpdated"),
  detailSegments: document.getElementById("detailSegments"),
  detailSource: document.getElementById("detailSource"),
  summaryTab: document.getElementById("summaryTab"),
  transcriptTab: document.getElementById("transcriptTab"),
  summaryView: document.getElementById("summaryView"),
  transcriptView: document.getElementById("transcriptView"),
  copySummary: document.getElementById("copySummary"),
  copyTranscript: document.getElementById("copyTranscript"),
  toast: document.getElementById("toast"),
};

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  nodes.toast.textContent = message;
  nodes.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => nodes.toast.classList.remove("show"), 1800);
}

function setLoading(isLoading) {
  nodes.refreshButton.disabled = isLoading;
  nodes.refreshButton.classList.toggle("spinning", isLoading);
}

function normalizePreview(text, fallback) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value || fallback;
}

function renderList() {
  nodes.cacheCount.textContent = `${state.items.length} 条记录`;
  if (!state.items.length) {
    nodes.list.innerHTML = `<div class="list-empty">暂无缓存</div>`;
    return;
  }

  nodes.list.innerHTML = state.items
    .map((item) => {
      const selected = item.id === state.selectedId ? " selected" : "";
      const title = escapeHtml(item.title || "未命名缓存");
      const preview = escapeHtml(normalizePreview(item.summary_preview || item.transcript_preview, "暂无内容预览"));
      const updated = escapeHtml(item.updated_at || "未知时间");
      const segments = Number(item.segment_count || 0);
      return `
        <button class="cache-item${selected}" type="button" data-id="${escapeHtml(item.id)}">
          <span class="item-title">${title}</span>
          <span class="item-preview">${preview}</span>
          <span class="item-meta">
            <span>${updated}</span>
            <span>${segments} 段字幕</span>
          </span>
        </button>
      `;
    })
    .join("");
}

function renderDetail() {
  const detail = state.selectedDetail;
  nodes.emptyState.hidden = Boolean(detail);
  nodes.detailPanel.hidden = !detail;
  if (!detail) return;

  nodes.detailTitle.textContent = detail.title || "未命名缓存";
  nodes.detailUrl.textContent = detail.url || "未记录 URL";
  nodes.detailUrl.href = detail.url || "#";
  nodes.detailUpdated.textContent = detail.updated_at || "未知";
  nodes.detailSegments.textContent = `${detail.segment_count || 0} 段`;
  nodes.detailSource.textContent = detail.source || "未知来源";
  nodes.statusBadge.textContent = detail.has_summary ? "已总结" : "仅字幕";

  const summary = String(detail.summary || "").trim();
  nodes.summaryView.innerHTML = summary
    ? escapeHtml(summary).split("\n").map((line) => `<p>${line || "&nbsp;"}</p>`).join("")
    : `<div class="content-empty">暂无总结内容</div>`;

  const transcript = Array.isArray(detail.transcript) ? detail.transcript : [];
  nodes.transcriptView.innerHTML = transcript.length
    ? transcript
        .map((seg) => `
          <div class="segment">
            <time>${escapeHtml(seg.time || "00:00")}</time>
            <p>${escapeHtml(seg.text || "")}</p>
          </div>
        `)
        .join("")
    : `<div class="content-empty">暂无字幕内容</div>`;

  renderTabs();
}

function renderTabs() {
  const isSummary = state.activeView === "summary";
  nodes.summaryTab.classList.toggle("active", isSummary);
  nodes.transcriptTab.classList.toggle("active", !isSummary);
  nodes.summaryView.hidden = !isSummary;
  nodes.transcriptView.hidden = isSummary;
}

async function loadList() {
  setLoading(true);
  try {
    const result = await bridge.apiGet("cache/list", { q: state.query });
    state.items = Array.isArray(result.items) ? result.items : [];
    if (state.selectedId && !state.items.some((item) => item.id === state.selectedId)) {
      state.selectedId = null;
      state.selectedDetail = null;
    }
    renderList();
    renderDetail();
  } catch (error) {
    console.error(error);
    showToast("缓存列表加载失败");
  } finally {
    setLoading(false);
  }
}

async function selectItem(id) {
  state.selectedId = id;
  state.activeView = "summary";
  renderList();
  nodes.emptyState.hidden = false;
  nodes.detailPanel.hidden = true;
  try {
    state.selectedDetail = await bridge.apiGet("cache/detail", { id });
    renderDetail();
  } catch (error) {
    console.error(error);
    state.selectedDetail = null;
    renderDetail();
    showToast("缓存详情加载失败");
  }
}

function copyTextFallback(value) {
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  textarea.style.width = "1px";
  textarea.style.height = "1px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);

  const selection = document.getSelection();
  const ranges = [];
  if (selection) {
    for (let i = 0; i < selection.rangeCount; i += 1) {
      ranges.push(selection.getRangeAt(i));
    }
  }

  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    console.error(error);
  }

  document.body.removeChild(textarea);
  if (selection) {
    selection.removeAllRanges();
    ranges.forEach((range) => selection.addRange(range));
  }

  return copied;
}

async function copyText(text, emptyMessage) {
  const value = String(text || "").trim();
  if (!value) {
    showToast(emptyMessage);
    return;
  }

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else if (!copyTextFallback(value)) {
      throw new Error("fallback copy failed");
    }
    showToast("已复制");
  } catch (error) {
    if (copyTextFallback(value)) {
      showToast("已复制");
      return;
    }
    console.error(error);
    showToast("复制失败，请手动选择文本");
  }
}

nodes.list.addEventListener("click", (event) => {
  const button = event.target.closest(".cache-item");
  if (button && button.dataset.id) selectItem(button.dataset.id);
});

nodes.refreshButton.addEventListener("click", loadList);
nodes.searchInput.addEventListener("input", () => {
  state.query = nodes.searchInput.value.trim();
  window.clearTimeout(nodes.searchInput.timer);
  nodes.searchInput.timer = window.setTimeout(loadList, 180);
});
nodes.summaryTab.addEventListener("click", () => {
  state.activeView = "summary";
  renderTabs();
});
nodes.transcriptTab.addEventListener("click", () => {
  state.activeView = "transcript";
  renderTabs();
});
nodes.copySummary.addEventListener("click", () => {
  copyText(state.selectedDetail ? state.selectedDetail.summary : "", "暂无总结可复制");
});
nodes.copyTranscript.addEventListener("click", () => {
  copyText(state.selectedDetail ? state.selectedDetail.transcript_text : "", "暂无字幕可复制");
});

async function init() {
  await bridge.ready();
  document.title = bridge.t("pages.cache-browser.title", "总结缓存");
  await loadList();
}

init();
