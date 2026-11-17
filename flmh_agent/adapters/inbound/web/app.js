"use strict";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const examples = {
  selection: "请帮我在美国查露营灯并写口播脚本，预算不超过 30",
  media: "查美国旅行杯并制作图片和视频，预算10",
  history: "查询 camping-lamp-001 的历史趋势",
  detail: "查询 camping-lamp-001 的详情",
};
const demoUsers = {
  alice: { email: "alice@demo.flmh.invalid", password: "DemoAlice2026!" },
  bob: { email: "bob@demo.flmh.invalid", password: "DemoBob2026!" },
  admin: { email: "admin@demo.flmh.invalid", password: "DemoAdmin2026!" },
};
const labels = {
  awaiting_confirmation: "等待确认", processing: "生产中", completed: "已完成",
  needs_input: "需补充条件", needs_configuration: "等待配置", needs_verification: "等待核实", retryable: "可重试",
  QUEUED: "已排队", RUNNING: "执行中", SUBMITTING: "正在提交", SUBMISSION_UNKNOWN: "提交结果待核实",
  UNKNOWN: "结果未知", MOCKED: "模拟完成", SUCCEEDED: "生产成功", FAILED: "生产失败", CANCELLED: "已取消",
};
const mediaNodeLabels = { image: "图片", video: "视频" };
const state = { current: null, submission: null, busy: false, controller: null, pollTimer: null,
  pollCount: 0, productionLocked: false, uncertain: false, closing: false,
  token: "", user: null, authBusy: false, sessionVersion: 0 };
let workflowEditor;
let sessionsPanel;
const accounts = { profile: null, selected: null, listing: null, page: 1, search: "", listingVersion: 0, pointsUncertain: new Map() };
const works = { listing: null, page: 1, selected: null, lineage: null, pendingSave: null };

