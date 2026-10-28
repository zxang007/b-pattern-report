(function(root){
'use strict';
function prepare(report){
 for(const table of report.querySelectorAll('table')){
  const cards=table.matches('[data-signal-list="1"]')||!!table.closest('#manual-exclusions');
  if(cards){
   table.classList.add('mobile-cards');
   const rows=Array.from(table.rows),header=rows.find(row=>row.querySelector('th'));
   if(!header)continue;
   header.classList.add('mobile-card-head');
   const labels=Array.from(header.cells,cell=>cell.textContent.trim());
   for(const row of rows){if(row===header)continue;row.classList.add('mobile-card-row');
    for(const [index,cell]of Array.from(row.cells).entries()){
     if(cell.colSpan>1){cell.classList.add('mobile-card-empty');continue;}
     const label=labels[index]||'';if(cell.dataset.label!==label)cell.dataset.label=label;
    }
   }
  }else if(!table.parentElement.classList.contains('report-table-scroll')){
   const wrap=report.ownerDocument.createElement('div');wrap.className='report-table-scroll';
   wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','数据表格，可左右滑动');
   table.before(wrap);wrap.append(table);
  }
 }
}
function observe(report){prepare(report);const observer=new report.ownerDocument.defaultView.MutationObserver(()=>prepare(report));observer.observe(report,{childList:true,subtree:true});return observer;}
root.ReportMobile={prepare,observe};if(typeof module!=='undefined')module.exports={prepare,observe};
})(globalThis);
