"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "..", "flmh_agent", "adapters", "inbound", "web", "sessions.js"), "utf8");

class Element {
  constructor(tag, attributes = "") {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    const key = attributes.match(/data-session="([^"]+)"/)?.[1];
    if (key) this.dataset.session = key;
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.content = "";
    this.attributes = {};
  }
  set innerHTML(value) {
    this.children = [...value.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)].map(match => new Element(match[1], match[2]));
  }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return this.content + this.children.map(child => child.textContent).join("\n"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.content = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(selector) {
    const rows = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    if (selector === "[data-session]") return rows.filter(row => row.dataset.session);
    if (selector === "button,input") return rows.filter(row => ["button", "input"].includes(row.tagName));
    throw new Error("unexpected selector " + selector);
  }
  addEventListener(kind, listener) { this.listeners.set(kind, listener); }
  dispatch(kind) { return this.listeners.get(kind)?.({ preventDefault() {} }); }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function settled() { await new Promise(resolve => setImmediate(resolve)); }

function harness({ authenticated = true } = {}) {
  const container = new Element("section");
  const calls = [];
  const fetchCalls = [];
  const opened = [];
  const timers = new Map();
  const windowEvents = new Map();
  const tags = [];
  let panel;
  let version = 1;
  let busy = false;
  let responder = async () => { throw new Error("no fixture response"); };
  let fetcher = async () => { throw new Error("no fixture stream"); };
  let timerId = 0;
  let cleared = 0;
  const host = {
    authenticated: () => authenticated,
    blocked: () => busy,
    version: () => version,
    busy(value) { busy = value; },
    async request(url, body, method = body === undefined ? "GET" : "POST") {
      calls.push({ url, body, method });
      return responder(url, body, method);
    },
    openRun(result) { opened.push(result); },
    streamHeaders: () => ({ Authorization: "Bearer memory-only-unit", "X-Agent-Key": "unit-service-key" }),
    clearSession() { authenticated = false; version += 1; busy = false; cleared += 1; panel.reset(); },
  };
  const context = vm.createContext({
    document: { createElement(tag) { tags.push(tag); return new Element(tag); } },
    window: { addEventListener(kind, listener) { windowEvents.set(kind, listener); } },
    URLSearchParams, AbortController, TextDecoder,
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) { fetchCalls.push({ url, options }); return fetcher(url, options); },
  });
  vm.runInContext(script, context);
  panel = context.FlmhSessions.mount(container, host);
  const elements = new Map(container.querySelectorAll("[data-session]").map(element => [element.dataset.session, element]));
  return {
    calls, fetchCalls, opened, timers, tags, panel, container,
    parser: context.FlmhSessions.eventParser,
    ui: key => elements.get(key),
    click: key => elements.get(key).dispatch("click"),
    submit: key => elements.get(key).dispatch("submit"),
    respond: handler => { responder = handler; },
    stream: handler => { fetcher = handler; },
    login() { authenticated = true; version += 1; panel.updateControls(); },
    logout: () => host.clearSession(),
    cleared: () => cleared,
    pagehide: () => windowEvents.get("pagehide")(),
  };
}

const record = { runId: "agent-one", title: "会话一", preview: "用户输入摘要", status: "awaiting_confirmation",
  version: 2, createdAt: "2026-11-17T10:00:00+00:00", updatedAt: "2026-11-17T10:01:00+00:00", deleted: false };
const result = { runId: record.runId, status: record.status, quote: { quoteId: "q-one" } };

async function selected(page, selectedRecord = record) {
  page.respond(async url => url.includes("?") ? { sessions: [selectedRecord], nextCursor: null } : { session: selectedRecord, run: result });
  page.click("load"); await settled();
  page.ui("list").children[0].dispatch("click"); await settled();
}

function readable(page, status = 200) {
  const queue = [];
  let pending;
  let aborted = false;
  const counts = { cancel: 0, release: 0 };
  const reader = {
    read() {
      if (aborted) return Promise.reject(new DOMException("aborted", "AbortError"));
      if (queue.length) return Promise.resolve(queue.shift());
      pending = deferred(); return pending.promise;
    },
    async cancel() { counts.cancel += 1; pending?.resolve({ done: true }); pending = null; },
    releaseLock() { counts.release += 1; },
  };
  page.stream(async (_url, options) => {
    options.signal.addEventListener("abort", () => {
      aborted = true;
      pending?.reject(new DOMException("aborted", "AbortError")); pending = null;
    });
    return { status, ok: status === 200, headers: new Headers({ "content-type": "text/event-stream; charset=utf-8" }),
      body: { getReader: () => reader } };
  });
  function push(value) {
    const chunk = { done: false, value };
    if (pending) { const waiter = pending; pending = null; waiter.resolve(chunk); }
    else queue.push(chunk);
  }
  return { counts, push, send: text => push(new TextEncoder().encode(text)),
    close() { if (pending) { pending.resolve({ done: true }); pending = null; } else queue.push({ done: true }); } };
}

