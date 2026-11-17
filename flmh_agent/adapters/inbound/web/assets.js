"use strict";

(() => {
  const limits = { "image/png": 25 * 1024 * 1024, "image/jpeg": 25 * 1024 * 1024, "video/mp4": 50 * 1024 * 1024, "video/webm": 50 * 1024 * 1024 };
  function mount(container, host) {
    container.innerHTML = `<summary>我的上传素材</summary>
      <p class="help">上传原始图片或视频供工作流使用。素材归当前账号所有，不计为模型生成作品。本地模式保存真实上传文件，生成节点仍明确使用 Mock。</p>
      <form data-a="upload-form"><label>素材文件<input data-a="file" type="file" accept="image/png,image/jpeg,video/mp4,video/webm"></label>
        <p class="help">PNG/JPEG 每张最多25MiB，MP4/WebM 每段最多50MiB；服务端会核实实际格式。上传不会自动生成内容或创建工作流。</p>
        <button data-a="upload" class="primary" type="submit">上传素材</button></form>
      <p data-a="message" class="help" role="status">选择文件上传，或读取已有素材。</p>
      <form data-a="list-form"><div class="field-pair"><label>素材类型<select data-a="type"><option value="">全部</option><option value="image">图片</option><option value="video">视频</option></select></label><button data-a="load" class="secondary" type="submit">读取素材</button></div></form>
      <div class="table-scroll"><table><thead><tr><th>文件 / 编号</th><th>类型 / 大小</th><th>来源 / 存储</th><th>操作</th></tr></thead><tbody data-a="rows"></tbody></table></div>
      <div class="button-row"><button data-a="prev" type="button" class="secondary">上一页</button><span data-a="page" class="help"></span><button data-a="next" type="button" class="secondary">下一页</button></div>
      <pre data-a="detail" class="generated" hidden></pre>`;
    const el = Object.fromEntries([...container.querySelectorAll("[data-a]")].map(item => [item.dataset.a, item]));
    let s;
    function node(tag, value) { const item=document.createElement(tag); if(value!==undefined)item.textContent=String(value); return item; }
    function button(label, action) { const item=node("button",label);item.type="button";item.className="secondary";item.addEventListener("click",action);return item; }
    function updateControls() {
      const blocked=host.blocked()||!host.authenticated(); container.hidden=!host.authenticated();
      for(const item of container.querySelectorAll("input,select,button"))item.disabled=blocked;
      el.file.disabled=blocked||Boolean(s.pending);
      el.upload.textContent=s.pending?"重试同一上传":"上传素材";
      el.prev.disabled=blocked||!s.list||s.list.page<=1;
      el.next.disabled=blocked||!s.list||s.list.page>=s.list.totalPages;
    }
    function reset() { s={pending:null,list:null,query:{}};el.file.value="";el.type.value="";el.rows.replaceChildren();el.page.textContent="";el.detail.textContent="";el.detail.hidden=true;el.message.textContent="选择文件上传，或读取已有素材。";updateControls(); }
    function show(page,query) {
      s.list=page;s.query=query;el.rows.replaceChildren();
      for(const asset of page.assets) {
        const row=node("tr"), name=node("td",asset.fileName);name.append(node("small",asset.id));
        const actions=node("td");actions.append(button("查看信息",()=>{el.detail.textContent=JSON.stringify(asset,null,2);el.detail.hidden=false;}),
          button("用于当前节点",()=>host.selectAsset({id:asset.id,type:asset.type})));
        if(asset.type==="image" && host.selectPortrait) actions.append(button("用于模特适配",()=>host.selectPortrait(asset)));
        row.append(name,node("td",`${asset.type} / ${asset.bytes} 字节`),node("td",`${asset.source} / ${asset.storageMode}`),actions);el.rows.append(row);
      }
      el.page.textContent=`第 ${page.page} / ${Math.max(1,page.totalPages)} 页 · 共 ${page.total} 件`;
    }
    async function act(operation) {
      if(host.blocked()||!host.authenticated())return;
      const epoch=host.version();host.busy(true);updateControls();
      try { await operation(epoch); }
      catch(error) { if(epoch===host.version())el.message.textContent=error.message+(s.pending?" 已保留原文件与幂等键，请重试同一上传，不要创建另一份。":""); }
      finally { if(epoch===host.version()){host.busy(false);updateControls();} }
    }
    async function list(page,query,epoch) {
      const result=await host.request(`/v1/assets?${new URLSearchParams({...query,page:String(page),pageSize:"20"})}`);
      if(epoch!==host.version())return;
      show(result,query);el.message.textContent="素材目录已读取。文件来源为本人上传，未触发模型生产。";
    }
    el["upload-form"].addEventListener("submit",event=>{event.preventDefault();
      if(host.blocked()||!host.authenticated())return;
      const file=s.pending?.file||el.file.files?.[0];
      if(!file||!limits[file.type]||file.size<=0||file.size>limits[file.type]||file.name.length>255){el.message.textContent="请选择符合格式和大小限制的文件。";return;}
      if(!s.pending)s.pending={file,key:`asset-${crypto.randomUUID()}`};
      const pending=s.pending;
      act(async epoch=>{
        let result;
        try { result=await host.upload(pending.file,pending.key); }
        catch(error) { if(epoch===host.version()&&error.status&&error.status<500&&error.status!==409){s.pending=null;} throw error; }
        if(epoch!==host.version())return;
        s.pending=null;el.file.value="";el.detail.textContent=JSON.stringify(result.asset,null,2);el.detail.hidden=false;
        el.message.textContent=result.reused?"原上传已存在，复用同一素材。":"素材已保存，可用于工作流节点。";
        show({assets:[result.asset],total:1,page:1,pageSize:20,totalPages:1},{});
      });
    });
    el["list-form"].addEventListener("submit",event=>{event.preventDefault();const query=el.type.value?{type:el.type.value}:{};act(epoch=>list(1,query,epoch));});
    el.prev.addEventListener("click",()=>{if(s.list?.page>1)act(epoch=>list(s.list.page-1,s.query,epoch));});
    el.next.addEventListener("click",()=>{if(s.list&&s.list.page<s.list.totalPages)act(epoch=>list(s.list.page+1,s.query,epoch));});
    reset();return {reset,updateControls};
  }
  globalThis.FlmhMediaAssets={mount};
})();
