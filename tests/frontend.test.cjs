// Execute dashboard behavior with a small DOM stand-in; not a browser layout test.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(id='') { this.id=id; this.dataset={}; this.style={}; this.value=''; this.hidden=false; this.children=[]; this.textContent=''; this.innerHTML=''; this.attributes={}; this.classList={toggle:()=>{}}; }
  replaceChildren(...items){ this.children=items; }
  setAttribute(k,v){ this.attributes[k]=v; }
  removeAttribute(k){ delete this.attributes[k]; }
  play(){ return Promise.resolve(); }
  pause(){} load(){} showModal(){this.open=true;} close(){this.open=false;}
}
async function boot(overrides={}) {
  const html=fs.readFileSync('static/index.html','utf8'), elements={};
  for(const [,id] of html.matchAll(/id="([^"]+)"/g)) elements[id]=new Element(id);
  elements['history-source'].value='audio'; elements['history-days'].value='7';
  const calls=[], now=Date.now()/1000;
  const status={now,services:{recorder:{state:'recording',updated:now,last_audio:now},worker:{state:'waiting',updated:now},metadata:{state:'ok',updated:now}},totals:{captured_today:3600,matched:12,queued:3,unknown:2,stored_bytes:1000000},settings:{chunk_seconds:15,provider:'shazam',auto_delete:false,retention_hours:72,min_free_bytes:5000000000},disk:{free:20000000000},alerts:[],timeline:[{id:'a'.repeat(32),started:now-15,duration:15,state:'matched'}],latest:{title:'<img src=x onerror=alert(1)>',artist:'Artist & Co',started:now-15},metadata:{artist:'Example',title:'Song'},events:[]};
  const routes={'/api/status':status,'/api/plays':{items:[]},'/api/chunks?':{items:[],total:0},...overrides};
  const document={getElementById:id=>elements[id],createElement:()=>new Element(),querySelectorAll:selector=>selector==='.page'?['overview','library','history','system'].map(id=>elements[id]):[],addEventListener:(type,fn)=>{document[type]=fn;}};
  const context={document,URLSearchParams,Date,console,setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,confirm:()=>true,fetch:async(path,opts)=>{calls.push({path,opts}); const key=Object.keys(routes).sort((a,b)=>b.length-a.length).find(k=>path.startsWith(k));return {ok:true,json:async()=>routes[key]||{ok:true}};}};
  vm.createContext(context); vm.runInContext(fs.readFileSync('static/app.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {context,elements,calls,document};
}
test('overview renders live status and uses safe text for station titles',async()=>{
  const {elements}=await boot();
  assert.equal(elements['connection'].textContent,'● Capturing');
  assert.equal(elements['match-title'].textContent,'<img src=x onerror=alert(1)>');
  assert.equal(elements['matched-count'].textContent,'12');
  assert.equal(elements.timeline.children.length,1);
  assert.ok(Math.abs(parseFloat(elements.timeline.children[0].style.width)-15/7200*100)<1e-10);
  assert.match(elements.policy.innerHTML,/Off · manual deletion only/);
});
test('library empty state and pagination work',async()=>{
  const {context,elements}=await boot();
  await context.selectTab('library');
  assert.equal(elements.overview.hidden,true);
  assert.equal(elements.library.hidden,false);
  assert.equal(elements.previous.disabled,true);
  assert.equal(elements.next.disabled,true);
  assert.match(elements['chunk-table'].innerHTML,/No recordings/);
});
test('rendered history escapes untrusted metadata',async()=>{
  const {context}=await boot();
  const html=context.playTable([{title:'<script>alert(1)</script>',artist:'A & B',started:1,duration:15,samples:1}]);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/A &amp; B/);
});
test('keep button sends the explicit review mutation',async()=>{
  const {document,calls}=await boot();
  const button={dataset:{review:'keep',id:'a'.repeat(32)}};
  await document.click({target:{closest:()=>button}});
  const call=calls.find(c=>c.path.endsWith('/review'));
  assert.equal(call.opts.method,'POST');
  assert.deepEqual(JSON.parse(call.opts.body),{review:'keep'});
  assert.equal(call.opts.headers['X-Airlog-Request'],'1');
});
test('bulk delete confirms global immediate deletion and refreshes the library',async()=>{
  const {context,elements,calls}=await boot({'/api/chunks/delete-unidentified':{ok:true,deleted:2}});
  let confirmation;
  context.confirm=message=>{confirmation=message;return true;};
  elements.filter.value='matched'; elements.search.value='Example';
  vm.runInContext('libraryOffset=50',context);
  await elements['delete-unidentified'].onclick();
  assert.match(confirmation,/ALL unidentified samples that are not kept/);
  assert.match(confirmation,/all pages and filters/);
  assert.match(confirmation,/immediately without the grace period/);
  const call=calls.find(c=>c.path==='/api/chunks/delete-unidentified');
  assert.equal(call.opts.method,'POST');
  assert.equal(call.opts.headers['X-Airlog-Request'],'1');
  assert.deepEqual(JSON.parse(call.opts.body),{});
  assert.equal(elements.toast.textContent,'Deleted 2 unidentified samples.');
  assert.ok(calls.some(c=>c.path.startsWith('/api/chunks?offset=0&state=matched&q=Example')));
  assert.equal(calls.filter(c=>c.path==='/api/status').length,2);
  assert.equal(elements['delete-unidentified'].disabled,false);
});
test('canceling bulk deletion sends no mutation',async()=>{
  const {context,elements,calls}=await boot();
  context.confirm=()=>false;
  await elements['delete-unidentified'].onclick();
  assert.equal(calls.filter(c=>c.opts?.method==='POST').length,0);
});
test('bulk deletion handles no eligible samples',async()=>{
  const {elements}=await boot({'/api/chunks/delete-unidentified':{ok:true,deleted:0}});
  await elements['delete-unidentified'].onclick();
  assert.equal(elements.toast.textContent,'No unkept unidentified samples to delete.');
});
test('bulk deletion prevents duplicate requests and allows retry after errors',async()=>{
  const {context,elements}=await boot();
  let finish, requests=0;
  context.fetch=()=>{requests++;return new Promise(resolve=>{finish=resolve;});};
  const pending=elements['delete-unidentified'].onclick();
  assert.equal(elements['delete-unidentified'].disabled,true);
  await elements['delete-unidentified'].onclick();
  assert.equal(requests,1);
  finish({ok:false,json:async()=>({error:'Update failed'})});
  await pending;
  assert.equal(elements.toast.textContent,'Update failed');
  assert.equal(elements['delete-unidentified'].disabled,false);
});
