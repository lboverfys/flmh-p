"use strict";

const assert = require("node:assert/strict");
const { File } = require("node:buffer");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "flmh_agent", "adapters", "inbound", "web", "assets.js"), "utf8");

function harness() {
  const htmlWrites = [], createdTags = [], calls = [], uploads = [], selections = [];
  class Element {
    constructor(tag, attributes = "") {
      this.tagName = tag; this.children = []; this.listeners = new Map(); this.dataset = {};
      this.content = ""; this.files = []; this._value = ""; this.type = ""; this.hidden = /\bhidden\b/.test(attributes); this.disabled = false;
      for (const [, name, value] of attributes.matchAll(/([\w-]+)="([^"]*)"/g)) {
        if (name === "data-a") this.dataset.a = value;
        if (name === "type") this.type = value;
        if (name === "value") this.value = value;
      }
    }
    set value(value) { this._value = value; if (this.type === "file" && value === "") this.files = []; }
    get value() { return this._value; }
    set textContent(value) { this.content = String(value); this.children = []; }
    get textContent() { return this.content + this.children.map(child => child.textContent).join(" "); }
    set innerHTML(html) {
      htmlWrites.push(html); this.children = []; this.content = "";
      const stack = [this];
      for (const token of html.matchAll(/<(\/?)([a-z][a-z0-9-]*)\b([^>]*)>|([^<]+)/gi)) {
        if (token[4] !== undefined) { stack.at(-1).content += token[4]; continue; }
        if (token[1]) { stack.pop(); continue; }
        const child = new Element(token[2], token[3]); stack.at(-1).append(child);
        if (!["input", "br", "hr", "img", "meta", "link"].includes(token[2])) stack.push(child);
      }
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.content = ""; }
    addEventListener(type, action) { this.listeners.set(type, action); }
    dispatch(type) { this.listeners.get(type)?.({ preventDefault() {} }); }
    querySelectorAll(selector) {
      const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
      return selector === "[data-a]" ? all.filter(item => item.dataset.a) : all.filter(item => selector.split(",").includes(item.tagName));
    }
  }
  let epoch = 1, busy = false, authenticated = true, uuid = 0;
  let requester = async () => pageResult([asset()]);
  let uploader = async file => ({ asset: asset({ fileName: file.name, bytes: file.size }), reused: false });
  const context = vm.createContext({ document: { createElement(tag) { createdTags.push(tag); return new Element(tag); } },
    URLSearchParams, crypto: { randomUUID: () => `uuid-${++uuid}` } });
  vm.runInContext(source, context, { filename: "assets.js" });
  const container = new Element("details");
  const editor = context.FlmhMediaAssets.mount(container, {
    authenticated: () => authenticated, blocked: () => busy, version: () => epoch, busy: value => { busy = value; },
    async request(url, body, method = body === undefined ? "GET" : "POST") { calls.push({ url, body, method, epoch }); return requester(url, body, method); },
    async upload(file, key) { uploads.push({ file, key, epoch }); return uploader(file, key); },
    selectAsset(value) { selections.push(JSON.parse(JSON.stringify(value))); },
  });
  const fields = Object.fromEntries(container.querySelectorAll("[data-a]").map(element => [element.dataset.a, element]));
  async function dispatch(field, type) { fields[field].dispatch(type); await settled(); }
  return { fields, calls, uploads, selections, htmlWrites, createdTags, container,
    request(handler) { requester = handler; }, upload(handler) { uploader = handler; },
    file(file) { fields.file.value = file.name; fields.file.files = [file]; },
    click: field => dispatch(field, "click"), submit: field => dispatch(field, "submit"),
    async row(index, action) { fields.rows.children[index].children[3].children[action].dispatch("click"); await settled(); },
    logout() { epoch++; authenticated = false; busy = false; editor.reset(); },
    login() { epoch++; authenticated = true; busy = false; editor.updateControls(); },
  };
}

function asset(overrides = {}) { return { id: "asset-1", fileName: "sample.png", type: "image", bytes: 8, source: "USER_UPLOAD", storageMode: "LOCAL_DISK", ...overrides }; }
function pageResult(assets, page = 1, total = assets.length) { return { assets, page, total, pageSize: 20, totalPages: Math.ceil(total / 20) }; }
function file(name = "sample.png", type = "image/png", contents = [137, 80, 78, 71, 13, 10, 26, 10]) { return new File([Uint8Array.from(contents)], name, { type }); }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function settled() { await new Promise(resolve => setImmediate(resolve)); }

test("真实File按原对象直接上传，成功清空待重试状态且不自动创建工作流", async () => {
  const page = harness(), first = file();
  page.file(first); await page.submit("upload-form");
  assert.equal(page.uploads.length, 1); assert.equal(page.uploads[0].file, first);
  assert.deepEqual(new Uint8Array(await page.uploads[0].file.arrayBuffer()), Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]));
  assert.equal(page.fields.file.value, ""); assert.equal(page.fields.file.files.length, 0); assert.equal(page.fields.file.disabled, false);
  assert.equal(page.fields.upload.textContent, "上传素材"); assert.equal(page.fields.detail.hidden, false);
  assert.match(page.fields.detail.textContent, /USER_UPLOAD/); assert.match(page.fields.message.textContent, /素材已保存/);
  assert.equal(page.calls.length, 0); assert.equal(page.selections.length, 0);
  const second = file("second.png"); page.file(second); await page.submit("upload-form");
  assert.equal(page.uploads[1].file, second); assert.notEqual(page.uploads[0].key, page.uploads[1].key);
});

