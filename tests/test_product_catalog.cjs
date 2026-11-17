"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "flmh_agent", "adapters", "inbound", "web", "products.js"), "utf8");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function settled() { await new Promise(resolve => setImmediate(resolve)); }

function harness({ authenticated = true } = {}) {
  const htmlWrites = [], tags = [], calls = [], selected = [];
  class Element {
    constructor(tag, attributes = "") {
      this.tagName = tag; this.children = []; this.listeners = new Map(); this.dataset = {};
      this.content = ""; this.value = ""; this.checked = false; this.disabled = false;
      this.hidden = /\bhidden\b/.test(attributes);
      const key = attributes.match(/data-c="([^"]+)"/)?.[1];
      if (key) this.dataset.c = key;
    }
    set innerHTML(value) {
      htmlWrites.push(value);
      this.children = [...value.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)].map(match => new Element(match[1], match[2]));
    }
    set textContent(value) { this.content = String(value); this.children = []; }
    get textContent() { return this.content + this.children.map(child => child.textContent).join(" "); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.content = ""; this.children = children; }
    addEventListener(kind, callback) { this.listeners.set(kind, callback); }
    dispatch(kind) { return this.listeners.get(kind)?.({ preventDefault() {} }); }
    querySelectorAll(selector) {
      const elements = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
      if (selector === "[data-c]") return elements.filter(element => element.dataset.c);
      if (selector === "input,select,button") return elements.filter(element => ["input", "select", "button"].includes(element.tagName));
      throw new Error("unexpected selector " + selector);
    }
  }
  let version = 1, busy = false, panel;
  let respond = async () => { throw new Error("missing response fixture"); };
  const context = vm.createContext({ document: {
    createElement(tag) { tags.push(tag); return new Element(tag); },
  }, URLSearchParams });
  vm.runInContext(source, context, { filename: "products.js" });
  const container = new Element("section");
  panel = context.FlmhProductCatalog.mount(container, {
    authenticated: () => authenticated, blocked: () => busy, version: () => version,
    busy(value) { busy = value; },
    async request(url) { calls.push(url); return respond(url); },
    selectProduct(id) { selected.push(id); },
  });
  const fields = Object.fromEntries(container.querySelectorAll("[data-c]").map(element => [element.dataset.c, element]));
  return {
    fields, calls, selected, tags, htmlWrites, container, panel,
    respond: handler => { respond = handler; },
    submit: () => fields.form.dispatch("submit"),
    click: key => fields[key].dispatch("click"),
    busy: () => busy,
    login() { version += 1; busy = false; authenticated = true; panel.reset(); },
    logout() { version += 1; busy = false; authenticated = false; panel.reset(); },
  };
}

const product = { id: "fixture-product", title: "本地目录夹具", region: "US", price: "12.50", currency: "USD",
  sales7d: 8, relatedAuthorCount: null, scores: { opportunityScore: null }, scoreDate: null,
  source: "LOCAL_FIXTURE", sourceRef: "fixture:catalog", capturedAt: null };
function result(page = 1, extra = {}) {
  return { products: [product], total: 3, page, pageSize: 1, totalPages: 3,
    limitations: ["明确标记的测试数据"], ...extra };
}
function params(url) { return Object.fromEntries(new URL(url, "http://local.invalid").searchParams); }

test("未登录不查询，登录提交只发一次并规范化地区币种", async () => {
  const page = harness({ authenticated: false });
  page.submit(); await settled(); assert.equal(page.calls.length, 0); assert.equal(page.container.hidden, true);
  page.login(); page.respond(async () => result());
  page.fields.region.value = " us "; page.fields.currency.value = " usd "; page.fields.keyword.value = " 灯 & 杯 ";
  page.submit(); await settled();
  assert.equal(page.calls.length, 1); assert.equal(page.container.hidden, false);
  assert.deepEqual(params(page.calls[0]), { region: "US", currency: "USD", keyword: "灯 & 杯", sort: "opportunityScore",
    direction: "desc", pageSize: "20", page: "1" });
  assert.equal(page.busy(), false); assert.equal(page.fields.prev.disabled, true);
});

test("金额筛选和价格排序要求币种，all与销量条件可独立提交", async () => {
  const page = harness(); page.respond(async () => result());
  for (const field of ["priceAmount", "saleAmount", "day7SaleAmount"]) {
    page.fields[field].value = "0:100"; page.submit(); await settled();
    assert.equal(page.calls.length, 0); assert.match(page.fields.message.textContent, /先填写币种/);
    page.fields[field].value = "";
  }
  page.fields.sort.value = "price"; page.submit(); await settled(); assert.equal(page.calls.length, 0);
  page.fields.sort.value = "sales7d"; page.fields.priceAmount.value = "all"; page.fields.day7SoldCount.value = "100:";
  page.submit(); await settled(); assert.equal(page.calls.length, 1);
  assert.equal(params(page.calls[0]).day7SoldCount, "100:");
});

