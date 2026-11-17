"use strict";

(() => {
  const terminal = new Set(["MOCKED", "SUCCEEDED", "FAILED", "CANCELLED"]);
  const copy = value => JSON.parse(JSON.stringify(value));
  const svgNS = "http://www.w3.org/2000/svg";
  function mount(container, host) {
    container.innerHTML = `
      <div class="section-heading"><h2>可编辑工作流</h2><button data-w="load" class="secondary" type="button">读取目录</button></div>
      <p data-w="message" class="help" role="status">保存节点和连线后获取报价；确认报价才会创建运行。Mock 模式没有真实媒体文件。</p>
      <div class="workflow-columns"><section>
        <div data-w="list" class="workflow-list"></div>
        <div class="button-row"><button data-w="prev" type="button" class="secondary">上一页</button><span data-w="page" class="help"></span><button data-w="next" type="button" class="secondary">下一页</button></div>
        <button data-w="new" type="button" class="secondary">新建定义</button>
        <form data-w="definition-form">
          <label>工作流名称<input data-w="name" maxlength="100" required></label>
          <label>说明<textarea data-w="description" maxlength="500" rows="2"></textarea></label>
          <p data-w="version" class="help">尚未保存</p>
          <button data-w="save" type="submit" class="primary">保存定义</button>
        </form>
        <label class="delete-confirm"><input data-w="delete-confirm" type="checkbox">确认归档定义，保留历史运行</label>
        <button data-w="delete" type="button" class="secondary">归档此定义</button>
        <div data-w="support" class="help"></div>
      </section><section>
        <svg data-w="canvas" class="workflow-canvas" viewBox="0 0 900 480" role="img" aria-label="工作流节点及依赖连线"></svg>
        <p class="help">点击节点编辑，拖动节点调整位置；箭头从上游指向下游。最多 20 个节点。</p>
        <form data-w="node-form">
          <div class="field-pair"><label>节点标识<input data-w="node-key" maxlength="64" pattern="([A-Za-z0-9_]|-)+" required></label><label>节点能力<select data-w="node-capability"></select></label></div>
          <label>节点名称<input data-w="node-name" maxlength="100"></label>
          <label>生成要求<textarea data-w="node-prompt" maxlength="2000" rows="3" required></textarea></label>
          <label>节点参数（JSON）<textarea data-w="node-params" rows="2" spellcheck="false">{"aspectRatio":"9:16"}</textarea></label>
          <label>固定素材（JSON 数组；真实执行支持范围以服务报告为准）<textarea data-w="node-assets" rows="2" spellcheck="false">[]</textarea></label>
          <div class="button-row"><button data-w="node-save" class="secondary" type="submit">应用节点</button><button data-w="node-new" class="secondary" type="button">新节点</button><button data-w="node-remove" class="secondary" type="button">移除所选节点</button></div>
        </form>
        <form data-w="edge-form"><div class="field-pair"><label>上游<select data-w="edge-parent"></select></label><label>下游<select data-w="edge-child"></select></label></div>
          <div class="field-pair"><label>输入口（可留空）<input data-w="edge-slot" maxlength="64"></label><label>顺序<input data-w="edge-order" type="number" min="0" max="2147483647" value="0" required></label></div><button data-w="edge-add" class="secondary" type="submit">添加连线</button></form>
        <div data-w="edges" class="workflow-list"></div>
      </section></div>
      <section class="workflow-production"><h3>报价与运行</h3>
        <form data-w="quote-form"><div class="field-pair"><label>商品编号<input data-w="product" maxlength="96" value="camping-lamp-001" required></label><label>预算上限<input data-w="budget" type="number" min="0" step="0.0001" value="10" required></label></div>
          <label>修订来源运行（可留空；成功且未变化的节点可复用）<input data-w="origin" maxlength="96"></label><button data-w="quote" type="submit" class="secondary">为已保存版本报价</button></form>
        <pre data-w="quote-detail" class="generated" hidden></pre><button data-w="confirm" class="primary" type="button" disabled>确认报价并创建运行</button>
        <div class="button-row"><button data-w="history" class="secondary" type="button">读取运行历史</button><button data-w="refresh" class="secondary" type="button">刷新当前运行</button><button data-w="advance" class="primary" type="button">推进就绪节点</button><button data-w="verify" class="secondary" type="button">仅核实已有请求</button></div>
        <label class="delete-confirm"><input data-w="cancel-confirm" type="checkbox">停止新提交；已有请求仍需核实，不等于撤销供应商生成</label><button data-w="cancel" class="secondary" type="button">请求取消</button>
        <pre data-w="run" class="generated" hidden></pre><p data-w="history-context" class="help"></p>
        <div data-w="history-list" class="workflow-list"></div>
        <div class="button-row"><button data-w="history-prev" type="button" class="secondary" aria-label="运行历史上一页">上一页</button><span data-w="history-page" class="help" role="status"></span><button data-w="history-next" type="button" class="secondary" aria-label="运行历史下一页">下一页</button></div>
      </section>`;
    const el = Object.fromEntries([...container.querySelectorAll("[data-w]")].map(item => [item.dataset.w, item]));
    let s, activeAction = 0;
    function initial() { return { saved: null, graph: { nodes: [], edges: [] }, selected: null, dirty: false,
      page: 1, pages: 0, history: null, capabilities: [], quote: null, pendingStart: null, run: null, uncertainAction: false, uncertainSave: false }; }
    function message(value) { el.message.textContent = value; }
    function changed() { if (s.pendingStart) return; s.dirty = true; s.quote = null; renderQuote(); renderGraph(); updateControls(); }
    function button(label, action) { const item = document.createElement("button"); item.type = "button"; item.className = "secondary"; item.textContent = label; item.addEventListener("click", action); return item; }
    function option(value, label) { const item = document.createElement("option"); item.value = value; item.textContent = label; return item; }
    async function act(operation) {
      if (!host.authenticated() || host.blocked()) return;
      const version = host.version(), action = ++activeAction; host.busy(true); updateControls(true);
      try { await operation(version); }
      catch (error) { if (version === host.version() && action === activeAction) message(s.uncertainSave
        ? "保存结果未知，请先读取目录并选中已保存项核对，不要重复新建。"
        : s.pendingStart ? `${error.message} 已保留同一次创建的幂等键，可重试核实，勿新建另一份生产任务。`
        : s.uncertainAction ? `${error.message} 请先刷新当前运行核实，再继续操作。` : error.message); }
      finally { if (version === host.version() && action === activeAction) { host.busy(false); updateControls(); } }
    }
    function reset() {
      s = initial();
      for (const key of ["name", "description", "node-key", "node-name", "node-prompt", "edge-slot", "origin", "product", "budget"]) el[key].value = "";
      for (const key of ["delete-confirm", "cancel-confirm"]) el[key].checked = false;
      for (const key of ["list", "support"]) el[key].replaceChildren();
      resetHistory();
      el.page.textContent = "";
      el["node-capability"].replaceChildren(); el["node-params"].value = '{"aspectRatio":"9:16"}'; el["node-assets"].value = "[]";
      message("保存节点和连线后获取报价；确认报价才会创建运行。Mock 模式没有真实媒体文件。");
      renderGraph(); renderQuote(); renderRun(); updateControls();
    }
    function updateControls(blocked = host.blocked() || !host.authenticated()) {
      container.hidden = !host.authenticated();
      for (const input of container.querySelectorAll("button,input,textarea,select")) input.disabled = blocked;
      if (s.pendingStart) for (const input of container.querySelectorAll(".workflow-columns button,.workflow-columns input,.workflow-columns textarea,.workflow-columns select")) input.disabled = true;
      el["node-key"].disabled = blocked || Boolean(s.selected);
      el["node-key"].disabled ||= Boolean(s.pendingStart);
      el.load.disabled = blocked || Boolean(s.pendingStart);
      el.prev.disabled = blocked || Boolean(s.pendingStart) || s.page <= 1; el.next.disabled = blocked || Boolean(s.pendingStart) || s.page >= s.pages;
      el.save.disabled = blocked || s.uncertainSave || Boolean(s.pendingStart);
      el.delete.disabled = blocked || Boolean(s.pendingStart) || !s.saved || !el["delete-confirm"].checked;
      el["node-remove"].disabled = blocked || Boolean(s.pendingStart) || !s.selected;
      el["node-save"].disabled = blocked || Boolean(s.pendingStart) || !s.capabilities.length;
      el.quote.disabled = blocked || !s.saved || s.dirty || Boolean(s.pendingStart);
      for (const key of ["product", "budget", "origin"]) el[key].disabled = blocked || Boolean(s.pendingStart);
      el.confirm.disabled = blocked || !s.quote || Boolean(s.run && !s.pendingStart);
      el.confirm.textContent = s.pendingStart ? "重试同一次确认与创建" : "确认报价并创建运行";
      el.history.disabled = blocked || Boolean(s.pendingStart) || !s.saved;
      el["history-prev"].disabled = blocked || Boolean(s.pendingStart) || !s.history || s.history.page <= 1;
      el["history-next"].disabled = blocked || Boolean(s.pendingStart) || !s.history || s.history.page >= s.history.pages;
      for (const item of el["history-list"].querySelectorAll("button")) item.disabled = blocked || Boolean(s.pendingStart);
      const running = s.run && !terminal.has(s.run.status);
      el.refresh.disabled = blocked || !s.run;
      el.advance.disabled = blocked || !running || s.uncertainAction || s.run.status === "SUBMISSION_UNKNOWN" || s.run.cancelRequested;
      el.verify.disabled = blocked || !running || s.uncertainAction;
      el.cancel.disabled = blocked || !running || s.uncertainAction || !el["cancel-confirm"].checked;
      el.version.textContent = s.saved ? `编号 ${s.saved.id} · 版本 ${s.saved.version}${s.dirty ? " · 有未保存修改" : ""}` : "尚未保存";
    }
    async function capabilities() {
      if (s.capabilities.length) return;
      const result = await host.request("/v1/workflows/meta/capabilities"); s.capabilities = result.capabilities;
      el["node-capability"].replaceChildren(...s.capabilities.map(item => option(item.id, `${item.label}${item.realAdapter === "UNSUPPORTED" ? "（仅模拟）" : ""}`)));
    }
    async function listing(page = 1) {
      await capabilities();
      const result = await host.request(`/v1/workflows?page=${page}&pageSize=10`);
      s.page = result.page; s.pages = result.totalPages;
      el.page.textContent = `${result.page} / ${Math.max(1, result.totalPages)}，共 ${result.total} 项`;
      el.list.replaceChildren(...result.workflows.map(item => button(`${item.name} · v${item.version}`, () => act(async () => load(await host.request(`/v1/workflows/${item.id}`))))));
      if (!result.workflows.length) el.list.textContent = "暂无已保存的工作流。";
    }
    function load(detail) {
      s.saved = detail; s.graph = copy(detail.definition); s.dirty = false; s.uncertainSave = false;
      s.quote = null; s.pendingStart = null; s.run = null; s.selected = null;
      resetHistory();
      el.name.value = detail.name; el.description.value = detail.description || "";
      el["delete-confirm"].checked = false;
      el["cancel-confirm"].checked = false;
      el.support.replaceChildren();
      for (const item of detail.executability.nodes) {
        const line = document.createElement("p");
        line.textContent = `${item.nodeKey}：${item.realAdapterSupported ? "具备真实适配器，执行前仍需完整配置" : item.reasons.join("；")}`;
        el.support.append(line);
      }
      message("定义已读取。后续编辑不改变已经创建的运行快照。");
      selectNode(null); renderGraph(); renderQuote(); renderRun(); updateControls();
    }
    function resetHistory() {
      s.history = s.saved ? { workflowId: s.saved.id, page: 1, pages: 0 } : null;
      el["history-list"].replaceChildren(); el["history-page"].textContent = "";
      el["history-context"].textContent = s.saved ? `运行历史：${s.saved.name} · 编号 ${s.saved.id}` : "";
    }
    function historyAction(context, operation) {
      if (!context || context !== s.history || s.pendingStart) return;
      return act(async version => {
        const current = () => version === host.version() && context === s.history;
        try { await operation(current); }
        catch (error) { if (current()) throw error; }
      });
    }
    function historyPage(page) {
      const context = s.history;
      return historyAction(context, async current => {
        const result = await host.request(`/v1/workflows/${context.workflowId}/runs?page=${page}&pageSize=20`);
        if (!current()) return;
        context.page = result.page; context.pages = result.totalPages;
        el["history-page"].textContent = `${result.page} / ${Math.max(1, result.totalPages)}，共 ${result.total} 次`;
        el["history-list"].replaceChildren(...result.runs.map(item => button(`${item.runId} · ${item.status}`, () => historyAction(context, async currentRun => {
          const run = await host.request(`/v1/workflow-runs/${item.runId}`);
          if (!currentRun()) return;
          s.run = run; s.uncertainAction = false; el["cancel-confirm"].checked = false; renderRun();
        }))));
        if (!result.runs.length) el["history-list"].textContent = result.total === 0 ? "该工作流暂无运行记录。" : "本页暂无运行记录，请返回上一页或重新读取。";
        message(`运行历史第 ${result.page} 页，显示 ${result.runs.length} 次，共 ${result.total} 次。`);
      });
    }
    function useAsset(asset) {
      if (!host.authenticated() || host.blocked() || s.pendingStart) return false;
      let assets;
      try { assets = JSON.parse(el["node-assets"].value); }
      catch { message("当前节点固定素材不是有效的 JSON 数组，请先修正。"); return false; }
      if (!Array.isArray(assets)) { message("当前节点固定素材不是有效的 JSON 数组，请先修正。"); return false; }
      if (assets.some(item => item?.assetId === asset.id)) { message("素材已在当前节点编辑框中，请应用节点并保存定义后再报价。"); return true; }
      if (assets.length >= 18) { message("当前节点最多支持 18 项固定素材，请先移除不需要的素材。"); return false; }
      assets.push({ type: asset.type, assetId: asset.id }); el["node-assets"].value = JSON.stringify(assets);
      message("素材已加入当前节点编辑框，请应用节点并保存定义后再报价。"); return true;
    }
    function selectNode(key) {
      s.selected = key;
      const item = s.graph.nodes.find(item => item.key === key);
      el["node-key"].value = item?.key || ""; el["node-name"].value = item?.name || ""; el["node-prompt"].value = item?.prompt || "";
      if (item) el["node-capability"].value = item.capability;
      el["node-params"].value = JSON.stringify(item?.params || { aspectRatio: "9:16" });
      el["node-assets"].value = JSON.stringify(item?.assets || []); updateControls();
    }
    function svg(tag, attributes, text) {
      const result = document.createElementNS(svgNS, tag);
      for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
      if (text !== undefined) result.textContent = text;
      return result;
    }
    function renderGraph() {
      el.canvas.replaceChildren();
      const height = Math.max(480, Math.ceil(s.graph.nodes.length / 3) * 100 + 25);
      el.canvas.setAttribute("viewBox", `0 0 900 ${height}`);
      const byKey = new Map(s.graph.nodes.map((item, index) => [item.key, { item, x: Math.max(0, Math.min(660, item.position?.x ?? 20 + index % 3 * 290)), y: Math.max(0, Math.min(height - 60, item.position?.y ?? 25 + Math.floor(index / 3) * 100)) }]));
      for (const edge of s.graph.edges) {
        const from = byKey.get(edge.parent), to = byKey.get(edge.child);
        if (!from || !to) continue;
        el.canvas.append(svg("path", { d: `M${from.x + 120},${from.y + 30} L${to.x + 120},${to.y + 30}`, stroke: "#9db7d5", "stroke-width": 3, fill: "none" }));
        el.canvas.append(svg("text", { x: (from.x + to.x) / 2 + 120, y: (from.y + to.y) / 2 + 22, fill: "#46617e", "font-size": 12 }, `${edge.parent} → ${edge.child}`));
      }
      for (const { item, x, y } of byKey.values()) {
        const group = svg("g", { transform: `translate(${x},${y})`, tabindex: "0", role: "button", "aria-label": `编辑节点 ${item.key}` });
        group.append(svg("rect", { width: 240, height: 60, rx: 12, fill: item.key === s.selected ? "#dbeafe" : "#fff", stroke: "#7c9bbb", "stroke-width": 2 }));
        group.append(svg("text", { x: 12, y: 24, fill: "#172f49", "font-size": 15 }, item.name || item.key));
        group.append(svg("text", { x: 12, y: 46, fill: "#46617e", "font-size": 12 }, item.capability));
        group.addEventListener("click", () => { selectNode(item.key); renderGraph(); });
        group.addEventListener("keydown", event => { if (event.key === "Enter") { selectNode(item.key); renderGraph(); } });
        group.addEventListener("pointerdown", event => {
          if (host.blocked() || !host.authenticated() || s.pendingStart) return;
          const originX = event.clientX, originY = event.clientY, width = el.canvas.getBoundingClientRect().width;
          let moved = false; group.setPointerCapture(event.pointerId);
          const move = next => {
            moved = true;
            item.position = { x: Math.max(0, Math.min(660, x + (next.clientX - originX) * 900 / width)), y: Math.max(0, Math.min(height - 60, y + (next.clientY - originY) * 900 / width)) };
            group.setAttribute("transform", `translate(${item.position.x},${item.position.y})`);
          };
          const end = () => { group.removeEventListener("pointermove", move); group.removeEventListener("pointerup", end); group.removeEventListener("pointercancel", end); if (moved) changed(); };
          group.addEventListener("pointermove", move); group.addEventListener("pointerup", end); group.addEventListener("pointercancel", end);
        });
        el.canvas.append(group);
      }
      for (const key of ["edge-parent", "edge-child"]) { const previous = el[key].value; el[key].replaceChildren(...s.graph.nodes.map(item => option(item.key, item.name || item.key))); if (byKey.has(previous)) el[key].value = previous; }
      el.edges.replaceChildren(...s.graph.edges.map((edge, index) => button(`${edge.parent} → ${edge.child}${edge.slot ? ` [${edge.slot}]` : ""} · 顺序 ${edge.order ?? index} · 移除`, () => { s.graph.edges.splice(index, 1); changed(); })));
    }
    function renderQuote() { el["quote-detail"].hidden = !s.quote; el["quote-detail"].textContent = s.quote ? `商品 ${s.quote.snapshot.productId}\n报价 ${s.quote.amount} ${s.quote.currency} / 预算 ${s.quote.budgetMax}\n模式 ${s.quote.snapshot.executionMode} · 定义版本 ${s.quote.snapshot.definitionVersion}\n修订来源 ${s.quote.snapshot.originRunId || "无"}\n逐节点金额 ${JSON.stringify(s.quote.snapshot.nodePrices)}\n复用节点 ${s.quote.snapshot.reusedNodes.join("、") || "无"}\n报价编号 ${s.quote.quoteId}` : ""; }
    function renderRun() { el.run.hidden = !s.run; el.run.textContent = s.run ? `运行 ${s.run.runId} · ${s.run.status}\n${s.run.snapshot.executionMode === "MOCK_ONLY" ? "仅模拟状态，没有生成媒体文件。\n" : ""}${s.run.nodes.map(item => `${item.nodeKey}: ${item.status}${item.reused ? "（复用）" : ""}${item.errorCode ? ` · ${item.errorCode}` : ""}`).join("\n")}\n产物数：${s.run.artifacts.length}` : ""; }
    el.load.addEventListener("click", () => act(() => listing()));
    el.prev.addEventListener("click", () => act(() => listing(s.page - 1)));
    el.next.addEventListener("click", () => act(() => listing(s.page + 1)));
    el.new.addEventListener("click", () => act(async () => { reset(); await capabilities(); }));
    el.name.addEventListener("input", changed); el.description.addEventListener("input", changed);
    el["definition-form"].addEventListener("submit", event => { event.preventDefault(); act(async version => {
      if (s.uncertainSave) return;
      const body = { name: el.name.value.trim(), description: el.description.value.trim(), definition: s.graph };
      if (s.saved) body.expectedVersion = s.saved.version;
      try { load(await host.request(s.saved ? `/v1/workflows/${s.saved.id}` : "/v1/workflows", body, s.saved ? "PUT" : "POST")); await listing(s.page); }
      catch (error) { if (version === host.version() && (!error.status || error.status >= 500)) { s.uncertainSave = true; message("保存结果未知，请先读取目录并选中已保存项核对，不要重复新建。"); } throw error; }
    }); });
    el["node-form"].addEventListener("submit", event => { event.preventDefault(); if (host.blocked() || s.pendingStart) return;
      try {
        const key = el["node-key"].value.trim(), params = JSON.parse(el["node-params"].value), assets = JSON.parse(el["node-assets"].value);
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(key) || !params || typeof params !== "object" || Array.isArray(params) || !Array.isArray(assets)) throw new Error("请核对节点标识、参数对象和素材数组。");
        const old = s.graph.nodes.find(item => item.key === key);
        if (!s.selected && old || !old && s.graph.nodes.length >= 20) throw new Error("节点标识重复或已达到20个节点上限。");
        const value = { key, name: el["node-name"].value.trim() || null, capability: el["node-capability"].value, prompt: el["node-prompt"].value, params, assets, position: old?.position || { x: 20 + s.graph.nodes.length % 3 * 290, y: 25 + Math.floor(s.graph.nodes.length / 3) * 100 } };
        if (old) s.graph.nodes[s.graph.nodes.indexOf(old)] = value; else s.graph.nodes.push(value);
        s.selected = key; changed();
      } catch (error) { message(error.message); }
    });
    el["node-new"].addEventListener("click", () => selectNode(null));
    el["node-remove"].addEventListener("click", () => { const key = s.selected; s.graph.nodes = s.graph.nodes.filter(item => item.key !== key); s.graph.edges = s.graph.edges.filter(item => item.parent !== key && item.child !== key); selectNode(null); changed(); });
    el["edge-form"].addEventListener("submit", event => { event.preventDefault();
      const edge = { parent: el["edge-parent"].value, child: el["edge-child"].value, slot: el["edge-slot"].value.trim() || null, order: Number(el["edge-order"].value) };
      if (!edge.parent || !edge.child || edge.parent === edge.child || s.graph.edges.some(item => item.parent === edge.parent && item.child === edge.child && item.slot === edge.slot)) { message("连线两端必须存在且不同，不能重复连接相同输入口。"); return; }
      s.graph.edges.push(edge); changed();
    });
    el["delete-confirm"].addEventListener("change", () => updateControls()); el["cancel-confirm"].addEventListener("change", () => updateControls());
    for (const key of ["product", "budget", "origin"]) el[key].addEventListener("input", () => { if (!s.pendingStart) { s.quote = null; renderQuote(); updateControls(); } });
    el.delete.addEventListener("click", () => act(async () => { if (!s.saved || !el["delete-confirm"].checked) return; await host.request(`/v1/workflows/${s.saved.id}`, undefined, "DELETE"); reset(); await listing(); }));
    el["quote-form"].addEventListener("submit", event => { event.preventDefault(); act(async () => {
      if (!s.saved || s.dirty || s.pendingStart) return;
      s.quote = null; renderQuote();
      s.quote = await host.request(`/v1/workflows/${s.saved.id}/quotes`, { definitionVersion: s.saved.version, productId: el.product.value.trim(), budgetMax: el.budget.value, ...(el.origin.value.trim() ? { originRunId: el.origin.value.trim() } : {}) });
      s.run = null; s.uncertainAction = false; renderQuote(); renderRun(); message("请核对逐节点报价、执行模式和复用清单，确认后才会创建运行。");
    }); });
    el.confirm.addEventListener("click", () => act(async version => {
      if (!s.quote || s.run && !s.pendingStart) return;
      s.pendingStart ||= { quoteId: s.quote.quoteId, workflowId: s.quote.snapshot.workflowId, key: crypto.randomUUID(), confirmed: false };
      try {
        if (!s.pendingStart.confirmed) { await host.request(`/v1/workflow-quotes/${s.pendingStart.quoteId}/confirmation`, { confirmed: true }); s.pendingStart.confirmed = true; }
        s.run = await host.request(`/v1/workflows/${s.pendingStart.workflowId}/runs`, { quoteId: s.pendingStart.quoteId }, "POST", { idempotencyKey: s.pendingStart.key });
        s.pendingStart = null; el["cancel-confirm"].checked = false; renderRun(); message("运行已创建，尚未自动提交生成；可手动推进就绪节点。");
      } catch (error) {
        if (version === host.version() && [400, 403, 404, 409, 422].includes(error.status)) { s.pendingStart = null; s.quote = null; renderQuote(); }
        throw error;
      }
    }));
    el.refresh.addEventListener("click", () => act(async () => { if (!s.run) return; s.run = await host.request(`/v1/workflow-runs/${s.run.runId}`); s.uncertainAction = false; renderRun(); }));
    for (const action of ["advance", "verify", "cancel"]) el[action].addEventListener("click", () => act(async version => {
      if (!s.run || terminal.has(s.run.status) || s.uncertainAction || action === "cancel" && !el["cancel-confirm"].checked) return;
      try { s.run = await host.request(`/v1/workflow-runs/${s.run.runId}/${action}`, undefined, "POST"); renderRun(); }
      catch (error) { if (version === host.version() && (!error.status || error.status >= 500)) s.uncertainAction = true; throw error; }
    }));
    el.history.addEventListener("click", () => historyPage(s.history?.page || 1));
    el["history-prev"].addEventListener("click", () => { if (s.history?.page > 1) historyPage(s.history.page - 1); });
    el["history-next"].addEventListener("click", () => { if (s.history && s.history.page < s.history.pages) historyPage(s.history.page + 1); });
    reset(); return { reset, updateControls, useAsset };
  }
  globalThis.FlmhWorkflowEditor = { mount };
})();
