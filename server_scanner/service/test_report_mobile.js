'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('/tmp/market-exclusion-ui-test/node_modules/jsdom');
const {prepare,observe}=require('./report_mobile.js');
const {View}=require('./report_exclusions.js');
async function main(){
 const dom=new JSDOM('<main id="report"><table data-signal-list="1"><tr><th>币名</th><th>放量启动</th><th>15分钟确认</th></tr><tr><td><button data-copy-name="MERLUSDT">MERLUSDT</button></td><td>2026-10-28 08:00</td><td>2026-10-28 08:15</td></tr></table><table id="detail"><tr><th>依据</th></tr><tr><td>完整记录</td></tr></table></main>',{url:'https://example.test/'});
 const report=dom.window.document.querySelector('#report');
 const table=report.querySelector('table'),before=table.textContent;
 const observer=observe(report);prepare(report);
 assert.equal(table.textContent,before);assert.equal(table.rows[1].cells[2].dataset.label,'15分钟确认');
 assert.equal(report.querySelectorAll('.report-table-scroll').length,1);
 const memory={};const view=new View(report,async()=>({generation:'test',by_symbol:{MERLUSDT:1}}),{getItem:k=>memory[k]||null,setItem:(k,v)=>memory[k]=v});
 view.mount('/latest/index.html');await view.refresh();await new Promise(r=>setTimeout(r,0));
 assert.equal(table.rows[1].cells[3].dataset.label,'操作');
 await view.act(table.querySelector('[data-exclusion-action]'));await new Promise(r=>setTimeout(r,0));
 assert.equal(table.rows[1].hidden,true);
 const excluded=report.querySelector('#manual-exclusions table');assert.ok(excluded.classList.contains('mobile-cards'));
 assert.equal(excluded.rows[1].cells[1].dataset.label,'排除时间');
 await view.act(excluded.querySelector('[data-exclusion-action="restore"]'));await new Promise(r=>setTimeout(r,0));
 assert.equal(table.rows[1].hidden,false);assert.ok(excluded.querySelector('.mobile-card-empty'));
 assert.equal(report.querySelectorAll('.report-table-scroll').length,1);
 const css=fs.readFileSync('report_mobile.css','utf8');assert.match(css,/@media\(max-width:700px\)/);assert.match(css,/#report tr\[hidden\]\{display:none!important\}/);assert.match(css,/minmax\(0,1fr\)/);
 observer.disconnect();view.destroy();dom.window.close();console.log('Mobile layout: labels, card conversion, dynamic exclusions, restore, unchanged content and scroll wrappers passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
