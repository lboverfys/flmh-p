"use strict";

(() => {
  function mount(container, host) {
    container.innerHTML = `<summary>商品目录与筛选</summary>
      <p class="help">查询已保存的商品观测。金额条件必须选择币种；未采集字段保持“未提供”，不会当作零。</p>
      <form data-c="form"><div class="catalog-filters">
        <label>关键词 / 商品编号<input data-c="keyword" maxlength="200"></label>
        <label>地区代码<input data-c="region" maxlength="2" placeholder="例如 US"></label>
        <label>币种<input data-c="currency" maxlength="3" placeholder="例如 USD"></label>
        <label>规范类目（含下级）<input data-c="category" maxlength="128" placeholder="例如 camping"></label>
        <label>来源<input data-c="source" maxlength="64" placeholder="例如 LOCAL_FIXTURE"></label>
        <label>排序<select data-c="sort"><option value="opportunityScore">机会评分</option><option value="growthScore">增长评分</option><option value="gmvScore">成交额评分</option><option value="creatorScore">达人评分</option><option value="contentScore">内容评分</option><option value="competitionScore">竞争评分</option><option value="priceScore">价格评分</option><option value="riskScore">风险评分</option><option value="trendScore">趋势评分</option><option value="price">价格</option><option value="sales7d">7日销量</option><option value="createdAt">首次观测时间</option></select></label>
        <label>方向<select data-c="direction"><option value="desc">从高到低 / 从新到旧</option><option value="asc">从低到高 / 从旧到新</option></select></label>
        <label>每页数量<select data-c="pageSize"><option>20</option><option>50</option><option>100</option></select></label>
      </div><details><summary>更多筛选条件</summary><p class="help">区间格式为“最小值:最大值”，可留空一端，例如 10:30 或 100:。佣金和达人出单率使用小数，0.1 表示 10%。</p>
        <div class="catalog-filters">
          <label>价格区间<input data-c="priceAmount" maxlength="51"></label><label>累计销量区间<input data-c="soldCount" maxlength="51"></label>
          <label>累计成交额区间<input data-c="saleAmount" maxlength="51"></label><label>7日销量区间<input data-c="day7SoldCount" maxlength="51"></label>
          <label>7日成交额区间<input data-c="day7SaleAmount" maxlength="51"></label><label>关联达人数区间<input data-c="relateAuthorCount" maxlength="51"></label>
          <label>佣金区间<input data-c="commission" maxlength="51"></label><label>达人出单率区间<input data-c="authorOrderRate" maxlength="51"></label>
          <label>商品状态<select data-c="productStatus"><option value="">全部</option><option value="on_sale">在售</option><option value="off_shelf">下架</option></select></label>
          <label>店铺类型<select data-c="shopType"><option value="">全部</option><option value="sshop">跨境店铺</option><option value="local">本地店铺</option></select></label>
        </div><div class="button-row"><label><input data-c="freeShipping" type="checkbox">仅包邮</label><label><input data-c="newProduct" type="checkbox">首次观测在30天内</label><label><input data-c="hotProduct" type="checkbox">机会评分至少60</label></div>
      </details><button data-c="search" class="primary" type="submit">查询商品</button></form>
      <p data-c="message" class="help" role="status">设置条件后查询。</p>
      <div class="table-scroll"><table><thead><tr><th>商品 / 编号</th><th>地区 / 价格</th><th>7日销量 / 达人</th><th>机会评分 / 日期</th><th>来源 / 观测时间</th><th>操作</th></tr></thead><tbody data-c="rows"></tbody></table></div>
      <div class="button-row"><button data-c="prev" class="secondary" type="button">上一页</button><span data-c="page" class="help"></span><button data-c="next" class="secondary" type="button">下一页</button></div>
      <pre data-c="detail" class="generated" hidden></pre>`;
    const el=Object.fromEntries([...container.querySelectorAll("[data-c]")].map(item=>[item.dataset.c,item]));
    const filters=["keyword","region","currency","category","source","sort","direction","pageSize","priceAmount","soldCount","saleAmount","day7SoldCount","day7SaleAmount","relateAuthorCount","commission","authorOrderRate","productStatus","shopType"];
    const flags=["freeShipping","newProduct","hotProduct"];
    let state;
    const value=v=>v===null||v===undefined?"未提供":String(v);
    function node(tag,text) { const n=document.createElement(tag);if(text!==undefined)n.textContent=value(text);return n; }
    function button(text,action) { const n=node("button",text);n.type="button";n.className="secondary";n.addEventListener("click",action);return n; }
    function updateControls() {
      const blocked=host.blocked()||!host.authenticated();container.hidden=!host.authenticated();
      for(const n of container.querySelectorAll("input,select,button"))n.disabled=blocked;
      el.prev.disabled=blocked||!state.result||state.result.page<=1;
      el.next.disabled=blocked||!state.result||state.result.page>=state.result.totalPages||state.result.page>=10000;
    }
    function reset() {
      state={result:null,query:null};
      for(const key of filters) el[key].value=({sort:"opportunityScore",direction:"desc",pageSize:"20"})[key]||"";
      for(const key of flags)el[key].checked=false;
      el.rows.replaceChildren();el.page.textContent="";el.detail.textContent="";el.detail.hidden=true;el.message.textContent="设置条件后查询。";updateControls();
    }
    async function search(page,query) {
      if(host.blocked()||!host.authenticated())return;
      const epoch=host.version();host.busy(true);updateControls();el.message.textContent="正在读取商品…";
      try {
        const params=new URLSearchParams({...query,page:String(page)});
        const result=await host.request(`/v1/products/catalog?${params}`);
        if(epoch!==host.version())return;
        state={result,query};el.rows.replaceChildren();el.detail.textContent="";el.detail.hidden=true;
        for(const product of result.products) {
          const row=node("tr");const title=node("td",product.title);title.append(node("small",product.id));
          const price=node("td",`${value(product.region)} / ${value(product.price)} ${value(product.currency)}`);
          const score=node("td",product.scores.opportunityScore);score.append(node("small",product.scoreDate));
          const source=node("td",product.source);source.append(node("small",product.sourceRef),node("small",product.capturedAt));
          const actions=node("td");actions.append(button("观测详情",()=>{el.detail.textContent=JSON.stringify(product,null,2);el.detail.hidden=false;}),
            button("交给 Agent",()=>host.selectProduct(product.id)));
          row.append(title,price,node("td",`${value(product.sales7d)} / ${value(product.relatedAuthorCount)}`),score,source,actions);el.rows.append(row);
        }
        el.page.textContent=`第 ${result.page} / ${Math.max(1,result.totalPages)} 页 · 共 ${result.total} 件`;
        el.message.textContent=(result.products.length?"查询完成。":"没有符合条件的已保存商品。")+" "+result.limitations.join(" ");
      } catch(error) {if(epoch===host.version())el.message.textContent=error.message;}
      finally {if(epoch===host.version()){host.busy(false);updateControls();}}
    }
    el.form.addEventListener("submit",event=>{event.preventDefault();const query={};
      for(const key of filters){const input=el[key].value.trim();if(input)query[key]=["region","currency"].includes(key)?input.toUpperCase():input;}
      for(const key of flags)if(el[key].checked)query[key]="true";
      if(!query.currency&&(query.sort==="price"||["priceAmount","saleAmount","day7SaleAmount"].some(k=>query[k]&&query[k]!=="all"))){el.message.textContent="金额筛选或价格排序前，请先填写币种。";return;}
      search(1,query);
    });
    el.prev.addEventListener("click",()=>{if(state.result?.page>1)search(state.result.page-1,state.query);});
    el.next.addEventListener("click",()=>{if(state.result&&state.result.page<state.result.totalPages)search(state.result.page+1,state.query);});
    reset();return {reset,updateControls};
  }
  globalThis.FlmhProductCatalog={mount};
})();