test("会话目录按需读取且每页一次请求，失败翻页不改变当前游标", async () => {
  const page = harness({ authenticated: false });
  assert.equal(page.container.hidden, true);
  page.click("load"); await settled(); assert.equal(page.calls.length, 0);
  page.login();
  page.respond(async () => ({ sessions: [record], nextCursor: "cursor-first" }));
  page.click("load"); await settled();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].url, "/v1/agent/sessions?limit=10");
  assert.match(page.ui("page").textContent, /第 1 页/);
  page.respond(async () => { throw new Error("page unavailable"); });
  page.click("next"); await settled();
  assert.equal(page.calls[1].url, "/v1/agent/sessions?limit=10&cursor=cursor-first");
  assert.match(page.ui("page").textContent, /第 1 页/);
  page.respond(async () => ({ sessions: [], nextCursor: null }));
  page.click("next"); await settled();
  assert.equal(page.calls[2].url, page.calls[1].url);
  assert.match(page.ui("page").textContent, /第 2 页/);
  assert.equal(page.ui("next").disabled, true);
});

test("选择会话只读元数据，展示已有结果不提交生产或自动轮询", async () => {
  const page = harness(); await selected(page);
  assert.equal(page.opened.length, 0);
  assert.equal(page.calls.length, 2);
  page.click("read"); await settled();
  assert.equal(page.opened.length, 1);
  assert.equal(page.opened[0].runId, record.runId);
  assert.equal(page.calls[2].url, "/v1/agent/sessions/agent-one");
  assert.ok(page.calls.every(call => call.method === "GET"));
  assert.equal(page.fetchCalls.length, 0);
  assert.equal(page.timers.size, 0);
});

test("改名携带版本，结果未知后锁定重提直到重新读取", async () => {
  const page = harness(); await selected(page);
  page.ui("name").value = "新名称";
  page.respond(async () => { throw new Error("response lost"); });
  page.submit("rename-form"); await settled();
  assert.equal(page.calls[2].method, "PATCH");
  assert.deepEqual(JSON.parse(JSON.stringify(page.calls[2].body)), { title: "新名称", expectedVersion: 2 });
  assert.equal(page.ui("rename").disabled, true);
  assert.match(page.ui("message").textContent, /结果待核实/);
  page.submit("rename-form"); await settled(); assert.equal(page.calls.length, 3);
  page.respond(async () => ({ session: { ...record, title: "已变更", version: 3 }, run: result }));
  page.click("read"); await settled();
  assert.equal(page.ui("name").value, "已变更");
  assert.equal(page.ui("rename").disabled, false);
});

test("删除需明确确认并携带版本，不取消供应商请求", async () => {
  const page = harness(); await selected(page);
  page.click("delete"); await settled(); assert.equal(page.calls.length, 2);
  page.ui("delete-confirm").checked = true;
  page.respond(async () => ({ ...record, deleted: true, version: 3 }));
  page.click("delete"); await settled();
  assert.equal(page.calls[2].method, "DELETE");
  assert.equal(page.calls[2].url, "/v1/agent/sessions/agent-one?expectedVersion=2");
  assert.equal(page.ui("detail").hidden, true);
  assert.match(page.ui("message").textContent, /供应商请求未取消/);
  assert.equal(page.calls.length, 3);
});

test("换用户后丢弃未返回的目录响应并清空会话展示", async () => {
  const page = harness(); await selected(page);
  const response = deferred();
  page.respond(() => response.promise);
  page.click("load");
  page.logout();
  response.resolve({ sessions: [record], nextCursor: "old" }); await settled();
  assert.equal(page.container.hidden, true);
  assert.equal(page.ui("list").children.length, 0);
  assert.equal(page.ui("detail").hidden, true);
  assert.equal(page.ui("preview").textContent, "");
  assert.equal(page.ui("name").value, "");
});

test("服务返回的标题与预览仅按文本展示", async () => {
  const page = harness();
  await selected(page, { ...record, title: '<img src=x onerror="alert(1)">', preview: "<script>bad()</script>" });
  assert.match(page.ui("title").textContent, /<img/);
  assert.match(page.ui("preview").textContent, /<script/);
  assert.ok(page.tags.every(tag => !["img", "script", "iframe"].includes(tag)));
});