test("上传响应丢失后重试沿用原文件和幂等键，成功复用后释放文件选择", async () => {
  const page = harness(), first = file(), replacement = file("replacement.png");
  let attempts = 0;
  page.upload(async () => { if (++attempts === 1) throw new Error("response lost"); return { asset: asset(), reused: true }; });
  page.file(first); await page.submit("upload-form");
  assert.equal(page.fields.file.disabled, true); assert.equal(page.fields.upload.textContent, "重试同一上传");
  assert.match(page.fields.message.textContent, /保留原文件与幂等键/);
  page.file(replacement); await page.submit("upload-form");
  assert.equal(page.uploads.length, 2); assert.equal(page.uploads[1].file, first); assert.equal(page.uploads[1].key, page.uploads[0].key);
  assert.equal(page.fields.file.disabled, false); assert.equal(page.fields.upload.textContent, "上传素材");
  assert.match(page.fields.message.textContent, /复用同一素材/); assert.equal(page.fields.file.files.length, 0);
  await page.submit("upload-form"); assert.equal(page.uploads.length, 2);
});

test("格式、空文件、文件名与大小前端校验拒绝无效输入", async () => {
  const page = harness();
  await page.submit("upload-form"); assert.equal(page.uploads.length, 0);
  const oversizedImage = file(), oversizedVideo = file("video.mp4", "video/mp4");
  Object.defineProperty(oversizedImage, "size", { value: 25 * 1024 * 1024 + 1 });
  Object.defineProperty(oversizedVideo, "size", { value: 50 * 1024 * 1024 + 1 });
  for (const invalid of [file("bad.html", "text/html"), file("empty.png", "image/png", []), file("x".repeat(256)), oversizedImage, oversizedVideo]) {
    page.file(invalid); await page.submit("upload-form");
    assert.equal(page.uploads.length, 0); assert.match(page.fields.message.textContent, /格式和大小限制/);
    assert.equal(page.fields.file.disabled, false); assert.equal(page.fields.upload.textContent, "上传素材");
  }
});

test("前端接受四种格式的上限边界（大小使用测试元数据）", async () => {
  const page = harness();
  for (const [type, limit] of [["image/png", 25], ["image/jpeg", 25], ["video/mp4", 50], ["video/webm", 50]]) {
    const selected = file("boundary", type); Object.defineProperty(selected, "size", { value: limit * 1024 * 1024 });
    page.file(selected); await page.submit("upload-form"); assert.equal(page.uploads.at(-1).file, selected);
  }
  assert.equal(page.uploads.length, 4);
});

test("明确的上传参数拒绝允许更换文件，冲突和服务失败保留原上传", async () => {
  for (const status of [400, 413, 415, 422, 409, 503]) {
    const page = harness(), first = file(), second = file("second.png");
    let attempts = 0;
    page.upload(async () => { if (++attempts === 1) throw Object.assign(new Error(`HTTP ${status}`), { status }); return { asset: asset(), reused: false }; });
    page.file(first); await page.submit("upload-form");
    const keep = status === 409 || status >= 500;
    assert.equal(page.fields.file.disabled, keep, `HTTP ${status}`);
    page.file(second); await page.submit("upload-form");
    assert.equal(page.uploads[1].file, keep ? first : second, `HTTP ${status}`);
    assert.equal(page.uploads[1].key === page.uploads[0].key, keep, `HTTP ${status}`);
  }
});

for (const failed of [false, true]) {
  test(`退出后迟到上传${failed ? "失败" : "成功"}不能污染新用户或释放新上传锁`, async () => {
    const page = harness(), old = deferred(), current = deferred();
    page.upload(() => page.uploads.length === 1 ? old.promise : current.promise);
    page.file(file("old.png")); await page.submit("upload-form");
    page.logout();
    assert.equal(page.container.hidden, true); assert.equal(page.fields.detail.hidden, true); assert.equal(page.fields.rows.textContent, "");
    assert.equal(page.fields.file.files.length, 0); assert.equal(page.fields.page.textContent, "");
    page.login(); page.file(file("current.png")); await page.submit("upload-form");
    const message = page.fields.message.textContent;
    if (failed) old.reject(Object.assign(new Error("old rejected"), { status: 422 }));
    else old.resolve({ asset: asset({ id: "old-user-asset" }), reused: false });
    await settled();
    assert.equal(page.fields.message.textContent, message); assert.equal(page.fields.rows.textContent, ""); assert.equal(page.fields.upload.disabled, true);
    await page.submit("upload-form"); assert.equal(page.uploads.length, 2);
    current.resolve({ asset: asset({ id: "current-user-asset" }), reused: false }); await settled();
    assert.match(page.fields.rows.textContent, /current-user-asset/); assert.doesNotMatch(page.fields.rows.textContent, /old-user-asset/);
    assert.equal(page.fields.file.disabled, false); assert.equal(page.fields.upload.textContent, "上传素材");
  });
}

