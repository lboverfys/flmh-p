"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "flmh_agent", "adapters", "inbound", "web", "workflows.js"), "utf8");

function harness() {
  const htmlWrites = [], createdTags = [], calls = [];
  class Element {
    constructor(tag, attributes = "") {
      this.tagName = tag; this.children = []; this.parent = null; this.listeners = new Map(); this.attributes = {};
      this.content = ""; this.value = ""; this.hidden = /\bhidden\b/.test(attributes); this.disabled = /\bdisabled\b/.test(attributes);
      this.checked = false; this.dataset = {}; this.className = "";
      for (const [, name, value] of attributes.matchAll(/([\w-]+)="([^"]*)"/g)) this.setAttribute(name, value);
    }
    setAttribute(name, value) {
      this.attributes[name] = value;
      if (name === "data-w") this.dataset.w = value;
      if (name === "class") this.className = value;
      if (name === "value") this.value = value;
    }
    set textContent(value) { this.content = String(value); this.children = []; }
    get textContent() { return this.content + this.children.map(child => child.textContent).join(" "); }
    set innerHTML(html) {
      htmlWrites.push(html); this.children = []; this.content = "";
      const stack = [this];
      for (const token of html.matchAll(/<(\/?)([a-z][a-z0-9-]*)\b([^>]*)>|([^<]+)/gi)) {
        if (token[4] !== undefined) {
          const current = stack.at(-1); current.content += token[4];
          if (current.tagName === "textarea") current.value += token[4];
          continue;
        }
        if (token[1]) { stack.pop(); continue; }
        const child = new Element(token[2], token[3]); stack.at(-1).append(child);
        if (!["input", "br", "hr", "img", "meta", "link"].includes(token[2])) stack.push(child);
      }
    }
    append(...items) {
      for (const item of items) { item.parent = this; this.children.push(item); }
      if (this.tagName === "select" && !this.value && this.children.length) this.value = this.children[0].value;
    }
    replaceChildren(...items) { this.children = []; this.content = ""; if (this.tagName === "select") this.value = ""; this.append(...items); }
    addEventListener(type, action) { const handlers = this.listeners.get(type) || []; handlers.push(action); this.listeners.set(type, handlers); }
    removeEventListener(type, action) { this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== action)); }
    dispatch(type) { for (const action of this.listeners.get(type) || []) action({ preventDefault() {} }); }
    querySelectorAll(selector) {
      const result = [], selectors = selector.split(",");
      const basic = (element, part) => part === "[data-w]" ? Boolean(element.dataset.w)
        : part.startsWith(".") ? element.className.split(/\s+/).includes(part.slice(1)) : element.tagName === part;
      const matches = (element, pattern) => {
        const parts = pattern.trim().split(/\s+/);
        if (!basic(element, parts.at(-1))) return false;
        if (parts.length === 1) return true;
        for (let parent = element.parent; parent; parent = parent.parent) if (basic(parent, parts[0])) return true;
        return false;
      };
      const visit = element => { for (const child of element.children) { if (selectors.some(pattern => matches(child, pattern))) result.push(child); visit(child); } };
      visit(this); return result;
    }
  }
  let epoch = 1, busy = false, authenticated = true, uuid = 0;
  let responder;
  const context = vm.createContext({ document: {
    createElement(tag) { createdTags.push(tag); return new Element(tag); },
    createElementNS(_namespace, tag) { createdTags.push(tag); return new Element(tag); },
  }, crypto: { randomUUID: () => `uuid-${++uuid}` } });
  vm.runInContext(source, context, { filename: "workflows.js" });
  const container = new Element("section");
  const editor = context.FlmhWorkflowEditor.mount(container, {
    authenticated: () => authenticated, blocked: () => busy, version: () => epoch, busy: value => { busy = value; },
    async request(url, body, method = body === undefined ? "GET" : "POST", options = {}) {
      calls.push({ url, body: body === undefined ? undefined : JSON.parse(JSON.stringify(body)), method, options: { ...options }, epoch });
      return responder(url, body, method, options);
    },
  });
  const fields = Object.fromEntries(container.querySelectorAll("[data-w]").map(element => [element.dataset.w, element]));
  const definition = {
    id: "wf-1", name: "图片工作流", description: "说明", version: 1,
    definition: { nodes: [{ key: "image", name: "封面", capability: "gemini:image", prompt: "生成封面", params: { aspectRatio: "9:16" }, assets: [] }], edges: [] },
    executability: { nodes: [{ nodeKey: "image", realAdapterSupported: true, reasons: [] }] },
  };
  let quoteNumber = 0;
  function run(id = "run-1") { return { runId: id, status: "RUNNING", cancelRequested: false,
    snapshot: { executionMode: "MOCK_ONLY" }, nodes: [{ nodeKey: "image", status: "QUEUED" }], artifacts: [] }; }
  async function defaults(url, body, method) {
    if (url.endsWith("/meta/capabilities")) return { capabilities: [{ id: "gemini:image", label: "图片", realAdapter: "SUPPORTED" }] };
    if (url.startsWith("/v1/workflows?")) return { workflows: [definition], page: 1, totalPages: 1, total: 1 };
    if (url === "/v1/workflows/wf-1" && method === "GET") return definition;
    if (url === "/v1/workflows/wf-1" && method === "PUT") return { ...definition, ...body, version: 2 };
    if (url.endsWith("/quotes")) return { quoteId: `quote-${++quoteNumber}`, amount: "2", currency: "USD", budgetMax: body.budgetMax,
      snapshot: { workflowId: "wf-1", productId: body.productId, definitionVersion: 1, executionMode: "MOCK_ONLY", nodePrices: { image: "2" }, reusedNodes: [], originRunId: body.originRunId || null } };
    if (url.endsWith("/confirmation")) return { quoteId: "quote-1", status: "CONFIRMED" };
    if (url === "/v1/workflows/wf-1/runs" && method === "POST") return run();
    if (url.startsWith("/v1/workflows/wf-1/runs?")) return { runs: [run("run-1"), run("run-2")], total: 2 };
    if (url.startsWith("/v1/workflow-runs/")) return run(url.split("/")[3]);
    throw new Error(`Unexpected request: ${method} ${url}`);
  }
  responder = defaults;
  async function dispatch(field, event) { fields[field].dispatch(event); await settled(); }
  return { fields, calls, htmlWrites, createdTags, definition, container,
    click: key => dispatch(key, "click"), submit: key => dispatch(key, "submit"),
    input(key, value) { fields[key].value = value; fields[key].dispatch("input"); },
    change(key, checked) { fields[key].checked = checked; fields[key].dispatch("change"); },
    respond(handler) { responder = (url, body, method, options) => handler(url, body, method, options, defaults); },
    async open() { await dispatch("load", "click"); fields.list.children[0].dispatch("click"); await settled(); },
    async quote() { fields.product.value = "p-1"; fields.budget.value = "10"; fields.origin.value = "origin-1"; await dispatch("quote-form", "submit"); },
    async chooseRun(index) { await dispatch("history", "click"); fields["history-list"].children[index].dispatch("click"); await settled(); },
    resetUser() { epoch++; busy = false; authenticated = true; editor.reset(); editor.updateControls(); },
    run,
  };
}

