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

const savedWork = { id: "work-1", title: "保存文字", module: "notes", type: "script", content: "真实用户文字", source: "USER_SAVED", createdAt: "2026-11-17" };
function workPage(records = [savedWork], page = 1, totalPages = 1) {
  return { records, page, pageSize: 10, total: records.length, totalPages, availableModules: ["notes", "content-production"], availableModulesTruncated: false };
}
function workResponder(page) {
  page.respond(async (url) => ({ ok: true, status: 200, json: async () => url.includes("/lineage")
    ? { workId: "work-1", entries: [], page: 1, pageSize: 20, total: 0, totalPages: 0 }
    : url.startsWith("/v1/works?") ? workPage() : { record: savedWork, reused: true } }));
}

test("作品目录受登录保护，分页查询一次且列表不逐条加载正文", async () => {
  const page = harness({ authenticated: false });
  assert.equal(page.ui("works-panel").hidden, true);
  page.submit("works-filter-form"); await settled(); assert.equal(page.calls.length, 0);
  page.run('state.token="unit";state.user={id:"alice",role:"USER"};updateControls();');
  page.response(workPage([savedWork], 1, 3));
  page.ui("works-type").value = "script"; page.ui("works-size").value = "10";
  page.submit("works-filter-form"); await settled();
  assert.equal(page.calls.length, 1);
  assert.match(page.calls[0].url, /^\/v1\/works\?page=1&pageSize=10&sort=createdAt_desc&type=script$/);
  assert.match(page.ui("works-rows").textContent, /保存文字/);
  assert.equal(page.ui("works-prev").disabled, true); assert.equal(page.ui("works-next").disabled, false);
  page.response(workPage([], 2, 3)); page.click("works-next"); await settled();
  assert.equal(page.calls.length, 2); assert.match(page.calls[1].url, /page=2/);
});

test("作品保存结果未知时保留同幂等键及原正文并阻止重复点击", async () => {
  const page = harness();
  page.ui("work-title").value = "保存文字"; page.ui("work-module").value = "notes"; page.ui("work-text").value = "原始正文";
  page.failure(new Error("connection lost"));
  page.submit("work-save-form"); page.submit("work-save-form"); await settled();
  assert.equal(page.calls.length, 1);
  const original = page.calls[0].options;
  assert.equal(original.headers["Idempotency-Key"], "work-test-uuid");
  assert.equal(page.ui("work-text").disabled, true); assert.match(page.ui("work-save-note").textContent, /幂等键已保留/);
  page.ui("work-text").value = "恶意改动界面字段";
  workResponder(page); page.submit("work-save-form"); await settled();
  const retry = page.calls[1].options;
  assert.equal(retry.headers["Idempotency-Key"], original.headers["Idempotency-Key"]);
  assert.equal(retry.body, original.body);
  assert.equal(page.run("works.pendingSave"), null);
  assert.equal(page.ui("work-text").value, ""); assert.match(page.ui("works-message").textContent, /没有重复创建/);
});

test("删除作品必须确认且换作品会清掉确认，删除不删除来源任务", async () => {
  const page = harness(); page.run(`renderWork(${JSON.stringify(savedWork)}); updateControls();`);
  page.click("work-delete"); await settled(); assert.equal(page.calls.length, 0);
  page.ui("work-delete-confirm").checked = true;
  page.run(`renderWork(${JSON.stringify({ ...savedWork, id: "work-2" })}); updateControls();`);
  assert.equal(page.ui("work-delete-confirm").checked, false);
  page.ui("work-delete-confirm").checked = true; workResponder(page); page.click("work-delete"); await settled();
  assert.equal(page.calls[0].url, "/v1/works/work-2"); assert.equal(page.calls[0].options.method, "DELETE");
  assert.equal(page.run("works.selected"), null); assert.match(page.ui("works-message").textContent, /来源任务和幂等记录保留/);
});

