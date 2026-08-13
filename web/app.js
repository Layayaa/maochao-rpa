(() => {
  "use strict";

  const TASK_META = {
    "realtime-inventory": { displayNo: 1, actualNo: 1, title: "实时库存" },
    "pincang-detail": { displayNo: 2, actualNo: 2, title: "库存分析-品仓明细表" },
    "system-order": { displayNo: 3, actualNo: 3, title: "系统单" },
    "po-list": { displayNo: 4, actualNo: 4, title: "补货单列表" },
    "channel-goods": { displayNo: 5, actualNo: 10, title: "库位明细" },
    "transfer-order": { displayNo: 6, actualNo: 11, title: "调拨单" }
  };
  const TASK_ORDER = Object.keys(TASK_META);
  const state = {
    tasks: [],
    accounts: [],
    runs: [],
    errors: [],
    files: [],
    runsPage: 1,
    runsPageSize: 10,
    filesPage: 1,
    filesPageSize: 10,
    worker: null,
    health: null,
    activeTab: "runs",
    selectedTaskKeys: new Set(TASK_ORDER),
    selectedAccountKeys: new Set(),
    accountSelectionInitialized: false,
    editingAccountKey: "",
    taskPanelOpen: false,
    connectionBannerDismissed: false,
    connectionBannerSignature: ""
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  function icon(name) {
    return `<svg class="icon" aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function taskMeta(taskKey) {
    return TASK_META[taskKey] || { displayNo: "", actualNo: "", title: taskKey || "未知任务" };
  }

  function taskLabel(taskKey) {
    const meta = taskMeta(taskKey);
    return meta.displayNo ? `任务${meta.displayNo} · ${meta.title}` : (taskKey || "未知任务");
  }

  function taskKeyFromFile(file) {
    const name = String(file.name || file.file_id || "");
    for (const key of TASK_ORDER) {
      const actualNo = String(taskMeta(key).actualNo).padStart(2, "0");
      if (name.startsWith(`${actualNo}_`)) return key;
    }
    return "";
  }

  function compactRunTime(run) {
    const value = String(run.created_at || run.started_at || "").replace("T", " ");
    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!match) return "未知时间";
    return `${match[2]}${match[3]}-${match[4]}${match[5]}`;
  }

  function runTimeValue(run) {
    const value = Date.parse(run.created_at || run.started_at || run.updated_at || "");
    return Number.isFinite(value) ? value : 0;
  }

  function runSerial(runId) {
    const sorted = [...state.runs].filter(runHasFiles).sort((a, b) => {
      const timeDiff = runTimeValue(b) - runTimeValue(a);
      return timeDiff || String(a.run_id || "").localeCompare(String(b.run_id || ""));
    });
    const index = sorted.findIndex((run) => run.run_id === runId);
    return index >= 0 ? String(index + 1).padStart(3, "0") : "";
  }

  function runDisplayNo(run) {
    const raw = String(run.run_id || "");
    const serial = runSerial(raw);
    return `${serial ? `大任务 ${serial}` : "大任务"} · ${compactRunTime(run)} · ${raw.slice(0, 8)}`;
  }

  function runDisplayById(runId) {
    const index = state.runs.findIndex((run) => run.run_id === runId);
    if (index < 0) return runId ? `R? · ${String(runId).slice(0, 8)}` : "—";
    return runDisplayNo(state.runs[index]);
  }

  function runHasFiles(run) {
    return (run.result || []).some((item) => item.raw_file || item.cleaned_file);
  }

  function runFileCount(run) {
    const paths = new Set();
    (run.result || []).forEach((item) => {
      ["raw_file", "cleaned_file"].forEach((field) => {
        if (item[field]) paths.add(item[field]);
      });
    });
    return paths.size;
  }

  function accountByKey(key) {
    return state.accounts.find((account) => account.key === key);
  }

  function accountLabel(key) {
    const account = accountByKey(key);
    return account ? (account.username || account.name || account.key) : (key || "全部账号");
  }

  function runTaskLabels(run) {
    return (run.task_keys || []).map(taskLabel).join("、") || "全部任务";
  }

  function runAccountLabels(run) {
    return (run.account_keys || []).map(accountLabel).join("、") || "全部启用账号";
  }

  function statusText(status) {
    return {
      pending: "排队中",
      running: "运行中",
      paused: "已暂停",
      succeeded: "成功",
      failed: "失败",
      cancelled: "已取消"
    }[status] || status || "未知";
  }

  function statusClass(status) {
    return {
      pending: "status-pending",
      running: "status-running",
      paused: "status-cancelled",
      succeeded: "status-success",
      failed: "status-failed",
      cancelled: "status-cancelled"
    }[status] || "status-neutral";
  }

  function displayRunStatus(run) {
    if (run.status === "running" && run.pause_requested) return "暂停中";
    return statusText(run.status);
  }

  function runStatusHint(run) {
    if (run.status === "pending") return "等待 worker 领取执行";
    if (run.status === "running" && run.pause_requested) return "已收到暂停请求，当前子任务完成后暂停";
    if (run.status === "running") return "正在执行，不能取消或调整顺序";
    if (run.status === "paused") return "已暂停，不会继续执行；点击继续运行恢复";
    if (run.status === "cancelled") return "已取消，仅保留查看";
    if (run.status === "failed") return "执行失败，可到错误中心查看原因";
    if (run.status === "succeeded") return "执行完成，可到下载中心取文件";
    return "";
  }

  function formatTime(value) {
    if (!value) return "—";
    return String(value).replace("T", " ");
  }

  function formatSize(value) {
    const size = Number(value);
    if (!Number.isFinite(size)) return value || "—";
    if (size < 1024) return `${Math.round(size)} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }

  function fileDownloadUrl(fileId) {
    return `/api/files/${String(fileId).split("/").map(encodeURIComponent).join("/")}/download`;
  }

  function runDownloadUrl(runId) {
    return `/api/runs/${encodeURIComponent(runId)}/files/download`;
  }

  function screenshotUrl(screenshot) {
    if (!screenshot) return "";
    const name = String(screenshot).split(/[\\/]/).pop();
    return `/api/screenshots/${encodeURIComponent(name)}`;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Accept": "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) }
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : response.text();
  }

  async function loadData() {
    try {
      const [health, worker, tasks, accounts, runs, errors, files] = await Promise.all([
        request("/api/health"),
        request("/api/worker"),
        request("/api/tasks"),
        request("/api/accounts?include_disabled=true"),
        request("/api/runs"),
        request("/api/errors"),
        request("/api/files")
      ]);
      state.health = health;
      state.worker = worker;
      state.tasks = tasks;
      state.accounts = accounts;
      state.runs = runs;
      state.errors = errors;
      state.files = files;
      if (!state.accountSelectionInitialized) {
        state.selectedAccountKeys = new Set(accounts.filter((account) => account.enabled !== false).map((account) => account.key));
        state.accountSelectionInitialized = true;
      } else {
        const available = new Set(accounts.filter((account) => account.enabled !== false).map((account) => account.key));
        state.selectedAccountKeys = new Set([...state.selectedAccountKeys].filter((key) => available.has(key)));
      }
      renderAll();
    } catch (error) {
      state.health = { status: "offline", error: error.message };
      state.worker = null;
      renderAll();
    }
  }

  function renderAll() {
    renderConnection();
    renderMetrics();
    renderTaskMode();
    renderTasks();
    renderAccountsSelection();
    renderQueue();
    renderRuns();
    renderErrors();
    renderRunDownloads();
    renderFiles();
    renderAccountsTable();
    updateCounts();
    updateSelectionSummary();
    updateActionState();
    $("#last-refresh").textContent = `最近刷新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  }

  function renderConnection() {
    const online = state.health && state.health.status === "ok";
    const workerOnline = Boolean(state.worker && state.worker.worker_online);
    const backendState = online ? "good" : "bad";
    const workerState = workerOnline ? "good" : "warn";
    $("#metric-card-backend").className = `metric metric-${backendState}`;
    $("#metric-card-worker").className = `metric metric-${workerState}`;
    $("#metric-backend-icon").innerHTML = icon(online ? "check" : "alert");
    $("#metric-worker-icon").innerHTML = icon(workerOnline ? "check" : "alert");
    $("#metric-backend").textContent = online ? "在线" : "离线";
    $("#metric-backend").className = `metric-value ${online ? "value-good" : "value-bad"}`;
    $("#metric-backend-detail").textContent = online ? "API 服务正常响应" : (state.health?.error || "无法连接 API");
    $("#metric-worker").textContent = workerOnline ? "在线" : "离线";
    $("#metric-worker").className = `metric-value ${workerOnline ? "value-good" : "value-warn"}`;
    $("#metric-worker-detail").textContent = workerOnline
      ? `心跳 ${state.worker.heartbeat_age_seconds ?? 0}s 前`
      : "任务会保留在队列中";
    const banner = $("#connection-banner");
    let bannerType = "";
    let bannerMessage = "";
    if (!online) {
      bannerType = "danger";
      bannerMessage = `后端服务不可用：${state.health?.error || "请检查后端地址和服务状态"}`;
    } else if (!workerOnline) {
      bannerType = "warning";
      bannerMessage = "RPA Worker 当前离线。新任务可以进入队列，但不会开始执行。";
    } else {
      banner.innerHTML = "";
      banner.className = "connection-banner hidden";
      state.connectionBannerDismissed = false;
      state.connectionBannerSignature = "";
      return;
    }
    const signature = `${bannerType}:${bannerMessage}`;
    if (signature !== state.connectionBannerSignature) {
      state.connectionBannerDismissed = false;
      state.connectionBannerSignature = signature;
    }
    if (state.connectionBannerDismissed) {
      banner.className = `connection-banner connection-${bannerType} hidden`;
      return;
    }
    banner.innerHTML = `<div class="banner-main">${icon("alert")}<span>${escapeHtml(bannerMessage)}</span></div><button class="banner-close" type="button" data-dismiss-banner aria-label="关闭提醒" title="关闭提醒">${icon("x")}</button>`;
    banner.className = `connection-banner connection-${bannerType}`;
  }

  function renderMetrics() {
    const running = state.runs.find((run) => run.status === "running");
    const pending = state.runs.filter((run) => run.status === "pending");
    $("#metric-card-running").className = `metric ${running ? "metric-running" : "metric-idle"}`;
    $("#metric-card-pending").className = `metric ${pending.length ? "metric-waiting" : "metric-empty"}`;
    $("#metric-running-icon").innerHTML = icon(running ? "play" : "pause");
    $("#metric-pending-icon").innerHTML = icon(pending.length ? "sliders" : "check");
    $("#metric-running").textContent = running ? accountLabel((running.account_keys || [])[0]) : "暂无";
    $("#metric-running-detail").textContent = running ? `${runTaskLabels(running)} · ${running.run_id.slice(0, 8)}` : "没有正在执行的任务";
    $("#metric-pending").textContent = pending.length;
    $("#metric-pending-detail").textContent = pending.length ? `队首：${runAccountLabels(pending[0])}` : "队列为空";
  }

  function renderTasks() {
    const source = TASK_ORDER.map((key) => state.tasks.find((task) => task.task_key === key) || { task_key: key });
    $("#task-list").innerHTML = source.map((task) => {
      const key = task.task_key;
      const meta = taskMeta(key);
      const checked = state.selectedTaskKeys.has(key);
      return `<label class="task-option ${checked ? "selected" : ""}">
        <input type="checkbox" data-task-key="${escapeHtml(key)}" ${checked ? "checked" : ""}>
        <span><strong>任务${meta.displayNo} · ${escapeHtml(meta.title)}</strong><span>实际 RPA 任务 ${meta.actualNo}</span></span>
      </label>`;
    }).join("");
  }

  function renderTaskMode() {
    $("#advanced-task-panel").classList.toggle("hidden", !state.taskPanelOpen);
    $("#task-toggle-button").innerHTML = `${icon("sliders")}<span>${state.taskPanelOpen ? "收起任务细选" : "选择任务"}</span>`;
  }

  function renderAccountsSelection() {
    const enabled = state.accounts.filter((account) => account.enabled !== false);
    const container = $("#account-selection");
    if (!enabled.length) {
      container.innerHTML = `<div class="empty-state empty-state-accounts">${icon("alert")}<strong>暂无启用账号</strong><span>先在账号管理中新增或启用账号</span></div>`;
      return;
    }
    container.innerHTML = enabled.map((account) => {
      const checked = state.selectedAccountKeys.has(account.key);
      return `<label class="account-check ${checked ? "selected" : ""}">
        <input type="checkbox" data-account-key="${escapeHtml(account.key)}" ${checked ? "checked" : ""}>
        <span class="account-check-text"><strong>${escapeHtml(account.username || account.name || account.key)}</strong><span>${escapeHtml(account.key)} · 端口 ${escapeHtml(account.port)}</span></span>
      </label>`;
    }).join("");
  }

  function renderQueue() {
    const runningItems = state.runs.filter((run) => run.status === "running");
    const running = runningItems[0];
    const pending = state.runs.filter((run) => run.status === "pending").sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0));
    const paused = state.runs.filter((run) => run.status === "paused");
    $("#queue-running-count").textContent = runningItems.length;
    $("#queue-pending-count").textContent = pending.length;
    $("#queue-paused-count").textContent = paused.length;
    const queueStatus = $("#queue-status");
    queueStatus.textContent = running ? (running.pause_requested ? "暂停中" : "正在执行") : pending.length ? "等待执行" : paused.length ? "有暂停任务" : "空闲";
    queueStatus.className = `status-chip ${running ? "status-running" : pending.length ? "status-pending" : paused.length ? "status-cancelled" : "status-neutral"}`;
    const group = (title, items, className) => {
      if (!items.length) return "";
      return `<div class="queue-group"><div class="queue-group-title"><span>${title}</span><span>${items.length}</span></div>${items.map((run, index) => queueItem(run, className, index, pending.length)).join("")}</div>`;
    };
    $("#queue-list").innerHTML = group("正在运行", runningItems, "running")
      + group("等待执行", pending, "pending")
      + group("已暂停", paused, "paused")
      + (!running && !pending.length && !paused.length ? `<div class="queue-empty">${icon("check")}<strong>队列空置</strong><span>没有运行中或等待执行的任务</span></div>` : "");
  }

  function queueItem(run, className, index, totalPending) {
    const isPending = run.status === "pending";
    const isPaused = run.status === "paused";
    const isRunning = run.status === "running";
    const controls = isPending
      ? `<div class="queue-controls">
          <button class="icon-action" data-run-action="pause" data-run-id="${escapeHtml(run.run_id)}" title="暂停排队" aria-label="暂停排队">${icon("pause")}</button>
          <button class="icon-action" data-run-action="move-up" data-run-id="${escapeHtml(run.run_id)}" title="上移" aria-label="上移" ${index === 0 ? "disabled" : ""}>${icon("chevron-up")}</button>
          <button class="icon-action" data-run-action="move-down" data-run-id="${escapeHtml(run.run_id)}" title="下移" aria-label="下移" ${index === totalPending - 1 ? "disabled" : ""}>${icon("chevron-down")}</button>
          <button class="icon-action danger" data-run-action="cancel" data-run-id="${escapeHtml(run.run_id)}" title="取消排队" aria-label="取消排队">${icon("x")}</button>
        </div>`
      : isPaused
        ? `<div class="queue-controls">
            <button class="icon-action" data-run-action="resume" data-run-id="${escapeHtml(run.run_id)}" title="继续运行" aria-label="继续运行">${icon("play")}</button>
            <button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(run.run_id)}" title="查看日志" aria-label="查看日志">${icon("file")}</button>
          </div>`
        : `<div class="queue-controls">
            ${isRunning ? `<button class="icon-action" data-run-action="${run.pause_requested ? "resume" : "pause"}" data-run-id="${escapeHtml(run.run_id)}" title="${run.pause_requested ? "继续运行" : "暂停"}" aria-label="${run.pause_requested ? "继续运行" : "暂停"}">${icon(run.pause_requested ? "play" : "pause")}</button>` : ""}
            <button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(run.run_id)}" title="查看日志" aria-label="查看日志">${icon("file")}</button>
          </div>`;
    return `<div class="queue-item ${className}">
      <div class="queue-main">
        <div>
          <strong>${escapeHtml(runAccountLabels(run))}</strong>
          <p>${escapeHtml(runTaskLabels(run))}</p>
          <span class="queue-meta">${escapeHtml(formatTime(run.started_at || run.created_at))} · ${escapeHtml(run.run_id.slice(0, 8))}</span>
        </div>
        <span class="status-chip ${statusClass(run.status)}">${escapeHtml(displayRunStatus(run))}</span>
      </div>
      ${controls}
    </div>`;
  }

  function renderRuns() {
    const body = $("#runs-table");
    if (!state.runs.length) {
      body.innerHTML = `<tr><td colspan="7"><div class="empty-state">${icon("file")}<strong>暂无运行记录</strong><span>任务执行后会自动出现在这里</span></div></td></tr>`;
      $("#runs-pagination").innerHTML = "";
      return;
    }
    const total = state.runs.length;
    const totalPages = Math.max(1, Math.ceil(total / state.runsPageSize));
    state.runsPage = Math.min(state.runsPage, totalPages);
    const start = (state.runsPage - 1) * state.runsPageSize;
    const pageRuns = state.runs.slice(start, start + state.runsPageSize);
    body.innerHTML = pageRuns.map((run) => `<tr>
      <td><span class="cell-main">${escapeHtml(runDisplayNo(run))}</span><span class="cell-sub">${escapeHtml(run.run_id.slice(0, 12))}</span></td>
      <td><span class="status-chip ${statusClass(run.status)}">${escapeHtml(displayRunStatus(run))}</span></td>
      <td><span class="cell-main">${escapeHtml(formatTime(run.started_at || run.created_at))}</span><span class="cell-sub">${run.finished_at ? `结束 ${escapeHtml(formatTime(run.finished_at))}` : "尚未结束"}</span></td>
      <td>${escapeHtml(runAccountLabels(run))}</td>
      <td><span class="cell-sub">${escapeHtml(runTaskLabels(run))}</span></td>
      <td><span class="cell-sub" title="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id.slice(0, 12))}</span></td>
      <td><div class="table-actions">
        ${runHasFiles(run)
          ? `<a class="icon-action download-link" href="${runDownloadUrl(run.run_id)}" download title="下载本次全部文件" aria-label="下载本次全部文件">${icon("download")}</a>`
          : `<button class="icon-action" type="button" disabled title="本次暂无文件" aria-label="本次暂无文件">${icon("download")}</button>`}
        <button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(run.run_id)}" title="查看日志" aria-label="查看日志">${icon("file")}</button>
      </div></td>
    </tr>`).join("");
    const currentStart = start + 1;
    const currentEnd = Math.min(total, start + pageRuns.length);
    $("#runs-pagination").innerHTML = `<div class="pagination-summary">第 ${currentStart}-${currentEnd} 项，共 ${total} 项</div>
      <div class="pagination-actions">
        <button class="mini-button" data-runs-page="prev" type="button" ${state.runsPage <= 1 ? "disabled" : ""}>上一页</button>
        <button class="mini-button" data-runs-page="next" type="button" ${state.runsPage >= totalPages ? "disabled" : ""}>下一页</button>
      </div>`;
  }

  function normalizeError(error) {
    const taskKey = error.task_key || error.task || "";
    const accountKey = error.account_key || error.account || "";
    return {
      ...error,
      taskKey,
      accountKey,
      message: error.message || error.error || "未提供错误原因",
      screenshot: error.screenshot || error.screenshot_id || ""
    };
  }

  function renderErrors() {
    const body = $("#errors-table");
    if (!state.errors.length) {
      body.innerHTML = `<tr><td colspan="6"><div class="empty-state">${icon("check")}<strong>暂无错误记录</strong><span>失败原因、截图和日志会集中保留</span></div></td></tr>`;
      return;
    }
    body.innerHTML = state.errors.map((raw) => {
      const error = normalizeError(raw);
      const shot = screenshotUrl(error.screenshot);
      return `<tr>
        <td>${escapeHtml(formatTime(error.created_at || error.finished_at || error.updated_at))}</td>
        <td>${escapeHtml(accountLabel(error.accountKey))}</td>
        <td>${escapeHtml(taskLabel(error.taskKey))}</td>
        <td><span class="cell-sub" title="${escapeHtml(error.message)}">${escapeHtml(String(error.message).split("\n")[0])}</span></td>
        <td><span class="cell-sub">${escapeHtml(String(error.run_id || "").slice(0, 12))}</span></td>
        <td><div class="table-actions">
          ${shot ? `<a class="icon-action" href="${shot}" target="_blank" rel="noreferrer" title="查看截图" aria-label="查看截图">${icon("image")}</a>` : ""}
          ${error.run_id ? `<button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(error.run_id)}" title="查看日志" aria-label="查看日志">${icon("file")}</button>` : ""}
        </div></td>
      </tr>`;
    }).join("");
  }

  function renderRunDownloads() {
    const container = $("#run-download-list");
    if (!container) return;
    const runs = state.runs
      .filter(runHasFiles)
      .sort((a, b) => runTimeValue(b) - runTimeValue(a));
    if (!runs.length) {
      container.innerHTML = `<div class="run-download-empty">${icon("download")}<strong>暂无大任务文件包</strong><span>任务完成并产出文件后会显示在这里</span></div>`;
      return;
    }
    container.innerHTML = runs.map((run) => `<div class="run-download-item">
      <div class="run-download-copy">
        <strong>${escapeHtml(runDisplayNo(run))}</strong>
        <span>${escapeHtml(runAccountLabels(run))}</span>
        <span>${escapeHtml(runTaskLabels(run))}</span>
      </div>
      <div class="run-download-side">
        <span>${escapeHtml(runFileCount(run))} 个文件</span>
        <a class="mini-button mini-button-primary" href="${runDownloadUrl(run.run_id)}" download>${icon("download")}下载全部</a>
      </div>
    </div>`).join("");
  }

  function renderFiles() {
    const body = $("#files-table");
    if (!state.files.length) {
      body.innerHTML = `<tr><td colspan="8"><div class="empty-state">${icon("download")}<strong>暂无可下载文件</strong><span>任务完成后会显示 raw / cleaned 文件</span></div></td></tr>`;
      $("#files-pagination").innerHTML = "";
      return;
    }
    const total = state.files.length;
    const totalPages = Math.max(1, Math.ceil(total / state.filesPageSize));
    state.filesPage = Math.min(state.filesPage, totalPages);
    const start = (state.filesPage - 1) * state.filesPageSize;
    const pageFiles = state.files.slice(start, start + state.filesPageSize);
    body.innerHTML = pageFiles.map((file) => {
      const taskKey = taskKeyFromFile(file);
      const parts = String(file.file_id || "").split("/");
      const kind = parts.includes("raw") ? "raw" : parts.includes("cleaned") ? "cleaned" : "文件";
      const accountKey = parts.find((part) => accountByKey(part)) || "";
      const runId = file.run_id || "";
      return `<tr>
        <td><span class="cell-main">${escapeHtml(file.name || parts.at(-1))}</span><span class="cell-sub" title="${escapeHtml(file.path || "")}">${escapeHtml(file.path || file.file_id || "")}</span></td>
        <td>${escapeHtml(kind)}</td>
        <td>${escapeHtml(accountLabel(accountKey))}</td>
        <td>${escapeHtml(taskKey ? taskLabel(taskKey) : "—")}</td>
        <td>${escapeHtml(runDisplayById(runId))}</td>
        <td>${escapeHtml(formatTime(file.updated_at))}</td>
        <td>${escapeHtml(formatSize(file.size))}</td>
        <td><a class="icon-action download-link" href="${fileDownloadUrl(file.file_id)}" download title="下载文件" aria-label="下载文件">${icon("download")}</a></td>
      </tr>`;
    }).join("");
    const currentStart = total === 0 ? 0 : start + 1;
    const currentEnd = Math.min(total, start + pageFiles.length);
    $("#files-pagination").innerHTML = `<div class="pagination-summary">第 ${currentStart}-${currentEnd} 项，共 ${total} 项</div>
      <div class="pagination-actions">
        <button class="mini-button" data-files-page="prev" type="button" ${state.filesPage <= 1 ? "disabled" : ""}>上一页</button>
        <button class="mini-button" data-files-page="next" type="button" ${state.filesPage >= totalPages ? "disabled" : ""}>下一页</button>
      </div>`;
  }

  function renderAccountsTable() {
    const body = $("#accounts-table");
    if (!state.accounts.length) {
      body.innerHTML = `<tr><td colspan="7"><div class="empty-state">${icon("plus")}<strong>暂无账号</strong><span>点击右上角新增账号</span></div></td></tr>`;
      return;
    }
    body.innerHTML = state.accounts.map((account) => `<tr>
      <td><span class="cell-main">${escapeHtml(account.username || account.name || account.key)}</span><span class="cell-sub">${escapeHtml(account.name || "")}</span></td>
      <td>${escapeHtml(account.key)}</td>
      <td>${escapeHtml(account.port)}</td>
      <td><span class="status-chip ${account.browser_status === "占用中" ? "status-running" : "status-neutral"}">${escapeHtml(account.browser_status || "空闲")}</span></td>
      <td>${account.enabled !== false ? "启用" : "停用"}</td>
      <td>${account.username_set ? "账号已填" : "账号未填"} / ${account.password_set ? "密码已填" : "密码未填"}</td>
      <td><div class="table-actions"><button class="icon-action" data-edit-account="${escapeHtml(account.key)}" title="编辑账号" aria-label="编辑账号">${icon("edit")}</button></div></td>
    </tr>`).join("");
  }

  function updateCounts() {
    $("#runs-count").textContent = state.runs.length;
    $("#errors-count").textContent = state.errors.length;
    $("#files-count").textContent = state.files.length;
    $("#accounts-count").textContent = state.accounts.length;
  }

  function updateSelectionSummary() {
    const accountText = `已选 ${state.selectedAccountKeys.size} 个账号`;
    const taskText = state.selectedTaskKeys.size === TASK_ORDER.length
      ? "默认执行任务1-6"
      : `已选 ${state.selectedTaskKeys.size} 个任务`;
    $("#selection-summary").textContent = `${accountText} · ${taskText}`;
    $("#task-selection-summary").textContent = state.selectedTaskKeys.size === TASK_ORDER.length
      ? "当前选择：任务1-任务6"
      : `当前选择：${state.selectedTaskKeys.size} 个任务`;
  }

  function updateActionState() {
    const backendOnline = state.health?.status === "ok";
    const hasTasks = state.selectedTaskKeys.size > 0;
    const hasAccounts = state.selectedAccountKeys.size > 0;
    $("#full-run-button").disabled = !backendOnline || !hasAccounts;
    $("#selected-run-button").disabled = !backendOnline || !hasTasks || !hasAccounts;
  }

  function onFilesPageChange(direction) {
    const totalPages = Math.max(1, Math.ceil(state.files.length / state.filesPageSize));
    if (direction === "prev" && state.filesPage > 1) state.filesPage -= 1;
    if (direction === "next" && state.filesPage < totalPages) state.filesPage += 1;
    renderFiles();
  }

  function onRunsPageChange(direction) {
    const totalPages = Math.max(1, Math.ceil(state.runs.length / state.runsPageSize));
    if (direction === "prev" && state.runsPage > 1) state.runsPage -= 1;
    if (direction === "next" && state.runsPage < totalPages) state.runsPage += 1;
    renderRuns();
  }

  async function createRun(taskKeys) {
    const accountKeys = [...state.selectedAccountKeys];
    if (!accountKeys.length) return showToast("请至少选择一个启用账号", true);
    if (!taskKeys.length) return showToast("请至少选择一个任务", true);
    try {
      await request("/api/runs", {
        method: "POST",
        body: JSON.stringify({ task_keys: taskKeys, account_keys: accountKeys, force_account_tasks: true, headed: $("#headed-input").checked })
      });
      showToast("任务已进入队列");
      await loadData();
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    }
  }

  async function runAction(action, runId) {
    if (action === "logs") return openLog(runId);
    const run = state.runs.find((item) => item.run_id === runId);
    const endpoint = { cancel: "cancel", "move-up": "move-up", "move-down": "move-down" }[action];
    const pauseEndpoint = { pause: "pause", resume: "resume" }[action];
    if (pauseEndpoint) {
      try {
        await request(`/api/runs/${encodeURIComponent(runId)}/${pauseEndpoint}`, { method: "POST", body: "{}" });
        showToast(action === "pause"
          ? (run?.status === "pending" ? "已暂停排队" : "已请求暂停")
          : "已恢复运行");
        await loadData();
      } catch (error) {
        showToast(`操作失败：${error.message}`, true);
      }
      return;
    }
    if (!endpoint) return;
    try {
      await request(`/api/runs/${encodeURIComponent(runId)}/${endpoint}`, { method: "POST", body: "{}" });
      showToast(action === "cancel" ? "已取消排队" : "队列顺序已更新");
      await loadData();
    } catch (error) {
      showToast(`操作失败：${error.message}`, true);
    }
  }

  async function openLog(runId) {
    $("#log-modal-title").textContent = `日志 · ${runId.slice(0, 12)}`;
    $("#log-content").textContent = "正在读取日志...";
    $("#log-modal").classList.remove("hidden");
    try {
      const text = await request(`/api/runs/${encodeURIComponent(runId)}/logs`);
      $("#log-content").textContent = text || "暂无日志内容";
    } catch (error) {
      $("#log-content").textContent = `日志读取失败：${error.message}`;
    }
  }

  function openAccountModal(account) {
    const editing = Boolean(account);
    state.editingAccountKey = account?.key || "";
    $("#account-modal-title").textContent = editing ? "编辑账号" : "新增账号";
    $("#account-modal-hint").textContent = editing
      ? "编辑已有账号时可调整系统字段；密码留空表示不修改。"
      : "新增账号只需填写手机号和登录密码，系统会自动分配账号 key、浏览器端口和目录。";
    $$(".account-system-field").forEach((item) => item.classList.toggle("hidden", !editing));
    $("#account-key").value = account?.key || "";
    $("#account-key").readOnly = editing;
    $("#account-key").required = editing;
    $("#account-username").value = account?.username || "";
    $("#account-name").value = account?.name || "";
    $("#account-port").value = account?.port || "";
    $("#account-port").required = editing;
    $("#account-profile").value = account?.profile_dir || "";
    $("#account-download").value = account?.download_dir || "";
    $("#account-password").value = "";
    $("#account-password").required = !editing;
    $("#account-note").value = account?.note || "";
    $("#account-enabled").checked = editing ? account.enabled !== false : true;
    $("#account-modal").classList.remove("hidden");
  }

  async function saveAccount(event) {
    event.preventDefault();
    const editing = Boolean(state.editingAccountKey);
    const data = {
      key: $("#account-key").value.trim(),
      username: $("#account-username").value.trim(),
      name: $("#account-name").value.trim(),
      port: Number($("#account-port").value),
      profile_dir: $("#account-profile").value.trim() || undefined,
      download_dir: $("#account-download").value.trim() || undefined,
      password: $("#account-password").value,
      note: $("#account-note").value.trim(),
      enabled: $("#account-enabled").checked
    };
    if (!data.username) return showToast("手机号不能为空", true);
    if (!editing && !data.password) return showToast("新增账号需要填写登录密码", true);
    if (editing && (!data.key || !data.port)) return showToast("账号 key 和端口不能为空", true);
    if (!editing) {
      delete data.key;
      delete data.port;
      delete data.profile_dir;
      delete data.download_dir;
    }
    if (!data.password) delete data.password;
    if (!data.name) delete data.name;
    Object.keys(data).forEach((key) => data[key] === undefined && delete data[key]);
    try {
      await request(editing ? `/api/accounts/${encodeURIComponent(state.editingAccountKey)}` : "/api/accounts", {
        method: editing ? "PATCH" : "POST",
        body: JSON.stringify(editing ? Object.fromEntries(Object.entries(data).filter(([key]) => key !== "key")) : data)
      });
      closeModal("account-modal");
      showToast("账号已保存");
      state.accountSelectionInitialized = false;
      await loadData();
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
    }
  }

  function closeModal(id) {
    $(`#${id}`).classList.add("hidden");
  }

  function showToast(message, error = false) {
    const toast = document.createElement("div");
    toast.className = `toast${error ? " error" : ""}`;
    toast.textContent = message;
    $("#toast-region").appendChild(toast);
    window.setTimeout(() => toast.remove(), 3400);
  }

  function bindEvents() {
    $("#refresh-button").addEventListener("click", loadData);
    $("#task-toggle-button").addEventListener("click", () => {
      state.taskPanelOpen = !state.taskPanelOpen;
      renderAll();
    });
    $("#files-page-size").addEventListener("change", (event) => {
      state.filesPageSize = Number(event.target.value) || 10;
      state.filesPage = 1;
      renderFiles();
    });
    $("#runs-page-size").addEventListener("change", (event) => {
      state.runsPageSize = Number(event.target.value) || 10;
      state.runsPage = 1;
      renderRuns();
    });
    $("#select-all-tasks").addEventListener("click", () => { state.selectedTaskKeys = new Set(TASK_ORDER); renderAll(); });
    $("#clear-tasks").addEventListener("click", () => { state.selectedTaskKeys.clear(); renderAll(); });
    $("#select-all-accounts").addEventListener("click", () => {
      state.selectedAccountKeys = new Set(state.accounts.filter((account) => account.enabled !== false).map((account) => account.key));
      renderAll();
    });
    $("#clear-accounts").addEventListener("click", () => { state.selectedAccountKeys.clear(); renderAll(); });
    $("#full-run-button").addEventListener("click", () => createRun(TASK_ORDER));
    $("#selected-run-button").addEventListener("click", () => createRun([...state.selectedTaskKeys]));
    $("#add-account-button").addEventListener("click", () => openAccountModal(null));
    $("#account-form").addEventListener("submit", saveAccount);

    document.addEventListener("change", (event) => {
      const taskKey = event.target.dataset?.taskKey;
      const accountKey = event.target.dataset?.accountKey;
      if (taskKey) {
        event.target.checked ? state.selectedTaskKeys.add(taskKey) : state.selectedTaskKeys.delete(taskKey);
        renderAll();
      }
      if (accountKey) {
        event.target.checked ? state.selectedAccountKeys.add(accountKey) : state.selectedAccountKeys.delete(accountKey);
        renderAll();
      }
    });
    document.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-tab]");
      if (tab) {
        state.activeTab = tab.dataset.tab;
        $$(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === state.activeTab));
        $$(".tab-panel").forEach((item) => item.classList.toggle("active", item.id === `tab-${state.activeTab}`));
      }
      const action = event.target.closest("[data-run-action]");
      if (action) runAction(action.dataset.runAction, action.dataset.runId);
      const edit = event.target.closest("[data-edit-account]");
      if (edit) openAccountModal(accountByKey(edit.dataset.editAccount));
      const close = event.target.closest("[data-close-modal]");
      if (close) closeModal(close.dataset.closeModal);
      const filePage = event.target.closest("[data-files-page]");
      if (filePage) onFilesPageChange(filePage.dataset.filesPage);
      const runPage = event.target.closest("[data-runs-page]");
      if (runPage) onRunsPageChange(runPage.dataset.runsPage);
      const dismiss = event.target.closest("[data-dismiss-banner]");
      if (dismiss) {
        state.connectionBannerDismissed = true;
        $("#connection-banner").classList.add("hidden");
      }
      if (event.target.classList.contains("modal-backdrop")) closeModal(event.target.id);
    });
  }

  bindEvents();
  loadData();
  window.setInterval(loadData, 3000);
})();
