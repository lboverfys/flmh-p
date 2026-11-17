"use strict";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const examples = {
  selection: "请帮我在美国查露营灯并写口播脚本，预算不超过 30",
  history: "查询 camping-lamp-001 的历史趋势",
  detail: "查询 camping-lamp-001 的详情",
};
const labels = {
  awaiting_confirmation: "等待确认", processing: "生产中", completed: "已完成",
  needs_input: "需补充条件", needs_configuration: "等待配置", retryable: "可重试",
  QUEUED: "已排队", SUBMITTING: "正在提交", SUBMISSION_UNKNOWN: "提交结果待核实",
  SUCCEEDED: "生产成功", FAILED: "生产失败", CANCELLED: "已取消",
};
const state = { current: null, submission: null, busy: false, controller: null, pollTimer: null,
  pollCount: 0, productionLocked: false, uncertain: false, closing: false };

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
function canRevise() {
  return Boolean(state.current) && !state.productionLocked && !state.current.contentTask?.taskId
    && !state.current.conclusionDependencies?.confirmed_quote && !state.uncertain;
}
function updateControls() {
  document.querySelectorAll("button").forEach(button => { button.disabled = state.busy; });
  ui.request.disabled = state.busy || Boolean(state.submission || state.current);
  ui.start.disabled = state.busy || Boolean(state.current);
  ui.start.textContent = state.submission && !state.current ? "再次发送同一请求" : "开始处理 →";
  ui.refresh.disabled = state.busy || !runId();
  ui.confirm.disabled = state.busy || state.uncertain;
  ui.reject.disabled = state.busy || state.uncertain;
  ui["api-key"].disabled = state.busy;
  ui["run-id"].disabled = state.busy;
  visible("retry", state.current?.status === "retryable" && !state.uncertain);
  visible("revision-panel", canRevise());
}
function errorMessage(status, data) {
  if (status === 401) return "访问被拒绝。请在“访问设置”中填写此服务的 API key，再刷新或重发。";
  if (status === 404) return "未找到该运行或商品。请核对编号；本地内存模式下，重启 Agent 会清空运行记录。";
  if (status === 409) return "运行状态或报价已变化。请刷新后再操作；已有生产任务不能改写。";
  if (status === 422 || status === 400) {
    const details = data.details?.map(item => `${item.path.join(".")}: ${item.message}`).join("；");
    return `请求未通过校验：${details || data.error || "请核对输入"}`;
  }
  return `服务暂时未能完成请求（HTTP ${status}，${data.error || "未知错误"}）。运行编号已保留，请先刷新，再决定是否重试。`;
}
async function request(path, body, method = body === undefined ? "GET" : "POST") {
  const controller = new AbortController();
  state.controller = controller;
  const timeout = setTimeout(() => controller.abort(), 60000);
  const headers = { Accept: "application/json" };
  const key = ui["api-key"].value.trim();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal, cache: "no-store", credentials: "same-origin" });
    let data;
    try { data = await response.json(); } catch { throw new Error("服务返回了无法识别的响应。运行编号已保留，请刷新状态。"); }
    if (!response.ok) {
      const error = new Error(errorMessage(response.status, data));
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
  if (state.busy) return;
  stopPolling();
  if (!poll) state.pollCount = 0;
  state.busy = true;
  setError("");
  ui.activity.textContent = activity;
  ui.results.setAttribute("aria-busy", "true");
  updateControls();
  try {
    const result = await operation();
    state.current = result;
    state.uncertain = false;
    state.productionLocked = Boolean(result.contentTask?.taskId || result.conclusionDependencies?.confirmed_quote);
    render(!poll);
  } catch (error) {
    if (!state.closing) {
      setError(error.message);
      ui.activity.textContent = "操作未确认完成；当前运行与结果已保留。";
      if (mutation && (!error.status || error.status >= 500)) state.uncertain = true;
    }
  } finally {
    state.busy = false;
    ui.results.removeAttribute("aria-busy");
    updateControls();
  }
  if (!state.closing && !ui["error-box"].hidden) return;
  schedulePoll();
}
function schedulePoll() {
  if (state.closing || state.current?.status !== "processing") { ui["poll-note"].textContent = ""; return; }
  if (state.pollCount >= 20) { ui["poll-note"].textContent = "自动刷新已结束；任务仍在处理时，可点击“刷新状态”。"; return; }
  ui["poll-note"].textContent = `正在自动刷新（${state.pollCount}/20）。离开页面会停止刷新。`;
  state.pollTimer = setTimeout(() => {
    state.pollCount += 1;
    refresh(true);
  }, 1500);
}
function refresh(poll = false) {
  const id = runId();
  if (!validRunId(id)) { setError("请填写有效的运行编号。允许字母、数字、点、下划线、冒号及短横线。"); return; }
  return perform(() => request(`/v1/agent/runs/${encodeURIComponent(id)}`), "正在读取最新状态…", { poll });
}
function startSubmission() {
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
  setError("");
  for (const id of ["summary-panel", "confirmation-panel", "task-panel", "products-panel", "detail-panel", "history-panel", "technical-panel", "revision-panel"]) visible(id, false);
  visible("empty-state", true);
  ui["run-status"].textContent = "尚未开始";
  ui["run-status"].className = "badge";
  ui.activity.textContent = "提交一条需求，结果会出现在右侧。";
  ui["poll-note"].textContent = "";
  ui["submission-note"].textContent = "内容生产会先展示报价，确认后才创建任务。";
  updateControls();
  ui.request.focus();
}
function render(fillRevision) {
  const result = state.current;
  const currentTask = task();
  visible("empty-state", false);
  visible("summary-panel", true);
  visible("technical-panel", true);
  ui["run-id"].value = result.runId;
  ui["run-status"].textContent = labels[result.status] || result.status;
  ui["run-status"].className = `badge ${result.status === "completed" ? "good" : result.status === "awaiting_confirmation" ? "amber" : ""}`;
  ui.activity.textContent = result.status === "awaiting_confirmation" ? "查询与报价已完成，请在右侧确认或拒绝。"
    : result.status === "needs_input" ? "请在下方补充条件后重新查询。"
    : result.status === "needs_configuration" ? "所需能力尚未配置；具体待办见结果。"
    : result.status === "retryable" ? "部分步骤未完成，可以重试失败步骤。"
    : result.status === "processing" ? "内容任务已提交，正在读取处理进度。" : "本次处理已结束，结果如下。";
  ui["submission-note"].textContent = "当前运行编号已保留。调整条件请使用修订；新的需求请点击“新请求”。";
  ui.summary.textContent = result.summary || "当前步骤暂无汇总，请查看任务状态或重试失败步骤。";
  ui["plan-source"].textContent = result.plan?.source === "RULES" ? "本地规则规划" : text(result.plan?.source);
  const todos = result.configurationTodos || [];
  ui["configuration-todos"].replaceChildren(...todos.map(item => node("li", item)));
  visible("configuration-todos", todos.length > 0);
  ui["raw-response"].textContent = JSON.stringify(result, null, 2);
  const quote = result.confirmationRequest?.quote || result.quote || {};
  const awaiting = result.status === "awaiting_confirmation" && Boolean(result.confirmationRequest?.quoteId);
  visible("confirmation-panel", awaiting);
  if (awaiting) {
    fields(ui["quote-fields"], [["报价", `${text(quote.amount)} ${text(quote.currency)}`], ["预算上限", `${text(quote.budgetMax)} ${text(quote.currency)}`],
      ["商品", result.confirmationRequest?.product?.title || quote.productId], ["报价依据", quote.pricingSource], ["报价编号", quote.quoteId]]);
    ui["quote-brief"].textContent = quote.brief || result.contentBrief?.body || "未提供内容要求";
  }
  visible("task-panel", Boolean(currentTask.taskId));
  if (currentTask.taskId) {
    ui["task-status"].textContent = labels[currentTask.status] || text(currentTask.status);
    ui["task-status"].className = `badge ${currentTask.status === "SUCCEEDED" ? "good" : currentTask.status === "FAILED" ? "bad" : "amber"}`;
    fields(ui["task-fields"], [["任务编号", currentTask.taskId], ["生成来源", currentTask.generatedSource], ["供应商", currentTask.generatedProvider]]);
    visible("generated-content", Boolean(currentTask.generatedContent));
    ui["generated-content"].textContent = currentTask.generatedContent || "";
    visible("task-failure", Boolean(currentTask.failureReason));
    ui["task-failure"].textContent = currentTask.failureReason || "";
  }
  renderProducts(result.products || []);
  renderDetail(result.productDetail);
  renderHistory(result.productHistory);
  if (fillRevision) {
    ui["rev-product"].value = result.slotValues?.product || "";
    ui["rev-region"].value = result.slotValues?.region || "";
    ui["rev-budget"].value = result.slotValues?.budget_max ?? "";
  }
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

ui["request-form"].addEventListener("submit", event => { event.preventDefault(); startSubmission(); });
ui["new-run"].addEventListener("click", () => resetRun());
document.querySelectorAll("[data-example]").forEach(button => button.addEventListener("click", () => resetRun(examples[button.dataset.example])));
ui["lookup-form"].addEventListener("submit", event => {
  event.preventDefault();
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
ui.retry.addEventListener("click", () => perform(() => request(`/v1/agent/runs/${encodeURIComponent(runId())}/retry`, {}), "正在恢复失败步骤…", { mutation: true }));
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
window.addEventListener("pagehide", () => { state.closing = true; stopPolling(); state.controller?.abort(); ui["api-key"].value = ""; });
window.addEventListener("pageshow", () => { state.closing = false; });

async function checkConnection() {
  try {
    const response = await fetch("/health/ready", { signal: AbortSignal.timeout(5000), cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    ui.connection.textContent = "Agent 服务已连接";
    ui.connection.className = "connection connected";
    if (data.environment === "production") ui["environment-notice"].textContent = "生产配置模式：本页面展示服务实际返回的数据及来源，模型和数据服务以你的配置为准。";
  } catch {
    ui.connection.textContent = "服务连接待检查";
    ui.connection.className = "connection failed";
  }
}
updateControls();
checkConnection();