test("退出清空作品和幂等输入，迟到作品响应不能泄漏给新用户", async () => {
  const page = harness(); let resolve;
  page.respond(() => new Promise(done => { resolve = done; })); page.submit("works-filter-form");
  page.run(`renderWork(${JSON.stringify(savedWork)}); works.pendingSave={key:"key",payload:{content:"secret"}};`);
  page.ui("work-text").value = "secret"; page.click("logout");
  page.run('state.token="bob";state.user={id:"bob",role:"USER"};updateControls();');
  resolve({ ok: true, status: 200, json: async () => workPage() }); await settled();
  assert.equal(page.run("works.selected"), null); assert.equal(page.run("works.pendingSave"), null);
  assert.equal(page.ui("work-text").value, ""); assert.equal(page.ui("works-rows").textContent, "");
  assert.equal(page.ui("work-detail").hidden, true);
});

test("作品详情只显示文本，媒体原始URL不会生成自动访问节点", () => {
  const page = harness(); page.run(`renderWork(${JSON.stringify({ ...savedWork, title: "<img src=x onerror=alert(1)>", type: "video", content: "<script>secret</script>", mediaUrl: "http://localhost/private" })});`);
  assert.match(page.ui("work-fields").textContent, /<img src=x/); assert.match(page.ui("work-content").textContent, /<script>/);
  assert.equal(page.createdTags.some(tag => ["a", "img", "video", "iframe", "script"].includes(tag)), false);
  assert.equal(page.calls.length, 0);
});

test("媒体下载逐块写文件并使用受控鉴权路径，不创建整量Blob", async () => {
  const page = harness();
  page.run(`renderWork(${JSON.stringify({ ...savedWork, type: "video", mediaUrl: "https://ignored.invalid/file" })}); var writes=[], closed=0, aborted=0; window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async c=>writes.push(c.byteLength),close:async()=>closed++,abort:async()=>aborted++})});`);
  let reads = 0, cancelled = 0;
  page.respond(async () => ({ ok: true, headers: { get: name => name === "Content-Type" ? "video/webm" : name === "Content-Length" ? "8" : null },
    blob() { throw new Error("must not allocate Blob"); },
    body: { getReader: () => ({ async read() { reads++; return reads <= 2 ? { done: false, value: new Uint8Array(4) } : { done: true }; }, async cancel() { cancelled++; } }) } }));
  page.click("work-download"); await settled();
  assert.equal(page.calls[0].url, "/v1/works/work-1/file"); assert.equal(page.calls[0].options.headers.Authorization, "Bearer unit-alice");
  assert.equal(page.run("writes.join(',')"), "4,4"); assert.equal(page.run("closed"), 1); assert.equal(page.run("aborted"), 0);
  assert.equal(cancelled, 1); assert.match(page.ui("works-message").textContent, /\.webm/);
});

test("下载超过200MiB时在写入前取消上游", async () => {
  const page = harness();
  page.run(`renderWork(${JSON.stringify({ ...savedWork, type: "video", mediaUrl: "https://ignored.invalid/file" })}); var opened=0; window.showSaveFilePicker=async()=>({createWritable:async()=>{opened++;throw new Error("must not write");}});`);
  let cancelled = 0;
  page.respond(async () => ({ ok: true, headers: { get: name => name === "Content-Type" ? "video/mp4" : String(200 * 1024 * 1024 + 1) }, body: { async cancel() { cancelled++; } } }));
  page.click("work-download"); await settled();
  assert.equal(page.run("opened"), 0); assert.equal(cancelled, 1); assert.match(page.ui("works-message").textContent, /200 MiB/);
});

test("账号面板只向已登录用户显示，管理区只向管理员显示", () => {
  const page = harness({ authenticated: false });
  assert.equal(page.ui("account-panel").hidden, true);
  page.run('state.token="unit"; state.user={id:"alice",role:"USER"}; updateControls();');
  assert.equal(page.ui("account-panel").hidden, false);
  assert.equal(page.ui("admin-users").hidden, true);
  page.submit("user-search-form");
  assert.equal(page.calls.length, 0);
  page.run('state.user.role="ADMIN"; updateControls();');
  assert.equal(page.ui("admin-users").hidden, false);
});

