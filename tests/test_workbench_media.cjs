"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const webRoot = path.join(__dirname, "..", "flmh_agent", "adapters", "inbound", "web");
const html = fs.readFileSync(path.join(webRoot, "index.html"), "utf8");
const script = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");

class Element {
  constructor(tagName, attributes = "") {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.value = "";
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = /\bdisabled\b/.test(attributes);
    this.dataset = {};
    this.id = attributes.match(/\bid="([^"]+)"/)?.[1];
    const example = attributes.match(/data-example="([^"]+)"/)?.[1];
    if (example) this.dataset.example = example;
    const demoUser = attributes.match(/data-demo-user="([^"]+)"/)?.[1];
    if (demoUser) this.dataset.demoUser = demoUser;
    this.content = "";
  }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return this.content + this.children.map(child => child.textContent).join("\n"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.content = ""; this.children = children; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatch(type) {
    for (const listener of this.listeners.get(type) || []) listener({ preventDefault() {} });
  }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
}

function harness({ authenticated = true } = {}) {
  const elements = new Map();
  const buttons = [];
  const createdTags = [];
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)) {
    const element = new Element(match[1], match[2]);
    if (element.id) elements.set(element.id, element);
    if (element.tagName === "button") buttons.push(element);
  }
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  let response = null;
  let status = 200;
  let failure = null;
  let responder = null;
  const windowEvents = new Map();
  const context = vm.createContext({
    document: {
      querySelectorAll(selector) {
        if (selector === "[id]") return [...elements.values()];
        if (selector === "button") return buttons;
        if (selector === "[data-example]") return buttons.filter(button => button.dataset.example);
        if (selector === "[data-demo-user]") return buttons.filter(button => button.dataset.demoUser);
        throw new Error(`Unexpected selector: ${selector}`);
      },
      createElement(tag) { createdTags.push(tag); return new Element(tag); },
    },
    window: { addEventListener(name, handler) { windowEvents.set(name, handler); } },
    crypto: { randomUUID: () => "test-uuid" },
    AbortController,
    AbortSignal,
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      if (url === "/health/ready") return { ok: true, json: async () => ({ environment: "local" }) };
      calls.push({ url, options });
      if (responder) return responder(url, options);
      if (failure) throw failure;
      return { ok: status >= 200 && status < 300, status, json: async () => response };
    },
  });
  vm.runInContext(script, context, { filename: "app.js" });
  if (authenticated) vm.runInContext('state.token = "unit-alice"; state.user = { id: "alice", role: "USER", source: "LOCAL_MOCK" }; updateControls();', context);
  return {
    calls, timers, createdTags,
    ui(id) { return elements.get(id); },
    click(id) { elements.get(id).dispatch("click"); },
    submit(id) { elements.get(id).dispatch("submit"); },
    windowEvent(name) { windowEvents.get(name)(); },
    example(name) { buttons.find(button => button.dataset.example === name).dispatch("click"); },
    demoUser(name) { buttons.find(button => button.dataset.demoUser === name).dispatch("click"); },
    run(code) { return vm.runInContext(code, context); },
    response(value, code = 200) { response = value; status = code; failure = null; responder = null; },
    respond(handler) { responder = handler; failure = null; },
    failure(error) { failure = error; },
    show(value) {
      context.fixture = value;
      vm.runInContext("state.current = fixture; render(true); updateControls();", context);
      response = value;
    },
  };
}

function mediaResult(status = "QUEUED", overall = "processing", overrides = {}) {
  return {
    runId: "agent-media-1", status: overall, products: [], plan: { source: "RULES", content_type: "product_media" },
    mediaStatus: { runId: "media-1", quoteId: "quote-1", status, executionMode: "MOCK_ONLY", nodes: [], artifacts: [], ...overrides },
  };
}

async function settled() { await new Promise(resolve => setImmediate(resolve)); }

