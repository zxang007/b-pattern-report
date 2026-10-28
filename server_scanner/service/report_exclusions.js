(function(root){
'use strict';
const KEY='market-manual-exclusions-v1';
const validSymbol=s=>typeof s==='string'&&s.endsWith('USDT')&&s.length<=80;
const validPath=p=>typeof p==='string'&&/^\/runs\/live-[0-9a-f]{20}\/groups\/[0-9a-f]{24}\.html$/.test(p);
function validActivity(a){return a&&typeof a.generation==='string'&&a.generation&&a.by_symbol&&typeof a.by_symbol==='object';}
class Store{
 constructor(storage,key=KEY){this.storage=storage;this.key=key;this.reload();}
 reload(){const raw=this.storage.getItem(this.key);this.entries={};let parsed;try{parsed=JSON.parse(raw||'{}');}catch{return;}
  if(!parsed||typeof parsed!=='object')return;
  for(const [symbol,value]of Object.entries(parsed)){if(!validSymbol(symbol)||!value||!Number.isSafeInteger(value.watermark)||value.watermark<0||typeof value.generation!=='string')continue;
   const c=value.context||{};this.entries[symbol]={watermark:value.watermark,generation:value.generation,at:typeof value.at==='string'?value.at:'',context:{confirmed:typeof c.confirmed==='string'?c.confirmed.slice(0,80):'',href:validPath(c.href)?c.href:'',count:Number.isSafeInteger(c.count)&&c.count>0?c.count:1}};}}
 commit(next){this.storage.setItem(this.key,JSON.stringify(next));this.entries=next;}
 exclude(symbol,activity,context,at=new Date().toISOString()){if(!validSymbol(symbol)||!validActivity(activity))throw Error('未能读取4小时命中状态，请重试');
  this.reload();const watermark=activity.by_symbol[symbol]||0;if(!Number.isSafeInteger(watermark)||watermark<0)throw Error('4小时命中状态异常');
  this.commit({...this.entries,[symbol]:{watermark,generation:activity.generation,at,context}});}
 restore(symbol){this.reload();const next={...this.entries};delete next[symbol];this.commit(next);}
 reconcile(activity){if(!validActivity(activity))throw Error('未能读取4小时命中状态，已保留排除记录');this.reload();const next={...this.entries},restored=[];let changed=false;
  for(const [symbol,value]of Object.entries(next)){const watermark=activity.by_symbol[symbol];
   if(value.generation!==activity.generation){next[symbol]={...value,generation:activity.generation,watermark:Number.isSafeInteger(watermark)?watermark:0};changed=true;continue;}
   if(Number.isSafeInteger(watermark)&&watermark>value.watermark){delete next[symbol];restored.push(symbol);changed=true;}}
  if(changed)this.commit(next);return restored;}
}
function element(doc,tag,text){const e=doc.createElement(tag);if(text!==undefined)e.textContent=text;return e;}
class View{
 constructor(report,provider,storage){this.report=report;this.provider=provider;this.store=new Store(storage);this.items=[];this.doc=report.ownerDocument;this.busy=false;
  this.click=e=>{const button=e.target.closest('button[data-exclusion-action]');if(button&&this.report.contains(button)){e.preventDefault();this.act(button);}};
  this.storageEvent=e=>{if(e.key===this.store.key){this.store.reload();this.render();}};
  this.focus=()=>this.refresh().catch(e=>this.message(e.message));}
 mount(path){if(!/^\/(?:latest\/index\.html|runs\/live-[0-9a-f]{20}\/index\.html)$/.test(path))return false;
  const table=this.report.querySelector('table[data-signal-list="1"]');if(!table)return false;this.table=table;
  const header=table.rows[0];header.append(element(this.doc,'th','操作'));
  for(const row of Array.from(table.rows).slice(1)){const name=row.querySelector('button[data-copy-name]');if(!name||!validSymbol(name.dataset.copyName))continue;const symbol=name.dataset.copyName;
   const link=row.querySelector('a[href]'),href=link?link.getAttribute('href'):'';const confirmed=row.cells[2]?.textContent.trim()||'';
   const td=element(this.doc,'td'),button=element(this.doc,'button','手动排除');button.type='button';button.className='manual-exclusion-action';button.dataset.exclusionAction='exclude';button.dataset.exclusionSymbol=symbol;td.append(button);row.append(td);this.items.push({row,symbol,confirmed,href:validPath(href)?href:''});}
  this.count=element(this.doc,'p');table.before(this.count);
  this.section=element(this.doc,'section');this.section.id='manual-exclusions';this.title=element(this.doc,'h2');this.section.append(this.title,element(this.doc,'p','按币种排除，记录保存在当前浏览器。新的4小时形态命中或旧形态重新出现时解除排除；普通扫描更新不恢复。恢复后仍须满足4小时＋15分钟联合条件。'));
  this.status=element(this.doc,'p');this.status.setAttribute('role','status');this.section.append(this.status);
  const wrap=element(this.doc,'div');wrap.style.overflowX='auto';const excludedTable=element(this.doc,'table'),head=element(this.doc,'thead'),tr=element(this.doc,'tr');for(const text of ['币名','排除时间','排除时信号数','排除时最近确认','原信号','操作'])tr.append(element(this.doc,'th',text));head.append(tr);this.body=element(this.doc,'tbody');excludedTable.append(head,this.body);wrap.append(excludedTable);this.section.append(wrap);table.after(this.section);
  this.report.addEventListener('click',this.click);this.doc.defaultView.addEventListener('storage',this.storageEvent);this.doc.defaultView.addEventListener('focus',this.focus);
  this.render();this.refresh().catch(e=>this.message(e.message));this.timer=this.doc.defaultView.setInterval(this.focus,30000);return true;}
 message(text){if(this.status)this.status.textContent=text;}
 context(symbol){const items=this.items.filter(i=>i.symbol===symbol);return {confirmed:items[0]?.confirmed||'',href:items[0]?.href||'',count:items.length||1};}
 async act(button){if(this.busy)return;this.busy=true;button.disabled=true;const symbol=button.dataset.exclusionSymbol;
  try{if(button.dataset.exclusionAction==='exclude'){const activity=await this.provider();this.store.reconcile(activity);this.store.exclude(symbol,activity,this.context(symbol));this.message(symbol+' 已移入手动排除列表');}
   else{this.store.restore(symbol);this.message(symbol+' 已恢复'+(this.items.some(i=>i.symbol===symbol)?'到候选列表':'参与筛选，当前轮暂无该币的联合候选'));}
   this.render();}catch(e){this.message('操作未保存：'+e.message);}finally{this.busy=false;button.disabled=false;}}
 async refresh(){const restored=this.store.reconcile(await this.provider());this.render();if(restored.length)this.message(restored.join('、')+' 出现新的4小时命中，已解除排除');}
 render(){if(!this.body)return;let visible=0;const symbols=new Set();for(const item of this.items){item.row.hidden=Object.hasOwn(this.store.entries,item.symbol);if(!item.row.hidden){visible++;symbols.add(item.symbol);}}
  const entries=Object.entries(this.store.entries).sort((a,b)=>b[1].at.localeCompare(a[1].at));this.title.textContent='手动排除列表（'+entries.length+'个币）';this.count.textContent='当前显示 '+visible+' 条候选（'+symbols.size+' 个币）；手动排除 '+entries.length+' 个币。';this.body.replaceChildren();
  for(const [symbol,value]of entries){const row=element(this.doc,'tr'),coin=element(this.doc,'td'),copy=element(this.doc,'button',symbol);copy.type='button';copy.dataset.copyName=symbol;coin.append(copy);row.append(coin,element(this.doc,'td',value.at?new Date(value.at).toLocaleString('zh-CN',{hour12:false}):''),element(this.doc,'td',String(value.context.count)),element(this.doc,'td',value.context.confirmed));
   const detail=element(this.doc,'td');if(validPath(value.context.href)){const a=element(this.doc,'a','查看排除时信号');a.setAttribute('href',value.context.href);detail.append(a);}row.append(detail);
   const td=element(this.doc,'td'),restore=element(this.doc,'button','恢复');restore.type='button';restore.className='manual-exclusion-action';restore.dataset.exclusionAction='restore';restore.dataset.exclusionSymbol=symbol;td.append(restore);row.append(td);this.body.append(row);}
  if(!entries.length){const row=element(this.doc,'tr'),td=element(this.doc,'td','暂无手动排除的币');td.colSpan=6;row.append(td);this.body.append(row);}}
 destroy(){if(this.timer)this.doc.defaultView.clearInterval(this.timer);this.report.removeEventListener('click',this.click);this.doc.defaultView.removeEventListener('storage',this.storageEvent);this.doc.defaultView.removeEventListener('focus',this.focus);}
}
root.ReportExclusions={Store,View};if(typeof module!=='undefined')module.exports={Store,View};
})(globalThis);