test("资料保存不包含权限字段，退出清空资料与管理密码", async () => {
  const page = harness();
  page.response({ id: "alice", name: "Alice", phone: null, role: "USER", points: 1, source: "LOCAL_MOCK" });
  page.click("profile-load"); await settled();
  assert.equal(page.ui("profile-form").hidden, false);
  page.ui("profile-name").value = "新姓名"; page.ui("profile-phone").value = "";
  page.submit("profile-form"); await settled();
  assert.deepEqual(JSON.parse(page.calls[1].options.body), { name: "新姓名", phone: null });
  assert.equal(page.calls[1].options.method, "PUT");
  page.ui("user-password").value = "private";
  page.click("logout");
  assert.equal(page.ui("profile-fields").textContent, "");
  assert.equal(page.ui("user-password").value, "");
  assert.equal(page.ui("account-panel").hidden, true);
});

test("改密必须显式提交当前密码且成功后撤销页面登录", async () => {
  const page = harness();
  page.response({ success: true });
  page.ui("current-password").value = "old-password"; page.ui("new-password").value = "new-password";
  page.submit("password-form"); await settled();
  assert.equal(page.calls[0].url, "/v1/identity/password");
  assert.deepEqual(JSON.parse(page.calls[0].options.body), { currentPassword: "old-password", newPassword: "new-password" });
  assert.equal(page.ui("current-password").value, "");
  assert.equal(page.ui("new-password").value, "");
  assert.equal(page.run("state.token"), "");
  assert.equal(page.run("state.user"), null);
  assert.match(page.ui("auth-status").textContent, /旧登录凭证已撤销/);
});

test("用户列表按页查询且把姓名按文本展示", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; updateControls();');
  page.response({ users: [{ id: "user1", name: "<img src=x>", email: "u@example.invalid", role: "USER", status: "ACTIVE", points: 12, source: "LOCAL_MOCK" }], total: 11, page: 1, totalPages: 2 });
  page.ui("user-search").value = "A&B"; page.submit("user-search-form"); await settled();
  assert.equal(page.calls[0].url, "/v1/identity/users?search=A%26B&page=1&pageSize=10");
  assert.match(page.ui("user-rows").textContent, /<img src=x>/);
  assert.equal(page.createdTags.includes("img"), false);
  assert.equal(page.ui("users-prev").disabled, true);
  assert.equal(page.ui("users-next").disabled, false);
});

test("用户翻页失败保留已显示页码并重试同一页，未提交搜索不影响翻页", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; updateControls();');
  page.response({ users: [], total: 30, page: 1, totalPages: 3 });
  page.ui("user-search").value = "Alice"; page.submit("user-search-form"); await settled();
  page.ui("user-search").value = "Bob";
  page.response({ error: "account_service_unavailable" }, 503);
  page.click("users-next"); await settled();
  assert.equal(page.calls[1].url, "/v1/identity/users?search=Alice&page=2&pageSize=10");
  assert.equal(page.run("accounts.page"), 1);
  assert.match(page.ui("users-page").textContent, /第 1 页/);
  assert.equal(page.ui("users-prev").disabled, true);
  page.response({ users: [], total: 30, page: 2, totalPages: 3 });
  page.click("users-next"); await settled();
  assert.equal(page.calls[2].url, "/v1/identity/users?search=Alice&page=2&pageSize=10");
  assert.equal(page.run("accounts.page"), 2);
  page.response({ users: [], total: 1, page: 1, totalPages: 1 });
  page.submit("user-search-form"); await settled();
  assert.equal(page.calls[3].url, "/v1/identity/users?search=Bob&page=1&pageSize=10");
});

test("积分结果未知阻止重发，重新读取并选择目标后只显示余额且清空旧调整", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; updateControls();');
  const account = { id: "target", name: "Target", role: "USER", points: 10 };
  page.response({ users: [account], total: 1, page: 1, totalPages: 1 });
  page.submit("user-search-form"); await settled(); page.run("selectAccount(accounts.listing.users[0]);");
  page.ui("points-amount").value = "5"; page.ui("points-reason").value = "adjust";
  page.response({ error: "account_service_unavailable" }, 503);
  page.submit("points-form"); await settled();
  assert.equal(page.ui("points-amount").value, ""); assert.equal(page.ui("points-reason").value, "");
  assert.equal(page.ui("points-save").disabled, true);
  page.submit("points-form"); assert.equal(page.calls.length, 2);
  page.run("selectAccount(accounts.listing.users[0]);");
  assert.equal(page.ui("points-save").disabled, true);
  page.response({ users: [{ ...account, points: 15 }], total: 1, page: 1, totalPages: 1 });
  page.submit("user-search-form"); await settled();
  assert.equal(page.ui("points-save").disabled, true);
  page.run("selectAccount(accounts.listing.users[0]);");
  assert.equal(page.ui("points-save").disabled, false);
  assert.equal(page.ui("points-amount").value, "");
  assert.match(page.ui("points-status").textContent, /15/);
  assert.match(page.ui("points-status").textContent, /不.*判定.*成功/);
  assert.equal(page.calls.filter(call => call.options.method === "PUT").length, 1);
});

