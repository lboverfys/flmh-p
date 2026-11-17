"use strict";

(() => {
  const MAX_EVENT_CHARS = 16384;
  const STREAM_TIMEOUT_MS = 65000;
  const statusLabels = {
    starting: "正在开始", awaiting_confirmation: "等待确认", processing: "生产中", completed: "流程完成",
    needs_input: "待补充条件", needs_configuration: "待配置", needs_verification: "结果待核实", retryable: "可重试",
  };

  function eventParser(onEvent) {
    let buffer = "";
    return chunk => {
      if (chunk.length > MAX_EVENT_CHARS) throw new Error("状态事件超过大小限制，订阅已停止。");
      buffer += chunk;
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        if (boundary.index > MAX_EVENT_CHARS) throw new Error("状态事件超过大小限制，订阅已停止。");
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let kind = "message";
        let id = "";
        const data = [];
        for (const line of block.split(/\r?\n/)) {
          const colon = line.indexOf(":");
          if (colon === 0) continue;
          const key = colon === -1 ? line : line.slice(0, colon);
          const raw = colon === -1 ? "" : line.slice(colon + 1);
          const value = raw.startsWith(" ") ? raw.slice(1) : raw;
          if (key === "event") kind = value;
          if (key === "id") id = value;
          if (key === "data") data.push(value);
        }
        if (data.length && onEvent({ kind, id, data: JSON.parse(data.join("\n")) }) === false) {
          buffer = "";
          return false;
        }
      }
      if (buffer.length > MAX_EVENT_CHARS) throw new Error("状态事件超过大小限制，订阅已停止。");
      return true;
    };
  }

  function mount(container, host) {
    container.innerHTML = `
      <div class="section-heading"><h2>我的会话</h2><button data-session="load" class="secondary" type="button">读取目录</button></div>
      <p class="help">按最近变化排序。这里只读取已有运行；生产确认仍在运行结果中进行。本地内存模式重启后会清空会话。</p>
      <p data-session="message" class="help" role="status" aria-live="polite">点击“读取目录”查看自己的会话。</p>
      <div class="session-columns"><div>
        <div data-session="list" class="session-list"></div>
        <div class="button-row"><button data-session="prev" type="button" class="secondary">上一页</button><span data-session="page" class="help"></span><button data-session="next" type="button" class="secondary">下一页</button></div>
      </div><section data-session="detail" hidden>
        <h3 data-session="title"></h3><p data-session="metadata" class="help"></p>
        <p data-session="preview" class="session-preview"></p>
        <div class="button-row"><button data-session="read" type="button" class="secondary">读取已有结果</button><button data-session="watch" type="button" class="secondary">订阅状态变化</button><button data-session="stop" type="button" class="secondary">停止订阅</button></div>
        <p data-session="event-status" class="help" role="status" aria-live="polite">尚未订阅。订阅不会推进、确认或重新提交任务。</p>
        <form data-session="rename-form"><label>会话名称<input data-session="name" maxlength="80" required></label><button data-session="rename" class="secondary" type="submit">保存名称</button></form>
        <label class="delete-confirm"><input data-session="delete-confirm" type="checkbox">确认从目录删除此会话；保留检查点，不取消已提交的供应商请求</label>
        <button data-session="delete" type="button" class="secondary">删除会话</button>
      </section></div>`;
    const el = Object.fromEntries([...container.querySelectorAll("[data-session]")].map(item => [item.dataset.session, item]));
    let state = initial();
    let stream = null;

    function initial() { return { rows: [], cursors: [null], page: 0, nextCursor: null, selected: null, uncertain: false, loaded: false }; }
    function label(status) { return statusLabels[status] || status; }
    function message(value) { el.message.textContent = value; }
    function element(tag, value, className = "") {
      const item = document.createElement(tag);
      if (value !== undefined) item.textContent = String(value);
      item.className = className;
      return item;
    }
    function updateControls(blocked = host.blocked() || !host.authenticated()) {
      container.hidden = !host.authenticated();
      for (const input of container.querySelectorAll("button,input")) input.disabled = blocked;
      el.prev.disabled = blocked || state.page === 0;
      el.next.disabled = blocked || !state.nextCursor;
      el.read.disabled = blocked || !state.selected;
      el.watch.disabled = blocked || !state.selected || Boolean(stream);
      el.stop.disabled = !stream;
      el.rename.disabled = blocked || !state.selected || state.uncertain;
      el.name.disabled = blocked || !state.selected || state.uncertain;
      el.delete.disabled = blocked || !state.selected || state.uncertain || !el["delete-confirm"].checked;
      el["delete-confirm"].disabled = blocked || !state.selected || state.uncertain;
    }
    function renderList() {
      el.list.replaceChildren();
      for (const row of state.rows) {
        const item = element("button", undefined, "session-item secondary");
        item.type = "button";
        item.setAttribute("aria-pressed", String(state.selected?.runId === row.runId));
        item.append(element("strong", row.title), element("span", `${label(row.status)} · ${row.updatedAt}`, "help"),
          element("span", row.preview, "session-preview"));
        item.addEventListener("click", () => act(version => select(row.runId, version)));
        el.list.append(item);
      }
      if (state.loaded && !state.rows.length) el.list.append(element("p", "这一页没有会话。", "help"));
      el.page.textContent = state.loaded ? `第 ${state.page + 1} 页 · ${state.rows.length} 条` : "";
    }
    function renderDetail() {
      const record = state.selected;
      el.detail.hidden = !record;
      el.title.textContent = record?.title || "";
      el.metadata.textContent = record ? `${record.runId} · ${label(record.status)} · 版本 ${record.version}` : "";
      el.preview.textContent = record?.preview || "";
      el.name.value = record?.title || "";
      el["delete-confirm"].checked = false;
    }
    function stop(note = "订阅已停止；生产任务状态不受影响。") {
      const active = stream;
      stream = null;
      if (active) {
        active.controller.abort();
        clearTimeout(active.timer);
      }
      el["event-status"].textContent = note;
      updateControls();
    }
    function reset() {
      stop("尚未订阅。订阅不会推进、确认或重新提交任务。");
      state = initial();
      renderList(); renderDetail();
      message("点击“读取目录”查看自己的会话。");
      updateControls();
    }
    async function act(operation) {
      if (host.blocked() || !host.authenticated()) return;
      const version = host.version();
      host.busy(true); updateControls(true);
      try { await operation(version); }
      catch (error) {
        if (version === host.version()) message(state.uncertain
          ? `${error.message} 修改结果待核实。请重新读取该会话，再操作；不要直接重复提交。`
          : error.message);
      } finally {
        if (version === host.version()) { host.busy(false); updateControls(); }
      }
    }
    async function page(index, cursor, version) {
      const params = new URLSearchParams({ limit: "10" });
      if (cursor) params.set("cursor", cursor);
      const data = await host.request(`/v1/agent/sessions?${params}`);
      if (version !== host.version()) return;
      if (index === 0) state.cursors = [null];
      state.cursors[index] = cursor;
      state.page = index; state.rows = data.sessions; state.nextCursor = data.nextCursor; state.loaded = true;
      renderList(); message("已读取会话目录。点击会话可查看元数据，不会触发生产。");
    }
    async function select(runId, version) {
      stop();
      const data = await host.request(`/v1/agent/sessions/${encodeURIComponent(runId)}`);
      if (version !== host.version()) return;
      state.selected = data.session; state.uncertain = false;
      renderList(); renderDetail();
      message("已读取此会话。需要展示已有结果时，点击“读取已有结果”。");
    }
    async function read(version) {
      const runId = state.selected.runId;
      const data = await host.request(`/v1/agent/sessions/${encodeURIComponent(runId)}`);
      if (version !== host.version()) return;
      state.selected = data.session; state.uncertain = false;
      renderDetail();
      if (data.run) { host.openRun(data.run); message("已展示检查点中的已有结果。没有自动确认或提交生产。"); }
      else message("这条会话尚未保存运行结果，可保留等待或从目录删除。");
    }
    async function rename(version) {
      const title = el.name.value.trim();
      if (!title) { message("请输入会话名称。"); return; }
      state.uncertain = true;
      const updated = await host.request(`/v1/agent/sessions/${encodeURIComponent(state.selected.runId)}`,
        { title, expectedVersion: state.selected.version }, "PATCH");
      if (version !== host.version()) return;
      state.selected = updated; state.uncertain = false;
      state.rows = state.rows.map(row => row.runId === updated.runId ? updated : row);
      renderList(); renderDetail(); message("会话名称已保存。");
    }
    async function remove(version) {
      if (!el["delete-confirm"].checked) return;
      const selected = state.selected;
      state.uncertain = true;
      const params = new URLSearchParams({ expectedVersion: String(selected.version) });
      await host.request(`/v1/agent/sessions/${encodeURIComponent(selected.runId)}?${params}`, undefined, "DELETE");
      if (version !== host.version()) return;
      stop();
      state.rows = state.rows.filter(row => row.runId !== selected.runId);
      state.selected = null; state.uncertain = false;
      renderList(); renderDetail(); message("会话已从目录删除。已有供应商请求未取消，检查点仍保留，旧运行编号不能重新提交。");
    }
    async function watch() {
      if (host.blocked() || !host.authenticated() || !state.selected || stream) return;
      const version = host.version();
      const runId = state.selected.runId;
      const active = { controller: new AbortController(), timer: null, reader: null };
      stream = active;
      active.timer = setTimeout(() => {
        if (stream === active) stop("订阅已到时停止。需要继续观察时，请再次点击订阅。");
      }, STREAM_TIMEOUT_MS);
      el["event-status"].textContent = "正在连接，只观察已有状态…";
      updateControls();
      try {
        const response = await fetch(`/v1/agent/runs/${encodeURIComponent(runId)}/events`, {
          headers: { ...host.streamHeaders(), Accept: "text/event-stream" }, signal: active.controller.signal,
          cache: "no-store", credentials: "omit",
        });
        if (version !== host.version() || stream !== active) return;
        if (response.status === 401) { host.clearSession("身份验证已失效，状态订阅已停止，请重新登录。"); return; }
        if (!response.ok) throw new Error(`状态订阅未开始（HTTP ${response.status}），请重新读取会话核对。`);
        if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) throw new Error("服务未返回状态事件流。");
        active.reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8", { fatal: true });
        let ended = false;
        const consume = eventParser(event => {
          if (version !== host.version() || stream !== active) return false;
          if (event.kind === "end") {
            ended = true;
            if (event.data.reason === "unauthorized") host.clearSession("身份验证已失效，状态订阅已停止，请重新登录。");
            else el["event-status"].textContent = event.data.reason === "session_not_found"
              ? "会话已删除或不可访问，订阅已结束。"
              : event.data.reason === "identity_service_unavailable"
                ? "身份服务暂时不可用，订阅已停止；恢复后可手动重连。"
                : "本次订阅已结束，需要继续观察时请再次点击订阅。";
            return false;
          }
          if (event.data.runId !== runId) throw new Error("事件运行编号不匹配，订阅已停止。");
          if (event.kind === "status") {
            if (typeof event.data.status !== "string") throw new Error("状态事件格式无效。");
            el["event-status"].textContent = `已观察到：${label(event.data.status)}。需要最新完整结果时，请点击“读取已有结果”。`;
          } else if (event.kind === "heartbeat") {
            el["event-status"].textContent = "订阅保持连接，尚无新的状态变化。可随时停止。";
          }
          return true;
        });
        while (stream === active && version === host.version()) {
          const { done, value } = await active.reader.read();
          if (version !== host.version() || stream !== active) break;
          if (done) {
            if (!ended) el["event-status"].textContent = "服务已断开订阅。需要继续观察时请手动重连。";
            break;
          }
          if (!consume(decoder.decode(value, { stream: true }))) break;
        }
      } catch (error) {
        if (version === host.version() && stream === active && error.name !== "AbortError") el["event-status"].textContent = error.message;
      } finally {
        clearTimeout(active.timer);
        active.controller.abort();
        if (active.reader) {
          try { await active.reader.cancel(); }
          catch (error) {
            if (version === host.version() && stream === active && error.name !== "AbortError") el["event-status"].textContent = "订阅连接已中止，读取器清理未确认；没有发出生产请求。";
          } finally { active.reader.releaseLock(); }
        }
        if (stream === active) stream = null;
        if (version === host.version()) updateControls();
      }
    }

    el.load.addEventListener("click", () => act(version => page(0, null, version)));
    el.prev.addEventListener("click", () => act(version => page(state.page - 1, state.cursors[state.page - 1], version)));
    el.next.addEventListener("click", () => act(version => page(state.page + 1, state.nextCursor, version)));
    el.read.addEventListener("click", () => act(read));
    el["rename-form"].addEventListener("submit", event => { event.preventDefault(); if (state.selected && !state.uncertain) act(rename); });
    el["delete-confirm"].addEventListener("change", () => updateControls());
    el.delete.addEventListener("click", () => { if (state.selected && !state.uncertain) act(remove); });
    el.watch.addEventListener("click", watch);
    el.stop.addEventListener("click", () => stop());
    window.addEventListener("pagehide", reset);
    reset();
    return { reset, updateControls, stop };
  }

  globalThis.FlmhSessions = { mount, eventParser };
})();