test("零元媒体报价保留显式确认并展示已报价的规格", async () => {
  const page = harness();
  page.show({
    runId: "agent-media-1", status: "awaiting_confirmation", plan: { content_type: "product_media" }, products: [],
    confirmationRequest: {
      quoteId: "quote-1", product: { title: "美国杯子" },
      quote: { quoteId: "quote-1", amount: 0, currency: "USD", budgetMax: 10, pricingSource: "LOCAL_ZERO_COST", executionMode: "MOCK_ONLY",
        input: { productId: "cup-001", imagePrompt: "杯子商品图", videoPrompt: "杯子展示视频", aspectRatio: "9:16", durationSeconds: 6 }, nodePrices: { image: 0, video: 0 }, reusedNodes: [] },
      mediaRequest: { product_id: "cup-001", image_prompt: "杯子商品图", video_prompt: "杯子展示视频", aspect_ratio: "9:16", duration_seconds: 6, budget_max: 10 },
    },
  });
  assert.equal(page.ui("confirmation-panel").hidden, false);
  assert.equal(page.ui("media-panel").hidden, true);
  assert.equal(page.ui("confirm").disabled, false);
  assert.match(page.ui("quote-fields").textContent, /0 USD/);
  assert.match(page.ui("quote-fields").textContent, /LOCAL_ZERO_COST/);
  assert.match(page.ui("quote-fields").textContent, /9:16/);
  assert.match(page.ui("quote-fields").textContent, /6 秒/);
  assert.match(page.ui("quote-fields").textContent, /图片节点报价\n0 USD/);
  assert.match(page.ui("quote-fields").textContent, /视频节点报价\n0 USD/);
  assert.match(page.ui("quote-fields").textContent, /复用节点\n无/);
  assert.match(page.ui("quote-brief").textContent, /杯子商品图[\s\S]*杯子展示视频/);
  assert.match(page.ui("quote-notice").textContent, /即使模拟报价为零.*确认/);
  assert.equal(page.calls.length, 0);
  page.response(mediaResult());
  page.click("confirm");
  await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].url, "/v1/agent/runs/agent-media-1/confirmation");
  assert.deepEqual(JSON.parse(page.calls[0].options.body), { confirmed: true, quote_id: "quote-1" });
  assert.equal(page.ui("revision-panel").hidden, true);
});

test("未登录时不能提交、读取或创建运行编号，Mock 账号按钮仅填表", async () => {
  const page = harness({ authenticated: false });
  assert.equal(page.ui("login-form").hidden, false);
  for (const id of ["start", "load-run", "refresh", "confirm", "media-advance"]) assert.equal(page.ui(id).disabled, true, id);
  page.ui("request").value = "查询商品";
  page.submit("request-form");
  page.ui("run-id").value = "some-run";
  page.submit("lookup-form");
  page.click("refresh");
  await settled();
  assert.equal(page.calls.length, 0);
  assert.equal(page.run("state.submission"), null);
  page.demoUser("alice");
  assert.equal(page.ui("login-email").value, "alice@demo.flmh.invalid");
  assert.equal(page.ui("login-password").value, "DemoAlice2026!");
  assert.equal(page.calls.length, 0);
  assert.match(page.ui("auth-status").textContent, /点击.*登录/);
});

test("显式登录后再核实身份，业务请求带用户 Bearer 且访问凭据独立", async () => {
  const page = harness({ authenticated: false });
  page.demoUser("alice");
  page.ui("api-key").value = "agent-access-only";
  page.respond(async url => ({ ok: true, status: 200, json: async () => url.endsWith("/login")
    ? { token: "alice-jwt", tokenType: "Bearer" } : { id: "demo-alice", role: "USER", source: "LOCAL_MOCK" } }));
  page.submit("login-form");
  await settled();
  assert.deepEqual(page.calls.map(call => call.url), ["/v1/identity/login", "/v1/identity/me"]);
  assert.equal(page.calls[0].options.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(page.calls[0].options.body), { email: "alice@demo.flmh.invalid", password: "DemoAlice2026!" });
  assert.equal(page.calls[1].options.headers.Authorization, "Bearer alice-jwt");
  assert.equal(page.calls[1].options.headers["X-Agent-Key"], "agent-access-only");
  assert.equal(page.calls[1].options.headers["X-Service-Key"], undefined);
  assert.equal(page.ui("login-password").value, "");
  assert.equal(page.ui("login-form").hidden, true);
  assert.match(page.ui("identity-fields").textContent, /demo-alice/);
  assert.equal(page.ui("identity-source").textContent, "本地 Mock");
  page.response(mediaResult());
  page.show(mediaResult());
  page.click("media-advance");
  await settled();
  assert.equal(page.calls[2].options.headers.Authorization, "Bearer alice-jwt");
  assert.equal(page.calls[2].options.credentials, "omit");
});