test("同一积分操作进行时重复提交只发一次，已成功响应后列表失败不再调整", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; selectAccount({id:"target",name:"Target",role:"USER",points:10});');
  let complete;
  page.respond(() => new Promise(resolve => { complete = resolve; }));
  page.ui("points-amount").value = "5"; page.ui("points-reason").value = "adjust";
  page.submit("points-form"); page.submit("points-form"); assert.equal(page.calls.length, 1);
  page.response({ error: "account_service_unavailable" }, 503);
  complete({ ok: true, status: 200, json: async () => ({ points: 15 }) }); await settled();
  assert.equal(page.calls.filter(call => call.options.method === "PUT").length, 1);
  assert.equal(page.ui("points-amount").value, ""); assert.equal(page.ui("points-reason").value, "");
  assert.equal(page.run("accounts.selected.points"), 15);
});

test("换用户后丢弃旧账号与积分迟到响应，不恢复资料或污染新会话", async () => {
  for (const operation of ["profile", "points"]) {
    const page = harness(); page.run('state.user.role="ADMIN"; selectAccount({id:"target",name:"Target",role:"USER",points:10});');
    let resolveOld;
    page.respond(() => new Promise(resolve => { resolveOld = resolve; }));
    if (operation === "profile") page.click("profile-load");
    else { page.ui("points-amount").value = "5"; page.ui("points-reason").value = "private"; page.submit("points-form"); }
    page.click("logout");
    page.run('state.token="bob-jwt"; state.user={id:"bob",role:"USER"}; updateControls();');
    resolveOld({ ok: true, status: 200, json: async () => operation === "profile"
      ? { id: "alice", name: "Alice private", role: "ADMIN", points: 10 } : { points: 15 } });
    await settled();
    assert.equal(page.run("state.user.id"), "bob"); assert.equal(page.run("state.user.role"), "USER");
    assert.equal(page.run("accounts.selected"), null); assert.equal(page.run("accounts.profile"), null);
    assert.equal(page.ui("profile-fields").textContent, ""); assert.equal(page.ui("points-reason").value, "");
    assert.equal(page.ui("account-message").textContent, ""); assert.equal(page.calls.length, 1);
  }
});

test("删除用户需选中目标并明确勾选，禁止删除当前登录用户", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; selectAccount({id:"target",name:"Target",role:"USER"});');
  page.click("user-delete"); assert.equal(page.calls.length, 0);
  page.ui("user-delete-confirm").checked = true;
  page.respond(async (url, options) => ({ ok: true, status: 200, json: async () => options.method === "DELETE" ? { success: true } : { users: [], total: 0, page: 1, totalPages: 0 } }));
  page.click("user-delete"); await settled();
  assert.equal(page.calls[0].url, "/v1/identity/users/target");
  assert.equal(page.calls[0].options.method, "DELETE");
  assert.equal(page.ui("user-adjustments").hidden, true);
  page.run('selectAccount({id:"alice",name:"Alice",role:"ADMIN"});');
  page.ui("user-delete-confirm").checked = true;
  page.click("user-delete"); assert.equal(page.calls.length, 2);
});

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