test("SSE 解析支持分块与多行数据，事件缓冲有界", () => {
  const page = harness();
  const events = [];
  const parse = page.parser(event => events.push(event));
  parse("event: status\r\n");
  parse("id: event-one\r\ndata: {\"runId\":\"agent-one\",\r\n");
  parse("data: \"status\":\"processing\"}\r\n\r\nevent: heartbeat\ndata: {}\n\n");
  assert.equal(events.length, 2);
  assert.equal(events[0].id, "event-one");
  assert.equal(events[0].data.status, "processing");
  assert.equal(events[1].kind, "heartbeat");
  const oversized = page.parser(() => {});
  assert.throws(() => oversized("x".repeat(16385)), /大小限制/);
  assert.throws(() => parse("event: status\ndata: invalid\n\n"), /JSON/);
});

test("订阅只发送鉴权 GET，UTF-8 分块正确且主动停止释放读取器和定时器", async () => {
  const page = harness(); await selected(page);
  const source = readable(page);
  const watching = page.click("watch"); await settled();
  assert.equal(page.fetchCalls.length, 1);
  const request = page.fetchCalls[0];
  assert.equal(request.url, "/v1/agent/runs/agent-one/events");
  assert.equal(request.options.headers.Authorization, "Bearer memory-only-unit");
  assert.equal(request.options.headers["X-Agent-Key"], "unit-service-key");
  assert.equal(request.options.body, undefined);
  assert.equal(request.options.credentials, "omit");
  const bytes = new TextEncoder().encode('event: status\ndata: {"runId":"agent-one","status":"processing","note":"中文"}\n\n');
  source.push(bytes.slice(0, bytes.length - 7)); await settled();
  source.push(bytes.slice(bytes.length - 7)); await settled();
  assert.match(page.ui("event-status").textContent, /生产中/);
  assert.equal(page.opened.length, 0);
  page.click("stop"); await watching;
  assert.equal(request.options.signal.aborted, true);
  assert.equal(source.counts.cancel, 1);
  assert.equal(source.counts.release, 1);
  assert.equal(page.timers.size, 0);
  assert.equal(page.ui("watch").disabled, false);
  assert.equal(page.calls.length, 2);
});

test("SSE身份撤销会清空页面并释放流，不自动重连", async () => {
  const page = harness(); await selected(page);
  const source = readable(page);
  const watching = page.click("watch"); await settled();
  source.send('event: end\ndata: {"reason":"unauthorized","reconnect":false}\n\n');
  await watching;
  assert.equal(page.cleared(), 1);
  assert.equal(page.ui("detail").hidden, true);
  assert.equal(page.ui("list").children.length, 0);
  assert.equal(page.fetchCalls.length, 1);
  assert.equal(page.timers.size, 0);
  assert.equal(source.counts.release, 1);
});

test("页面离开或退出登录终止订阅，延迟事件不会写回旧用户", async () => {
  for (const action of ["logout", "pagehide"]) {
    const page = harness(); await selected(page);
    const source = readable(page);
    const watching = page.click("watch"); await settled();
    page[action](); await watching;
    source.send('event: status\ndata: {"runId":"agent-one","status":"completed"}\n\n'); await settled();
    assert.equal(page.ui("detail").hidden, true);
    assert.equal(page.ui("preview").textContent, "");
    assert.equal(source.counts.cancel, 1);
    assert.equal(source.counts.release, 1);
    assert.equal(page.timers.size, 0);
  }
});

test("限时结束和服务断开均要求手动重连", async () => {
  for (const reason of ["end", "disconnect", "timeout"]) {
    const page = harness(); await selected(page);
    const source = readable(page);
    const watching = page.click("watch"); await settled();
    if (reason === "end") source.send('event: end\ndata: {"reason":"reconnect_required","reconnect":true}\n\n');
    else if (reason === "disconnect") source.close();
    else [...page.timers.values()][0].callback();
    await watching;
    assert.equal(page.fetchCalls.length, 1);
    assert.equal(page.timers.size, 0);
    assert.equal(page.ui("watch").disabled, false);
    assert.equal(source.counts.release, 1);
    assert.equal(page.calls.length, 2);
  }
});

test("异常运行编号或超大SSE事件会中止订阅", async () => {
  for (const content of ['event: status\ndata: {"runId":"foreign","status":"completed"}\n\n', "x".repeat(16385)]) {
    const page = harness(); await selected(page);
    const source = readable(page);
    const watching = page.click("watch"); await settled();
    source.send(content); await watching;
    assert.match(page.ui("event-status").textContent, /不匹配|大小限制/);
    assert.equal(source.counts.release, 1);
    assert.equal(page.calls.length, 2);
  }
});
