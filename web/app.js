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
  const TASK_FOLDERS = {
    "realtime-inventory": "实时库存",
    "pincang-detail": "库存分析",
    "system-order": "系统单",
    "po-list": "补货单列表",
    "channel-goods": "库位明细",
    "transfer-order": "调拨单"
  };

  const savedView = localStorage.getItem("maochao_view");
  const state = {
    accounts: [],
    runs: [],
    errors: [],
    files: [],
    worker: null,
    health: null,
    activeView: savedView === "admin" ? "repair" : (savedView || "home"),
    selectedOperatorId: localStorage.getItem("maochao_operator_id") || "",
    selectedSupplierKeys: new Set(),
    operators: [],
    accountSuppliers: [],
    assignedSuppliers: [],
    supplierSelectionInitialized: false,
    editingAccountKey: "",
    connectionBannerDismissed: false,
    connectionBannerSignature: "",
    cabinet: { operatorId: "", date: "", folder: "" },
    cabinetTouched: false,
    cabinetSupplierFilter: "",
    cabinetLayerFiles: [],
    retryingKeys: new Set(),
    cellDetail: "",
    pendingRunOptions: null,
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

  function taskFolder(taskKey) {
    return TASK_FOLDERS[taskKey] || taskMeta(taskKey).title || taskKey || "其他";
  }

  function shortSupplierName(value) {
    const text = String(value || "").trim();
    if (!text) return "未知供应商";
    const match = text.match(/^\d+-(.+)$/);
    return match ? match[1] : text;
  }

  function todayStamp() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  }

  function dateStamp(value) {
    const text = String(value || "").replace("T", " ");
    const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
    return match ? match[1] : "";
  }

  function isTaskRun(run) {
    return (run.run_kind || "tasks") !== "sync_suppliers";
  }

  function taskKeyFromFile(file) {
    const name = String(file.name || file.file_id || "");
    for (const key of TASK_ORDER) {
      const actualNo = String(taskMeta(key).actualNo).padStart(2, "0");
      if (name.startsWith(`${actualNo}_`)) return key;
    }
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

  function supplierKey(accountKey, supplierId) {
    return `${accountKey}::${supplierId}`;
  }

  function enabledAccounts() {
    return state.accounts.filter((account) => account.enabled !== false);
  }

  function runnableSuppliers() {
    return state.assignedSuppliers.filter((item) => item.visible);
  }

  function operatorRuns() {
    return state.runs.filter((run) => {
      if (!isTaskRun(run)) return false;
      if (!state.selectedOperatorId) return true;
      if (!run.operator_id) return true;
      return run.operator_id === state.selectedOperatorId;
    });
  }

  function liveTaskRuns() {
    return state.runs.filter((run) => isTaskRun(run) && ["pending", "running", "paused"].includes(run.status));
  }

  function runOperatorName(run) {
    if (run.operator_name) return run.operator_name;
    const operator = state.operators.find((item) => item.operator_id === run.operator_id);
    return operator?.name || "其他组员";
  }

  function liveRuns() {
    return operatorRuns().filter((run) => ["pending", "running", "paused"].includes(run.status));
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

  async function requestOptional(path, fallback) {
    try {
      return await request(path);
    } catch (error) {
      console.warn(`optional ${path} failed:`, error.message);
      return fallback;
    }
  }

  async function loadData() {
    try {
      const health = await request("/api/health");
      const [worker, accounts, runs, errors, files, operators, accountSuppliers] = await Promise.all([
        request("/api/worker"),
        request("/api/accounts?include_disabled=true"),
        request("/api/runs"),
        request("/api/errors"),
        request("/api/files"),
        requestOptional("/api/operators", []),
        requestOptional("/api/suppliers", [])
      ]);
      state.health = health;
      state.worker = worker;
      state.accounts = accounts;
      state.runs = runs;
      state.errors = errors;
      state.files = files;
      state.operators = operators || [];
      state.accountSuppliers = accountSuppliers || [];
      if (state.selectedOperatorId && !operators.some((item) => item.operator_id === state.selectedOperatorId)) {
        state.selectedOperatorId = operators[0]?.operator_id || "";
      } else if (!state.selectedOperatorId && operators.length) {
        state.selectedOperatorId = operators[0].operator_id;
      }
      if (state.selectedOperatorId) localStorage.setItem("maochao_operator_id", state.selectedOperatorId);
      await loadAssignedSuppliers();
      renderAll();
    } catch (error) {
      state.health = { status: "offline", error: error.message };
      state.worker = null;
      renderAll();
    }
  }

  async function loadAssignedSuppliers() {
    if (!state.selectedOperatorId) {
      state.assignedSuppliers = [];
      state.selectedSupplierKeys.clear();
      return;
    }
    const rows = await request(`/api/operators/${encodeURIComponent(state.selectedOperatorId)}/suppliers`);
    const seen = new Set();
    state.assignedSuppliers = (rows || []).filter((item) => {
      const key = supplierKey(item.account_key, item.supplier_id);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const runnableKeys = new Set(
      state.assignedSuppliers
        .filter((item) => item.visible)
        .map((item) => supplierKey(item.account_key, item.supplier_id))
    );
    if (!state.supplierSelectionInitialized) {
      state.selectedSupplierKeys = runnableKeys;
      state.supplierSelectionInitialized = true;
    } else {
      const next = new Set([...state.selectedSupplierKeys].filter((key) => runnableKeys.has(key)));
      state.selectedSupplierKeys = next.size ? next : runnableKeys;
    }
  }

  function setView(view) {
    state.activeView = view === "settings" || view === "repair" ? view : "home";
    localStorage.setItem("maochao_view", state.activeView);
    renderNav();
    renderConnection();
  }

  function renderNav() {
    $$("[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === state.activeView));
    $("#view-home")?.classList.toggle("hidden", state.activeView !== "home");
    $("#view-settings")?.classList.toggle("hidden", state.activeView !== "settings");
    $("#view-repair")?.classList.toggle("hidden", state.activeView !== "repair");
  }

  function renderAll() {
    renderNav();
    renderConnection();
    renderToday();
    renderOperators();
    renderSupplierSelection();
    renderSuppliersTable();
    renderProgressBoard();
    renderCabinet();
    renderRepair();
    renderAccountsTable();
  }

  function renderConnection() {
    const online = state.health && state.health.status === "ok";
    const banner = $("#connection-banner");
    if (!banner) return;
    if (online) {
      banner.innerHTML = "";
      banner.className = "connection-banner hidden";
      state.connectionBannerDismissed = false;
      state.connectionBannerSignature = "";
      return;
    }
    const message = "服务离线";
    const signature = `danger:${message}`;
    if (signature !== state.connectionBannerSignature) {
      state.connectionBannerDismissed = false;
      state.connectionBannerSignature = signature;
    }
    if (state.connectionBannerDismissed) {
      banner.className = "connection-banner connection-danger hidden";
      return;
    }
    banner.innerHTML = `<div class="banner-main">${icon("alert")}<span>${escapeHtml(message)}</span></div><button class="banner-close" type="button" data-dismiss-banner aria-label="关闭提醒">${icon("x")}</button>`;
    banner.className = "connection-banner connection-danger";
  }

  function preferredFiles() {
    const groups = new Map();
    state.files.forEach((file) => {
      const taskKey = file.task_key || taskKeyFromFile(file);
      if (!taskKey || !TASK_FOLDERS[taskKey]) return;
      const day = dateStamp(file.updated_at) || "";
      const key = `${file.operator_id || ""}::${day}::${file.account_key || ""}::${file.supplier_id || file.supplier_name || ""}::${taskKey}`;
      const rank = file.kind === "cleaned" ? 2 : file.kind === "raw" ? 1 : 0;
      const current = groups.get(key);
      const currentRank = current ? (current.kind === "cleaned" ? 2 : current.kind === "raw" ? 1 : 0) : -1;
      if (!current || rank > currentRank || (rank === currentRank && String(file.updated_at || "") >= String(current.updated_at || ""))) {
        groups.set(key, { ...file, task_key: taskKey });
      }
    });
    return [...groups.values()];
  }

  function fileForCell(row, taskKey) {
    const day = todayStamp();
    const matches = preferredFiles().filter((file) => {
      if ((file.task_key || taskKeyFromFile(file)) !== taskKey) return false;
      if (dateStamp(file.updated_at) !== day) return false;
      if (state.selectedOperatorId && file.operator_id && file.operator_id !== state.selectedOperatorId) return false;
      if (row.supplier_id && file.supplier_id && String(file.supplier_id) !== String(row.supplier_id)) return false;
      if (row.account_key && file.account_key && file.account_key !== row.account_key) return false;
      return true;
    });
    matches.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    return matches[0] || null;
  }

  function downloadFiles(files) {
    if (!files.length) return;
    files.forEach((file, index) => {
      window.setTimeout(() => {
        downloadOneFile(file);
      }, index * 280);
    });
  }

  async function downloadOneFile(file) {
    const name = file.download_name || cabinetFileName(file) || "download.xlsx";
    try {
      const response = await fetch(fileDownloadUrl(file.file_id));
      if (!response.ok) return;
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => {
        URL.revokeObjectURL(url);
        link.remove();
      }, 1500);
    } catch (_) {}
  }

  function todayBoard() {
    const today = todayStamp();
    const todayRuns = operatorRuns().filter((run) => {
      const stamp = dateStamp(run.created_at || run.started_at || run.updated_at);
      return stamp === today || ["pending", "running", "paused"].includes(run.status);
    }).sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    const rows = [];
    const seen = new Set();
    const pushRow = (supplierId, supplierName, accountKey) => {
      if (!supplierId && !supplierName) return;
      const key = supplierKey(accountKey || "", supplierId || supplierName || "");
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({
        key,
        supplier_id: supplierId || "",
        supplier_name: supplierName || supplierId || "未知供应商",
        account_key: accountKey || ""
      });
    };
    runnableSuppliers().forEach((item) => pushRow(item.supplier_id, item.supplier_name, item.account_key));
    todayRuns.forEach((run) => {
      (run.suppliers || []).forEach((item) => pushRow(item.supplier_id, item.supplier_name, item.account_key || (run.account_keys || [])[0]));
      (run.result || []).forEach((item) => pushRow(item.supplier_id, item.supplier_name, item.account || item.account_key));
    });
    const cells = {};
    const cellId = (rowKey, taskKey) => `${rowKey}::${taskKey}`;
    todayRuns.forEach((run) => {
      (run.result || []).forEach((item) => {
        const taskKey = item.task || item.task_key || "";
        if (!TASK_FOLDERS[taskKey]) return;
        const rowKey = supplierKey(item.account || item.account_key || (run.account_keys || [])[0] || "", item.supplier_id || item.supplier_name || "");
        const ok = item.status === "ok";
        const hasFile = Boolean(item.raw_file || item.cleaned_file);
        if (!ok) cells[cellId(rowKey, taskKey)] = { kind: "failed", label: "重试", note: item.note || item.error || "失败" };
        else if (!hasFile) cells[cellId(rowKey, taskKey)] = { kind: "empty", label: "无数据", note: item.note || "无数据" };
        else cells[cellId(rowKey, taskKey)] = { kind: "ok", label: "成功", note: item.note || "已下载" };
      });
    });
    todayRuns.forEach((run) => {
      if (!["pending", "running", "paused"].includes(run.status)) return;
      const suppliers = (run.suppliers || []).length
        ? run.suppliers
        : (run.result || []).map((item) => ({
          supplier_id: item.supplier_id,
          supplier_name: item.supplier_name,
          account_key: item.account || item.account_key
        }));
      const done = new Set(
        (run.result || []).map((item) => cellId(
          supplierKey(item.account || item.account_key || (run.account_keys || [])[0] || "", item.supplier_id || item.supplier_name || ""),
          item.task || item.task_key || ""
        ))
      );
      const tasks = (run.task_keys || []).filter((key) => TASK_FOLDERS[key]);
      suppliers.forEach((item) => {
        const rowKey = supplierKey(item.account_key || (run.account_keys || [])[0] || "", item.supplier_id || item.supplier_name || "");
        tasks.forEach((taskKey) => {
          const id = cellId(rowKey, taskKey);
          if (done.has(id)) return;
          if (run.status === "paused" || run.pause_requested) cells[id] = { kind: "pending", label: "已暂停", note: "已暂停" };
          else if (run.status === "pending") cells[id] = { kind: "pending", label: "排队", note: "排队中" };
          else cells[id] = { kind: "running", label: "进行中", note: "正在下载" };
        });
      });
    });
    return { rows, cells, today };
  }

  function renderToday() {
    const hero = $("#today-hero");
    const title = $("#hero-title");
    const hint = $("#run-hint");
    const button = $("#full-run-button");
    const label = $("#full-run-label");
    const cancel = $("#cancel-run-button");
    if (!hero || !hint || !button) return;

    const online = state.health && state.health.status === "ok";
    const workerOnline = Boolean(state.worker && state.worker.worker_online);
    const hasOperator = Boolean(state.selectedOperatorId);
    const hasAccounts = enabledAccounts().length > 0;
    const hasSuppliers = state.selectedSupplierKeys.size > 0;
    const live = liveRuns();
    const running = live.find((run) => run.status === "running") || live[0];
    const machineLive = liveTaskRuns().find((run) => run.status === "running") || liveTaskRuns()[0];
    const otherLive = machineLive && (!running || machineLive.run_id !== running.run_id) ? machineLive : null;
    const board = todayBoard();
    const cellList = Object.values(board.cells);
    const doneCount = cellList.filter((item) => item.kind === "ok" || item.kind === "empty").length;
    const totalCount = board.rows.length * TASK_ORDER.length;
    const canStart = online && workerOnline && hasOperator && hasAccounts && hasSuppliers && !machineLive;

    hero.classList.remove("is-running", "is-done", "is-fail");
    cancel.classList.add("hidden");

    if (!online) {
      title.textContent = "服务离线";
      hint.textContent = "";
      label.textContent = "开始下载";
      button.disabled = true;
      return;
    }
    if (running) {
      hero.classList.add("is-running");
      title.textContent = running.status === "paused" ? "已暂停" : running.status === "pending" ? "排队中" : "正在下载";
      hint.textContent = otherLive
        ? `${runOperatorName(otherLive)}正在下载，本任务保留在队列中`
        : `${state.selectedSupplierKeys.size} 家 · 任务 1–6`;
      label.textContent = "等待完成";
      button.disabled = true;
      cancel.classList.remove("hidden");
      cancel.dataset.runId = running.run_id;
      cancel.textContent = running.status === "pending" ? "取消" : "暂停";
      return;
    }
    if (otherLive) {
      hero.classList.add("is-running");
      title.textContent = "机器忙";
      hint.textContent = `${runOperatorName(otherLive)}${otherLive.status === "pending" ? "已排队" : otherLive.status === "paused" ? "已暂停" : "正在下载"}`;
      label.textContent = "等待机器";
      button.disabled = true;
      return;
    }
    if (!workerOnline) {
      title.textContent = "下载服务离线";
      hint.textContent = "";
      label.textContent = "开始下载";
      button.disabled = true;
      return;
    }
    if (!hasOperator) {
      title.textContent = "未选组员";
      hint.textContent = "";
      label.textContent = "开始下载";
      button.disabled = true;
      return;
    }
    if (!hasAccounts) {
      title.textContent = "账号未启用";
      hint.textContent = "";
      label.textContent = "开始下载";
      button.disabled = true;
      return;
    }
    if (!hasSuppliers) {
      title.textContent = "无供应商";
      hint.textContent = "";
      label.textContent = "开始下载";
      button.disabled = true;
      return;
    }

    if (totalCount && doneCount === totalCount) {
      hero.classList.add("is-done");
      title.textContent = "已完成";
      hint.textContent = `${state.selectedSupplierKeys.size} 家 · 任务 1–6`;
      label.textContent = "重新下载";
    } else {
      title.textContent = "开始下载";
      hint.textContent = `${state.selectedSupplierKeys.size} 家 · 任务 1–6`;
      label.textContent = doneCount ? "重新下载" : "开始下载";
    }
    button.disabled = !canStart;
  }

  function setScrollable(element, count) {
    if (!element) return;
    element.classList.toggle("is-scroll", count > 10);
  }

  async function selectOperator(operatorId) {
    state.selectedOperatorId = operatorId;
    localStorage.setItem("maochao_operator_id", state.selectedOperatorId);
    state.supplierSelectionInitialized = false;
    state.cabinet = { operatorId: state.selectedOperatorId, date: "", folder: "" };
    state.cabinetTouched = false;
    await loadAssignedSuppliers();
    renderAll();
  }

  function renderOperators() {
    const select = $("#operator-select");
    const list = $("#operator-list");
    if (select) {
      if (!state.operators.length) {
        select.innerHTML = `<option value="">未选择</option>`;
      } else {
        select.innerHTML = state.operators.map((item) =>
          `<option value="${escapeHtml(item.operator_id)}" ${item.operator_id === state.selectedOperatorId ? "selected" : ""}>${escapeHtml(item.name)}</option>`
        ).join("");
      }
    }
    if (!list) return;
    if (!state.operators.length) {
      list.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
      setScrollable(list, 0);
      return;
    }
    list.innerHTML = state.operators.map((item) =>
      `<button class="pick-item ${item.operator_id === state.selectedOperatorId ? "active" : ""}" type="button" data-operator-id="${escapeHtml(item.operator_id)}"><span>${escapeHtml(item.name)}</span></button>`
    ).join("");
    setScrollable(list, state.operators.length);
  }

  function renderSupplierSelection() {
    const container = $("#supplier-selection");
    if (!container) return;
    const runnable = runnableSuppliers();
    if (!runnable.length) {
      container.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
      setScrollable(container, 0);
      return;
    }
    container.innerHTML = runnable.map((item) => {
      const key = supplierKey(item.account_key, item.supplier_id);
      const checked = state.selectedSupplierKeys.has(key);
      return `<label class="pick-item ${checked ? "active" : ""}">
        <input type="checkbox" data-supplier-key="${escapeHtml(key)}" ${checked ? "checked" : ""}>
        <span>${escapeHtml(shortSupplierName(item.supplier_name || item.supplier_id))}</span>
      </label>`;
    }).join("");
    setScrollable(container, runnable.length);
  }

  function renderSuppliersTable() {
    const body = $("#suppliers-table");
    if (!body) return;
    const rows = state.accountSuppliers.filter((item) => item.visible !== false);
    if (!rows.length) {
      body.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
      setScrollable(body, 0);
      return;
    }
    const assigned = new Set(state.assignedSuppliers.map((item) => supplierKey(item.account_key, item.supplier_id)));
    const canAssign = Boolean(state.selectedOperatorId);
    body.innerHTML = rows.map((item) => {
      const key = supplierKey(item.account_key, item.supplier_id);
      const mine = assigned.has(key);
      return `<label class="pick-item ${mine ? "active" : ""}">
        <input type="checkbox" data-assign-supplier="${escapeHtml(key)}" ${mine ? "checked" : ""} ${canAssign ? "" : "disabled"}>
        <span>${escapeHtml(shortSupplierName(item.supplier_name || item.supplier_id))}</span>
      </label>`;
    }).join("");
    setScrollable(body, rows.length);
  }

  function renderProgressBoard() {
    const board = $("#progress-board");
    const caption = $("#progress-caption");
    const detail = $("#cell-detail");
    if (!board) return;
    const { rows, cells } = todayBoard();
    if (!rows.length) {
      board.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
      if (caption) caption.textContent = "";
      if (detail) detail.classList.add("hidden");
      setScrollable(board, 0);
      return;
    }
    const done = Object.values(cells).filter((item) => item.kind === "ok" || item.kind === "empty").length;
    const failed = Object.values(cells).filter((item) => item.kind === "failed").length;
    if (caption) caption.textContent = failed
      ? `${done}/${rows.length * TASK_ORDER.length} · ${failed} 项失败`
      : `${done}/${rows.length * TASK_ORDER.length}`;
    board.innerHTML = `<table class="progress-table"><thead><tr><th>供应商</th>${TASK_ORDER.map((key) => `<th>${escapeHtml(taskFolder(key))}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>
      <th>${escapeHtml(shortSupplierName(row.supplier_name))}</th>
      ${TASK_ORDER.map((taskKey) => {
        const cell = cells[`${row.key}::${taskKey}`] || { kind: "idle", label: "未开始", note: "未开始" };
        const file = (cell.kind === "ok") ? fileForCell(row, taskKey) : null;
        const fileAttr = file ? ` data-file-id="${escapeHtml(file.file_id)}" data-file-name="${escapeHtml(cabinetFileName(file))}"` : "";
        const retryAttr = cell.kind === "failed" && row.account_key && row.supplier_id
          ? ` data-retry-task="${escapeHtml(taskKey)}" data-retry-account="${escapeHtml(row.account_key)}" data-retry-supplier="${escapeHtml(row.supplier_id)}" data-retry-name="${escapeHtml(row.supplier_name || "")}"`
          : "";
        return `<td><button class="progress-cell progress-${cell.kind}" type="button" data-cell-note="${escapeHtml(`${shortSupplierName(row.supplier_name)} · ${taskFolder(taskKey)}：${cell.note}`)}"${fileAttr}${retryAttr}>${escapeHtml(cell.label)}</button></td>`;
      }).join("")}
    </tr>`).join("")}</tbody></table>`;
    setScrollable(board, rows.length);
    if (detail) {
      if (state.cellDetail) {
        detail.textContent = state.cellDetail;
        detail.classList.remove("hidden");
      } else {
        detail.classList.add("hidden");
      }
    }
  }

  function cabinetFiles() {
    return preferredFiles();
  }

  function cabinetFileName(file) {
    const taskKey = file.task_key || taskKeyFromFile(file);
    const folder = taskFolder(taskKey);
    const supplier = shortSupplierName(file.supplier_name || file.supplier_id);
    const original = String(file.name || "");
    const ext = original.includes(".") ? `.${original.split(".").pop()}` : ".xlsx";
    return `${supplier}_${folder}${ext}`;
  }

  function renderCabinet() {
    const list = $("#cabinet-list");
    const crumb = $("#cabinet-crumb");
    const filter = $("#cabinet-supplier-filter");
    const pack = $("#cabinet-pack");
    if (!list || !crumb) return;
    const files = cabinetFiles();
    const operators = new Map();
    files.forEach((file) => {
      const id = file.operator_id || "unassigned";
      const name = file.operator_name || "未分配";
      if (!operators.has(id)) operators.set(id, { operator_id: id, name, count: 0 });
      operators.get(id).count += 1;
    });
    if (!state.cabinet.operatorId && state.selectedOperatorId) {
      state.cabinet.operatorId = state.selectedOperatorId;
    }
    const operatorAll = files.filter((file) => (file.operator_id || "unassigned") === state.cabinet.operatorId);
    const supplierIds = new Set(operatorAll.map((file) => String(file.supplier_id || "")).filter(Boolean));
    if (state.cabinetSupplierFilter && !supplierIds.has(state.cabinetSupplierFilter)) {
      state.cabinetSupplierFilter = "";
    }
    const matchSupplier = (file) => !state.cabinetSupplierFilter || String(file.supplier_id || "") === state.cabinetSupplierFilter;
    const operatorFiles = operatorAll.filter(matchSupplier);
    const dates = new Map();
    operatorFiles.forEach((file) => {
      const stamp = dateStamp(file.updated_at) || "未知日期";
      if (!dates.has(stamp)) dates.set(stamp, []);
      dates.get(stamp).push(file);
    });
    if (!state.cabinetTouched && dates.has(todayStamp()) && !state.cabinet.date) {
      state.cabinet.date = todayStamp();
    }
    if (state.cabinet.date && !dates.has(state.cabinet.date)) state.cabinet.date = "";
    const dateFiles = state.cabinet.date ? (dates.get(state.cabinet.date) || []) : [];
    const folders = new Map();
    dateFiles.forEach((file) => {
      const folder = taskFolder(file.task_key || taskKeyFromFile(file));
      if (!folders.has(folder)) folders.set(folder, []);
      folders.get(folder).push(file);
    });
    if (state.cabinet.folder && !folders.has(state.cabinet.folder)) state.cabinet.folder = "";
    const folderFiles = state.cabinet.folder ? (folders.get(state.cabinet.folder) || []) : [];
    const currentOperator = operators.get(state.cabinet.operatorId) || state.operators.find((item) => item.operator_id === state.cabinet.operatorId);

    const crumbs = [`<button type="button" data-cabinet-level="root">文件柜</button>`];
    if (state.cabinet.operatorId) crumbs.push(`<span>/</span><button type="button" data-cabinet-level="operator">${escapeHtml(currentOperator?.name || "组员")}</button>`);
    if (state.cabinet.date) crumbs.push(`<span>/</span><button type="button" data-cabinet-level="date">${escapeHtml(state.cabinet.date)}</button>`);
    if (state.cabinet.folder) crumbs.push(`<span>/</span><span>${escapeHtml(state.cabinet.folder)}</span>`);
    crumb.innerHTML = crumbs.join("");

    if (filter) {
      const source = state.cabinet.folder ? folderFiles : state.cabinet.date ? dateFiles : operatorFiles;
      const suppliers = new Map();
      source.forEach((file) => {
        if (!file.supplier_id) return;
        suppliers.set(file.supplier_id, file.supplier_name || file.supplier_id);
      });
      filter.innerHTML = [`<option value="">全部供应商</option>`].concat(
        [...suppliers.entries()].map(([id, name]) => `<option value="${escapeHtml(id)}" ${state.cabinetSupplierFilter === id ? "selected" : ""}>${escapeHtml(shortSupplierName(name))}</option>`)
      ).join("");
      filter.classList.toggle("hidden", suppliers.size < 2);
    }
    if (pack) {
      const layerFiles = state.cabinet.folder
        ? folderFiles
        : state.cabinet.date
          ? dateFiles
          : state.cabinet.operatorId
            ? operatorFiles
            : files.filter(matchSupplier);
      state.cabinetLayerFiles = layerFiles;
      pack.disabled = layerFiles.length === 0;
      pack.classList.remove("hidden");
    }

    if (!files.length) {
      list.innerHTML = `<div class="empty-state"><strong>暂无文件</strong></div>`;
      setScrollable(list, 0);
      return;
    }
    if (!state.cabinet.operatorId) {
      const items = [...operators.values()];
      list.innerHTML = items.map((item) => `<button class="cabinet-item" type="button" data-cabinet-operator="${escapeHtml(item.operator_id)}">
        <span class="cabinet-icon">${icon("folder")}</span>
        <span><strong>${escapeHtml(item.name)}</strong><span>${item.count} 个文件</span></span>
      </button>`).join("");
      setScrollable(list, items.length);
      return;
    }
    if (!state.cabinet.date) {
      const stamps = [...dates.keys()].sort((a, b) => b.localeCompare(a));
      if (!stamps.length) {
        list.innerHTML = `<div class="empty-state"><strong>暂无文件</strong></div>`;
        setScrollable(list, 0);
        return;
      }
      list.innerHTML = stamps.map((stamp) => `<button class="cabinet-item" type="button" data-cabinet-date="${escapeHtml(stamp)}">
        <span class="cabinet-icon">${icon("folder")}</span>
        <span><strong>${escapeHtml(stamp)}</strong><span>${dates.get(stamp).length} 个文件</span></span>
      </button>`).join("");
      setScrollable(list, stamps.length);
      return;
    }
    if (!state.cabinet.folder) {
      const names = TASK_ORDER.map((key) => taskFolder(key)).filter((name) => folders.has(name));
      [...folders.keys()].forEach((name) => {
        if (!names.includes(name)) names.push(name);
      });
      list.innerHTML = names.map((name) => `<button class="cabinet-item" type="button" data-cabinet-folder="${escapeHtml(name)}">
        <span class="cabinet-icon">${icon("folder")}</span>
        <span><strong>${escapeHtml(name)}</strong><span>${folders.get(name).length} 个文件</span></span>
      </button>`).join("");
      setScrollable(list, names.length);
      return;
    }
    if (!folderFiles.length) {
      list.innerHTML = `<div class="empty-state"><strong>暂无文件</strong></div>`;
      setScrollable(list, 0);
      return;
    }
    list.innerHTML = folderFiles.map((file) => `<button class="cabinet-item cabinet-file" type="button" data-cabinet-file="${escapeHtml(file.file_id)}" data-file-name="${escapeHtml(cabinetFileName(file))}">
      <span class="cabinet-icon">${icon("file")}</span>
      <span><strong>${escapeHtml(cabinetFileName(file))}</strong><span>${escapeHtml(formatSize(file.size))} · ${escapeHtml(formatTime(file.updated_at))}</span></span>
    </button>`).join("");
    setScrollable(list, folderFiles.length);
  }

  function renderRepair() {
    const machine = $("#repair-machine");
    if (machine) {
      const online = state.health && state.health.status === "ok";
      const workerOnline = Boolean(state.worker && state.worker.worker_online);
      if (!online) machine.textContent = `服务离线${state.health?.error ? ` · ${state.health.error}` : ""}`;
      else if (!workerOnline) machine.textContent = "服务在线 · 下载进程离线";
      else machine.textContent = `服务在线 · 下载进程在线 · 心跳 ${state.worker.heartbeat_age_seconds ?? 0}s`;
    }

    const jobsBox = $("#repair-jobs");
    const jobsCaption = $("#repair-job-caption");
    const stuck = state.runs.filter((run) => ["pending", "running", "paused"].includes(run.status));
    if (jobsCaption) jobsCaption.textContent = stuck.length ? `${stuck.length} 个未完成` : "";
    if (jobsBox) {
      if (!stuck.length) {
        jobsBox.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
        setScrollable(jobsBox, 0);
      } else {
        jobsBox.innerHTML = stuck.map((run) => {
          const status = run.status === "running" && run.pause_requested ? "暂停中" : { pending: "排队", running: "运行中", paused: "已暂停" }[run.status];
          return `<div class="repair-item">
            <div>
              <strong>${escapeHtml(status)}</strong>
              <span>${escapeHtml(formatTime(run.started_at || run.created_at))}</span>
            </div>
            <div class="table-actions">
              ${run.status === "paused" || run.pause_requested
                ? `<button class="icon-action" data-run-action="resume" data-run-id="${escapeHtml(run.run_id)}" title="恢复">${icon("play")}</button>`
                : `<button class="icon-action" data-run-action="pause" data-run-id="${escapeHtml(run.run_id)}" title="暂停">${icon("pause")}</button>`}
              ${run.status === "pending" ? `<button class="icon-action danger" data-run-action="cancel" data-run-id="${escapeHtml(run.run_id)}" title="取消">${icon("x")}</button>` : ""}
              <button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(run.run_id)}" title="日志">${icon("file")}</button>
            </div>
          </div>`;
        }).join("");
        setScrollable(jobsBox, stuck.length);
      }
    }

    const errorsBox = $("#repair-errors");
    if (!errorsBox) return;
    if (!state.errors.length) {
      errorsBox.innerHTML = `<div class="empty-state"><strong>暂无</strong></div>`;
      setScrollable(errorsBox, 0);
      return;
    }
    const errorRows = state.errors.slice(0, 40);
    errorsBox.innerHTML = errorRows.map((raw) => {
      const message = raw.message || raw.error || "失败";
      const shot = screenshotUrl(raw.screenshot || raw.screenshot_id || "");
      const taskKey = raw.task_key || raw.task || "";
      return `<div class="repair-item">
        <div>
          <strong>${escapeHtml(taskFolder(taskKey) || "任务")}</strong>
          <span>${escapeHtml(formatTime(raw.created_at || raw.finished_at))} · ${escapeHtml(String(message).split("\n")[0])}</span>
        </div>
        <div class="table-actions">
          ${shot ? `<a class="icon-action" href="${shot}" target="_blank" rel="noreferrer" title="截图">${icon("image")}</a>` : ""}
          ${raw.run_id ? `<button class="icon-action" data-run-action="logs" data-run-id="${escapeHtml(raw.run_id)}" title="日志">${icon("file")}</button>` : ""}
        </div>
      </div>`;
    }).join("");
    setScrollable(errorsBox, errorRows.length);
  }

  function renderAccountsTable() {
    const body = $("#accounts-table");
    if (!body) return;
    if (!state.accounts.length) {
      body.innerHTML = `<tr><td colspan="4"><div class="empty-state"><strong>暂无账号</strong></div></td></tr>`;
      setScrollable($("#accounts-table-wrap"), 0);
      return;
    }
    body.innerHTML = state.accounts.map((account) => `<tr>
      <td><span class="cell-main">${escapeHtml(account.username || account.name || account.key)}</span></td>
      <td>${escapeHtml(account.browser_status || "空闲")}</td>
      <td>${account.enabled !== false ? "启用" : "停用"}</td>
      <td><div class="table-actions"><button class="icon-action" data-edit-account="${escapeHtml(account.key)}" title="编辑">${icon("edit")}</button></div></td>
    </tr>`).join("");
    setScrollable($("#accounts-table-wrap"), state.accounts.length);
  }

  async function createRun(options = {}) {
    if (!state.selectedOperatorId) return showToast("未选组员", true);
    const suppliers = options.suppliers || runnableSuppliers().filter((item) => state.selectedSupplierKeys.has(supplierKey(item.account_key, item.supplier_id)));
    if (!suppliers.length) return showToast("无供应商", true);
    const supplierAccountKeys = [...new Set(suppliers.map((item) => item.account_key).filter(Boolean))];
    const accountKeys = options.accountKeys || supplierAccountKeys;
    if (!accountKeys.length) return showToast("账号未启用", true);
    const taskKeys = options.taskKeys?.length ? options.taskKeys : TASK_ORDER;
    try {
      await request("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          task_keys: taskKeys,
          account_keys: accountKeys,
          operator_id: state.selectedOperatorId,
          suppliers,
          force_account_tasks: true,
          headed: $("#headed-input") ? $("#headed-input").checked : true
        })
      });
      showToast(options.toast || "已开始下载");
      await loadData();
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    }
  }

  function openGuide(markSeen = false) {
    $("#guide-modal")?.classList.remove("hidden");
    if (markSeen) localStorage.setItem("maochao_guide_seen", "1");
  }

  function closeGuide(markSeen = true) {
    if (markSeen) localStorage.setItem("maochao_guide_seen", "1");
    closeModal("guide-modal");
  }

  function recordRiskAck() {
    const operator = state.operators.find((item) => item.operator_id === state.selectedOperatorId);
    const entry = {
      at: new Date().toISOString(),
      operator_id: state.selectedOperatorId,
      operator_name: operator?.name || "",
    };
    let rows = [];
    try {
      rows = JSON.parse(localStorage.getItem("maochao_risk_acks") || "[]");
    } catch {
      rows = [];
    }
    if (!Array.isArray(rows)) rows = [];
    rows.push(entry);
    localStorage.setItem("maochao_risk_acks", JSON.stringify(rows.slice(-80)));
  }

  function confirmRiskThenCreateRun(options = {}) {
    if (!state.selectedOperatorId) return showToast("未选组员", true);
    state.pendingRunOptions = options;
    const box = $("#risk-ack");
    const button = $("#risk-confirm-button");
    if (box) box.checked = false;
    if (button) button.disabled = true;
    $("#risk-modal")?.classList.remove("hidden");
  }

  async function acceptRiskAndStart() {
    if (!$("#risk-ack")?.checked) return showToast("请先勾选确认", true);
    recordRiskAck();
    const options = state.pendingRunOptions || {};
    state.pendingRunOptions = null;
    closeModal("risk-modal");
    await createRun(options);
  }

  async function retryFailedCell(button) {
    const taskKey = button.dataset.retryTask;
    const accountKey = button.dataset.retryAccount;
    const supplierId = button.dataset.retrySupplier;
    const supplierName = button.dataset.retryName || "";
    if (!taskKey || !accountKey || !supplierId) return;
    const key = `${accountKey}::${supplierId}::${taskKey}`;
    if (state.retryingKeys.has(key)) return;
    state.retryingKeys.add(key);
    try {
      await confirmRiskThenCreateRun({
        taskKeys: [taskKey],
        accountKeys: [accountKey],
        suppliers: [{ account_key: accountKey, supplier_id: supplierId, supplier_name: supplierName }],
        toast: "已开始重试"
      });
    } finally {
      state.retryingKeys.delete(key);
    }
  }

  async function runAction(action, runId) {
    if (action === "logs") return openLog(runId);
    const run = state.runs.find((item) => item.run_id === runId);
    const pauseEndpoint = { pause: "pause", resume: "resume" }[action];
    if (pauseEndpoint) {
      try {
        await request(`/api/runs/${encodeURIComponent(runId)}/${pauseEndpoint}`, { method: "POST", body: "{}" });
        showToast(action === "pause" ? "已暂停" : "已恢复");
        await loadData();
      } catch (error) {
        showToast(`操作失败：${error.message}`, true);
      }
      return;
    }
    if (action !== "cancel") return;
    try {
      await request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: "{}" });
      showToast("已取消");
      await loadData();
    } catch (error) {
      showToast(`操作失败：${error.message}`, true);
    }
  }

  async function openLog(runId) {
    $("#log-modal-title").textContent = "日志";
    $("#log-content").textContent = "读取中";
    $("#log-modal").classList.remove("hidden");
    try {
      const text = await request(`/api/runs/${encodeURIComponent(runId)}/logs`);
      $("#log-content").textContent = text || "无内容";
    } catch (error) {
      $("#log-content").textContent = `读取失败：${error.message}`;
    }
  }

  function openAccountModal(account) {
    const editing = Boolean(account);
    state.editingAccountKey = account?.key || "";
    $("#account-modal-title").textContent = editing ? "编辑账号" : "新增账号";
    $("#account-modal-hint").textContent = "";
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
    if (!data.username) return showToast("手机号为空", true);
    if (!editing && !data.password) return showToast("密码为空", true);
    if (editing && (!data.key || !data.port)) return showToast("账号或端口为空", true);
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
      await loadData();
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
    }
  }

  function closeModal(id) {
    const modal = $(`#${id}`);
    if (!modal) return;
    modal.classList.add("hidden");
    if (id === "risk-modal") state.pendingRunOptions = null;
  }

  function showToast(message, error = false) {
    const toast = document.createElement("div");
    toast.className = `toast${error ? " error" : ""}`;
    toast.textContent = message;
    $("#toast-region").appendChild(toast);
    window.setTimeout(() => toast.remove(), 3400);
  }

  async function addOperator() {
    const name = $("#operator-name-input")?.value.trim();
    if (!name) return showToast("姓名为空", true);
    try {
      const created = await request("/api/operators", { method: "POST", body: JSON.stringify({ name }) });
      $("#operator-name-input").value = "";
      state.selectedOperatorId = created.operator_id;
      localStorage.setItem("maochao_operator_id", created.operator_id);
      state.supplierSelectionInitialized = false;
      showToast("组员已添加");
      await loadData();
    } catch (error) {
      showToast(`新增失败：${error.message}`, true);
    }
  }

  async function syncEnabledAccountSuppliers() {
    const accountKeys = enabledAccounts().map((account) => account.key);
    if (!accountKeys.length) return showToast("账号未启用", true);
    try {
      for (const accountKey of accountKeys) {
        await request(`/api/accounts/${encodeURIComponent(accountKey)}/suppliers/sync`, { method: "POST", body: "{}" });
      }
      showToast("已提交");
      await loadData();
    } catch (error) {
      showToast(`同步失败：${error.message}`, true);
    }
  }

  async function toggleAssignedSupplier(key, checked) {
    if (!state.selectedOperatorId) return showToast("未选组员", true);
    const [accountKey, supplierId] = String(key).split("::");
    const current = state.assignedSuppliers.filter((item) => item.account_key === accountKey).map((item) => item.supplier_id);
    const next = checked ? [...new Set([...current, supplierId])] : current.filter((item) => item !== supplierId);
    try {
      await request(`/api/operators/${encodeURIComponent(state.selectedOperatorId)}/suppliers`, {
        method: "PUT",
        body: JSON.stringify({ account_key: accountKey, supplier_ids: next })
      });
      state.supplierSelectionInitialized = false;
      await loadData();
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
      await loadData();
    }
  }

  function bindEvents() {
    $("#refresh-button")?.addEventListener("click", loadData);
    $("#operator-select")?.addEventListener("change", (event) => selectOperator(event.target.value));
    $("#add-operator-button")?.addEventListener("click", addOperator);
    $("#sync-suppliers-button")?.addEventListener("click", syncEnabledAccountSuppliers);
    $("#full-run-button")?.addEventListener("click", () => confirmRiskThenCreateRun());
    $("#nav-guide")?.addEventListener("click", () => openGuide());
    $("#open-guide-inline")?.addEventListener("click", () => openGuide());
    $("#guide-done-button")?.addEventListener("click", () => closeGuide(true));
    $("#risk-ack")?.addEventListener("change", (event) => {
      const button = $("#risk-confirm-button");
      if (button) button.disabled = !event.target.checked;
    });
    $("#risk-confirm-button")?.addEventListener("click", acceptRiskAndStart);
    $("#cancel-run-button")?.addEventListener("click", (event) => {
      const runId = event.currentTarget.dataset.runId;
      const run = state.runs.find((item) => item.run_id === runId);
      if (!runId) return;
      runAction(run?.status === "pending" ? "cancel" : "pause", runId);
    });
    $("#add-account-button")?.addEventListener("click", () => openAccountModal(null));
    $("#account-form")?.addEventListener("submit", saveAccount);
    $("#cabinet-supplier-filter")?.addEventListener("change", (event) => {
      state.cabinetSupplierFilter = event.target.value;
      renderCabinet();
    });
    $("#cabinet-pack")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      downloadFiles(state.cabinetLayerFiles || []);
    });

    document.addEventListener("change", async (event) => {
      const supplierKeyValue = event.target.dataset?.supplierKey;
      const assignSupplier = event.target.dataset?.assignSupplier;
      if (supplierKeyValue) {
        event.target.checked ? state.selectedSupplierKeys.add(supplierKeyValue) : state.selectedSupplierKeys.delete(supplierKeyValue);
        renderAll();
      }
      if (assignSupplier) await toggleAssignedSupplier(assignSupplier, event.target.checked);
    });

    document.addEventListener("click", (event) => {
      if (event.target.closest("#cabinet-pack")) return;
      const nav = event.target.closest("[data-view]");
      if (nav?.dataset.view) setView(nav.dataset.view);
      const cabinetFile = event.target.closest("[data-cabinet-file]");
      if (cabinetFile) {
        event.preventDefault();
        downloadFiles([{ file_id: cabinetFile.dataset.cabinetFile, download_name: cabinetFile.dataset.fileName || "" }]);
        return;
      }
      const operatorPick = event.target.closest("[data-operator-id]");
      if (operatorPick) selectOperator(operatorPick.dataset.operatorId);
      const action = event.target.closest("[data-run-action]");
      if (action) runAction(action.dataset.runAction, action.dataset.runId);
      const edit = event.target.closest("[data-edit-account]");
      if (edit) openAccountModal(state.accounts.find((account) => account.key === edit.dataset.editAccount));
      const close = event.target.closest("[data-close-modal]");
      if (close) closeModal(close.dataset.closeModal);
      const dismiss = event.target.closest("[data-dismiss-banner]");
      if (dismiss) {
        state.connectionBannerDismissed = true;
        $("#connection-banner").classList.add("hidden");
      }
      const cell = event.target.closest("[data-cell-note]");
      if (cell) {
        state.cellDetail = cell.dataset.cellNote;
        const detail = $("#cell-detail");
        if (detail) {
          detail.textContent = state.cellDetail;
          detail.classList.remove("hidden");
        }
        if (cell.dataset.fileId) {
          downloadFiles([{ file_id: cell.dataset.fileId, download_name: cell.dataset.fileName || "" }]);
        }
        if (cell.dataset.retryTask) retryFailedCell(cell);
      }
      const cabinetOperator = event.target.closest("[data-cabinet-operator]");
      if (cabinetOperator) {
        state.cabinetTouched = true;
        state.cabinet = { operatorId: cabinetOperator.dataset.cabinetOperator, date: todayStamp(), folder: "" };
        renderCabinet();
        return;
      }
      const cabinetDate = event.target.closest("[data-cabinet-date]");
      if (cabinetDate) {
        state.cabinetTouched = true;
        state.cabinet.date = cabinetDate.dataset.cabinetDate;
        state.cabinet.folder = "";
        renderCabinet();
        return;
      }
      const cabinetFolder = event.target.closest("[data-cabinet-folder]");
      if (cabinetFolder) {
        state.cabinetTouched = true;
        state.cabinet.folder = cabinetFolder.dataset.cabinetFolder;
        renderCabinet();
        return;
      }
      const cabinetLevel = event.target.closest("[data-cabinet-level]");
      if (cabinetLevel) {
        state.cabinetTouched = true;
        const level = cabinetLevel.dataset.cabinetLevel;
        if (level === "root") state.cabinet = { operatorId: "", date: "", folder: "" };
        if (level === "operator") state.cabinet = { operatorId: state.cabinet.operatorId, date: "", folder: "" };
        if (level === "date") state.cabinet.folder = "";
        renderCabinet();
      }
      if (event.target.classList.contains("modal-backdrop")) closeModal(event.target.id);
    });
  }

  bindEvents();
  loadData();
  if (!localStorage.getItem("maochao_guide_seen")) openGuide();
  window.setInterval(loadData, 3000);
})();