test("不允许修订的媒体状态不发起推进、核实或修订", async () => {
  for (const status of ["MOCKED", "SUCCEEDED", "CANCELLED", "UNKNOWN", "SUBMITTING"]) {
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

const revisionInput = { product_id: "product-1", image_prompt: "原商品图片事实", video_prompt: "原视频要求", aspect_ratio: "9:16", duration_seconds: 6, budget_max: 10 };
function failedMedia() {
  return { ...mediaResult("FAILED", "completed"), revision: 2, mediaRequest: revisionInput,
    slotValues: { product: "原商品", region: "US", budget_max: 10 }, conclusionDependencies: { confirmed_quote: "old-quote" } };
}

test("已失败媒体可修订且只提交改过的视频字段，不隐式复用旧图片事实", async () => {
  const page = harness();
  page.run("state.productionLocked=true"); page.show(failedMedia());
  assert.equal(page.ui("revision-panel").hidden, false);
  assert.equal(page.ui("revision-media-fields").hidden, false);
  assert.equal(page.ui("rev-image-prompt").value, "");
  assert.equal(page.ui("rev-video-prompt").value, "");
  assert.equal(page.ui("media-advance").disabled, true);
  assert.equal(page.ui("media-verify").disabled, true);
  page.ui("rev-product").value = "新商品"; page.ui("rev-video-prompt").value = "海边展示，保留标点；完整文字";
  page.response({ runId: "agent-media-1", revision: 3, status: "awaiting_confirmation", mediaRequest: { ...revisionInput, video_prompt: "海边展示，保留标点；完整文字" },
    mediaOriginRun: { runId: "media-1", status: "FAILED" }, quote: { quoteId: "new-quote", amount: 0, currency: "USD", reusedNodes: [] },
    confirmationRequest: { quoteId: "new-quote", mediaRequest: revisionInput }, products: [] });
  page.submit("revision-form"); await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].url, "/v1/agent/runs/agent-media-1/revision");
  assert.deepEqual(JSON.parse(page.calls[0].options.body), { expected_revision: 2, slot_updates: { product: "新商品", region: "US", budget_max: 10 },
    media_updates: { video_prompt: "海边展示，保留标点；完整文字" } });
  assert.equal(page.ui("confirmation-panel").hidden, false);
  assert.match(page.ui("quote-fields").textContent, /media-1/);
  assert.match(page.ui("quote-fields").textContent, /生产失败/);
  assert.equal(page.run("state.productionLocked"), false);
});

test("普通条件修订不发送媒体配置，未知操作结果阻止失败媒体再次提交", async () => {
  const page = harness(); page.show({ runId: "query-1", revision: 0, status: "completed", products: [], slotValues: { region: "US" } });
  assert.equal(page.ui("revision-media-fields").hidden, true);
  page.submit("revision-form"); await settled();
  assert.equal(Object.hasOwn(JSON.parse(page.calls[0].options.body), "media_updates"), false);
  page.show(failedMedia()); page.failure(new TypeError("offline"));
  page.ui("rev-duration").value = "10"; page.submit("revision-form"); await settled();
  assert.equal(page.run("state.uncertain"), true);
  assert.equal(page.ui("revision-panel").hidden, true);
  page.submit("revision-form"); await settled();
  assert.equal(page.calls.length, 2);
});

test("媒体修订拒绝非整数秒数，创作要求留空不覆盖原输入，退出清空内容", async () => {
  const page = harness(); page.show(failedMedia());
  page.ui("rev-duration").value = "2.5"; page.submit("revision-form"); await settled();
  assert.equal(page.calls.length, 0);
  page.ui("rev-duration").value = "10"; page.ui("rev-image-prompt").value = " "; page.submit("revision-form"); await settled();
  assert.equal(page.calls.length, 1);
  assert.deepEqual(JSON.parse(page.calls[0].options.body).media_updates, { duration_seconds: 10 });
  page.click("logout");
  assert.equal(page.ui("rev-video-prompt").value, "");
  assert.equal(page.ui("rev-duration").value, "");
});

test("管理员重置自己密码后直接退出而不使用撤销令牌再读身份", async () => {
  const page = harness(); page.run('state.user.role="ADMIN"; accounts.selected={id:"alice"}; updateControls();');
  page.ui("user-name").value="管理员"; page.ui("user-email").value="a@example.invalid";
  page.ui("user-password").value="replacement-password"; page.ui("user-role").value="ADMIN"; page.ui("user-status").value="ACTIVE";
  page.response({ id: "alice", role: "ADMIN" }); page.submit("user-form"); await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.run("state.token"), "");
  assert.match(page.ui("auth-status").textContent, /密码已重置/);
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