test("退出换用户清除实际结果内容、历史、条件和运行编号", async () => {
  const page = harness();
  page.show({ ...mediaResult(), summary: "Alice confidential", productDetail: { product: { id: "alice-product", title: "Alice product" } },
    productHistory: { product: { id: "alice-product" }, metrics: [{ metricVersion: 1, source: "Alice source" }] },
    slotValues: { product: "alice-product", region: "US", budget_max: 20 } });
  page.run('state.submission = { run_id: "old-run" };');
  assert.match(page.ui("raw-response").textContent, /Alice confidential/);
  assert.match(page.ui("history-rows").textContent, /Alice source/);
  page.click("logout");
  assert.equal(page.run("state.current"), null);
  assert.equal(page.run("state.submission"), null);
  assert.equal(page.run("state.token"), "");
  for (const id of ["run-id", "request", "rev-product", "rev-region", "rev-budget", "api-key"]) assert.equal(page.ui(id).value, "", id);
  for (const id of ["summary", "raw-response", "products", "history-rows", "detail-fields", "media-nodes"]) assert.equal(page.ui(id).textContent, "", id);
  assert.equal(page.timers.size, 0);
  page.demoUser("bob");
  page.respond(async url => ({ ok: true, status: 200, json: async () => url.endsWith("/login")
    ? { token: "bob-jwt", tokenType: "Bearer" } : { id: "demo-bob", role: "USER", source: "LOCAL_MOCK" } }));
  page.submit("login-form");
  await settled();
  assert.equal(page.run("state.token"), "bob-jwt");
  assert.equal(page.ui("run-id").value, "");
  assert.equal(page.ui("history-rows").textContent, "");
  assert.equal(page.ui("technical-panel").hidden, true);
});

test("业务身份失效后清空旧结果并回到登录，禁止后续操作", async () => {
  const page = harness();
  page.show(mediaResult());
  page.response({ error: "unauthorized" }, 401);
  page.click("media-advance");
  await settled();
  assert.equal(page.run("state.token"), "");
  assert.equal(page.run("state.current"), null);
  assert.equal(page.ui("login-form").hidden, false);
  assert.equal(page.ui("run-id").value, "");
  assert.equal(page.ui("raw-response").textContent, "");
  assert.match(page.ui("auth-status").textContent, /登录已失效/);
  page.click("media-advance");
  page.click("refresh");
  assert.equal(page.calls.length, 1);
});

test("换用户后丢弃旧请求的迟到响应，不覆盖新身份或重新展示历史", async () => {
  const page = harness();
  page.show(mediaResult());
  let resolveOld;
  page.respond(() => new Promise(resolve => { resolveOld = resolve; }));
  page.click("refresh");
  assert.equal(page.run("state.busy"), true);
  page.click("logout");
  page.demoUser("bob");
  page.respond(async url => ({ ok: true, status: 200, json: async () => url.endsWith("/login")
    ? { token: "bob-jwt", tokenType: "Bearer" } : { id: "demo-bob", role: "USER", source: "LOCAL_MOCK" } }));
  page.submit("login-form");
  await settled();
  resolveOld({ ok: true, status: 200, json: async () => ({ ...mediaResult(), summary: "Alice private" }) });
  await settled();
  assert.equal(page.run("state.token"), "bob-jwt");
  assert.equal(page.run("state.user.id"), "demo-bob");
  assert.equal(page.run("state.current"), null);
  assert.equal(page.ui("raw-response").textContent, "");
  assert.equal(page.ui("run-id").value, "");
  assert.equal(page.ui("error-box").hidden, true);
});

test("页面离开清空内存登录态，返回后需要重新登录", () => {
  const page = harness();
  page.show(mediaResult());
  page.ui("api-key").value = "agent-access";
  page.windowEvent("pagehide");
  page.windowEvent("pageshow");
  assert.equal(page.run("state.token"), "");
  assert.equal(page.run("state.user"), null);
  assert.equal(page.ui("api-key").value, "");
  assert.equal(page.ui("raw-response").textContent, "");
  assert.equal(page.ui("start").disabled, true);
  assert.equal(page.ui("login-form").hidden, false);
  assert.equal(page.timers.size, 0);
});

test("登录失败不保留密码或获得会话，不调用业务接口", async () => {
  const page = harness({ authenticated: false });
  page.demoUser("admin");
  page.response({ error: "invalid_credentials_or_inactive_user" }, 401);
  page.submit("login-form");
  await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.run("state.token"), "");
  assert.equal(page.ui("login-password").value, "");
  assert.equal(page.ui("start").disabled, true);
  assert.match(page.ui("auth-status").textContent, /密码错误/);
});

test("媒体推进由单次点击触发无正文 POST，刷新只读且不自动轮询", async () => {
  const page = harness();
  page.show(mediaResult());
  page.run("schedulePoll()");
  assert.equal(page.ui("media-advance").disabled, false);
  assert.equal(page.ui("media-verify").disabled, true);
  assert.equal(page.timers.size, 0);
  assert.equal(page.calls.length, 0);
  page.response(mediaResult("RUNNING"));
  page.click("media-advance");
  await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].url, "/v1/agent/runs/agent-media-1/media/advance");
  assert.equal(page.calls[0].options.method, "POST");
  assert.equal(page.calls[0].options.body, undefined);
  assert.equal(page.calls[0].options.headers["Content-Type"], undefined);
  assert.equal(page.timers.size, 0);
  page.click("refresh");
  await settled();
  assert.equal(page.calls.length, 2);
  assert.equal(page.calls[1].url, "/v1/agent/runs/agent-media-1");
  assert.equal(page.calls[1].options.method, "GET");
});