async function settled() { await new Promise(resolve => setImmediate(resolve)); }

test("商品预算和来源变化立即作废旧报价，确认不会发送过期请求", async () => {
  const page = harness(); await page.open();
  for (const field of ["product", "budget", "origin"]) {
    await page.quote();
    assert.equal(page.fields.confirm.disabled, false);
    assert.match(page.fields["quote-detail"].textContent, /商品 p-1/);
    assert.match(page.fields["quote-detail"].textContent, /修订来源 origin-1/);
    const before = page.calls.length;
    page.input(field, field === "budget" ? "1" : "changed");
    assert.equal(page.fields.confirm.disabled, true); assert.equal(page.fields["quote-detail"].hidden, true);
    await page.click("confirm"); assert.equal(page.calls.length, before);
  }
});

test("已应用节点修改必须重新保存和报价才允许确认", async () => {
  const page = harness(); await page.open(); await page.quote();
  const group = page.fields.canvas.children.find(element => element.tagName === "g"); group.dispatch("click");
  page.fields["node-prompt"].value = "全新要求"; await page.submit("node-form");
  assert.equal(page.fields.confirm.disabled, true); assert.equal(page.fields.quote.disabled, true);
  assert.equal(page.fields["quote-detail"].hidden, true);
  assert.match(page.fields.version.textContent, /未保存修改/);
  const before = page.calls.length; await page.click("confirm"); assert.equal(page.calls.length, before);
});