test("翻页沿用已提交条件快照，编辑中的筛选不会混入旧结果", async () => {
  const page = harness(); page.respond(async url => result(Number(params(url).page)));
  page.fields.keyword.value = "已提交"; page.fields.pageSize.value = "1"; page.fields.freeShipping.checked = true;
  page.submit(); await settled();
  page.fields.keyword.value = "未提交"; page.fields.region.value = "ID"; page.fields.freeShipping.checked = false;
  page.fields.pageSize.value = "100";
  page.click("next"); await settled();
  assert.deepEqual(params(page.calls[1]), { keyword: "已提交", sort: "opportunityScore", direction: "desc",
    pageSize: "1", freeShipping: "true", page: "2" });
  page.click("prev"); await settled(); assert.deepEqual(params(page.calls[2]), params(page.calls[0]));
  page.submit(); await settled();
  assert.equal(params(page.calls[3]).keyword, "未提交"); assert.equal(params(page.calls[3]).page, "1");
  assert.equal(params(page.calls[3]).region, "ID"); assert.equal(params(page.calls[3]).freeShipping, undefined);
});

test("失败翻页保持当前页面和条件，重试仍请求同一页", async () => {
  const page = harness(); page.respond(async () => result()); page.submit(); await settled();
  page.respond(async () => { throw new Error("读取失败"); });
  page.click("next"); await settled();
  assert.match(page.fields.page.textContent, /第 1 \/ 3 页/); assert.equal(page.fields.rows.children.length, 1);
  assert.match(page.fields.message.textContent, /读取失败/); assert.equal(page.busy(), false);
  page.respond(async () => result(2)); page.click("next"); await settled();
  assert.equal(page.calls[1], page.calls[2]); assert.match(page.fields.page.textContent, /第 2 \/ 3 页/);
});

test("查询期间拒绝重复提交，空结果和页数上限正确禁用导航", async () => {
  const page = harness(), pending = deferred(); page.respond(() => pending.promise);
  page.submit(); page.submit(); page.click("next");
  assert.equal(page.calls.length, 1); assert.equal(page.fields.search.disabled, true);
  pending.resolve(result(1, { products: [], total: 0, totalPages: 0 })); await settled();
  assert.equal(page.fields.rows.children.length, 0); assert.match(page.fields.message.textContent, /没有符合条件/);
  assert.equal(page.fields.prev.disabled, true); assert.equal(page.fields.next.disabled, true);
  page.respond(async () => result(10000, { totalPages: 10001 })); page.submit(); await settled();
  assert.equal(page.fields.next.disabled, true);
});

test("跨会话迟到成功不会覆盖新用户页面或解除新查询的忙碌状态", async () => {
  const page = harness(), old = deferred(), current = deferred();
  page.respond(() => old.promise); page.fields.keyword.value = "旧用户"; page.submit();
  page.logout(); page.login(); page.respond(() => current.promise); page.fields.keyword.value = "新用户"; page.submit();
  old.resolve(result()); await settled();
  assert.equal(page.fields.rows.children.length, 0); assert.equal(page.busy(), true);
  assert.match(page.fields.message.textContent, /正在读取/);
  current.resolve(result(1, { products: [{ ...product, id: "new-product", title: "新用户结果" }] })); await settled();
  assert.match(page.fields.rows.textContent, /新用户结果/); assert.doesNotMatch(page.fields.rows.textContent, /本地目录夹具/);
  assert.equal(page.busy(), false);
});

test("跨会话迟到失败不污染新会话提示，退出清除详情和查询条件", async () => {
  const page = harness(), pending = deferred(); page.respond(async () => result());
  page.fields.freeShipping.checked = true; page.submit(); await settled();
  page.fields.rows.children[0].children[5].children[0].dispatch("click");
  assert.equal(page.fields.detail.hidden, false);
  page.respond(() => pending.promise); page.submit(); page.logout();
  pending.reject(new Error("旧用户失败详情")); await settled();
  assert.equal(page.container.hidden, true); assert.equal(page.fields.rows.children.length, 0);
  assert.equal(page.fields.detail.hidden, true); assert.equal(page.fields.detail.textContent, "");
  assert.equal(page.fields.freeShipping.checked, false); assert.equal(page.fields.keyword.value, "");
  assert.doesNotMatch(page.fields.message.textContent, /旧用户失败详情/);
});

test("商品文本和来源按文本显示，详情只读且选择商品不会自动生产", async () => {
  const page = harness();
  const unsafe = '<img src=x onerror="alert(1)">';
  page.respond(async () => result(1, { products: [{ ...product, title: unsafe, sourceRef: "<script>bad()</script>" }] }));
  page.submit(); await settled();
  assert.match(page.fields.rows.textContent, /<img/); assert.match(page.fields.rows.textContent, /<script/);
  assert.match(page.fields.rows.textContent, /未提供/);
  const actions = page.fields.rows.children[0].children[5].children;
  actions[0].dispatch("click"); assert.match(page.fields.detail.textContent, /onerror/);
  actions[1].dispatch("click"); assert.deepEqual(page.selected, [product.id]);
  assert.equal(page.calls.length, 1); assert.equal(page.htmlWrites.length, 1);
  assert.ok(page.tags.every(tag => !["script", "img", "iframe"].includes(tag)));
});
