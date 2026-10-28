'use strict';
const assert=require('node:assert/strict');
const {Store,View}=require('./report_exclusions.js');
const {JSDOM}=require('/tmp/market-exclusion-ui-test/node_modules/jsdom');
function storage(){let data={};return {getItem:k=>data[k]||null,setItem:(k,v)=>{data[k]=v;}};}
const activity=(id=2,generation='one')=>({generation,by_symbol:{MERLUSDT:id}});
const s=storage(),store=new Store(s);
store.exclude('MERLUSDT',activity(),{});
assert.equal(new Store(s).entries.MERLUSDT.watermark,2);
assert.deepEqual(store.reconcile(activity()),[]);
assert.deepEqual(store.reconcile(activity(1)),[]);
assert.deepEqual(store.reconcile(activity(3)),['MERLUSDT']);
store.exclude('MERLUSDT',activity(),{});
assert.deepEqual(store.reconcile(activity(10,'reset')),[]);
assert.ok(store.entries.MERLUSDT);
assert.deepEqual(store.reconcile(activity(11,'reset')),['MERLUSDT']);
store.exclude('MERLUSDT',activity(),{});store.restore('MERLUSDT');assert.deepEqual(store.entries,{});
const broken=storage(),bs=new Store(broken);broken.setItem=()=>{throw Error('disk');};
assert.throws(()=>bs.exclude('MERLUSDT',activity(),{}));assert.deepEqual(bs.entries,{});
async function main(){
 const saved=storage();let current=activity();
 const html='<div id="report"><table data-signal-list="1"><tr><th>币</th><th>开始</th><th>确认</th></tr>'+['MERLUSDT','MERLUSDT','ARIAUSDT'].map(s=>`<tr><td><button data-copy-name="${s}">${s}</button></td><td>x</td><td>today</td></tr>`).join('')+'</table></div>';
 const dom=new JSDOM(html,{url:'https://example.test/'}),report=dom.window.document.querySelector('#report');
 let view=new View(report,async()=>current,saved);assert.ok(view.mount('/latest/index.html'));await view.refresh();
 assert.equal(report.querySelectorAll('[data-exclusion-action="exclude"]').length,3);
 await view.act(report.querySelector('[data-exclusion-action="exclude"]'));
 assert.equal(view.items.filter(i=>i.row.hidden).length,2);assert.equal(Object.keys(view.store.entries).length,1);
 assert.equal(view.table.nextElementSibling.id,'manual-exclusions');
 view.destroy();dom.window.close();
 const second=new JSDOM(html,{url:'https://example.test/'});view=new View(second.window.document.querySelector('#report'),async()=>current,saved);
 view.mount('/latest/index.html');await view.refresh();assert.equal(view.items.filter(i=>i.row.hidden).length,2);
 current=activity(3);await view.refresh();assert.equal(view.items.filter(i=>i.row.hidden).length,0);
 await view.act(view.report.querySelector('[data-exclusion-action="exclude"]'));
 await view.act(view.report.querySelector('[data-exclusion-action="restore"]'));
 assert.equal(view.items.filter(i=>i.row.hidden).length,0);
 view.destroy();second.window.close();
 console.log('Manual exclusions: persistence, multirow hiding, reset, automatic and manual restore passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