test("素材分页固定使用已提交筛选，重新读取才采用新筛选并回到第一页", async () => {
  const page = harness();
  page.request(async url => {
    const query = new URL(url, "http://localhost").searchParams;
    return pageResult([asset({ id: `${query.get("type")}-${query.get("page")}` })], Number(query.get("page")), 21);
  });
  page.fields.type.value = "image"; await page.submit("list-form");
  assert.equal(page.fields.page.textContent, "第 1 / 2 页 · 共 21 件");
  assert.equal(page.fields.prev.disabled, true); assert.equal(page.fields.next.disabled, false);
  const first = page.calls.length; await page.click("prev"); assert.equal(page.calls.length, first);
  page.fields.type.value = "video"; await page.click("next");
  assert.match(page.fields.rows.textContent, /image-2/); assert.equal(page.fields.next.disabled, true);
  const last = page.calls.length; await page.click("next"); assert.equal(page.calls.length, last);
  await page.click("prev"); await page.submit("list-form");
  assert.match(page.fields.rows.textContent, /video-1/);
  assert.deepEqual(page.calls.map(call => [call.url, call.method, call.body]), [
    ["/v1/assets?type=image&page=1&pageSize=20", "GET", undefined], ["/v1/assets?type=image&page=2&pageSize=20", "GET", undefined],
    ["/v1/assets?type=image&page=1&pageSize=20", "GET", undefined], ["/v1/assets?type=video&page=1&pageSize=20", "GET", undefined],
  ]);
});

test("目录请求失败保留原页与筛选快照，不发送空列表覆盖旧结果", async () => {
  const page = harness();
  let requests = 0;
  page.request(async () => { if (++requests > 1) throw new Error("asset directory unavailable"); return pageResult([asset()], 1, 21); });
  page.fields.type.value = "image"; await page.submit("list-form");
  const rows = page.fields.rows.textContent, label = page.fields.page.textContent;
  page.fields.type.value = "video"; await page.click("next");
  assert.equal(page.fields.rows.textContent, rows); assert.equal(page.fields.page.textContent, label);
  assert.match(page.fields.message.textContent, /directory unavailable/); assert.equal(page.fields.next.disabled, false);
  await page.click("next"); assert.equal(page.calls.at(-1).url, "/v1/assets?type=image&page=2&pageSize=20");
});

for (const failed of [false, true]) {
  test(`退出后迟到目录${failed ? "失败" : "成功"}不能覆盖新用户目录`, async () => {
    const page = harness(), old = deferred(), current = deferred();
    page.request(() => page.calls.length === 1 ? old.promise : current.promise);
    await page.submit("list-form"); page.logout(); page.login(); await page.submit("list-form");
    const message = page.fields.message.textContent;
    if (failed) old.reject(new Error("old directory failed")); else old.resolve(pageResult([asset({ id: "old-user-asset" })], 2, 21));
    await settled();
    assert.equal(page.fields.message.textContent, message); assert.equal(page.fields.rows.textContent, ""); assert.equal(page.fields.load.disabled, true);
    current.resolve(pageResult([asset({ id: "current-user-asset" })])); await settled();
    assert.match(page.fields.rows.textContent, /current-user-asset/); assert.doesNotMatch(page.fields.rows.textContent, /old-user-asset/);
    assert.equal(page.fields.page.textContent, "第 1 / 1 页 · 共 1 件"); assert.equal(page.fields.load.disabled, false);
  });
}

test("素材行只通过host选择素材，查看信息不发送请求且不可信文字只作为文本", async () => {
  const page = harness(), untrusted = '<img src=x onerror="alert(1)">';
  page.request(async () => pageResult([asset({ id: "picked-asset", fileName: untrusted, source: untrusted })]));
  await page.submit("list-form"); const requests = page.calls.length;
  await page.row(0, 0); assert.equal(page.fields.detail.hidden, false); assert.match(page.fields.detail.textContent, /<img src=x/);
  await page.row(0, 1);
  assert.deepEqual(page.selections, [{ id: "picked-asset", type: "image" }]);
  assert.equal(page.calls.length, requests); assert.equal(page.uploads.length, 0);
  assert.match(page.fields.rows.textContent, /<img src=x/); assert.equal(page.htmlWrites.length, 1); assert.equal(page.htmlWrites[0].includes(untrusted), false);
  assert.equal(page.createdTags.some(tag => ["img", "script", "iframe", "video"].includes(tag)), false);
});

test("未登录时目录和上传均不发送请求", async () => {
  const page = harness(); page.logout(); page.file(file());
  await page.submit("upload-form"); await page.submit("list-form"); await page.click("prev"); await page.click("next");
  assert.equal(page.uploads.length, 0); assert.equal(page.calls.length, 0); assert.equal(page.container.hidden, true);
});
