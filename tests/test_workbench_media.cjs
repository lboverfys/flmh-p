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

function harness() {
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
  let failure = null;
  const context = vm.createContext({
    document: {
      querySelectorAll(selector) {
        if (selector === "[id]") return [...elements.values()];
        if (selector === "button") return buttons;
        if (selector === "[data-example]") return buttons.filter(button => button.dataset.example);
        throw new Error(`Unexpected selector: ${selector}`);
      },
      createElement(tag) { createdTags.push(tag); return new Element(tag); },
    },
    window: { addEventListener() {} },
    crypto: { randomUUID: () => "test-uuid" },
    AbortController,
    AbortSignal,
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      if (url === "/health/ready") return { ok: true, json: async () => ({ environment: "demo" }) };
      calls.push({ url, options });
      if (failure) throw failure;
      return { ok: true, json: async () => response };
    },
  });
  vm.runInContext(script, context, { filename: "app.js" });
  return {
    calls, timers, createdTags,
    ui(id) { return elements.get(id); },
    click(id) { elements.get(id).dispatch("click"); },
    example(name) { buttons.find(button => button.dataset.example === name).dispatch("click"); },
    run(code) { return vm.runInContext(code, context); },
    response(value) { response = value; failure = null; },
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
