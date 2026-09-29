const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.hash.slice(1));
if(params.get('token')) { sessionStorage.setItem('testlab-token',params.get('token')); history.replaceState(null,'',location.pathname); }
const token = sessionStorage.getItem('testlab-token') || '';
let selected = '', report = null, tab = 'memory', busy = false, lastChat = '';
const stateNames = {queued:'排队中',starting:'启动中',running:'回放中',ready:'可交互',completed:'已完成',failed:'失败',stopped:'已停止',interrupted:'上次运行中断'};
async function api(path,body) {
  const r = await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const j = await r.json(); if(!r.ok) throw new Error(j.error || r.status); return j;
}
function notice(text=''){$('notice').textContent=text;}
function node(tag,text,cls){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function pretty(value){return JSON.stringify(value,(key,v)=>{if(typeof v==='string'&&['sidebar','qa','sources','aliases','trace','fields','attachments','payload','item_ids'].includes(key)){try{return JSON.parse(v);}catch{}}return v;},2);}
async function guarded(fn){try{notice();await fn();await refresh();}catch(e){notice(e.message);}}
$('rows').value=pretty([
 {sender:'lin',native_id:'m1',text:'我们这周需要讨论项目进展。',expect:{reply_count:0}},
 {sender:'owner',native_id:'m2',text:'帮我们设计两套开会时间和地点',at:true},
 {sender:'owner',native_id:'m3',text:'第二个方案提前半小时',at:true},
 {sender:'owner',native_id:'m4',text:'就按刚修改的第二个方案定了',at:true},
 {sender:'lin',native_id:'m5',text:'最终怎么安排？',at:true}
]);
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function options(){return {title:$('title').value.trim()||'新实验',model_limit:Number($('limit').value)};}
$('create').onclick=()=>guarded(async()=>{selected=(await api('runs',options())).ids[0];});
$('batch').onclick=()=>guarded(async()=>{
 const p={...options(),copies:Number($('copies').value),integrate:$('integrate').checked};
 if($('dataset').value)p.dataset=$('dataset').value;
 else {const raw=$('rows').value.trim(); p.rows=raw.startsWith('[')?JSON.parse(raw):raw.split('\n').filter(x=>x.trim()).map(x=>JSON.parse(x));}
 selected=(await api('runs',p)).ids[0];
});
$('upload').onchange=()=>guarded(async()=>{const file=$('upload').files[0];if(file){$('rows').value=await file.text();$('dataset').value='';}});
$('stop').onclick=()=>guarded(()=>api('runs/'+selected+'/stop',{}));
$('export').onclick=()=>guarded(async()=>{const data=await api('runs/'+selected);const url=URL.createObjectURL(new Blob([pretty(data)],{type:'application/json'}));const a=node('a','');a.href=url;a.download=selected+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('compose').onsubmit=e=>{e.preventDefault();if(!selected||busy)return;guarded(async()=>{
 busy=true;buttons();try{const p={sender:$('sender').value,text:$('text').value,at:$('mention').checked};if($('reply').value)p.reply_to=$('reply').value;await api('runs/'+selected+'/step',p);$('text').value='';}finally{busy=false;buttons();}
});};
for(const action of ['read','consolidate'])$(action).onclick=()=>guarded(async()=>{busy=true;buttons();try{await api('runs/'+selected+'/memory',{action,day:$('day').value});}finally{busy=false;buttons();}});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('selected',x===b));guarded(renderDetails);});
function buttons(){const ready=report?.state==='ready'&&!busy;$('send').disabled=!ready;$('read').disabled=!ready;$('consolidate').disabled=!ready;$('export').disabled=!report;$('stop').disabled=!report||!['ready','running','starting','queued'].includes(report.state);}
async function renderDetails(){if(!report)return;const s=report.snapshot||{};if(tab==='log'){$('details').textContent=(await api('runs/'+selected+'/log')).text;return;}if(tab==='memory'){$('details').textContent=pretty({counts:s.counts,items:s.items,day_views:s.tables?.day_views,anchor_topics:s.tables?.anchor_topics,anchor_facts:s.tables?.anchor_facts,digests:s.tables?.digests,drafts:s.tables?.drafts,lexicon:s.tables?.lexicon});}else $('details').textContent=pretty(s[tab]||[]);}
async function refresh(){
 const {runs}=await api('runs');$('runs').replaceChildren();$('run-count').textContent=runs.length;
 for(const r of runs.reverse()){const b=node('button','', 'run'+(r.id===selected?' selected':''));b.append(node('strong',r.title),node('small',`${stateNames[r.state]||r.state} · ${r.progress}/${r.total||'手动'} · ${r.calls} 次调用`));b.onclick=()=>{selected=r.id;lastChat='';guarded(refresh);};$('runs').append(b);}
 if(!selected&&runs.length)selected=runs[0].id;if(!selected)return;
 report=await api('runs/'+selected);$('run-title').textContent=report.title;$('run-state').textContent=stateNames[report.state]||report.state;
 const calls=report.snapshot?.calls||[];const tokens=calls.reduce((n,c)=>n+(c.usage?.input_other||0)+(c.usage?.input_cached||0)+(c.usage?.output||0),0);
 $('metrics').replaceChildren(...[`${calls.length}/${report.model_limit} 模型调用`,`${tokens.toLocaleString()} tokens`,`${report.progress} 条已处理`,`${report.snapshot?.counts?.day_views||0} 天目录`,`${report.checks_passed}/${report.checks_total} 断言通过`].map(x=>node('span',x,'metric')));
 const key=JSON.stringify(report.timeline);if(key!==lastChat){const chat=$('chat');const near=chat.scrollHeight-chat.scrollTop-chat.clientHeight<60;chat.replaceChildren();for(const m of report.timeline){const b=node('div','','bubble '+m.role);b.append(node('div',`${m.role==='assistant'?'爱音':m.sender}${m.mention?' @':''} · #${m.message_id}`,'meta'),node('div',m.kind==='recall'?'[撤回消息]':m.text,'body'));chat.append(b);}if(near||!lastChat)chat.scrollTop=chat.scrollHeight;lastChat=key;}
 buttons();if(report.error)notice(report.error);if(tab!=='log')await renderDetails();
}
guarded(async()=>{const info=await api('info');$('environment').textContent=`${info.model_mode==='host'?'真实模型':'模拟模型'} · 最多 ${info.parallel} 批同时运行`;for(const path of info.datasets){const o=node('option',path);o.value=path;$('dataset').append(o);}for(const d of info.differences)$('differences').append(node('li',d));});
setInterval(()=>refresh().catch(e=>notice(e.message)),1800);