test("创建结果未知时锁定输入，重试沿用幂等键且不重复确认", async () => {
  const page = harness(); await page.open(); await page.quote();
  let starts = 0;
  page.respond((url, body, method, options, defaults) => {
    if (url === "/v1/workflows/wf-1/runs" && method === "POST" && ++starts === 1) throw new Error("response lost");
    return defaults(url, body, method, options);
  });
  await page.click("confirm");
  assert.match(page.fields.confirm.textContent, /重试同一次/);
  for (const field of ["product", "budget", "origin", "load", "save", "quote"]) assert.equal(page.fields[field].disabled, true, field);
  await page.click("confirm");
  const requests = page.calls.filter(call => call.url === "/v1/workflows/wf-1/runs" && call.method === "POST");
  assert.equal(requests.length, 2); assert.equal(requests[0].options.idempotencyKey, requests[1].options.idempotencyKey);
  assert.deepEqual(requests[0].body, requests[1].body);
  assert.equal(page.calls.filter(call => call.url.endsWith("/confirmation")).length, 1);
  assert.match(page.fields.run.textContent, /run-1/); assert.equal(page.fields.advance.disabled, false);
});

test("明确409拒绝释放创建状态，用户可查历史或重新报价", async () => {
  const page = harness(); await page.open(); await page.quote();
  page.respond((url, body, method, options, defaults) => {
    if (url === "/v1/workflows/wf-1/runs" && method === "POST") throw Object.assign(new Error("already running"), { status: 409 });
    return defaults(url, body, method, options);
  });
  await page.click("confirm");
  for (const field of ["product", "budget", "origin", "load", "history", "quote", "new"]) assert.equal(page.fields[field].disabled, false, field);
  assert.equal(page.fields.confirm.disabled, true); assert.equal(page.fields["quote-detail"].hidden, true);
  await page.chooseRun(0); assert.match(page.fields.run.textContent, /run-1/);
});

test("换用户清空商品预算与旧响应，迟到保存拒绝不能锁住新用户", async () => {
  const page = harness(); await page.open(); await page.quote();
  let reject;
  page.respond((url, body, method, options, defaults) => {
    if (method === "PUT") return new Promise((_resolve, failure) => { reject = failure; });
    return defaults(url, body, method, options);
  });
  const pending = page.submit("definition-form"); await settled();
  assert.equal(typeof reject, "function");
  page.resetUser();
  const message = page.fields.message.textContent;
  for (const field of ["name", "description", "product", "budget", "origin", "node-prompt"]) assert.equal(page.fields[field].value, "", field);
  assert.equal(page.fields.page.textContent, ""); assert.equal(page.fields.run.hidden, true); assert.equal(page.fields["quote-detail"].hidden, true);
  reject(new Error("previous user disconnected")); await pending; await settled();
  assert.equal(page.fields.message.textContent, message); assert.equal(page.fields.save.disabled, false);
});

test("迟到运行拒绝不影响新用户恢复的运行", async () => {
  const page = harness(); await page.open(); await page.chooseRun(0);
  let reject;
  page.respond((url, body, method, options, defaults) => {
    if (url.endsWith("/advance")) return new Promise((_resolve, failure) => { reject = failure; });
    return defaults(url, body, method, options);
  });
  const pending = page.click("advance"); await settled();
  page.resetUser(); await page.open(); await page.chooseRun(1);
  const message = page.fields.message.textContent;
  reject(new Error("old action unknown")); await pending; await settled();
  assert.match(page.fields.run.textContent, /run-2/); assert.equal(page.fields.advance.disabled, false);
  assert.equal(page.fields.message.textContent, message);
});

test("切换运行后取消必须重新确认，不能沿用原运行勾选", async () => {
  const page = harness(); await page.open(); await page.chooseRun(0);
  page.change("cancel-confirm", true); assert.equal(page.fields.cancel.disabled, false);
  await page.chooseRun(1); assert.equal(page.fields["cancel-confirm"].checked, false); assert.equal(page.fields.cancel.disabled, true);
  const before = page.calls.length; await page.click("cancel"); assert.equal(page.calls.length, before);
  page.change("cancel-confirm", true); await page.click("cancel");
  assert.equal(page.calls.at(-1).url, "/v1/workflow-runs/run-2/cancel");
});

test("节点与定义的不可信文字只作为文本，不写入动态HTML", async () => {
  const page = harness();
  const untrusted = '<img src=x onerror="alert(1)">';
  page.definition.name = untrusted; page.definition.definition.nodes[0].name = untrusted;
  page.definition.executability.nodes[0] = { nodeKey: untrusted, realAdapterSupported: false, reasons: [untrusted] };
  await page.open();
  assert.equal(page.fields.name.value, untrusted);
  assert.match(page.fields.canvas.textContent, /<img src=x/);
  assert.match(page.fields.support.textContent, /<img src=x/);
  assert.equal(page.htmlWrites.length, 1); assert.equal(page.htmlWrites[0].includes(untrusted), false);
  assert.equal(page.createdTags.some(tag => ["img", "video", "iframe", "script"].includes(tag)), false);
});
