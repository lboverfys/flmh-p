"use strict";

(() => {
  const terminal = new Set(["MOCKED", "SUCCEEDED", "FAILED", "CANCELLED"]);
  const groups = {
    shared: { nationality: "国籍", hairStyle: "发型", ageRange: "年龄段" },
    female: { eyes: "眼部", nose: "鼻形", mouth: "唇形", faceShape: "脸型", skinColor: "肤色", makeup: "妆容", expression: "表情", necklace: "项链", earrings: "耳饰", scene: "背景", bag: "手持物" },
    male: { beard: "胡须", muscle: "体型", clothing: "服饰" },
  };
  const labels = {QUEUED:"已排队", RUNNING:"执行中", CANCELLING:"取消中", FAILED:"失败", CANCELLED:"已取消", SUCCEEDED:"已归档", MOCKED:"模拟完成，未生成文件", SUBMISSION_UNKNOWN:"供应商结果未知，须先核实"};
  function mount(container, host) {
    const fields=Object.entries(groups).map(([group,items])=>Object.entries(items).map(([key,label])=>`<label data-group="${group}">${label}<input name="${key}" maxlength="100"></label>`).join("")).join("");
    container.innerHTML=`<summary>模特适配</summary>
      <form data-p="form"><div class="field-pair"><label>原图素材编号<input name="originalAssetId" required placeholder="我的上传素材" autocomplete="off"></label>
      <label>输出比例<select name="aspectRatio"><option>3:4</option><option>1:1</option><option>9:16</option><option>4:3</option><option>16:9</option></select></label></div>
      <label>人物参数<select name="gender"><option value="auto">自动</option><option value="female">女模特</option><option value="male">男模特</option></select></label>
      <div class="field-pair">${fields}</div><label>其他要求<textarea name="custom" maxlength="500" rows="2"></textarea></label>
      <div class="field-pair"><label>面部权重（%）<input name="faceWeight" type="number" min="0" max="100" value="80" required></label>
      <label>发型权重（%）<input name="hairWeight" type="number" min="0" max="100" value="50" required></label>
      <label>背景权重（%）<input name="backgroundWeight" type="number" min="0" max="100" value="30" required></label></div>
      <div class="field-pair"><label>预算上限<input name="budgetMax" type="number" min="0" step="0.0001" required></label>
      <label>预算币种<input name="currency" minlength="3" maxlength="3" pattern="[A-Z]{3}" placeholder="例如 USD" required></label></div>
      <button type="submit" class="secondary" data-p="quote">核算报价</button></form>
      <p data-p="summary" class="help" aria-live="polite"></p><div class="button-row"><button type="button" class="primary" data-p="start">确认报价并创建任务</button>
      <button type="button" class="secondary" data-p="advance">推进生成</button><button type="button" class="secondary" data-p="verify">核实供应商状态</button>
      <button type="button" class="secondary" data-p="refresh">刷新状态</button><button type="button" class="secondary" data-p="new">新建适配</button></div>
      <p data-p="message" class="help" role="status"></p><form data-p="lookup"><div class="field-pair"><label>恢复已有报价<input name="lookupId" maxlength="96" required></label>
      <button type="submit" class="secondary">读取报价</button></div></form>`;
    const p=Object.fromEntries([...container.querySelectorAll("[data-p]")].map(e=>[e.dataset.p,e]));
    const f=p.form.elements, lookup=p.lookup.elements.lookupId;
    let quote, run, pending, dirty, needsRead, uncertainAction;
    const message=text=>{p.message.textContent=text;};
    function updateControls() {
      container.hidden=!host.authenticated(); const blocked=host.blocked()||container.hidden;
      const active=Boolean(run&&!terminal.has(run.status));
      for(const el of container.querySelectorAll("input,select,textarea,button")) el.disabled=blocked;
      for(const el of f) if(el!==p.quote) el.disabled=blocked||active||Boolean(pending)||needsRead;
      f.originalAssetId.disabled=f.aspectRatio.disabled=blocked||Boolean(quote)||Boolean(pending)||needsRead;
      for(const label of container.querySelectorAll("[data-group]")) label.hidden=label.dataset.group!=="shared"&&label.dataset.group!==f.gender.value;
      p.quote.disabled=blocked||active||Boolean(pending)||needsRead;
      p.start.disabled=blocked||!quote||dirty||Boolean(run)||needsRead||!["PENDING_CONFIRMATION","CONFIRMED"].includes(quote.status);
      p.start.textContent=pending?"恢复同一次任务创建":"确认报价并创建任务";
      p.advance.disabled=blocked||!active||run?.status==="SUBMISSION_UNKNOWN"||uncertainAction;
      p.verify.disabled=blocked||run?.status!=="SUBMISSION_UNKNOWN"||uncertainAction;
      p.refresh.disabled=blocked||!run; p.new.disabled=blocked||active||Boolean(pending)||needsRead;
      p.summary.textContent=quote?`报价 ${quote.quoteId} · 第 ${quote.snapshot.definitionVersion} 版 · ${quote.amount} ${quote.currency} · ${quote.snapshot.executionMode}${dirty?" · 参数已改，须重新报价":""}${run?`\n任务 ${run.runId} · ${labels[run.status]||run.status}`:""}`:"";
    }
    function reset(){quote=run=pending=null;dirty=needsRead=uncertainAction=false;p.form.reset();p.lookup.reset();message("");updateControls();}
    async function act(fn){if(host.blocked()||!host.authenticated())return;const epoch=host.version(),current=()=>epoch===host.version();host.busy(true);
      try {await fn(current);}catch(e){if(current())message(e.message);}finally{if(current()){host.busy(false);updateControls();}}}
    function load(q){quote=q;run=pending=null;dirty=needsRead=uncertainAction=false;const img=q.snapshot.imageAdaptation;
      p.form.reset();f.originalAssetId.value=img.originalAssetId;f.aspectRatio.value=img.aspectRatio;
      for(const [k,v] of Object.entries(img.face)) if(f.namedItem(k))f.namedItem(k).value=v;
      for(const [k,v] of Object.entries(img.weights)) f.namedItem(k).value=v;
      f.budgetMax.value=q.budgetMax;f.currency.value=q.currency;lookup.value=q.quoteId;}
    function useAsset(asset){if(host.blocked()||!host.authenticated()||asset.type!=="image")return;container.open=true;
      if(quote||pending||needsRead){message("本次适配已锁定原图。另一张原图须新建适配。");return;}f.originalAssetId.value=asset.id;message(asset.fileName);}
    function settings(){const face={gender:f.gender.value};
      for(const key of [...Object.keys(groups.shared),...Object.keys(groups[f.gender.value]||{}),"custom"])if(f.namedItem(key).value.trim())face[key]=f.namedItem(key).value.trim();
      return {face,weights:Object.fromEntries(["faceWeight","hairWeight","backgroundWeight"].map(k=>[k,Number(f.namedItem(k).value)])),budgetMax:f.budgetMax.value,currency:f.currency.value};}
    p.form.addEventListener("input",()=>{if(quote)dirty=true;updateControls();});
    p.form.addEventListener("submit",ev=>{ev.preventDefault();if(p.quote.disabled)return;const data=settings(),before=quote;
      if(!before)Object.assign(data,{originalAssetId:f.originalAssetId.value.trim(),aspectRatio:f.aspectRatio.value});
      act(async current=>{let next;try{next=await host.request(before?`/v1/image-adaptations/quotes/${before.quoteId}/revisions`:"/v1/image-adaptations/quotes",data);}
        catch(e){if(current()&&before&&(!e.status||e.status>=500||e.status===409)){needsRead=true;lookup.value=before.quoteId;throw new Error("原报价可能已更新。请读取报价核实。");}throw e;}
        if(current()){load(next);message("请核对金额、币种和执行模式后确认。");}});});
    p.start.addEventListener("click",()=>{if(p.start.disabled)return;pending??={key:crypto.randomUUID(),confirmed:quote.status==="CONFIRMED"};const attempt=pending, q=quote;
      act(async current=>{if(!attempt.confirmed){await host.request(`/v1/workflow-quotes/${q.quoteId}/confirmation`,{confirmed:true});if(!current())return;attempt.confirmed=true;}
        const value=await host.request(`/v1/workflows/${q.snapshot.workflowId}/runs`,{quoteId:q.quoteId},"POST",{idempotencyKey:attempt.key});
        if(current()){run=value;pending=null;message("任务已登记。");}});});
    async function readRun(id,current){const result=await host.request(`/v1/workflow-runs/${id}`);if(current()){run=result;uncertainAction=false;message(labels[result.status]||result.status);}}
    for(const action of ["advance","verify"])p[action].addEventListener("click",()=>{if(p[action].disabled)return;act(async current=>{try{const result=await host.request(`/v1/workflow-runs/${run.runId}/${action}`,undefined,"POST");
      if(current()){run=result;message(labels[result.status]||result.status);}}catch(e){if(current()&&(!e.status||e.status>=500)){uncertainAction=true;throw new Error("操作回执未知，请刷新任务状态。");}throw e;}});});
    p.refresh.addEventListener("click",()=>{if(run)act(current=>readRun(run.runId,current));});
    p.lookup.addEventListener("submit",ev=>{ev.preventDefault();const id=lookup.value.trim();if(!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(id))return;
      act(async current=>{const saved=await host.request(`/v1/image-adaptations/quotes/${id}`);if(!current())return;
        const held=pending, same=quote?.quoteId===id;load(saved.quote);
        if(saved.nextQuoteId){needsRead=true;lookup.value=saved.nextQuoteId;message("这份报价已有更新版本，读取新编号即可继续。");return;}
        if(saved.runId){needsRead=true;await readRun(saved.runId,current);if(current())needsRead=false;}
        else {if(same&&held)pending=held;message("已读取报价；尚未创建任务。");}});});
    p.new.addEventListener("click",()=>{if(!p.new.disabled)reset();});
    reset();return {reset,updateControls,useAsset};
  }
  globalThis.FlmhPortraits={mount};
})();