test("最新媒体状态未知时仅核实已有请求，禁止推进和通用重试", async () => {
  const page = harness();
  page.show({ ...mediaResult("SUBMISSION_UNKNOWN", "needs_verification"), mediaRun: { runId: "media-1", status: "QUEUED" } });
  assert.equal(page.ui("media-advance").disabled, true);
  assert.equal(page.ui("media-verify").disabled, false);
  assert.equal(page.ui("retry").hidden, true);
  page.click("media-advance");
  page.click("retry");
  await settled();
  assert.equal(page.calls.length, 0);
  page.click("media-verify");
  await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].url, "/v1/agent/runs/agent-media-1/media/verify");
  assert.equal(page.calls[0].options.method, "POST");
  assert.equal(page.calls[0].options.body, undefined);
});

test("终态和未允许的媒体状态不发起推进、核实或修订", async () => {
  for (const status of ["MOCKED", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN", "SUBMITTING"]) {
    const page = harness();
    page.show(mediaResult(status, status === "UNKNOWN" ? "needs_verification" : "completed"));
    assert.equal(page.ui("media-advance").disabled, true, status);
    assert.equal(page.ui("media-verify").disabled, true, status);
    assert.equal(page.ui("revision-panel").hidden, true, status);
    page.click("media-advance");
    page.click("media-verify");
    page.click("retry");
    await settled();
    assert.equal(page.calls.length, 0, status);
  }
});

test("操作权限以运行状态为准，节点状态不自行放行核实", () => {
  const page = harness();
  page.show(mediaResult("RUNNING", "processing", { nodes: [{ nodeKey: "image", status: "SUBMISSION_UNKNOWN" }] }));
  assert.equal(page.ui("media-advance").disabled, false);
  assert.equal(page.ui("media-verify").disabled, true);
  page.show(mediaResult("RUNNING", "needs_verification"));
  assert.equal(page.ui("media-advance").disabled, true);
  assert.equal(page.ui("media-verify").disabled, true);
});

test("写入响应丢失时先锁定操作，读取最新状态后才能继续", async () => {
  const page = harness();
  page.show(mediaResult());
  page.failure(new Error("connection lost"));
  page.click("media-advance");
  await settled();
  assert.equal(page.ui("media-advance").disabled, true);
  assert.equal(page.ui("media-verify").disabled, true);
  assert.match(page.ui("media-action-note").textContent, /先刷新/);
  page.click("media-advance");
  page.click("media-verify");
  await settled();
  assert.equal(page.calls.length, 1);
  page.response(mediaResult("SUBMISSION_UNKNOWN", "needs_verification"));
  page.click("refresh");
  await settled();
  assert.equal(page.ui("media-advance").disabled, true);
  assert.equal(page.ui("media-verify").disabled, false);
});

test("模拟完成如实显示零产物，节点和产物地址仅按文本展示", () => {
  const page = harness();
  const url = "https://example.invalid/<img src=x onerror=alert(1)>";
  page.show(mediaResult("MOCKED", "completed", {
    nodes: [{ nodeKey: "image", status: "MOCKED", attempt: 1, requestId: "request-1", providerTaskId: "mock-1", outputUrl: null, source: "LOCAL_MOCK", errorCode: null }],
  }));
  assert.equal(page.ui("media-panel").hidden, false);
  assert.match(page.ui("summary").textContent, /返回产物 0 项/);
  assert.match(page.ui("media-notice").textContent, /未生成真实图片或视频文件/);
  assert.equal(page.ui("media-artifact-note").textContent, "暂无图片或视频文件。");
  assert.match(page.ui("media-nodes").textContent, /LOCAL_MOCK/);
  page.show(mediaResult("SUCCEEDED", "completed", {
    executionMode: "PROVIDER", nodes: [{ nodeKey: "image", status: "SUCCEEDED", outputUrl: url }], artifacts: [{ outputUrl: url }],
  }));
  assert.match(page.ui("media-nodes").textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(page.ui("media-artifacts").textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(page.createdTags.some(tag => ["a", "img", "video", "iframe", "script"].includes(tag)), false);
  page.example("media");
  assert.equal(page.ui("request").value, "查美国旅行杯并制作图片和视频，预算10");
  assert.equal(page.ui("media-panel").hidden, true);
});