function text(value) { return value === null || value === undefined || value === "" ? "未提供" : String(value); }
function node(tag, value, className = "") {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = text(value);
  if (className) element.className = className;
  return element;
}
function fields(target, entries) {
  target.replaceChildren();
  for (const [label, value] of entries) target.append(node("span", label, "key"), node("span", value, "value"));
}
function visible(id, show) { ui[id].hidden = !show; }
function setError(message) { ui["error-box"].textContent = message || ""; visible("error-box", Boolean(message)); }
function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
function runId() { return state.current?.runId || state.submission?.run_id || ui["run-id"].value.trim(); }
function validRunId(value) { return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value); }
function task() { return state.current?.taskStatus?.taskId ? state.current.taskStatus : state.current?.contentTask || {}; }
function media() { return state.current?.mediaStatus?.runId ? state.current.mediaStatus : state.current?.mediaRun || {}; }
function canAdvanceMedia() {
  return Boolean(media().runId) && ["QUEUED", "RUNNING"].includes(media().status)
    && state.current?.status !== "needs_verification" && !state.uncertain;
}
function canVerifyMedia() { return Boolean(media().runId) && media().status === "SUBMISSION_UNKNOWN" && !state.uncertain; }
function canRevise() {
  return Boolean(state.current) && !state.productionLocked && !state.current.contentTask?.taskId
    && !media().runId && !state.current.conclusionDependencies?.confirmed_quote && !state.uncertain;
}
function requireUser() {
  if (state.token && state.user) return true;
  ui["auth-status"].textContent = "请先登录，再提交需求或读取自己的运行。";
  ui["login-email"].focus();
  return false;
}
function updateControls() {
  const blocked = state.busy || state.authBusy || !state.user;
  document.querySelectorAll("button").forEach(button => { button.disabled = blocked; });
  ui.login.disabled = state.busy || state.authBusy;
  ui.logout.disabled = false;
  document.querySelectorAll("[data-demo-user]").forEach(button => { button.disabled = state.authBusy; });
  ui["login-email"].disabled = state.authBusy;
  ui["login-password"].disabled = state.authBusy;
  visible("login-form", !state.user);
  visible("session-panel", Boolean(state.user));
  visible("identity-source", Boolean(state.user));
  visible("account-panel", Boolean(state.user));
  visible("works-panel", Boolean(state.user));
  updateWorkControls(blocked);
  visible("admin-users", isAdministrator());
  for (const id of ["profile-name", "profile-phone", "current-password", "new-password", "user-search", "user-name", "user-email", "user-phone", "user-password", "user-role", "user-status", "points-amount", "points-reason", "membership-level", "membership-expiry", "user-delete-confirm"]) ui[id].disabled = blocked;
  ui["users-prev"].disabled = blocked || !isAdministrator() || accounts.page <= 1;
  ui["users-next"].disabled = blocked || !isAdministrator() || !accounts.listing || accounts.page >= accounts.listing.totalPages;
  const pointsPending = accounts.selected && accounts.pointsUncertain.has(accounts.selected.id);
  const pointsBlocked = blocked || !isAdministrator() || !accounts.selected || pointsPending;
  for (const id of ["points-amount", "points-reason", "points-save"]) ui[id].disabled = Boolean(pointsBlocked);
  ui["points-status"].textContent = pointsPending
    ? "原积分调整结果未确认，已清空输入。请重新查询并选择此用户，核对当前余额后再填写新的调整。"
    : accounts.selected ? `当前读取余额：${text(accounts.selected.points)}。这不能判定之前的调整是否成功；新的提交会再次调整积分。` : "先从用户列表选择目标。";
  ui["user-delete"].disabled = blocked || !isAdministrator() || !accounts.selected || accounts.selected.id === state.user?.id || !ui["user-delete-confirm"].checked;
  ui.request.disabled = blocked || Boolean(state.submission || state.current);
  ui.start.disabled = blocked || Boolean(state.current);
  ui.start.textContent = state.submission && !state.current ? "再次发送同一请求" : "开始处理 →";
  ui.refresh.disabled = blocked || !runId();
  ui.confirm.disabled = blocked || state.uncertain;
  ui.reject.disabled = blocked || state.uncertain;
  ui["api-key"].disabled = state.busy || state.authBusy || Boolean(state.user);
  ui["run-id"].disabled = blocked;
  visible("retry", state.current?.status === "retryable" && !media().runId && !state.uncertain);
  ui["media-advance"].disabled = blocked || !canAdvanceMedia();
  ui["media-verify"].disabled = blocked || !canVerifyMedia();
  if (media().runId) {
    ui["media-action-note"].textContent = state.uncertain ? "上次操作结果尚未确认，请先刷新状态。"
      : canVerifyMedia() ? "提交结果未知，只能核实已有请求；核实不会重新提交。"
      : canAdvanceMedia() ? "点击一次只推进一步。刷新状态只读取记录，不推进任务。"
      : ["MOCKED", "SUCCEEDED", "FAILED", "CANCELLED"].includes(media().status) ? "本次媒体流程已结束，无需继续推进或核实。"
      : "当前状态不允许推进，请刷新状态查看服务返回的最新记录。";
  }
  visible("revision-panel", canRevise());
  workflowEditor?.updateControls(blocked);
  sessionsPanel?.updateControls(blocked);
}
function errorMessage(status, data) {
  if (data.error?.startsWith("work_") || data.error?.startsWith("invalid_work_")) {
    return { work_not_found: "作品或来源记录不存在，或不属于当前用户。", work_conflict: "作品保存冲突，或原作品已删除。请读取目录核对。",
      work_input_rejected: "作品服务拒绝了输入，请核对内容。", invalid_work_input: "作品输入无效，请核对必填内容和长度。",
      work_service_unavailable: "作品服务暂时不可用；保存可能已经成功，重试会使用相同幂等键。",
      work_file_size_exceeded: "文件超过下载上限或大小无效。" }[data.error] || "作品请求未完成，请刷新目录核对。";
  }
  if (data.error?.startsWith("account_") || data.error === "admin_required" || data.error === "invalid_account_input") {
    return { account_conflict: "账号变更冲突，邮箱可能已存在，或该用户仍被业务数据引用。请重新读取后核对。",
      account_not_found: "用户已不存在，请重新查询。", admin_required: "当前账号没有管理员权限。",
      account_input_rejected: "账号服务拒绝本次输入，请检查邮箱、密码、积分或会员信息。",
      invalid_account_input: "账号输入格式无效，请检查必填项与长度。",
      account_service_unavailable: "账号服务暂时不可用。变更可能已被接收，请先重新读取，勿重复提交积分调整。" }[data.error] || "账号操作未完成，请重新读取后核对。";
  }
  if (status === 401) return "登录已失效或访问凭据不匹配，请重新登录；服务要求 Agent 访问凭据时，也需填写访问设置。";
  if (status === 404) return "未找到该运行或商品。请核对编号；本地内存模式下，重启 Agent 会清空运行记录。";
  if (status === 409) return "运行状态或报价已变化。请刷新后再操作；已有生产任务不能改写。";
  if (status === 422 || status === 400) {
    const details = data.details?.map(item => `${item.path.join(".")}: ${item.message}`).join("；");
    return `请求未通过校验：${details || data.error || "请核对输入"}`;
  }
  return `服务暂时未能完成请求（HTTP ${status}，${data.error || "未知错误"}）。运行编号已保留，请先刷新，再决定是否重试。`;
}
async function request(path, body, method = body === undefined ? "GET" : "POST", { authenticated = true, idempotencyKey } = {}) {
  if (authenticated && !state.token) {
    const error = new Error("请先登录，再访问自己的运行。");
    error.status = 401;
    throw error;
  }
  const version = state.sessionVersion;
  const controller = new AbortController();
  state.controller = controller;
  const timeout = setTimeout(() => controller.abort(), 60000);
  const headers = { Accept: "application/json" };
  const key = ui["api-key"].value.trim();
  if (authenticated) headers.Authorization = `Bearer ${state.token}`;
  if (key) headers["X-Agent-Key"] = key;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal, cache: "no-store", credentials: "omit" });
    let data;
    try { data = await response.json(); } catch { throw new Error("服务返回了无法识别的响应。运行编号已保留，请刷新状态。"); }
    if (version !== state.sessionVersion) throw new Error("用户会话已改变，已丢弃原用户的响应。");
    if (!response.ok) {
      const message = !authenticated && response.status === 401 ? "邮箱或密码错误、账号不可用，或服务要求的 Agent 访问凭据未提供。" : errorMessage(response.status, data);
      if (authenticated && response.status === 401) clearSession(message);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求已停止或超时。服务可能已经接收操作，请先刷新当前运行，避免重复提交。");
    if (error instanceof TypeError) throw new Error("无法连接本机服务。请检查服务是否运行，再刷新当前运行；已保留运行编号。");
    throw error;
  } finally {
    clearTimeout(timeout);
    if (state.controller === controller) state.controller = null;
  }
}
async function perform(operation, activity, { mutation = false, poll = false } = {}) {
  if (state.busy || !requireUser()) return;
  const version = state.sessionVersion;
  stopPolling();
  if (!poll) state.pollCount = 0;
  state.busy = true;
  setError("");
  ui.activity.textContent = activity;
  ui.results.setAttribute("aria-busy", "true");
  updateControls();
  try {
    const result = await operation();
    if (version !== state.sessionVersion) return;
    state.current = result;
    state.uncertain = false;
    state.productionLocked = Boolean(result.contentTask?.taskId || result.mediaRun?.runId || result.mediaStatus?.runId
      || result.conclusionDependencies?.confirmed_quote);
    render(!poll);
  } catch (error) {
    if (!state.closing && version === state.sessionVersion) {
      setError(error.message);
      ui.activity.textContent = "操作未确认完成；当前运行与结果已保留。";
      if (mutation && (!error.status || error.status >= 500)) state.uncertain = true;
    }
  } finally {
    if (version === state.sessionVersion) {
      state.busy = false;
      ui.results.removeAttribute("aria-busy");
      updateControls();
    }
  }
  if (version !== state.sessionVersion) return;
  if (!state.closing && !ui["error-box"].hidden) return;
  schedulePoll();
}
function schedulePoll() {
  if (!state.user) return;
  if (media().runId) { ui["poll-note"].textContent = "媒体任务需手动推进或核实；刷新状态只读取记录。"; return; }
  if (state.closing || state.current?.status !== "processing") { ui["poll-note"].textContent = ""; return; }
  if (state.pollCount >= 20) { ui["poll-note"].textContent = "自动刷新已结束；任务仍在处理时，可点击“刷新状态”。"; return; }
  ui["poll-note"].textContent = `正在自动刷新（${state.pollCount}/20）。离开页面会停止刷新。`;
  state.pollTimer = setTimeout(() => {
    state.pollCount += 1;
    refresh(true);
  }, 1500);
}
function refresh(poll = false) {
  if (!requireUser()) return;
  const id = runId();
  if (!validRunId(id)) { setError("请填写有效的运行编号。允许字母、数字、点、下划线、冒号及短横线。"); return; }
  return perform(() => request(`/v1/agent/runs/${encodeURIComponent(id)}`), "正在读取最新状态…", { poll });
}
function startSubmission() {
  if (state.busy || !requireUser()) return;
  if (!state.submission) {
    const input = ui.request.value.trim();
    if (!input) { ui.request.focus(); return; }
    state.submission = { run_id: `web-${crypto.randomUUID()}`, user_request: input };
    ui["run-id"].value = state.submission.run_id;
  }
  return perform(() => request("/v1/agent/runs", state.submission), "正在规划并查询商品…", { mutation: true });
}
function resetRun(prompt = "") {
  if (state.busy) return;
  stopPolling();
  state.current = null;
  state.submission = null;
  state.productionLocked = false;
  state.uncertain = false;
  state.pollCount = 0;
  ui.request.value = prompt;
  ui["run-id"].value = "";
  for (const id of ["rev-product", "rev-region", "rev-budget"]) ui[id].value = "";
  for (const id of ["summary", "raw-response", "configuration-todos", "quote-fields", "quote-brief", "task-fields", "task-status",
    "generated-content", "task-failure", "media-fields", "media-status", "media-notice", "media-nodes", "media-artifacts", "media-artifact-note",
    "media-action-note", "products", "product-count", "detail-fields", "history-rows", "history-count", "history-next", "plan-source"]) ui[id].replaceChildren();
  setError("");
  for (const id of ["summary-panel", "confirmation-panel", "task-panel", "media-panel", "products-panel", "detail-panel", "history-panel", "technical-panel", "revision-panel"]) visible(id, false);
  visible("empty-state", true);
  ui["run-status"].textContent = "尚未开始";
  ui["run-status"].className = "badge";
  ui.activity.textContent = "提交一条需求，结果会出现在右侧。";
  ui["poll-note"].textContent = "";
  ui["submission-note"].textContent = "内容生产会先展示报价，确认后才创建任务。";
  updateControls();
  ui.request.focus();
}
function clearSession(message = "已退出。请登录后继续；旧结果与运行编号已清空。") {
  state.sessionVersion += 1;
  state.token = "";
  state.user = null;
  state.controller?.abort();
  state.controller = null;
  state.busy = false;
  state.authBusy = false;
  ui["login-email"].value = "";
  ui["login-password"].value = "";
  ui["api-key"].value = "";
  ui["identity-fields"].replaceChildren();
  ui["identity-source"].textContent = "";
  clearAccounts();
  clearWorks();
  workflowEditor?.reset();
  sessionsPanel?.reset();
  ui.results.removeAttribute("aria-busy");
  resetRun();
  ui["auth-status"].textContent = message;
  updateControls();
}
function updateWorkControls(blocked) {
  for (const id of ["works-type", "works-module", "works-sort", "works-size", "work-delete-confirm"]) ui[id].disabled = blocked;
  for (const id of ["work-title", "work-module", "work-type", "work-text"]) ui[id].disabled = blocked || Boolean(works.pendingSave);
  ui["works-prev"].disabled = blocked || works.page <= 1;
  ui["works-next"].disabled = blocked || !works.listing || works.page >= works.listing.totalPages;
  ui["lineage-prev"].disabled = blocked || !works.lineage || works.lineage.page <= 1;
  ui["lineage-next"].disabled = blocked || !works.lineage || works.lineage.page >= works.lineage.totalPages;
  ui["work-delete"].disabled = blocked || !works.selected || !ui["work-delete-confirm"].checked;
  ui["work-download"].disabled = blocked || !works.selected;
  ui["work-save"].textContent = works.pendingSave ? "重试同一保存请求" : "保存文字作品";
  ui["work-save-note"].textContent = works.pendingSave
    ? "本次保存结果尚未确认。内容与幂等键已保留，再次发送不会创建第二件作品。"
    : "正文最多 20,000 字；一次保存形成一件作品。";
}
function clearWorkDetail() {
  works.selected = null; works.lineage = null;
  ui["work-delete-confirm"].checked = false;
  for (const id of ["work-fields", "work-content", "work-lineage", "lineage-page"]) ui[id].replaceChildren();
  visible("work-detail", false);
}
function clearWorks() {
  works.listing = null; works.page = 1; works.pendingSave = null;
  clearWorkDetail();
  for (const id of ["works-rows", "works-page", "works-message", "works-facets-note"]) ui[id].replaceChildren();
  for (const id of ["work-title", "work-module", "work-text", "works-type"]) ui[id].value = "";
  ui["work-type"].value = "script"; ui["works-sort"].value = "createdAt_desc"; ui["works-size"].value = "10";
  const option = node("option", "全部"); option.value = ""; ui["works-module"].replaceChildren(option); ui["works-module"].value = "";
  ui["works-panel"].open = false;
}
async function workAction(operation, message) {
  if (state.busy || state.authBusy || !requireUser()) return;
  const version = state.sessionVersion;
  state.busy = true; stopPolling(); updateControls();
  ui["works-message"].textContent = "正在处理作品请求…";
  try {
    const result = await operation(version);
    if (version === state.sessionVersion) ui["works-message"].textContent = result || message;
  } catch (error) {
    if (version === state.sessionVersion) ui["works-message"].textContent = error.message;
  } finally {
    if (version === state.sessionVersion) { state.busy = false; updateControls(); }
  }
}
async function loadWorks(page = 1) {
  const params = [["page", page], ["pageSize", ui["works-size"].value || "10"], ["sort", ui["works-sort"].value || "createdAt_desc"]];
  for (const [name, id] of [["module", "works-module"], ["type", "works-type"]]) if (ui[id].value) params.push([name, ui[id].value]);
  const listing = await request("/v1/works?" + params.map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&"));
  works.listing = listing; works.page = listing.page;
  ui["works-rows"].replaceChildren();
  for (const work of listing.records) {
    const row = node("tr"); const name = node("td", work.title); name.append(node("small", work.type));
    const source = node("td", work.module); source.append(node("small", work.source));
    const action = node("td"); const button = node("button", "查看", "secondary"); button.type = "button";
    button.addEventListener("click", () => workAction(() => showWork(work.id), "已读取作品与来源记录。"));
    action.append(button); row.append(name, source, node("td", work.createdAt), action); ui["works-rows"].append(row);
  }
  if (!listing.records.length) { const row = node("tr"); const empty = node("td", "当前条件下暂无作品"); empty.colSpan = 4; row.append(empty); ui["works-rows"].append(row); }
  ui["works-page"].textContent = `第 ${listing.page} 页 / 共 ${listing.totalPages} 页 · ${listing.total} 件`;
  const selectedModule = ui["works-module"].value;
  const modules = [...listing.availableModules]; if (selectedModule && !modules.includes(selectedModule)) modules.push(selectedModule);
  ui["works-module"].replaceChildren();
  for (const value of ["", ...modules]) { const option = node("option", value || "全部"); option.value = value; ui["works-module"].append(option); }
  ui["works-module"].value = selectedModule;
  ui["works-facets-note"].textContent = listing.availableModulesTruncated ? "来源模块超过 100 个，仅显示前 100 个。" : "来源模块按当前类型汇总，不受当前页限制。";
}
async function loadLineage(page = 1) {
  const data = await request(`/v1/works/${encodeURIComponent(works.selected.id)}/lineage?page=${page}&pageSize=20`);
  works.lineage = data; ui["work-lineage"].replaceChildren();
  for (const item of data.entries) {
    const entry = node("div", undefined, "key-values work-origin");
    fields(entry, [["来源类型", item.kind], ["运行 / 节点", `${item.originId} / ${item.nodeKey}`], ["当前状态", item.sourceExists ? item.currentStatus : "来源已不存在"],
      ["尝试次数", item.attempt], ["请求编号", item.requestId], ["原运行", item.originRunId], ["输入指纹", item.inputFingerprint]]);
    ui["work-lineage"].append(entry);
  }
  if (!data.entries.length) ui["work-lineage"].append(node("p", "手动文字作品没有生产任务血缘。", "help"));
  ui["lineage-page"].textContent = `第 ${data.page} 页 / 共 ${data.totalPages} 页 · ${data.total} 条`;
}
function renderWork(work) {
  clearWorkDetail(); works.selected = work;
  fields(ui["work-fields"], [["标题", work.title], ["编号", work.id], ["类型 / 模块", `${work.type} / ${work.module}`],
    ["来源", work.source], ["供应商", work.provider], ["保存时间", work.createdAt]]);
  ui["work-content"].textContent = work.content || ""; visible("work-content", Boolean(work.content));
  const downloadable = ["image", "video"].includes(work.type) && Boolean(work.mediaUrl);
  visible("work-download", downloadable); visible("work-download-note", downloadable); visible("work-detail", true);
}
async function showWork(id) { renderWork(await request(`/v1/works/${encodeURIComponent(id)}`)); await loadLineage(); }
async function saveWork() {
  if (!works.pendingSave) {
    const payload = { module: ui["work-module"].value.trim(), title: ui["work-title"].value.trim(), type: ui["work-type"].value || "script", content: ui["work-text"].value };
    if (!payload.module || !payload.title || !payload.content.trim()) throw new Error("请填写作品模块、标题和正文。");
    works.pendingSave = { key: `work-${crypto.randomUUID()}`, payload };
  }
  updateControls();
  let result;
  try { result = await request("/v1/works", works.pendingSave.payload, "POST", { idempotencyKey: works.pendingSave.key }); }
  catch (error) {
    if (error.status === 400 || error.status === 422) works.pendingSave = null;
    throw error;
  }
  works.pendingSave = null;
  for (const id of ["work-title", "work-text"]) ui[id].value = "";
  renderWork(result.record); await loadLineage(); await loadWorks(1);
  return result.reused ? "已读取同一请求保存的作品，没有重复创建。" : "文字作品已保存，来源为 USER_SAVED。";
}
async function downloadWork(version) {
  const selected = works.selected;
  if (!selected || !["image", "video"].includes(selected.type)) return;
  if (typeof window.showSaveFilePicker !== "function") throw new Error("当前浏览器不支持分块保存文件。请使用本机 Chrome 的文件保存功能；本页面不会把大文件缓存为 Blob。");
  const handle = await window.showSaveFilePicker({ suggestedName: selected.id });
  if (version !== state.sessionVersion) return;
  const controller = new AbortController(); state.controller = controller;
  let writer, reader, saved = false;
  const timeout = setTimeout(() => controller.abort(), 300000);
  try {
    const headers = { Authorization: `Bearer ${state.token}` };
    if (ui["api-key"].value.trim()) headers["X-Agent-Key"] = ui["api-key"].value.trim();
    const response = await fetch(`/v1/works/${encodeURIComponent(selected.id)}/file`, { headers, signal: controller.signal, cache: "no-store", credentials: "omit" });
    if (version !== state.sessionVersion) throw new Error("用户已改变，下载已取消。");
    if (!response.ok) {
      const message = errorMessage(response.status, await response.json());
      if (response.status === 401) clearSession(message);
      throw new Error(message);
    }
    const extensions = { "image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4", "video/webm": ".webm" };
    const extension = extensions[response.headers.get("Content-Type")?.split(";")[0]];
    const size = Number(response.headers.get("Content-Length"));
    if (!extension || !Number.isSafeInteger(size) || size <= 0 || size > 200 * 1024 * 1024 || !response.body) {
      await response.body?.cancel(); throw new Error("文件类型或大小无效；下载上限为 200 MiB。");
    }
    writer = await handle.createWritable(); reader = response.body.getReader();
    let total = 0;
    while (true) {
      const item = await reader.read();
      if (version !== state.sessionVersion) throw new Error("用户已改变，下载已取消。");
      if (item.done) break;
      total += item.value.byteLength;
      if (total > size) throw new Error("实际文件大小超过服务声明，下载已停止。");
      await writer.write(item.value);
    }
    if (total !== size) throw new Error("文件下载不完整，未保存残缺文件。");
    await writer.close(); saved = true;
    return `文件已分块保存（${total} 字节）。实际格式为 ${extension}，文件名未含后缀时请使用此后缀。`;
  } finally {
    clearTimeout(timeout); controller.abort();
    if (reader) await reader.cancel().catch(() => {});
    if (writer && !saved) await writer.abort().catch(() => {});
    if (state.controller === controller) state.controller = null;
  }
}
function isAdministrator() { return ["ADMIN", "SUPER_ADMIN"].includes(state.user?.role); }
function clearAccounts() {
  accounts.profile = null; accounts.selected = null; accounts.listing = null; accounts.page = 1;
  accounts.search = ""; accounts.listingVersion = 0; accounts.pointsUncertain.clear();
  for (const id of ["profile-fields", "user-rows", "users-page", "account-message"]) ui[id].replaceChildren();
  for (const id of ["profile-name", "profile-phone", "current-password", "new-password", "user-search", "user-name", "user-email", "user-phone", "user-password", "points-amount", "points-reason", "membership-expiry"]) ui[id].value = "";
  ui["user-delete-confirm"].checked = false;
  ui["user-role"].value = "USER"; ui["user-status"].value = "ACTIVE"; ui["membership-level"].value = "FREE";
  ui["user-save"].textContent = "创建用户"; ui["user-edit-target"].textContent = "创建新用户";
  for (const id of ["profile-form", "user-status-field", "user-adjustments"]) visible(id, false);
}
async function accountAction(operation, message) {
  if (state.busy || state.authBusy || !requireUser()) return;
  const version = state.sessionVersion;
  state.busy = true; stopPolling(); updateControls();
  ui["account-message"].textContent = "正在处理账号请求…";
  try {
    await operation(version);
    if (version === state.sessionVersion) ui["account-message"].textContent = message;
  } catch (error) {
    if (version === state.sessionVersion) ui["account-message"].textContent = error.message;
  } finally {
    if (version === state.sessionVersion) { state.busy = false; updateControls(); }
  }
}
function showProfile(profile) {
  accounts.profile = profile;
  state.user = { ...state.user, role: profile.role };
  fields(ui["profile-fields"], [["邮箱", profile.email], ["角色", profile.role], ["积分", profile.points], ["会员", profile.membershipLevel], ["来源", profile.source]]);
  ui["profile-name"].value = profile.name; ui["profile-phone"].value = profile.phone || "";
  visible("profile-form", true);
}
function selectAccount(user) {
  if (user && accounts.pointsUncertain.has(user.id) && accounts.listingVersion > accounts.pointsUncertain.get(user.id)
      && accounts.listing.users.includes(user)) accounts.pointsUncertain.delete(user.id);
  accounts.selected = user;
  ui["user-edit-target"].textContent = user ? `正在编辑：${user.name}（${user.id}）` : "创建新用户";
  for (const [field, key] of [["user-name", "name"], ["user-email", "email"], ["user-phone", "phone"]]) ui[field].value = user?.[key] || "";
  ui["user-role"].value = user?.role || "USER"; ui["user-status"].value = user?.status || "ACTIVE";
  ui["user-password"].value = ""; ui["points-amount"].value = ""; ui["points-reason"].value = "";
  ui["user-delete-confirm"].checked = false; ui["membership-level"].value = user?.membershipLevel || "FREE";
  const expiry = user?.membershipExpireAt ? new Date(user.membershipExpireAt) : null;
  ui["membership-expiry"].value = expiry ? new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  visible("user-status-field", Boolean(user)); visible("user-adjustments", Boolean(user));
  ui["user-save"].textContent = user ? "保存用户变更" : "创建用户";
  updateControls();
}
async function loadAccounts(page = accounts.page, search = accounts.search) {
  const result = await request(`/v1/identity/users?search=${encodeURIComponent(search)}&page=${page}&pageSize=10`);
  accounts.listing = result; accounts.page = result.page; accounts.search = search; accounts.listingVersion += 1;
  ui["user-rows"].replaceChildren();
  ui["users-page"].textContent = `第 ${result.page} 页 / 共 ${result.totalPages} 页，${result.total} 个用户`;
  for (const user of result.users) {
    const row = node("tr"); const name = node("td", user.name); name.append(node("small", user.email));
    const controls = node("td"); const edit = node("button", "编辑"); edit.type = "button";
    edit.addEventListener("click", () => { if (!state.busy && isAdministrator()) selectAccount(user); }); controls.append(edit);
    row.append(name, node("td", `${user.role} / ${user.status}`), node("td", `${user.points} / ${user.source}`), controls); ui["user-rows"].append(row);
  }
}
async function login() {
  if (state.authBusy || state.busy) return;
  const email = ui["login-email"].value.trim();
  const password = ui["login-password"].value;
  if (!email || !password) { ui["auth-status"].textContent = "请填写邮箱和密码，再点击登录。"; return; }
  const version = state.sessionVersion;
  state.authBusy = true;
  ui["auth-status"].textContent = "正在核实登录身份…";
  updateControls();
  try {
    const result = await request("/v1/identity/login", { email, password }, "POST", { authenticated: false });
    if (version !== state.sessionVersion) return;
    if (!result.token || result.tokenType !== "Bearer") throw new Error("登录服务未返回可用凭证，请重新登录。");
    state.token = result.token;
    const user = await request("/v1/identity/me");
    if (version !== state.sessionVersion) return;
    state.user = user;
    resetRun();
    fields(ui["identity-fields"], [["用户", user.id], ["角色", user.role], ["身份来源", user.source]]);
    ui["identity-source"].textContent = user.source === "LOCAL_MOCK" ? "本地 Mock" : "已核实用户";
    ui["auth-status"].textContent = "登录成功。当前只展示此用户的运行；可以选择示例开始演示。";
  } catch (error) {
    if (version === state.sessionVersion) {
      state.token = "";
      state.user = null;
      ui["auth-status"].textContent = error.message;
    }
  } finally {
    if (version === state.sessionVersion) {
      ui["login-password"].value = "";
      state.authBusy = false;
      updateControls();
    }
  }
}
function render(fillRevision) {
  const result = state.current;
  const currentTask = task();
  const currentMedia = media();
  visible("empty-state", false);
  visible("summary-panel", true);
  visible("technical-panel", true);
  ui["run-id"].value = result.runId;
  ui["run-status"].textContent = labels[result.status] || result.status;
  ui["run-status"].className = `badge ${result.status === "completed" ? "good" : ["awaiting_confirmation", "needs_verification"].includes(result.status) ? "amber" : ""}`;
  ui.activity.textContent = result.status === "awaiting_confirmation" ? "查询与报价已完成，请在右侧确认或拒绝。"
    : result.status === "needs_input" ? "请在下方补充条件后重新查询。"
    : result.status === "needs_configuration" ? "所需能力尚未配置；具体待办见结果。"
    : result.status === "needs_verification" ? "媒体提交结果待核实，请使用右侧的核实按钮。"
    : result.status === "retryable" ? "部分步骤未完成，可以重试失败步骤。"
    : result.status === "processing" && currentMedia.runId ? "媒体任务已创建，请按当前状态手动推进或核实。"
    : result.status === "processing" ? "内容任务已提交，正在读取处理进度。" : "本次处理已结束，结果如下。";
  ui["submission-note"].textContent = "当前运行编号已保留。调整条件请使用修订；新的需求请点击“新请求”。";
  if (currentMedia.runId) {
    ui.summary.textContent = [`商品查询返回 ${(result.products || []).length} 项。`,
      `媒体任务状态：${labels[currentMedia.status] || text(currentMedia.status)}。`,
      `执行模式：${text(currentMedia.executionMode)}；返回产物 ${(currentMedia.artifacts || []).length} 项。`,
      currentMedia.status === "MOCKED" ? "本次仅完成模拟流程，未生成真实图片或视频文件。" : ""].filter(Boolean).join("\n");
  } else if (currentTask.taskId) {
    const status = labels[currentTask.status] || text(currentTask.status);
    const source = currentTask.generationSource ? `生成来源：${currentTask.generationSource}。` : "";
    ui.summary.textContent = [`商品查询返回 ${(result.products || []).length} 项。`, `内容任务状态：${status}。`, source]
      .filter(Boolean).join("\n");
  } else {
    ui.summary.textContent = result.summary || "当前步骤暂无汇总，请查看任务状态或重试失败步骤。";
  }
  ui["plan-source"].textContent = result.plan?.source === "RULES" ? "本地规则规划" : text(result.plan?.source);
  const todos = result.configurationTodos || [];
  ui["configuration-todos"].replaceChildren(...todos.map(item => node("li", item)));
  visible("configuration-todos", todos.length > 0);
  ui["raw-response"].textContent = JSON.stringify(result, null, 2);
  const quote = result.confirmationRequest?.quote || result.quote || {};
  const mediaRequest = result.confirmationRequest?.mediaRequest;
  const awaiting = result.status === "awaiting_confirmation" && Boolean(result.confirmationRequest?.quoteId);
  visible("confirmation-panel", awaiting);
  if (awaiting) {
    const entries = [["报价", `${text(quote.amount)} ${text(quote.currency)}`], ["预算上限", `${text(quote.budgetMax ?? mediaRequest?.budget_max)} ${text(quote.currency)}`],
      ["商品", result.confirmationRequest?.product?.title || quote.productId], ["报价依据", quote.pricingSource], ["报价编号", quote.quoteId]];
    ui["quote-title"].textContent = mediaRequest ? "确认图片与视频报价" : "确认内容报价";
    ui["quote-notice"].textContent = mediaRequest
      ? "请核对图片、视频要求和规格。即使模拟报价为零，也需要确认后才创建媒体任务。" : "请核对以下信息。点击确认后才提交内容生产。";
    if (mediaRequest) {
      entries.push(["执行模式", quote.executionMode], ["画面比例", quote.input?.aspectRatio ?? mediaRequest.aspect_ratio],
        ["视频时长", `${text(quote.input?.durationSeconds ?? mediaRequest.duration_seconds)} 秒`]);
      for (const [key, amount] of Object.entries(quote.nodePrices || {})) {
        entries.push([`${mediaNodeLabels[key] || key}节点报价`, `${text(amount)} ${text(quote.currency)}`]);
      }
      entries.push(["复用节点", quote.reusedNodes?.length === 0 ? "无" : quote.reusedNodes?.map(key => mediaNodeLabels[key] || key).join("、")]);
      if (quote.originRunId) entries.push(["重跑来源", quote.originRunId]);
    }
    fields(ui["quote-fields"], entries);
    ui["quote-brief"].textContent = mediaRequest
      ? `图片要求：${text(quote.input?.imagePrompt ?? mediaRequest.image_prompt)}\n视频要求：${text(quote.input?.videoPrompt ?? mediaRequest.video_prompt)}`
      : quote.brief || result.contentBrief?.body || "未提供内容要求";
  }
  visible("task-panel", Boolean(currentTask.taskId));
  if (currentTask.taskId) {
    ui["task-status"].textContent = labels[currentTask.status] || text(currentTask.status);
    ui["task-status"].className = `badge ${currentTask.status === "SUCCEEDED" ? "good" : currentTask.status === "FAILED" ? "bad" : "amber"}`;
    fields(ui["task-fields"], [["任务编号", currentTask.taskId], ["生成来源", currentTask.generationSource], ["适配器", currentTask.generationProvider]]);
    visible("generated-content", Boolean(currentTask.generatedContent));
    ui["generated-content"].textContent = currentTask.generatedContent || "";
    visible("task-failure", Boolean(currentTask.failureReason));
    ui["task-failure"].textContent = currentTask.failureReason || "";
  }
  renderMedia(currentMedia);
  renderProducts(result.products || []);
  renderDetail(result.productDetail);
  renderHistory(result.productHistory);
  if (fillRevision) {
    ui["rev-product"].value = result.slotValues?.product || "";
    ui["rev-region"].value = result.slotValues?.region || "";
    ui["rev-budget"].value = result.slotValues?.budget_max ?? "";
  }
}
function renderMedia(currentMedia) {
  visible("media-panel", Boolean(currentMedia.runId));
  ui["media-nodes"].replaceChildren();
  ui["media-artifacts"].replaceChildren();
  if (!currentMedia.runId) return;
  const artifacts = currentMedia.artifacts || [];
  ui["media-status"].textContent = labels[currentMedia.status] || text(currentMedia.status);
  ui["media-status"].className = `badge ${currentMedia.status === "SUCCEEDED" ? "good" : currentMedia.status === "FAILED" ? "bad" : "amber"}`;
  ui["media-notice"].textContent = currentMedia.status === "MOCKED" || currentMedia.executionMode === "MOCK_ONLY"
    ? "本次使用本地模拟适配器，未生成真实图片或视频文件。MOCKED 表示模拟流程完成。"
    : "媒体状态和产物均以服务实际返回为准。地址只显示文本，本页面不会自动打开或加载媒体。";
  fields(ui["media-fields"], [["媒体运行编号", currentMedia.runId], ["报价编号", currentMedia.quoteId],
    ["执行模式", currentMedia.executionMode], ["产物数量", artifacts.length]]);
  for (const item of currentMedia.nodes || []) {
    const card = node("article", undefined, "media-node");
    const heading = node("div", undefined, "section-heading");
    heading.append(node("h3", mediaNodeLabels[item.nodeKey] || item.nodeKey), node("span", labels[item.status] || text(item.status), "badge"));
    const info = node("div", undefined, "key-values");
    fields(info, [["执行次数", item.attempt], ["请求编号", item.requestId], ["供应商任务编号", item.providerTaskId],
      ["输出地址", item.outputUrl], ["来源", item.source], ["错误代码", item.errorCode]]);
    card.append(heading, info);
    ui["media-nodes"].append(card);
  }
  ui["media-artifact-note"].textContent = artifacts.length ? "以下为服务返回的产物记录，地址仅作文本展示。" : "暂无图片或视频文件。";
  for (const artifact of artifacts) ui["media-artifacts"].append(node("pre", typeof artifact === "string" ? artifact : JSON.stringify(artifact, null, 2), "media-artifact"));
}
function renderProducts(products) {
  ui.products.replaceChildren();
  visible("products-panel", products.length > 0);
  ui["product-count"].textContent = `${products.length} 项`;
  for (const product of products) {
    const card = node("article", undefined, "product-card");
    const info = node("div", undefined, "key-values");
    fields(info, [["地区", product.region], ["类目", product.category], ["7 日销量", product.metrics?.sales7d], ["来源", product.source], ["引用", product.sourceRef]]);
    card.append(node("h3", product.title), node("p", `${text(product.price)} ${text(product.currency)}`, "price"), info);
    if (product.id) {
      const actions = node("div", undefined, "product-actions");
      for (const [label, intent] of [["查看详情", "详情"], ["查看历史", "历史趋势"]]) {
        const button = node("button", label);
        button.type = "button";
        button.addEventListener("click", () => { resetRun(`查询 ${product.id} 的${intent}`); });
        actions.append(button);
      }
      card.append(actions);
    }
    ui.products.append(card);
  }
}
function renderDetail(detail) {
  visible("detail-panel", Boolean(detail?.product));
  if (!detail?.product) return;
  const product = detail.product;
  fields(ui["detail-fields"], [["商品", product.title], ["编号", product.id], ["价格", `${text(product.price)} ${text(product.currency)}`],
    ["地区", product.region], ["规范类目", product.category], ["来源类目", detail.externalCategory], ["店铺", detail.shopName], ["卖家", detail.sellerId],
    ["来源", product.source], ["来源引用", product.sourceRef], ["图片地址", detail.imageUrl], ["源站链接", detail.detailUrl]]);
}
function renderHistory(history) {
  visible("history-panel", Boolean(history?.product));
  ui["history-rows"].replaceChildren();
  if (!history?.product) return;
  const metrics = history.metrics || [];
  ui["history-count"].textContent = `${metrics.length} 条`;
  for (const metric of metrics) {
    const row = node("tr");
    const version = node("td", `v${metric.metricVersion}`);
    version.append(node("small", metric.capturedAt));
    const source = node("td", metric.source);
    source.append(node("small", metric.sourceRef));
    row.append(version, node("td", metric.sales7d), node("td", metric.salesAmount7d), node("td", metric.trendScore), source);
    ui["history-rows"].append(row);
  }
  ui["history-next"].textContent = history.nextBeforeVersion !== null && history.nextBeforeVersion !== undefined
    ? `还有更早快照。当前仅展示本页，下一页版本游标为 ${history.nextBeforeVersion}。`
    : metrics.length ? "已展示本次返回的全部历史快照。" : "该商品暂时没有已保存的指标快照。";
}

ui["profile-load"].addEventListener("click", () => accountAction(async () => showProfile(await request("/v1/identity/profile")), "已读取当前账号资料。"));
ui["profile-form"].addEventListener("submit", event => {
  event.preventDefault();
  accountAction(async () => showProfile(await request("/v1/identity/profile", { name: ui["profile-name"].value.trim(), phone: ui["profile-phone"].value.trim() || null }, "PUT")), "资料已保存。");
});
ui["password-form"].addEventListener("submit", event => {
  event.preventDefault();
  const payload = { currentPassword: ui["current-password"].value, newPassword: ui["new-password"].value };
  accountAction(async version => {
    try { await request("/v1/identity/password", payload, "PUT"); }
    finally { if (version === state.sessionVersion) { ui["current-password"].value = ""; ui["new-password"].value = ""; } }
  }, "密码已修改，下次登录请使用新密码。");
});
ui["user-search-form"].addEventListener("submit", event => {
  event.preventDefault(); if (state.busy || !isAdministrator()) return;
  accountAction(() => loadAccounts(1, ui["user-search"].value.trim()), "已读取用户列表。点击编辑可修改所选用户。");
});
for (const [id, delta] of [["users-prev", -1], ["users-next", 1]]) ui[id].addEventListener("click", () => {
  if (state.busy || !isAdministrator() || !accounts.listing) return;
  const next = accounts.page + delta;
  if (next < 1 || next > accounts.listing.totalPages) return;
  accountAction(() => loadAccounts(next), "已读取用户列表。");
});
ui["user-new"].addEventListener("click", () => { if (!state.busy && isAdministrator()) selectAccount(null); });
ui["user-form"].addEventListener("submit", event => {
  event.preventDefault(); if (!isAdministrator()) return;
  const selected = accounts.selected;
  const payload = { name: ui["user-name"].value.trim(), email: ui["user-email"].value.trim(), phone: ui["user-phone"].value.trim() || null, role: ui["user-role"].value };
  if (ui["user-password"].value) payload.password = ui["user-password"].value;
  if (!selected && !payload.password) { ui["account-message"].textContent = "创建用户需要填写密码。"; return; }
  if (selected) payload.status = ui["user-status"].value;
  accountAction(async version => {
    try {
      const user = await request(`/v1/identity/users${selected ? "/" + encodeURIComponent(selected.id) : ""}`, payload, selected ? "PUT" : "POST");
      selectAccount(user);
      if (user.id === state.user.id) {
        state.user = await request("/v1/identity/me");
        if (!isAdministrator()) { clearAccounts(); return; }
      }
      await loadAccounts();
    } finally { if (version === state.sessionVersion) ui["user-password"].value = ""; }
  }, selected ? "用户变更已保存。" : "用户已创建。");
});
ui["points-form"].addEventListener("submit", event => {
  event.preventDefault(); if (!isAdministrator() || !accounts.selected) return;
  const id = accounts.selected.id;
  if (accounts.pointsUncertain.has(id)) return;
  accountAction(async version => {
    let result;
    try {
      result = await request(`/v1/identity/users/${encodeURIComponent(id)}/points`, { amount: Number(ui["points-amount"].value), reason: ui["points-reason"].value.trim() }, "PUT");
    } catch (error) {
      if (version === state.sessionVersion && (!error.status || error.status >= 500)) {
        accounts.pointsUncertain.set(id, accounts.listingVersion);
        ui["points-amount"].value = ""; ui["points-reason"].value = "";
      }
      throw error;
    }
    accounts.selected = { ...accounts.selected, points: result.points };
    ui["points-amount"].value = ""; ui["points-reason"].value = "";
    await loadAccounts();
  }, "积分调整已保存，列表展示持久化后的实际余额。");
});
ui["membership-form"].addEventListener("submit", event => {
  event.preventDefault(); if (!isAdministrator() || !accounts.selected) return;
  const id = accounts.selected.id;
  const expiry = ui["membership-expiry"].value;
  accountAction(async () => {
    selectAccount(await request(`/v1/identity/users/${encodeURIComponent(id)}/membership`, {
      membershipLevel: ui["membership-level"].value, membershipExpireAt: expiry ? new Date(expiry).toISOString() : null,
    }, "PUT"));
    await loadAccounts();
  }, "会员设置已保存。");
});
ui["user-delete-confirm"].addEventListener("change", updateControls);
ui["user-delete"].addEventListener("click", () => {
  if (!isAdministrator() || !accounts.selected || accounts.selected.id === state.user.id || !ui["user-delete-confirm"].checked) return;
  const id = accounts.selected.id;
  accountAction(async () => {
    await request(`/v1/identity/users/${encodeURIComponent(id)}`, undefined, "DELETE");
    selectAccount(null); await loadAccounts(1);
  }, "所选用户已删除。");
});
ui["works-filter-form"].addEventListener("submit", event => { event.preventDefault(); workAction(() => loadWorks(1), "已刷新作品目录。"); });
ui["works-prev"].addEventListener("click", () => { if (works.page > 1) workAction(() => loadWorks(works.page - 1), "已读取上一页。"); });
ui["works-next"].addEventListener("click", () => { if (works.listing && works.page < works.listing.totalPages) workAction(() => loadWorks(works.page + 1), "已读取下一页。"); });
ui["works-type"].addEventListener("change", () => { ui["works-module"].value = ""; });
ui["work-save-form"].addEventListener("submit", event => { event.preventDefault(); workAction(saveWork, "已保存作品。"); });
ui["work-delete-confirm"].addEventListener("change", updateControls);
ui["work-delete"].addEventListener("click", () => {
  if (!works.selected || !ui["work-delete-confirm"].checked) return;
  const id = works.selected.id;
  workAction(async () => {
    try { await request(`/v1/works/${encodeURIComponent(id)}`, undefined, "DELETE"); }
    catch (error) { if (error.status !== 404) throw error; }
    clearWorkDetail(); await loadWorks(1);
  }, "作品已从目录移除，来源任务和幂等记录保留。");
});
ui["lineage-prev"].addEventListener("click", () => { if (works.lineage?.page > 1) workAction(() => loadLineage(works.lineage.page - 1), "已读取上一页来源。"); });
ui["lineage-next"].addEventListener("click", () => { if (works.lineage && works.lineage.page < works.lineage.totalPages) workAction(() => loadLineage(works.lineage.page + 1), "已读取下一页来源。"); });
ui["work-download"].addEventListener("click", () => workAction(downloadWork, "下载已取消。"));
ui["login-form"].addEventListener("submit", event => { event.preventDefault(); login(); });
ui.logout.addEventListener("click", () => clearSession());
document.querySelectorAll("[data-demo-user]").forEach(button => button.addEventListener("click", () => {
  if (state.authBusy || state.user) return;
  const user = demoUsers[button.dataset.demoUser];
  ui["login-email"].value = user.email;
  ui["login-password"].value = user.password;
  ui["auth-status"].textContent = "已填入本地 Mock 凭据。请点击“登录”核实身份。";
}));
ui["request-form"].addEventListener("submit", event => { event.preventDefault(); startSubmission(); });
ui["new-run"].addEventListener("click", () => resetRun());
document.querySelectorAll("[data-example]").forEach(button => button.addEventListener("click", () => resetRun(examples[button.dataset.example])));
ui["lookup-form"].addEventListener("submit", event => {
  event.preventDefault();
  if (!requireUser()) return;
  const id = ui["run-id"].value.trim();
  if (!validRunId(id)) { setError("请填写有效的运行编号。允许字母、数字、点、下划线、冒号及短横线。"); return; }
  perform(async () => {
    const result = await request(`/v1/agent/runs/${encodeURIComponent(id)}`);
    if (state.submission?.run_id !== id) state.submission = null;
    return result;
  }, "正在读取已有运行…");
});
ui.refresh.addEventListener("click", () => refresh());
ui["run-id"].addEventListener("input", updateControls);
ui.retry.addEventListener("click", () => {
  if (state.current?.status !== "retryable" || media().runId || state.uncertain) return;
  perform(() => request(`/v1/agent/runs/${encodeURIComponent(runId())}/retry`, {}), "正在恢复失败步骤…", { mutation: true });
});
for (const [id, action, allowed, activity] of [
  ["media-advance", "advance", canAdvanceMedia, "正在推进媒体任务一步…"],
  ["media-verify", "verify", canVerifyMedia, "正在核实已有媒体请求…"],
]) {
  ui[id].addEventListener("click", () => {
    if (!allowed()) return;
    perform(() => request(`/v1/agent/runs/${encodeURIComponent(runId())}/media/${action}`, undefined, "POST"), activity, { mutation: true });
  });
}
for (const [id, confirmed] of [["confirm", true], ["reject", false]]) {
  ui[id].addEventListener("click", () => {
    const quoteId = state.current?.confirmationRequest?.quoteId;
    if (state.current?.status !== "awaiting_confirmation" || !quoteId || state.uncertain) return;
    if (confirmed) state.productionLocked = true;
    perform(() => request(`/v1/agent/runs/${encodeURIComponent(runId())}/confirmation`, { confirmed, quote_id: quoteId }),
      confirmed ? "正在确认报价并提交内容任务…" : "正在记录拒绝决定…", { mutation: true });
  });
}
ui["revision-form"].addEventListener("submit", event => {
  event.preventDefault();
  if (!canRevise()) return;
  const budget = ui["rev-budget"].value;
  const updates = { product: ui["rev-product"].value.trim() || null,
    region: ui["rev-region"].value.trim().toUpperCase(), budget_max: budget === "" ? null : Number(budget) };
  perform(() => request(`/v1/agent/runs/${encodeURIComponent(runId())}/revision`, {
    expected_revision: state.current.revision, slot_updates: updates,
  }), "正在更新条件并重新查询…", { mutation: true });
});
window.addEventListener("pagehide", () => { state.closing = true; clearSession("页面已离开，登录凭证与结果已清空。请重新登录。"); });
window.addEventListener("pageshow", () => { state.closing = false; });

async function checkConnection() {
  try {
    const response = await fetch("/health/ready", { signal: AbortSignal.timeout(5000), cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    ui.connection.textContent = "Agent 服务已连接";
    ui.connection.className = "connection connected";
    visible("demo-accounts", data.environment === "local");
    if (data.environment === "production") ui["environment-notice"].textContent = "生产配置模式：本页面展示服务实际返回的数据及来源，模型和数据服务以你的配置为准。";
  } catch {
    ui.connection.textContent = "服务连接待检查";
    ui.connection.className = "connection failed";
  }
}
workflowEditor = globalThis.FlmhWorkflowEditor?.mount(ui["workflow-panel"], {
  request, authenticated: () => Boolean(state.user && state.token), blocked: () => state.busy || state.authBusy,
  version: () => state.sessionVersion, busy: value => { state.busy = value; updateControls(); },
});
sessionsPanel = globalThis.FlmhSessions?.mount(ui["agent-sessions-panel"], {
  request, authenticated: () => Boolean(state.user && state.token), blocked: () => state.busy || state.authBusy,
  version: () => state.sessionVersion, busy: value => { state.busy = value; updateControls(); },
  streamHeaders: () => ({ Authorization: `Bearer ${state.token}`, ...(ui["api-key"].value.trim() ? { "X-Agent-Key": ui["api-key"].value.trim() } : {}) }),
  clearSession,
  openRun: result => {
    stopPolling(); state.current = result; state.submission = null; state.uncertain = false;
    state.productionLocked = Boolean(result.contentTask?.taskId || result.mediaRun?.runId || result.mediaStatus?.runId || result.conclusionDependencies?.confirmed_quote);
    render(true); stopPolling(); updateControls();
  },
});
updateControls();
checkConnection();
