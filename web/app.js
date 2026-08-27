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

  const savedSyncAccountKeys = (() => {
    try {
      const saved = JSON.parse(localStorage.getItem("maochao_sync_account_keys") || "null");
      return Array.isArray(saved) ? saved : null;
    } catch (_) {
      return null;
    }
  })();
  const savedView = localStorage.getItem("maochao_view");
  const savedAuthToken = sessionStorage.getItem("maochao_auth_token") || "";
  const state = {
    accounts: [],
    runs: [],
    errors: [],
    files: [],
    schedules: [],
    worker: null,
    health: null,
    authToken: savedAuthToken,
    user: null,
    loginRole: "supply_chain",
    activeView: savedView === "admin" ? "repair" : (savedView || "home"),
    selectedOperatorId: localStorage.getItem("maochao_operator_id") || "",
    selectedSupplierKeys: new Set(),
    selectedSyncAccountKeys: new Set(savedSyncAccountKeys || []),
    syncingAccountKeys: new Set(),
    showSelectedSyncAccounts: false,
    operators: [],
    supplyChainUsers: [],
    itemIdConfig: { rows: [], uploads: [] },
    accountSuppliers: [],
    assignedSuppliers: [],
    supplierSelectionInitialized: false,
    syncAccountSelectionInitialized: Array.isArray(savedSyncAccountKeys),
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
    deletingAccountKey: "",
    deletingOperatorId: "",
    selectedAssignCompanyKey: "",
    selectedRunCompanyKey: "",
    loadRevision: 0,
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

  function shortSubjectName(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    return text.split(/\s*[-－–—]\s*/)[0].trim() || text;
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

  async function openScreenshot(screenshot) {
    const url = screenshotUrl(screenshot);
    if (!url) return;
    try {
      const response = await fetch(url, {
        headers: state.authToken ? { "Authorization": `Bearer ${state.authToken}` } : {}
      });
      if (response.status === 401 && state.authToken) resetAuth();
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      const blobUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = String(screenshot).split(/[\\/]/).pop() || "error-screenshot.png";
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => {
        URL.revokeObjectURL(blobUrl);
        link.remove();
      }, 1500);
      showToast("截图已下载");
    } catch (error) {
      showToast(`截图打开失败：${error.message}`, true);
    }
  }

  function supplierKey(accountKey, supplierId) {
    return `${accountKey}::${supplierId}`;
  }

  function normalizedSupplierIdentity(value) {
    return String(value || "")
      .trim()
      .replace(/^name:/, "")
      .replace(/[\s\-—–_]+/g, "");
  }

  function supplierIdentityValues(supplierId, supplierName) {
    const values = [];
    const add = (value) => {
      const text = String(value || "").trim();
      if (text && !values.includes(text)) values.push(text);
      if (text.startsWith("name:")) {
        const withoutPrefix = text.slice(5).trim();
        if (withoutPrefix && !values.includes(withoutPrefix)) values.push(withoutPrefix);
      }
    };
    add(supplierId);
    add(supplierName);
    return values;
  }

  function supplierKeys(accountKey, supplierId, supplierName) {
    return supplierIdentityValues(supplierId, supplierName).map((value) => supplierKey(accountKey || "", value));
  }

  function enabledAccounts() {
    return state.accounts.filter((account) => account.enabled !== false);
  }

  function syncableAccounts() {
    return enabledAccounts().filter((account) => account.key);
  }

  function accountTitle(account) {
    return shortSubjectName(account.name) || account.username || account.key || "未知账号";
  }

  function hasActiveSupplierSync(accountKey) {
    return state.runs.some((run) =>
      run.run_kind === "sync_suppliers"
      && ["pending", "running", "paused"].includes(run.status)
      && (run.account_keys || []).includes(accountKey)
    );
  }

  function companyNameForAccountKey(accountKey) {
    const account = state.accounts.find((item) => item.key === accountKey);
    return account ? accountTitle(account) : (String(accountKey || "").trim() || "未知公司");
  }

  function supplierCompanyGroups(rows) {
    const accountOrder = new Map(state.accounts.map((account, index) => [account.key, index]));
    const groups = new Map();
    rows.forEach((item) => {
      const name = companyNameForAccountKey(item.account_key);
      const key = name || item.account_key || "未知公司";
      const order = accountOrder.has(item.account_key) ? accountOrder.get(item.account_key) : Number.MAX_SAFE_INTEGER;
      if (!groups.has(key)) groups.set(key, { key, name: key, rows: [], order });
      const group = groups.get(key);
      group.rows.push(item);
      group.order = Math.min(group.order, order);
    });
    return [...groups.values()].sort((left, right) =>
      left.order - right.order || left.name.localeCompare(right.name, "zh-Hans-CN")
    );
  }

  function normalizeCompanySelection(stateKey, groups) {
    if (!groups.length) {
      state[stateKey] = "";
      return "";
    }
    if (!groups.some((group) => group.key === state[stateKey])) {
      state[stateKey] = groups[0].key;
    }
    return state[stateKey];
  }

  function renderCompanyPicker(groups, selectedKey, dataName) {
    if (!groups.length) return "";
    return `<div class="company-picker">
      ${groups.map((group) => `<button class="company-tab ${group.key === selectedKey ? "active" : ""}" type="button" data-${dataName}="${escapeHtml(group.key)}">
        <span>${escapeHtml(group.name)}</span>
        <small>${group.rows.length}</small>
      </button>`).join("")}
    </div>`;
  }

  function isMember() {
    return false;
  }

  function isSupplyChain() {
    return state.user?.role === "supply_chain";
  }

  function isAdmin() {
    return state.user?.role === "admin";
  }

  function persistSyncAccountSelection() {
    localStorage.setItem("maochao_sync_account_keys", JSON.stringify([...state.selectedSyncAccountKeys]));
  }

  function normalizeSyncAccountSelection() {
    const accounts = syncableAccounts();
    if (!accounts.length) return accounts;
    const enabledKeys = new Set(accounts.map((account) => account.key));
    if (!state.syncAccountSelectionInitialized) {
      state.selectedSyncAccountKeys = new Set(enabledKeys);
      state.syncAccountSelectionInitialized = true;
    } else {
      state.selectedSyncAccountKeys = new Set([...state.selectedSyncAccountKeys].filter((key) => enabledKeys.has(key)));
    }
    persistSyncAccountSelection();
    return accounts;
  }

  function selectedSyncAccounts() {
    return normalizeSyncAccountSelection().filter((account) => state.selectedSyncAccountKeys.has(account.key));
  }

  function runnableSuppliers() {
    return state.assignedSuppliers.filter((item) => item.visible);
  }

  function selectedRunSuppliers() {
    return runnableSuppliers().filter((item) => state.selectedSupplierKeys.has(supplierKey(item.account_key, item.supplier_id)));
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

  function unfinishedRuns() {
    return state.runs.filter((run) => ["pending", "running", "paused"].includes(run.status));
  }

  function runOperatorName(run) {
    if (run.operator_name) return run.operator_name;
    const operator = state.operators.find((item) => item.operator_id === run.operator_id);
    return operator?.name || "其他组员";
  }

  function scheduleOperatorIds(schedule) {
    const raw = Array.isArray(schedule?.operator_ids) ? schedule.operator_ids : [];
    const values = raw.length ? raw : (schedule?.operator_id ? [schedule.operator_id] : []);
    return [...new Set(values.map((item) => String(item || "").trim()).filter(Boolean))];
  }

  function scheduleOperatorNames(schedule) {
    if (schedule?.all_operators) return "全部组员";
    const names = scheduleOperatorIds(schedule).map((operatorId) => {
      const operator = state.operators.find((item) => item.operator_id === operatorId);
      return operator?.name || operatorId;
    });
    return names.join("、") || "未选择组员";
  }

  function runStatusText(run) {
    if (run.status === "running" && run.pause_requested) return "暂停中";
    return { pending: "排队", running: "运行中", paused: "已暂停" }[run.status] || run.status || "未知";
  }

  function runStatusSummary(runs) {
    const running = runs.filter((run) => run.status === "running").length;
    const pending = runs.filter((run) => run.status === "pending").length;
    const paused = runs.filter((run) => run.status === "paused" || run.pause_requested).length;
    const parts = [];
    if (running) parts.push(`${running} 运行`);
    if (pending) parts.push(`${pending} 排队`);
    if (paused) parts.push(`${paused} 暂停`);
    return parts.join(" · ") || "0 个";
  }

  function liveRuns() {
    return operatorRuns().filter((run) => ["pending", "running", "paused"].includes(run.status));
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: {
        "Accept": "application/json",
        ...(state.authToken ? { "Authorization": `Bearer ${state.authToken}` } : {}),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {})
      }
    });
    if (response.status === 401 && state.authToken) {
      resetAuth();
    }
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

  function resetAuth() {
    state.authToken = "";
    state.user = null;
    state.schedules = [];
    sessionStorage.removeItem("maochao_auth_token");
    $("#app-view")?.classList.add("hidden");
    $("#login-view")?.classList.remove("hidden");
  }

  function applyRoleView() {
    const member = false;
    $("#nav-home")?.classList.remove("hidden");
    $("#nav-settings")?.classList.toggle("hidden", member);
    $("#nav-repair")?.classList.toggle("hidden", member);
    $("#operator-pick-wrap")?.classList.toggle("hidden", member);
    $("#change-password-button")?.classList.add("hidden");
    $("#supply-chain-block")?.classList.toggle("hidden", !isAdmin());
    if (member) state.activeView = "home";
    $("#view-home")?.classList.remove("member-cabinet-only");
  }

  async function loadLoginOperators() {
    return;
  }

  function setLoginRole(role) {
    state.loginRole = role === "admin" ? "admin" : "supply_chain";
    $$('[data-login-role]').forEach((item) => item.classList.toggle("active", item.dataset.loginRole === state.loginRole));
    $("#member-login-fields")?.classList.toggle("hidden", state.loginRole !== "supply_chain");
    $("#admin-login-fields")?.classList.toggle("hidden", state.loginRole !== "admin");
    if ($("#login-supply-username")) $("#login-supply-username").required = state.loginRole === "supply_chain";
    if ($("#login-member-password")) $("#login-member-password").required = state.loginRole === "supply_chain";
    if ($("#login-username")) $("#login-username").required = state.loginRole === "admin";
    if ($("#login-password")) $("#login-password").required = state.loginRole === "admin";
    $("#login-error")?.classList.add("hidden");
  }

  async function login(event) {
    event.preventDefault();
    const body = state.loginRole === "admin"
      ? { role: "admin", username: $("#login-username").value.trim(), password: $("#login-password").value }
      : { role: "supply_chain", username: $("#login-supply-username").value.trim(), password: $("#login-member-password").value };
    try {
      const result = await request("/api/auth/login", { method: "POST", body: JSON.stringify(body) });
      state.authToken = result.token;
      state.user = result.user;
      sessionStorage.setItem("maochao_auth_token", state.authToken);
      $("#login-view")?.classList.add("hidden");
      $("#app-view")?.classList.remove("hidden");
      if ($("#login-member-password")) $("#login-member-password").value = "";
      applyRoleView();
      await loadData();
    } catch (error) {
      $("#login-error").textContent = error.message;
      $("#login-error").classList.remove("hidden");
    }
  }

  async function logout() {
    try {
      await request("/api/auth/logout", { method: "POST", body: "{}" });
    } catch (_) {}
    resetAuth();
    await loadLoginOperators();
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
    if (!state.authToken || !state.user) return;
    const loadRevision = ++state.loadRevision;
    try {
      const health = await request("/api/health");
      if (isMember()) {
        const [worker, runs, files] = await Promise.all([
          request("/api/worker"),
          request("/api/runs"),
          request("/api/files")
        ]);
        if (loadRevision !== state.loadRevision) return;
        state.health = health;
        state.worker = worker;
        state.runs = runs || [];
        state.files = files || [];
        state.errors = [];
        state.operators = [{ operator_id: state.user.operator_id, name: state.user.operator_name }];
        state.accountSuppliers = [];
        state.selectedOperatorId = state.user.operator_id;
        state.cabinet.operatorId = state.user.operator_id;
        if (!await loadAssignedSuppliers(loadRevision)) return;
        state.accounts = [...new Set(state.assignedSuppliers.map((item) => item.account_key).filter(Boolean))]
          .map((key) => ({ key, name: key, enabled: true }));
        renderAll();
        return;
      }
      const [worker, accounts, runs, errors, files, operators, accountSuppliers, schedules, supplyChainUsers, itemIdConfig] = await Promise.all([
        request("/api/worker"),
        request("/api/accounts?include_disabled=true"),
        request("/api/runs"),
        request("/api/errors"),
        request("/api/files"),
        requestOptional("/api/operators", []),
        requestOptional("/api/suppliers", []),
        requestOptional("/api/schedules", []),
        isAdmin() ? requestOptional("/api/supply-chain-users?include_disabled=true", []) : Promise.resolve([]),
        requestOptional("/api/item-id-config", { rows: [], uploads: [] })
      ]);
      if (loadRevision !== state.loadRevision) return;
      state.health = health;
      state.worker = worker;
      state.accounts = accounts;
      state.runs = runs;
      state.errors = errors;
      state.files = files;
      state.operators = operators || [];
      state.accountSuppliers = accountSuppliers || [];
      state.schedules = schedules || [];
      state.supplyChainUsers = supplyChainUsers || [];
      state.itemIdConfig = itemIdConfig || { rows: [], uploads: [] };
      if (isSupplyChain()) {
        const ownedOperators = state.operators.filter((item) => item.supply_chain_user_id === state.user?.user_id);
        if (!ownedOperators.some((item) => item.operator_id === state.selectedOperatorId)) {
          state.selectedOperatorId = ownedOperators[0]?.operator_id || state.operators[0]?.operator_id || "";
        }
      }
      if (state.selectedOperatorId && !operators.some((item) => item.operator_id === state.selectedOperatorId)) {
        state.selectedOperatorId = operators[0]?.operator_id || "";
      } else if (!state.selectedOperatorId && operators.length) {
        state.selectedOperatorId = operators[0].operator_id;
      }
      if (state.selectedOperatorId) localStorage.setItem("maochao_operator_id", state.selectedOperatorId);
      if (!await loadAssignedSuppliers(loadRevision)) return;
      renderAll();
    } catch (error) {
      if (loadRevision !== state.loadRevision) return;
      state.health = { status: "offline", error: error.message };
      state.worker = null;
      renderAll();
    }
  }

  async function loadAssignedSuppliers(expectedLoadRevision = null) {
    const operatorId = state.selectedOperatorId;
    if (!operatorId) {
      state.assignedSuppliers = [];
      state.selectedSupplierKeys.clear();
      return true;
    }
    const rows = await request(`/api/operators/${encodeURIComponent(operatorId)}/suppliers`);
    if (
      operatorId !== state.selectedOperatorId
      || (expectedLoadRevision !== null && expectedLoadRevision !== state.loadRevision)
    ) {
      return false;
    }
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
    return true;
  }

  function setView(view) {
    state.activeView = isMember() ? "home" : (view === "settings" || view === "repair" ? view : "home");
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
    applyRoleView();
    renderNav();
    renderConnection();
    renderToday();
    renderCurrentProcess();
    renderOperators();
    renderSupplyChainUsers();
    renderItemIdConfig();
    renderSupplierSelection();
    renderSyncAccountList();
    renderSuppliersTable();
    renderProgressBoard();
    renderCabinet();
    renderRepair();
    renderAccountsTable();
    if (!document.querySelector("[data-schedule-editor]:not(.hidden)")) {
      renderSchedules();
    }
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
      const response = await fetch(fileDownloadUrl(file.file_id), {
        headers: state.authToken ? { "Authorization": `Bearer ${state.authToken}` } : {}
      });
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
      const aliases = supplierKeys(accountKey || "", supplierId, supplierName);
      const key = aliases[0] || supplierKey(accountKey || "", supplierId || supplierName || "");
      if (aliases.some((alias) => seen.has(alias))) return;
      aliases.forEach((alias) => seen.add(alias));
      rows.push({
        key,
        aliases,
        supplier_id: supplierId || "",
        supplier_name: supplierName || supplierId || "未知供应商",
        account_key: accountKey || ""
      });
    };
    runnableSuppliers().forEach((item) => pushRow(item.supplier_id, item.supplier_name, item.account_key));
    const cells = {};
    const cellId = (rowKey, taskKey) => `${rowKey}::${taskKey}`;
    const setCell = (accountKey, supplierId, supplierName, taskKey, value) => {
      const aliases = supplierKeys(accountKey || "", supplierId, supplierName);
      (aliases.length ? aliases : [supplierKey(accountKey || "", supplierId || supplierName || "")]).forEach((rowKey) => {
        cells[cellId(rowKey, taskKey)] = value;
      });
    };
    todayRuns.forEach((run) => {
      (run.result || []).forEach((item) => {
        const taskKey = item.task || item.task_key || "";
        if (!TASK_FOLDERS[taskKey]) return;
        const ok = item.status === "ok";
        const hasFile = Boolean(item.raw_file || item.cleaned_file);
        const accountKey = item.account || item.account_key || (run.account_keys || [])[0] || "";
        if (!ok) setCell(accountKey, item.supplier_id, item.supplier_name, taskKey, { kind: "failed", label: "重试", note: item.note || item.error || "失败" });
        else if (!hasFile) setCell(accountKey, item.supplier_id, item.supplier_name, taskKey, { kind: "empty", label: "无数据", note: item.note || "无数据" });
        else setCell(accountKey, item.supplier_id, item.supplier_name, taskKey, { kind: "ok", label: "成功", note: item.note || "已下载" });
      });
    });
    todayRuns.forEach((run) => {
      const active = ["pending", "running", "paused"].includes(run.status);
      if (!active && run.status !== "failed") return;
      const suppliers = (run.suppliers || []).length
        ? run.suppliers
        : (run.result || []).map((item) => ({
          supplier_id: item.supplier_id,
          supplier_name: item.supplier_name,
          account_key: item.account || item.account_key
        }));
      const tasks = (run.task_keys || []).filter((key) => TASK_FOLDERS[key]);
      const supplierEntries = suppliers.map((item, index) => {
        const accountKey = item.account_key || (run.account_keys || [])[0] || "";
        const aliases = supplierKeys(accountKey, item.supplier_id, item.supplier_name);
        return {
          item,
          index,
          accountKey,
          aliases: aliases.length ? aliases : [supplierKey(accountKey, item.supplier_id || item.supplier_name || "")],
          reportedTasks: new Set()
        };
      });
      const reported = new Set();
      (run.result || []).forEach((item) => {
        const taskKey = item.task || item.task_key || "";
        if (!TASK_FOLDERS[taskKey]) return;
        const aliases = supplierKeys(item.account || item.account_key || (run.account_keys || [])[0] || "", item.supplier_id, item.supplier_name);
        aliases.forEach((rowKey) => reported.add(cellId(rowKey, taskKey)));
        supplierEntries.forEach((entry) => {
          if (aliases.some((alias) => entry.aliases.includes(alias))) entry.reportedTasks.add(taskKey);
        });
      });
      const failedNote = String(run.error || "任务未完成").split("\n")[0];
      const currentIndex = run.status === "running"
        ? (() => {
          const partialIndex = supplierEntries.findIndex((entry) => {
            const count = tasks.filter((taskKey) => entry.reportedTasks.has(taskKey)).length;
            return count > 0 && count < tasks.length;
          });
          if (partialIndex >= 0) return partialIndex;
          return supplierEntries.findIndex((entry) => tasks.some((taskKey) => !entry.reportedTasks.has(taskKey)));
        })()
        : -1;
      supplierEntries.forEach((entry) => {
        tasks.forEach((taskKey) => {
          if (entry.aliases.some((rowKey) => reported.has(cellId(rowKey, taskKey)))) return;
          entry.aliases.forEach((rowKey) => {
            const id = cellId(rowKey, taskKey);
            if (["ok", "empty"].includes(cells[id]?.kind)) return;
            if (run.status === "paused" || run.pause_requested) cells[id] = { kind: "pending", label: "已暂停", note: "已暂停" };
            else if (run.status === "pending") cells[id] = { kind: "pending", label: "排队", note: "排队中" };
            else if (run.status === "running" && entry.index === currentIndex) cells[id] = { kind: "running", label: "进行中", note: "正在下载" };
            else if (run.status === "running") cells[id] = { kind: "pending", label: "排队", note: "排队中" };
            else cells[id] = { kind: "failed", label: "重试", note: failedNote };
          });
        });
      });
    });
    return { rows, cells, today };
  }

  function cellForRow(cells, row, taskKey) {
    const aliases = row.aliases?.length ? row.aliases : [row.key];
    for (const alias of aliases) {
      const cell = cells[`${alias}::${taskKey}`];
      if (cell) return cell;
    }
    return null;
  }

  function retryCellForRow(row, taskKey) {
    const aliases = supplierIdentityValues(row.supplier_id, row.supplier_name);
    for (const value of aliases) {
      if (state.retryingKeys.has(`${row.account_key}::${value}::${taskKey}`)) {
        return { kind: "pending", label: "排队", note: "重试已提交，等待执行" };
      }
    }
    return null;
  }

  function cellForSupplier(board, supplier, taskKey) {
    const aliases = supplierKeys(supplier.account_key, supplier.supplier_id, supplier.supplier_name);
    for (const rowKey of aliases) {
      const cell = board.cells[`${rowKey}::${taskKey}`];
      if (cell) return cell;
    }
    return null;
  }

  function remainingRunBatches(board = todayBoard()) {
    const groups = new Map();
    selectedRunSuppliers().forEach((supplier) => {
      const taskKeys = TASK_ORDER.filter((taskKey) => {
        const cell = cellForSupplier(board, supplier, taskKey);
        return !cell || !["ok", "empty"].includes(cell.kind);
      });
      if (!taskKeys.length) return;
      const groupKey = taskKeys.join(",");
      if (!groups.has(groupKey)) groups.set(groupKey, { taskKeys, suppliers: [] });
      groups.get(groupKey).suppliers.push(supplier);
    });
    return [...groups.values()].map((group) => ({
      taskKeys: group.taskKeys,
      suppliers: group.suppliers,
      accountKeys: [...new Set(group.suppliers.map((item) => item.account_key).filter(Boolean))]
    }));
  }

  function supplierCountInBatches(batches) {
    const keys = new Set();
    batches.forEach((batch) => {
      batch.suppliers.forEach((supplier) => keys.add(supplierKey(supplier.account_key, supplier.supplier_id)));
    });
    return keys.size;
  }

  function defaultRunOptions() {
    const batches = remainingRunBatches();
    if (!batches.length) return {};
    const supplierCount = supplierCountInBatches(batches);
    return {
      batches,
      toast: `已提交 ${supplierCount} 家未完成供应商`
    };
  }

  function runWorkText(run) {
    if (!isTaskRun(run)) return "同步供应商清单";
    const supplierCount = (run.suppliers || []).length;
    const taskCount = (run.task_keys || []).filter((key) => TASK_FOLDERS[key]).length;
    return `${supplierCount} 家供应商 · ${taskCount || TASK_ORDER.length} 项`;
  }

  function runButton(action, runId, iconName, label, options = {}) {
    const classes = ["mini-button", options.danger ? "danger" : ""].filter(Boolean).join(" ");
    return `<button class="${classes}" data-run-action="${escapeHtml(action)}" data-run-id="${escapeHtml(runId)}" type="button" title="${escapeHtml(options.title || label)}"${options.disabled ? " disabled" : ""}>${icon(iconName)}${escapeHtml(label)}</button>`;
  }

  function renderRunControls(run, options = {}) {
    const buttons = [];
    const manageable = Boolean(options.manageable);
    const pendingCount = Number(options.pendingCount || 0);
    const queuePosition = Number(run.queue_position || 0);
    if (manageable) {
      if (run.status === "pending") {
        buttons.push(runButton("move-up", run.run_id, "chevron-up", "上移", { disabled: queuePosition <= 1 }));
        buttons.push(runButton("move-down", run.run_id, "chevron-down", "下移", { disabled: pendingCount > 0 && queuePosition >= pendingCount }));
        buttons.push(runButton("cancel", run.run_id, "x", "取消", { danger: true }));
      } else if (run.status === "paused") {
        buttons.push(runButton("resume", run.run_id, "play", "继续"));
        buttons.push(runButton("cancel", run.run_id, "x", "取消", { danger: true }));
      } else if (run.pause_requested) {
        buttons.push(runButton("pause", run.run_id, "pause", "暂停中", { disabled: true }));
      } else if (run.status === "running") {
        buttons.push(runButton("pause", run.run_id, "pause", "暂停"));
      }
    }
    buttons.push(runButton("logs", run.run_id, "file", "日志"));
    return buttons.join("");
  }

  function renderProcessRun(run, options = {}) {
    const queueText = run.status === "pending" && run.queue_position ? ` · 队列第 ${run.queue_position} 位` : "";
    const statusKind = run.status === "running" ? "running" : run.status === "pending" ? "pending" : "neutral";
    return `<div class="queue-item ${escapeHtml(run.status)}">
      <div class="queue-main">
        <div>
          <strong>${escapeHtml(runStatusText(run))} · ${escapeHtml(runOperatorName(run))}</strong>
          <p>${escapeHtml(runWorkText(run))}${escapeHtml(queueText)} · ${escapeHtml(formatTime(run.started_at || run.created_at))}</p>
          <span class="queue-meta">${escapeHtml(run.run_id)}</span>
        </div>
        <span class="status-chip status-${escapeHtml(statusKind)}">${escapeHtml(runStatusText(run))}</span>
      </div>
      <div class="queue-controls">
        ${renderRunControls(run, options)}
      </div>
    </div>`;
  }

  function renderProcessGroup(title, runs, options = {}) {
    if (!runs.length) return "";
    const caption = options.readonly ? `${runStatusSummary(runs)} · 只读` : runStatusSummary(runs);
    return `<section class="process-group">
      <div class="queue-group-title">
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(caption)}</span>
      </div>
      ${runs.map((run) => renderProcessRun(run, options)).join("")}
    </section>`;
  }

  function renderCurrentProcess() {
    const caption = $("#process-caption");
    const summary = $("#process-summary");
    const list = $("#process-list");
    if (!summary || !list) return;
    const live = unfinishedRuns();
    const myRuns = liveRuns();
    const myRunIds = new Set(myRuns.map((run) => run.run_id));
    const otherRuns = live.filter((run) => isTaskRun(run) && !myRunIds.has(run.run_id));
    const machineRuns = live.filter((run) => !isTaskRun(run));
    const running = live.filter((run) => run.status === "running").length;
    const pending = live.filter((run) => run.status === "pending").length;
    const paused = live.filter((run) => run.status === "paused" || run.pause_requested).length;
    const workerOnline = Boolean(state.worker && state.worker.worker_online);
    if (caption) caption.textContent = live.length ? `${live.length} 个未完成` : "空闲";
    summary.innerHTML = `
      <div><span>下载进程</span><strong>${workerOnline ? "在线" : "离线"}</strong></div>
      <div><span>运行中</span><strong>${running}</strong></div>
      <div><span>排队</span><strong>${pending}</strong></div>
    `;
    if (!live.length) {
      list.innerHTML = `<div class="queue-empty"><strong>暂无未完成任务</strong></div>`;
      return;
    }
    list.innerHTML = [
      renderProcessGroup(state.selectedOperatorId ? "我的任务" : "任务队列", myRuns, { manageable: true, pendingCount: pending }),
      renderProcessGroup("其他组员", otherRuns, { manageable: false, pendingCount: pending, readonly: true }),
      renderProcessGroup("机器 / 同步", machineRuns, { manageable: true, pendingCount: pending })
    ].join("");
    if (paused && caption) caption.textContent = `${live.length} 个未完成 · ${paused} 个暂停`;
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
    const batches = remainingRunBatches(board);
    const remainingSupplierCount = supplierCountInBatches(batches);
    const visibleCells = board.rows.flatMap((row) => TASK_ORDER.map((taskKey) => cellForRow(board.cells, row, taskKey)).filter(Boolean));
    const doneCount = visibleCells.filter((item) => item.kind === "ok" || item.kind === "empty").length;
    const totalCount = board.rows.length * TASK_ORDER.length;
    const selectedOperator = state.operators.find((item) => item.operator_id === state.selectedOperatorId);
    const canManageOperator = isAdmin() || selectedOperator?.supply_chain_user_id === state.user?.user_id;
    const canStart = online && workerOnline && hasOperator && hasAccounts && hasSuppliers && !running && canManageOperator;

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
        : `${remainingSupplierCount || state.selectedSupplierKeys.size} 家 · 任务 1–6`;
      label.textContent = "等待完成";
      button.disabled = true;
      cancel.classList.remove("hidden");
      cancel.dataset.runId = running.run_id;
      cancel.dataset.runAction = running.status === "pending" ? "cancel" : running.status === "paused" ? "resume" : "pause";
      cancel.textContent = running.pause_requested ? "暂停中" : { cancel: "取消", resume: "继续", pause: "暂停" }[cancel.dataset.runAction];
      cancel.disabled = Boolean(running.pause_requested);
      return;
    }
    if (otherLive) {
      hero.classList.add("is-running");
      title.textContent = "机器忙";
      hint.textContent = `${runOperatorName(otherLive)}${otherLive.status === "pending" ? "已排队" : otherLive.status === "paused" ? "已暂停" : "正在下载"}`;
      label.textContent = "加入队列";
      button.disabled = !canStart;
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
      hint.textContent = `${remainingSupplierCount || state.selectedSupplierKeys.size} 家 · 任务 1–6`;
      label.textContent = doneCount ? "下载未完成" : "开始下载";
    }
    button.disabled = !canStart;
  }

  function setScrollable(element, count) {
    if (!element) return;
    element.classList.toggle("is-scroll", count > 10);
  }

  async function selectOperator(operatorId) {
    state.loadRevision += 1;
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
    const ownerSelect = $("#operator-owner-select");
    if (ownerSelect) {
      ownerSelect.classList.toggle("hidden", !isAdmin());
      ownerSelect.innerHTML = `<option value="">未分配供应链</option>${state.supplyChainUsers.filter((user) => user.enabled !== false).map((user) => `<option value="${escapeHtml(user.user_id)}">${escapeHtml(user.name)}</option>`).join("")}`;
    }
    list.innerHTML = state.operators.map((item) => {
      const owner = state.supplyChainUsers.find((user) => user.user_id === item.supply_chain_user_id);
      const manageable = isAdmin() || (isSupplyChain() && item.supply_chain_user_id === state.user?.user_id);
      return `<div class="pick-item operator-item ${item.operator_id === state.selectedOperatorId ? "active" : ""}" data-operator-id="${escapeHtml(item.operator_id)}">
        <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(owner?.name || "未分配供应链")}</small></span>
        <div class="operator-actions">
          ${manageable ? `<button class="mini-button" type="button" data-edit-operator="${escapeHtml(item.operator_id)}">改名</button><button class="mini-button danger" type="button" data-delete-operator="${escapeHtml(item.operator_id)}">删除</button>` : ""}
        </div>
      </div>`
    }).join("");
    setScrollable(list, state.operators.length);
  }

  function renderSupplyChainUsers() {
    const list = $("#supply-chain-list");
    if (!list || !isAdmin()) return;
    list.innerHTML = state.supplyChainUsers.length ? state.supplyChainUsers.map((user) => `
      <div class="pick-item operator-item">
        <span><strong>${escapeHtml(user.name)}</strong><small>${escapeHtml(user.username)} · ${user.enabled === false ? "已停用" : "启用"}</small></span>
        <div class="operator-actions">
          <button class="mini-button" type="button" data-toggle-supply-chain="${escapeHtml(user.user_id)}" data-next-enabled="${user.enabled === false ? "1" : "0"}">${user.enabled === false ? "启用" : "停用"}</button>
          <button class="mini-button danger" type="button" data-delete-supply-chain="${escapeHtml(user.user_id)}">删除</button>
        </div>
      </div>`).join("") : `<div class="empty-state"><strong>暂无供应链账号</strong></div>`;
  }

  function renderItemIdConfig() {
    const box = $("#item-id-summary");
    const caption = $("#item-id-caption");
    if (!box) return;
    const rows = state.itemIdConfig?.rows || [];
    const groups = new Set(rows.map((item) => `${item.account_key}::${item.supplier_id}`));
    if (caption) caption.textContent = `已配置 ${groups.size} 个账号/供应商组合 · ${rows.length} 个货品 ID`;
    const uploads = (state.itemIdConfig?.uploads || []).slice(0, 5);
    box.innerHTML = uploads.length
      ? uploads.map((upload) => `<div class="repair-item"><div><strong>${upload.status === "active" ? "当前版本" : "历史版本"}：${escapeHtml(upload.original_name)}</strong><span>${escapeHtml(formatTime(upload.uploaded_at))} · ${upload.row_count} 条</span></div>${upload.status === "active" ? "" : `<button class="mini-button" type="button" data-rollback-item-upload="${escapeHtml(upload.upload_id)}">恢复此版本</button>`}</div>`).join("")
      : `<div class="empty-state"><strong>尚未上传货品 ID 配置</strong></div>`;
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
    const groups = supplierCompanyGroups(runnable);
    const selectedCompany = normalizeCompanySelection("selectedRunCompanyKey", groups);
    const currentRows = groups.find((group) => group.key === selectedCompany)?.rows || [];
    container.innerHTML = renderCompanyPicker(groups, selectedCompany, "run-company") + currentRows.map((item) => {
      const key = supplierKey(item.account_key, item.supplier_id);
      const checked = state.selectedSupplierKeys.has(key);
      return `<label class="pick-item ${checked ? "active" : ""}">
        <input type="checkbox" data-supplier-key="${escapeHtml(key)}" ${checked ? "checked" : ""}>
        <span>${escapeHtml(shortSupplierName(item.supplier_name || item.supplier_id))}</span>
      </label>`;
    }).join("");
    setScrollable(container, currentRows.length);
  }

  function renderSyncAccountList() {
    const list = $("#sync-account-list");
    const button = $("#sync-suppliers-button");
    const selectedOnly = $("#sync-show-selected");
    const accounts = normalizeSyncAccountSelection();
    const selected = accounts.filter((account) => state.selectedSyncAccountKeys.has(account.key));
    const visibleAccounts = state.showSelectedSyncAccounts ? selected : accounts;
    if (button) {
      button.textContent = selected.length ? `同步清单 ${selected.length}` : "选择同步账号";
      button.disabled = !selected.length;
    }
    if (!list) return;
    if (selectedOnly) selectedOnly.checked = state.showSelectedSyncAccounts;
    if (!visibleAccounts.length) {
      list.innerHTML = `<div class="empty-state"><strong>暂无启用账号</strong></div>`;
      setScrollable(list, 0);
      return;
    }
    list.innerHTML = visibleAccounts.map((account) => {
      const checked = state.selectedSyncAccountKeys.has(account.key);
      return `<label class="account-check ${checked ? "selected" : ""}">
        <input type="checkbox" data-sync-account="${escapeHtml(account.key)}" ${checked ? "checked" : ""}>
        <span class="account-check-text">
          <strong>${escapeHtml(accountTitle(account))}</strong>
          <span>${escapeHtml(account.key)} · ${escapeHtml(account.browser_status || "空闲")}</span>
        </span>
      </label>`;
    }).join("");
    setScrollable(list, visibleAccounts.length);
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
    const groups = supplierCompanyGroups(rows);
    const selectedCompany = normalizeCompanySelection("selectedAssignCompanyKey", groups);
    const currentRows = groups.find((group) => group.key === selectedCompany)?.rows || [];
    const assigned = new Set(state.assignedSuppliers.map((item) => supplierKey(item.account_key, item.supplier_id)));
    const selectedOperator = state.operators.find((item) => item.operator_id === state.selectedOperatorId);
    const canAssign = Boolean(state.selectedOperatorId) && (isAdmin() || selectedOperator?.supply_chain_user_id === state.user?.user_id);
    body.innerHTML = renderCompanyPicker(groups, selectedCompany, "assign-company") + currentRows.map((item) => {
      const key = supplierKey(item.account_key, item.supplier_id);
      const mine = assigned.has(key);
      const syncing = hasActiveSupplierSync(item.account_key);
      return `<label class="pick-item ${mine ? "active" : ""} ${syncing ? "disabled" : ""}">
        <input type="checkbox" data-assign-supplier="${escapeHtml(key)}" ${mine ? "checked" : ""} ${canAssign && !syncing ? "" : "disabled"} ${syncing ? 'title="同步中，暂不可分配"' : ""}>
        <span>${escapeHtml(shortSupplierName(item.supplier_name || item.supplier_id))}</span>
      </label>`;
    }).join("");
    setScrollable(body, currentRows.length);
  }

  function renderSchedules() {
    const container = $("#schedule-list");
    if (!container) return;
    if (!state.operators.length) {
      container.innerHTML = `<div class="empty-state"><strong>请先新增组员，再配置定时任务</strong></div>`;
      return;
    }
    const dayLabels = ["一", "二", "三", "四", "五", "六", "日"];
    const schedules = [...state.schedules].sort((a, b) =>
      String(a.time_of_day || "").localeCompare(String(b.time_of_day || ""))
    );
    const taskTitle = (taskKey) => taskMeta(taskKey).title.replace(/^\d+[、.]\s*/, "");
    const taskTags = (schedule) => (schedule.task_keys || []).map((taskKey) =>
      `<span class="schedule-task-tag">${escapeHtml(taskTitle(taskKey))}</span>`
    ).join("");
    const dayTags = (schedule) => (schedule.weekdays || []).map((day) =>
      `<span class="schedule-day-tag">${escapeHtml(dayLabels[Number(day)] || "")}</span>`
    ).join("");
    const rows = schedules.length
      ? schedules.map((schedule) => `<section class="schedule-item ${schedule.enabled ? "active" : ""}" data-schedule-row="${escapeHtml(schedule.schedule_id)}">
          <div class="schedule-time-block">
            <strong>${escapeHtml(schedule.time_of_day || "09:00")}</strong>
            <span>${schedule.enabled ? "启用" : "暂停"}</span>
          </div>
          <div class="schedule-main">
            <div class="schedule-task-tags">${taskTags(schedule) || '<span class="muted">未选择任务</span>'}</div>
            <div class="schedule-day-tags">${dayTags(schedule)}</div>
            <span class="schedule-meta">${escapeHtml(scheduleOperatorNames(schedule))} · ${schedule.enabled ? "按所选日期执行" : "暂不执行"}</span>
          </div>
          <div class="schedule-actions">
            ${isAdmin() ? `
            <label class="toggle schedule-toggle" title="${schedule.enabled ? "暂停定时任务" : "启用定时任务"}">
              <input type="checkbox" data-schedule-enable="${escapeHtml(schedule.schedule_id)}" ${schedule.enabled ? "checked" : ""}>
              <span class="toggle-track"></span>
            </label>
            <button class="icon-button" type="button" data-schedule-edit="${escapeHtml(schedule.schedule_id)}" title="编辑定时任务" aria-label="编辑定时任务">${icon("edit")}</button>
            <button class="icon-button danger" type="button" data-schedule-delete="${escapeHtml(schedule.schedule_id)}" title="删除定时任务" aria-label="删除定时任务">${icon("trash")}</button>
            ` : `<span class="muted">只读</span>`}
          </div>
        </section>`).join("")
      : `<div class="empty-state"><strong>还没有定时任务</strong></div>`;
    container.innerHTML = `
      <div class="schedule-toolbar">
        <div>
          <strong>任务闹钟</strong>
          <span class="muted">一条闹钟可以包含多个任务、多个组员，规则可随时调整</span>
        </div>
        ${isAdmin() ? `<button class="button button-secondary" type="button" data-schedule-add>${icon("plus")}<span>新增闹钟</span></button>` : ""}
      </div>
      <div class="schedule-editor hidden" data-schedule-editor></div>
      <div class="schedule-rows">${rows}</div>
    `;
  }

  function renderScheduleEditor(schedule = null) {
    const editor = $("[data-schedule-editor]");
    if (!editor) return;
    const selectedTasks = new Set(schedule?.task_keys || []);
    const selectedOperators = new Set(scheduleOperatorIds(schedule));
    const allOperators = Boolean(schedule?.all_operators);
    const selectedDays = new Set((schedule?.weekdays || [0, 1, 2, 3, 4, 5, 6]).map(Number));
    const dayLabels = ["一", "二", "三", "四", "五", "六", "日"];
    editor.dataset.scheduleId = schedule?.schedule_id || "";
    editor.innerHTML = `
      <div class="schedule-editor-head">
        <strong>${schedule ? "编辑闹钟" : "新增闹钟"}</strong>
        <button class="icon-button" type="button" data-schedule-cancel title="关闭" aria-label="关闭">${icon("x")}</button>
      </div>
      <div class="schedule-editor-grid">
        <fieldset class="schedule-choice-group">
          <legend>执行任务</legend>
          <div class="schedule-task-options">
            ${TASK_ORDER.map((taskKey) => `<label class="schedule-choice">
              <input type="checkbox" data-schedule-edit-task value="${escapeHtml(taskKey)}" ${selectedTasks.has(taskKey) ? "checked" : ""}>
              <span>${escapeHtml(taskMeta(taskKey).title)}</span>
            </label>`).join("")}
          </div>
        </fieldset>
        <fieldset class="schedule-choice-group">
          <legend>执行日</legend>
          <div class="schedule-day-options">
            ${dayLabels.map((label, index) => `<label class="schedule-day-choice">
              <input type="checkbox" data-schedule-edit-day value="${index}" ${selectedDays.has(index) ? "checked" : ""}>
              <span>周${label}</span>
            </label>`).join("")}
          </div>
        </fieldset>
        <div class="schedule-editor-fields">
          <div class="schedule-field schedule-operator-field">
            <span>执行组员</span>
            <label class="schedule-choice">
              <input type="checkbox" data-schedule-all-operators ${allOperators ? "checked" : ""}>
              <span>全部组员（后续新增自动包含）</span>
            </label>
            <div class="schedule-operator-options">
              ${state.operators.map((operator) => `<label class="schedule-choice">
                <input type="checkbox" data-schedule-edit-operator value="${escapeHtml(operator.operator_id)}" ${selectedOperators.has(operator.operator_id) ? "checked" : ""}>
                <span>${escapeHtml(operator.name)}</span>
              </label>`).join("")}
            </div>
          </div>
          <label class="schedule-field">执行时间
            <input type="time" data-schedule-edit-time value="${escapeHtml(schedule?.time_of_day || "09:00")}">
          </label>
          <label class="toggle schedule-editor-enabled">
            <input type="checkbox" data-schedule-edit-enabled ${schedule?.enabled !== false ? "checked" : ""}>
            <span class="toggle-track"></span>
            <span>启用</span>
          </label>
        </div>
      </div>
      <div class="schedule-editor-actions">
        <button class="button button-primary" type="button" data-schedule-save>${schedule ? "保存修改" : "添加闹钟"}</button>
        <button class="button button-secondary" type="button" data-schedule-cancel>取消</button>
      </div>
    `;
    editor.classList.remove("hidden");
  }

  function scheduleDraft() {
    const editor = $("[data-schedule-editor]");
    const operatorIds = $$("[data-schedule-edit-operator]:checked").map((item) => item.value);
    const allOperators = Boolean(editor?.querySelector("[data-schedule-all-operators]")?.checked);
    return {
      task_keys: $$("[data-schedule-edit-task]:checked").map((item) => item.value),
      operator_id: operatorIds[0] || "",
      operator_ids: operatorIds,
      all_operators: allOperators,
      time_of_day: editor?.querySelector("[data-schedule-edit-time]")?.value || "09:00",
      weekdays: $$("[data-schedule-edit-day]:checked").map((item) => Number(item.value)),
      enabled: Boolean(editor?.querySelector("[data-schedule-edit-enabled]")?.checked),
      headed: true
    };
  }

  async function saveSchedule() {
    const editor = $("[data-schedule-editor]");
    const payload = scheduleDraft();
    if (!payload.task_keys.length || (!payload.all_operators && !payload.operator_id) || !payload.weekdays.length) {
      showToast("请选择任务、执行组员和执行日", true);
      return;
    }
    const scheduleId = editor?.dataset.scheduleId || "";
    try {
      await request(scheduleId ? `/api/schedules/${encodeURIComponent(scheduleId)}` : "/api/schedules", {
        method: scheduleId ? "PATCH" : "POST",
        body: JSON.stringify(payload)
      });
      showToast(scheduleId ? "闹钟已保存" : "闹钟已添加");
      editor?.classList.add("hidden");
      await loadData();
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
      renderSchedules();
    }
  }

  async function toggleSchedule(scheduleId, enabled) {
    const schedule = state.schedules.find((item) => item.schedule_id === scheduleId);
    if (!schedule) return;
    try {
      await request(`/api/schedules/${encodeURIComponent(scheduleId)}`, {
        method: "PATCH",
        body: JSON.stringify({
          task_keys: schedule.task_keys,
          operator_id: scheduleOperatorIds(schedule)[0] || "",
          operator_ids: scheduleOperatorIds(schedule),
          all_operators: Boolean(schedule.all_operators),
          time_of_day: schedule.time_of_day,
          weekdays: schedule.weekdays,
          enabled,
          headed: schedule.headed
        })
      });
      await loadData();
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
      renderSchedules();
    }
  }

  async function deleteSchedule(scheduleId) {
    if (!window.confirm("删除这个闹钟？")) return;
    try {
      await request(`/api/schedules/${encodeURIComponent(scheduleId)}`, { method: "DELETE" });
      showToast("闹钟已删除");
      await loadData();
    } catch (error) {
      showToast(`删除失败：${error.message}`, true);
    }
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
    const visibleCells = rows.flatMap((row) => TASK_ORDER.map((taskKey) => cellForRow(cells, row, taskKey)).filter(Boolean));
    const done = visibleCells.filter((item) => item.kind === "ok" || item.kind === "empty").length;
    const failed = visibleCells.filter((item) => item.kind === "failed").length;
    const total = rows.length * TASK_ORDER.length;
    if (caption) caption.textContent = failed
      ? `${done}/${total} · ${failed} 项失败`
      : `${done}/${total}`;
    board.innerHTML = `<table class="progress-table"><thead><tr><th>供应商</th>${TASK_ORDER.map((key) => `<th>${escapeHtml(taskFolder(key))}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>
      <th>${escapeHtml(shortSupplierName(row.supplier_name))}</th>
      ${TASK_ORDER.map((taskKey) => {
        const cell = cellForRow(cells, row, taskKey) || retryCellForRow(row, taskKey) || { kind: "idle", label: "未开始", note: "未开始" };
        const retryAttr = ["ok", "failed"].includes(cell.kind) && row.account_key && row.supplier_id
          ? ` data-retry-task="${escapeHtml(taskKey)}" data-retry-account="${escapeHtml(row.account_key)}" data-retry-supplier="${escapeHtml(row.supplier_id)}" data-retry-name="${escapeHtml(row.supplier_name || "")}"`
          : "";
        return `<td><button class="progress-cell progress-${cell.kind}" type="button" data-cell-note="${escapeHtml(`${shortSupplierName(row.supplier_name)} · ${taskFolder(taskKey)}：${cell.note}`)}"${retryAttr} title="${retryAttr ? "点击重新下载该单项" : escapeHtml(cell.note)}">${escapeHtml(cell.label)}</button></td>`;
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

  function cabinetSupplierKey(supplierId, supplierName) {
    return normalizedSupplierIdentity(supplierName || supplierId);
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
    const suppliers = new Map();
    const addSupplier = (supplierId, supplierName) => {
      const key = cabinetSupplierKey(supplierId, supplierName);
      if (!key) return;
      if (!suppliers.has(key)) suppliers.set(key, supplierName || supplierId || key);
    };
    operatorAll.forEach((file) => addSupplier(file.supplier_id, file.supplier_name));
    if (state.cabinet.operatorId === state.selectedOperatorId) {
      state.assignedSuppliers.forEach((supplier) => addSupplier(supplier.supplier_id, supplier.supplier_name));
    }
    if (state.cabinetSupplierFilter && !suppliers.has(state.cabinetSupplierFilter)) {
      state.cabinetSupplierFilter = "";
    }
    const matchSupplier = (file) =>
      !state.cabinetSupplierFilter
      || cabinetSupplierKey(file.supplier_id, file.supplier_name) === state.cabinetSupplierFilter;
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
      filter.innerHTML = [`<option value="">全部供应商</option>`].concat(
        [...suppliers.entries()].map(([id, name]) => `<option value="${escapeHtml(id)}" ${state.cabinetSupplierFilter === id ? "selected" : ""}>${escapeHtml(shortSupplierName(name))}</option>`)
      ).join("");
      filter.classList.remove("hidden");
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
    const live = unfinishedRuns();
    const closeButton = $("#close-idle-browsers-button");
    if (closeButton) {
      closeButton.disabled = !(state.health && state.health.status === "ok");
      closeButton.title = live.length ? "关闭空闲浏览器，使用中账号自动跳过" : "关闭未被任务占用的 RPA Chrome 窗口";
    }
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
              ${run.status === "paused"
                ? `<button class="icon-action" data-run-action="resume" data-run-id="${escapeHtml(run.run_id)}" title="继续">${icon("play")}</button>`
                : run.pause_requested
                  ? `<button class="icon-action" type="button" disabled title="暂停中">${icon("pause")}</button>`
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
          ${shot ? `<button class="icon-action" type="button" data-screenshot="${escapeHtml(raw.screenshot || raw.screenshot_id || "")}" title="截图">${icon("image")}</button>` : ""}
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
      body.innerHTML = `<tr><td colspan="5"><div class="empty-state"><strong>暂无账号</strong></div></td></tr>`;
      setScrollable($("#accounts-table-wrap"), 0);
      return;
    }
    body.innerHTML = state.accounts.map((account) => {
      const disabled = account.enabled === false || !account.key;
      const syncing = state.syncingAccountKeys.has(account.key) || hasActiveSupplierSync(account.key);
      return `<tr>
      <td><span class="cell-main">${escapeHtml(shortSubjectName(account.name) || account.key)}</span></td>
      <td>${escapeHtml(account.username || "-")}</td>
      <td>${escapeHtml(account.browser_status || "空闲")}</td>
      <td>${account.enabled !== false ? "启用" : "停用"}</td>
      <td><div class="table-actions">
        <button class="mini-button mini-button-primary" data-sync-account-row="${escapeHtml(account.key)}" type="button" ${disabled || syncing ? "disabled" : ""}>${syncing ? "同步中" : "同步清单"}</button>
        ${account.can_edit !== false ? `<button class="icon-action" data-edit-account="${escapeHtml(account.key)}" title="编辑">${icon("edit")}</button>` : ""}
        ${account.can_delete !== false ? `<button class="icon-action danger" data-delete-account="${escapeHtml(account.key)}" title="删除">${icon("trash")}</button>` : ""}
      </div></td>
    </tr>`;
    }).join("");
    setScrollable($("#accounts-table-wrap"), state.accounts.length);
  }

  async function createRun(options = {}) {
    if (!state.selectedOperatorId) return showToast("未选组员", true);
    if (options.batches?.length) {
      try {
        for (const batch of options.batches) {
          if (!batch.suppliers?.length) continue;
          await request("/api/runs", {
            method: "POST",
            body: JSON.stringify({
              task_keys: batch.taskKeys?.length ? batch.taskKeys : TASK_ORDER,
              account_keys: batch.accountKeys?.length ? batch.accountKeys : [...new Set(batch.suppliers.map((item) => item.account_key).filter(Boolean))],
              operator_id: state.selectedOperatorId,
              suppliers: batch.suppliers,
              force_account_tasks: true,
              headed: $("#headed-input") ? $("#headed-input").checked : true
            })
          });
        }
        showToast(options.toast || "已开始下载");
        await loadData();
      } catch (error) {
        showToast(`提交失败：${error.message}`, true);
      }
      return;
    }
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
    renderProgressBoard();
    try {
      await createRun({
        taskKeys: [taskKey],
        accountKeys: [accountKey],
        suppliers: [{ account_key: accountKey, supplier_id: supplierId, supplier_name: supplierName }],
        toast: "已提交重试，等待排队"
      });
    } finally {
      state.retryingKeys.delete(key);
      renderProgressBoard();
    }
  }

  async function runAction(action, runId) {
    if (action === "logs") return openLog(runId);
    const moveEndpoint = { "move-up": "move-up", "move-down": "move-down" }[action];
    if (moveEndpoint) {
      try {
        await request(`/api/runs/${encodeURIComponent(runId)}/${moveEndpoint}`, { method: "POST", body: "{}" });
        showToast(action === "move-up" ? "已上移" : "已下移");
        await loadData();
      } catch (error) {
        showToast(`操作失败：${error.message}`, true);
      }
      return;
    }
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

  async function closeIdleBrowsers() {
    try {
      const result = await request("/api/browsers/close-idle", { method: "POST", body: "{}" });
      const failed = (result.items || []).filter((item) => item.status === "close_failed").length;
      const inUse = (result.items || []).filter((item) => item.status === "in_use").length;
      if (failed) showToast(`已关闭 ${result.closed || 0} 个，${failed} 个关闭失败`, true);
      else if (result.closed) showToast(`已关闭 ${result.closed} 个空闲浏览器${inUse ? `，跳过 ${inUse} 个使用中账号` : ""}`);
      else showToast(inUse ? `没有可关闭的空闲浏览器，${inUse} 个账号使用中` : "没有需要关闭的浏览器");
      await loadData();
    } catch (error) {
      showToast(`关闭失败：${error.message}`, true);
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
    if (!data.username) return showToast("账号为空", true);
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

  function openDeleteAccountModal(account) {
    if (!account) return;
    state.deletingAccountKey = account.key;
    $("#delete-account-hint").textContent = `将删除“${shortSubjectName(account.name) || account.key}”及其供应商关联。此操作不可恢复。`;
    $("#delete-account-modal").classList.remove("hidden");
  }

  async function deleteAccount() {
    const accountKey = state.deletingAccountKey;
    if (!accountKey) return;
    try {
      await request(`/api/accounts/${encodeURIComponent(accountKey)}`, { method: "DELETE" });
      closeModal("delete-account-modal");
      showToast("账号已删除");
      await loadData();
    } catch (error) {
      showToast(`删除失败：${error.message}`, true);
    }
  }

  function closeModal(id) {
    const modal = $(`#${id}`);
    if (!modal) return;
    modal.classList.add("hidden");
    if (id === "risk-modal") state.pendingRunOptions = null;
    if (id === "password-modal") {
      $("#old-operator-password").value = "";
      $("#new-operator-password").value = "";
    }
    if (id === "delete-account-modal") state.deletingAccountKey = "";
    if (id === "delete-operator-modal") state.deletingOperatorId = "";
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
      const created = await request("/api/operators", { method: "POST", body: JSON.stringify({
        name,
        supply_chain_user_id: isAdmin() ? ($("#operator-owner-select")?.value || "") : ""
      }) });
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

  async function editOperatorName(operatorId) {
    const operator = state.operators.find((item) => item.operator_id === operatorId);
    if (!operator) return;
    const name = window.prompt("请输入新姓名", operator.name);
    if (name === null || !name.trim() || name.trim() === operator.name) return;
    try {
      await request(`/api/operators/${encodeURIComponent(operatorId)}`, {
        method: "PATCH", body: JSON.stringify({ name: name.trim() })
      });
      showToast("组员姓名已更新");
      await loadData();
    } catch (error) {
      showToast(`更新失败：${error.message}`, true);
    }
  }

  async function addSupplyChainUser() {
    const username = $("#supply-chain-username")?.value.trim();
    const name = $("#supply-chain-name")?.value.trim();
    const password = $("#supply-chain-password")?.value || "";
    if (!username || !name || !password) return showToast("请填写账号、姓名和初始密码", true);
    try {
      await request("/api/supply-chain-users", { method: "POST", body: JSON.stringify({ username, name, password }) });
      $("#supply-chain-username").value = "";
      $("#supply-chain-name").value = "";
      $("#supply-chain-password").value = "";
      showToast("供应链账号已创建");
      await loadData();
    } catch (error) {
      showToast(`创建失败：${error.message}`, true);
    }
  }

  async function toggleSupplyChainUser(userId, enabled) {
    try {
      await request(`/api/supply-chain-users/${encodeURIComponent(userId)}`, {
        method: "PATCH", body: JSON.stringify({ enabled })
      });
      showToast(enabled ? "供应链账号已启用" : "供应链账号已停用");
      await loadData();
    } catch (error) {
      showToast(`操作失败：${error.message}`, true);
    }
  }

  async function deleteSupplyChainUser(userId) {
    if (!window.confirm("删除这个供应链账号？")) return;
    try {
      await request(`/api/supply-chain-users/${encodeURIComponent(userId)}`, { method: "DELETE" });
      showToast("供应链账号已删除");
      await loadData();
    } catch (error) {
      showToast(`删除失败：${error.message}`, true);
    }
  }

  function downloadItemIdTemplate() {
    requestBlob("/api/item-id-config/template", "货品ID配置导入模板.xlsx");
  }

  async function rollbackItemIdConfig(uploadId) {
    if (!window.confirm("恢复这个货品 ID 配置版本？")) return;
    try {
      await request(`/api/item-id-config/uploads/${encodeURIComponent(uploadId)}/rollback`, { method: "POST" });
      showToast("已恢复历史版本");
      await loadData();
    } catch (error) {
      showToast(`恢复失败：${error.message}`, true);
    }
  }

  async function requestBlob(path, fileName) {
    try {
      const response = await fetch(path, { headers: { "Authorization": `Bearer ${state.authToken}` }, cache: "no-store" });
      if (!response.ok) throw new Error(`${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => { URL.revokeObjectURL(url); link.remove(); }, 1500);
    } catch (error) {
      showToast(`下载模板失败：${error.message}`, true);
    }
  }

  async function uploadItemIdConfig(file) {
    if (!file) return;
    try {
      const response = await fetch("/api/item-id-config/upload", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${state.authToken}`,
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "X-File-Name": encodeURIComponent(file.name)
        },
        body: file
      });
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload.detail;
        const message = typeof detail === "object" ? (detail.errors || []).slice(0, 3).map((item) => `第${item.row}行 ${item.error}`).join("；") : detail;
        throw new Error(message || `HTTP ${response.status}`);
      }
      showToast(`配置已上传：${payload.row_count} 个货品 ID`);
      await loadData();
    } catch (error) {
      showToast(`上传失败：${error.message}`, true);
    } finally {
      if ($("#item-id-upload")) $("#item-id-upload").value = "";
    }
  }

  function openPasswordModal() {
    $("#old-operator-password").value = "";
    $("#new-operator-password").value = "";
    $("#password-modal").classList.remove("hidden");
  }

  async function changeOperatorPassword(event) {
    event.preventDefault();
    const oldPassword = $("#old-operator-password").value;
    const newPassword = $("#new-operator-password").value;
    if (!newPassword) return showToast("新密码为空", true);
    try {
      await request("/api/operators/password/change", {
        method: "POST",
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
      });
      closeModal("password-modal");
      showToast("密码已修改");
    } catch (error) {
      showToast(`修改失败：${error.message}`, true);
    }
  }

  async function resetOperatorPassword(operatorId) {
    if (!operatorId) return;
    const operator = state.operators.find((item) => item.operator_id === operatorId);
    try {
      await request(`/api/operators/${encodeURIComponent(operatorId)}/password/reset`, { method: "POST", body: "{}" });
      showToast(`${operator?.name || "组员"} 密码已重置为 123456`);
    } catch (error) {
      showToast(`重置失败：${error.message}`, true);
    }
  }

  function openDeleteOperatorModal(operator) {
    if (!operator) return;
    state.deletingOperatorId = operator.operator_id;
    $("#delete-operator-hint").textContent = `将删除组员“${operator.name}”及其供应商分配，并停用其定时任务。此操作不可恢复。`;
    $("#delete-operator-modal").classList.remove("hidden");
  }

  async function deleteOperator() {
    const operatorId = state.deletingOperatorId;
    if (!operatorId) return;
    try {
      await request(`/api/operators/${encodeURIComponent(operatorId)}`, { method: "DELETE" });
      closeModal("delete-operator-modal");
      if (state.selectedOperatorId === operatorId) {
        state.selectedOperatorId = "";
        localStorage.removeItem("maochao_operator_id");
      }
      state.supplierSelectionInitialized = false;
      showToast("组员已删除");
      await loadData();
      await loadLoginOperators();
    } catch (error) {
      showToast(`删除失败：${error.message}`, true);
    }
  }

  async function syncEnabledAccountSuppliers() {
    const accountKeys = syncableAccounts()
      .filter((account) => state.selectedSyncAccountKeys.has(account.key))
      .map((account) => account.key);
    if (!accountKeys.length) return showToast("请选择要同步的账号", true);
    try {
      for (const accountKey of accountKeys) {
        await request(`/api/accounts/${encodeURIComponent(accountKey)}/suppliers/sync`, { method: "POST", body: "{}" });
      }
      showToast(`已提交 ${accountKeys.length} 个账号`);
      await loadData();
    } catch (error) {
      showToast(`同步失败：${error.message}`, true);
    }
  }

  async function syncAccountSuppliers(accountKey) {
    const account = state.accounts.find((item) => item.key === accountKey);
    if (!account) return showToast("账号不存在", true);
    if (account.enabled === false) return showToast("账号已停用", true);
    if (state.syncingAccountKeys.has(accountKey)) return;
    state.syncingAccountKeys.add(accountKey);
    renderAccountsTable();
    try {
      await request(`/api/accounts/${encodeURIComponent(accountKey)}/suppliers/sync`, { method: "POST", body: "{}" });
      showToast(`已提交“${accountTitle(account)}”同步清单`);
      await loadData();
    } catch (error) {
      showToast(`同步失败：${error.message}`, true);
    } finally {
      state.syncingAccountKeys.delete(accountKey);
      renderAccountsTable();
    }
  }

  function selectSyncAccounts(keys) {
    const enabledKeys = new Set(syncableAccounts().map((account) => account.key));
    state.selectedSyncAccountKeys = new Set(keys.filter((key) => enabledKeys.has(key)));
    state.syncAccountSelectionInitialized = true;
    persistSyncAccountSelection();
    renderSyncAccountList();
  }

  async function toggleAssignedSupplier(key, checked) {
    if (!state.selectedOperatorId) return showToast("未选组员", true);
    const operatorId = state.selectedOperatorId;
    const [accountKey, supplierId] = String(key).split("::");
    if (hasActiveSupplierSync(accountKey)) {
      showToast("供应商清单同步中，请完成后再分配", true);
      await loadData();
      return;
    }
    const current = state.assignedSuppliers
      .filter((item) => item.account_key === accountKey && item.visible)
      .map((item) => item.supplier_id);
    const next = checked ? [...new Set([...current, supplierId])] : current.filter((item) => item !== supplierId);
    try {
      await request(`/api/operators/${encodeURIComponent(operatorId)}/suppliers`, {
        method: "PUT",
        body: JSON.stringify({ account_key: accountKey, supplier_ids: next })
      });
      state.loadRevision += 1;
      state.supplierSelectionInitialized = false;
      if (state.selectedOperatorId === operatorId) {
        await loadAssignedSuppliers();
        renderAll();
      }
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
      await loadData();
    }
  }

  function bindEvents() {
    $("#login-form")?.addEventListener("submit", login);
    $("#logout-button")?.addEventListener("click", logout);
    $("#refresh-button")?.addEventListener("click", loadData);
    $("#close-idle-browsers-button")?.addEventListener("click", closeIdleBrowsers);
    $("#operator-select")?.addEventListener("change", (event) => selectOperator(event.target.value));
    $("#add-operator-button")?.addEventListener("click", addOperator);
    $("#add-supply-chain-button")?.addEventListener("click", addSupplyChainUser);
    $("#download-item-id-template")?.addEventListener("click", downloadItemIdTemplate);
    $("#item-id-upload")?.addEventListener("change", (event) => uploadItemIdConfig(event.target.files?.[0]));
    $("#sync-suppliers-button")?.addEventListener("click", syncEnabledAccountSuppliers);
    $("#sync-accounts-all")?.addEventListener("click", () => selectSyncAccounts(syncableAccounts().map((account) => account.key)));
    $("#sync-accounts-none")?.addEventListener("click", () => selectSyncAccounts([]));
    $("#sync-show-selected")?.addEventListener("change", (event) => {
      state.showSelectedSyncAccounts = event.target.checked;
      renderSyncAccountList();
    });
    $("#full-run-button")?.addEventListener("click", () => confirmRiskThenCreateRun(defaultRunOptions()));
    $("#risk-ack")?.addEventListener("change", (event) => {
      const button = $("#risk-confirm-button");
      if (button) button.disabled = !event.target.checked;
    });
    $("#risk-confirm-button")?.addEventListener("click", acceptRiskAndStart);
    $("#cancel-run-button")?.addEventListener("click", (event) => {
      const runId = event.currentTarget.dataset.runId;
      const run = state.runs.find((item) => item.run_id === runId);
      if (!runId) return;
      runAction(event.currentTarget.dataset.runAction || (run?.status === "pending" ? "cancel" : "pause"), runId);
    });
    $("#add-account-button")?.addEventListener("click", () => openAccountModal(null));
    $("#confirm-delete-account")?.addEventListener("click", deleteAccount);
    $("#confirm-delete-operator")?.addEventListener("click", deleteOperator);
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
      const syncAccount = event.target.dataset?.syncAccount;
      const scheduleId = event.target.dataset?.scheduleEnable;
      if (scheduleId) {
        await toggleSchedule(scheduleId, event.target.checked);
        return;
      }
      if (supplierKeyValue) {
        event.target.checked ? state.selectedSupplierKeys.add(supplierKeyValue) : state.selectedSupplierKeys.delete(supplierKeyValue);
        renderAll();
      }
      if (syncAccount) {
        event.target.checked ? state.selectedSyncAccountKeys.add(syncAccount) : state.selectedSyncAccountKeys.delete(syncAccount);
        state.syncAccountSelectionInitialized = true;
        persistSyncAccountSelection();
        renderSyncAccountList();
      }
      if (assignSupplier) await toggleAssignedSupplier(assignSupplier, event.target.checked);
    });

    document.addEventListener("click", (event) => {
      if (event.target.closest("#cabinet-pack")) return;
      const nav = event.target.closest("[data-view]");
      if (nav?.dataset.view) setView(nav.dataset.view);
      const loginTab = event.target.closest("[data-login-role]");
      if (loginTab) setLoginRole(loginTab.dataset.loginRole);
      const cabinetFile = event.target.closest("[data-cabinet-file]");
      if (cabinetFile) {
        event.preventDefault();
        downloadFiles([{ file_id: cabinetFile.dataset.cabinetFile, download_name: cabinetFile.dataset.fileName || "" }]);
        return;
      }
      const operatorPick = event.target.closest("[data-operator-id]");
      if (operatorPick && !event.target.closest(".operator-actions")) selectOperator(operatorPick.dataset.operatorId);
      const editOperator = event.target.closest("[data-edit-operator]");
      if (editOperator) {
        event.preventDefault();
        event.stopPropagation();
        editOperatorName(editOperator.dataset.editOperator);
        return;
      }
      const toggleSupply = event.target.closest("[data-toggle-supply-chain]");
      if (toggleSupply) {
        toggleSupplyChainUser(toggleSupply.dataset.toggleSupplyChain, toggleSupply.dataset.nextEnabled === "1");
        return;
      }
      const deleteSupply = event.target.closest("[data-delete-supply-chain]");
      if (deleteSupply) {
        deleteSupplyChainUser(deleteSupply.dataset.deleteSupplyChain);
        return;
      }
      const rollbackUpload = event.target.closest("[data-rollback-item-upload]");
      if (rollbackUpload) {
        rollbackItemIdConfig(rollbackUpload.dataset.rollbackItemUpload);
        return;
      }
      const deleteOperatorButton = event.target.closest("[data-delete-operator]");
      if (deleteOperatorButton) {
        event.preventDefault();
        event.stopPropagation();
        openDeleteOperatorModal(state.operators.find((operator) => operator.operator_id === deleteOperatorButton.dataset.deleteOperator));
        return;
      }
      const assignCompany = event.target.closest("[data-assign-company]");
      if (assignCompany) {
        state.selectedAssignCompanyKey = assignCompany.dataset.assignCompany;
        renderSuppliersTable();
        return;
      }
      const runCompany = event.target.closest("[data-run-company]");
      if (runCompany) {
        state.selectedRunCompanyKey = runCompany.dataset.runCompany;
        renderSupplierSelection();
        return;
      }
      const scheduleAdd = event.target.closest("[data-schedule-add]");
      if (scheduleAdd) {
        renderScheduleEditor();
        return;
      }
      const scheduleEdit = event.target.closest("[data-schedule-edit]");
      if (scheduleEdit) {
        const schedule = state.schedules.find((item) => item.schedule_id === scheduleEdit.dataset.scheduleEdit);
        if (schedule) renderScheduleEditor(schedule);
        return;
      }
      const scheduleSave = event.target.closest("[data-schedule-save]");
      if (scheduleSave) {
        saveSchedule();
        return;
      }
      const scheduleCancel = event.target.closest("[data-schedule-cancel]");
      if (scheduleCancel) {
        renderSchedules();
        return;
      }
      const scheduleDelete = event.target.closest("[data-schedule-delete]");
      if (scheduleDelete) {
        deleteSchedule(scheduleDelete.dataset.scheduleDelete);
        return;
      }
      const screenshot = event.target.closest("[data-screenshot]");
      if (screenshot) {
        event.preventDefault();
        openScreenshot(screenshot.dataset.screenshot);
        return;
      }
      const action = event.target.closest("[data-run-action]");
      if (action) runAction(action.dataset.runAction, action.dataset.runId);
      const syncAccountRow = event.target.closest("[data-sync-account-row]");
      if (syncAccountRow) {
        event.preventDefault();
        event.stopPropagation();
        syncAccountSuppliers(syncAccountRow.dataset.syncAccountRow);
        return;
      }
      const edit = event.target.closest("[data-edit-account]");
      if (edit) openAccountModal(state.accounts.find((account) => account.key === edit.dataset.editAccount));
      const remove = event.target.closest("[data-delete-account]");
      if (remove) openDeleteAccountModal(state.accounts.find((account) => account.key === remove.dataset.deleteAccount));
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
  (async () => {
    if (state.authToken) {
      try {
        const result = await request("/api/auth/me");
        state.user = result.user;
        if (isMember()) {
          state.selectedOperatorId = state.user.operator_id;
          state.cabinet.operatorId = state.user.operator_id;
        }
        $("#login-view")?.classList.add("hidden");
        $("#app-view")?.classList.remove("hidden");
        applyRoleView();
        await loadData();
        return;
      } catch (_) {}
    }
    resetAuth();
    await loadLoginOperators();
  })();
  window.setInterval(() => {
    if (state.authToken && state.user) loadData();
  }, 3000);
})();
