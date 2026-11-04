'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const number = n => Number(n || 0).toLocaleString();
const date = t => t ? new Date(t * 1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const time = t => new Date(t * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
const bytes = n => n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${(n / 1e6).toFixed(1)} MB`;
const duration = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2,'0')}`;
const ago = t => !t ? '—' : (Date.now()/1000-t < 60 ? 'Just now' : `${Math.floor((Date.now()/1000-t)/60)} min ago`);
const empty = (title, subtitle) => `<div class="empty"><strong>${escapeHTML(title)}</strong>${escapeHTML(subtitle)}</div>`;
let playingId=null;
let activeTab='overview', libraryOffset=0, libraryTotal=0, historyOffset=0, statusData=null, toastTimer, searchTimer;
const pageTitles={overview:'Every moment, accounted for.',library:'A place for the unidentified.',history:'What’s been on the air.',system:'A clear view behind the scenes.'};
async function api(path, body) {
  const response=await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-Airlog-Request':'1'},body:JSON.stringify(body)});
  if (!response.ok) { const result=await response.json().catch(()=>({})); throw new Error(result.error || `Request failed (${response.status})`); }
  return response.json();
}
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,5000); }
function setText(id,value) { $(id).textContent=value; }
async function selectTab(tab) {
  activeTab=tab;
  document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==tab);
  document.querySelectorAll('nav button').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));
  setText('page-label',tab.toUpperCase()); setText('page-title',pageTitles[tab]);
  try { if(tab==='library') await loadLibrary(); if(tab==='history') await loadHistory(); } catch(e) { toast(e.message); }
}
function play(id,title,started) {
  playingId=id;
  $('player').hidden=false; setText('player-title',title || 'Preserved recording'); setText('player-subtitle',date(started));
  $('audio').src=`/api/chunks/${id}/audio`; $('download').href=$('audio').src;
  $('audio').play().catch(()=>toast('Press play to listen. If unavailable, the file may have been removed.'));
}
function albumArt(url) {
  const image = typeof url === 'string' && /^https:\/\//i.test(url)
    ? `<img class="album-cover" src="${escapeHTML(url)}" alt="" width="44" height="44" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : '';
  return `<span class="album-art" aria-hidden="true"><span class="album-placeholder">♫</span>${image}</span>`;
}
function playTable(items) {
  if(!items.length) return empty('No audio matches yet','Recognized songs will appear here once the worker has analyzed a recording.');
  return `<table><thead><tr><th>Observed at</th><th>Track / artist</th><th>Matched span</th><th class="run-samples">Samples</th></tr></thead><tbody>${items.map(row=>`<tr><td class="time-cell">${escapeHTML(date(row.started))}</td><td><div class="history-track">${albumArt(row.artwork_url)}<div class="history-track-text"><div class="track-name">${escapeHTML(row.title)}</div><div class="track-artist">${escapeHTML(row.artist)}</div></div></div></td><td>${duration(row.duration)}</td><td class="run-samples">${number(row.samples)}</td></tr>`).join('')}</tbody></table>`;
}
function renderStatus(s) {
  statusData=s;
  const recorder=s.services.recorder, healthy=recorder.state==='recording'&&!recorder.stale;
  setText('connection',healthy?'● Capturing':'Capture: '+recorder.state); $('connection').className='pill '+(healthy?'good':'bad');
  setText('sidebar-status',healthy?'Recorder connected':'Recorder '+recorder.state); $('sidebar-dot').classList.toggle('offline',!healthy);
  $('alerts').innerHTML=s.alerts.map(x=>`<div class="alert">${escapeHTML(x)}</div>`).join('');
  setText('capture-state',recorder.state); $('capture-state').className='pill '+(healthy?'good':'bad');
  const captured=s.totals.captured_today;
  $('capture-hours').innerHTML=`${Math.floor(captured/3600)}<span>h</span> ${String(Math.floor(captured%3600/60)).padStart(2,'0')}<span>m</span>`;
  const coverage=Math.min(100,captured/86400*100);
  $('coverage-fill').style.width=coverage+'%'; setText('coverage-label',coverage.toFixed(1)+'% observed coverage');
  setText('chunk-label',s.settings.chunk_seconds+'s chunks'); setText('last-audio',ago(recorder.last_audio));
  setText('recognition-state',s.services.worker.state+(s.services.worker.retry_in>5?` · retry in ${Math.ceil(s.services.worker.retry_in)}s`:''));
  for(const [id,key] of [['matched-count','matched'],['queued-count','queued'],['unknown-count','unknown'],['queue-badge','queued']]) setText(id,number(s.totals[key]));
  setText('storage-count',bytes(s.totals.stored_bytes)); setText('disk-caption',`${bytes(s.disk.free)} free on audio volume`);
  if(s.latest) { setText('match-title',s.latest.title); setText('match-artist',s.latest.artist); setText('match-age',ago(s.latest.started)); setText('match-detail',`Audio fingerprint match · ${date(s.latest.started)}`); }
  setText('metadata-title',s.metadata?.title ? `${s.metadata.artist ? s.metadata.artist+' — ' : ''}${s.metadata.title}${s.services.metadata.stale || s.services.metadata.state!=='ok'?' (stale)':''}` : 'No usable metadata yet');
  const start=s.now-7200;
  $('timeline').replaceChildren(...s.timeline.map(c=>{
    const block=document.createElement('button');
    const left=Math.max(0,(c.started-start)/7200*100),width=Math.min(100-left,c.duration/7200*100);
    block.className=c.state; block.style.left=left+'%'; block.style.width=width+'%';
    block.dataset.audio=c.id; block.dataset.start=c.started;
    block.title=date(c.started)+' · '+c.state; block.setAttribute('aria-label',block.title+' recording');
    return block;
  }));
  setText('timeline-start',time(start));
  $('service-cards').innerHTML=Object.entries(s.services).map(([name,v])=>`<article class="service-card"><h2>${escapeHTML(name[0].toUpperCase()+name.slice(1))}</h2><span class="pill ${v.stale||v.error?'bad':'good'}">${escapeHTML(v.stale?'Heartbeat stale':v.state)}</span><p>Updated ${escapeHTML(ago(v.updated))}</p>${v.error?`<p>${escapeHTML(v.error)}</p>`:''}</article>`).join('');
  const policies=[['Recognition',s.settings.provider==='shazam'?'ShazamIO · free, unofficial':'Disabled'],['Automatic deletion',s.settings.auto_delete?'Enabled · repeated-match heuristic':'Off · manual deletion only'],['Grace period',s.settings.retention_hours+' hours'],['Audio format','16 kHz mono WAV · ~2.76 GB/day'],['Protected from automatic cleanup','Unknown, failed, pending & manually kept'],['Disk reserve',bytes(s.settings.min_free_bytes)]];
  $('policy').innerHTML=policies.map(([k,v])=>`<div class="policy-line"><span>${escapeHTML(k)}</span><strong>${escapeHTML(v)}</strong></div>`).join('')+`<p class="footnote">${s.settings.auto_delete?'Automatic deletion cannot reliably detect speech over music. Turn it off to disable heuristic cleanup.':'Released recordings are eligible for cleanup after the grace period. Set AUTO_DELETE_MATCHED in your server configuration to opt into heuristic cleanup.'} The library’s Delete unidentified action immediately deletes unkept unidentified audio. Browser times use your local timezone.</p>`;
  $('events').innerHTML=s.events.length?s.events.map(e=>`<div class="event"><span>${escapeHTML(date(e.observed))}</span><div><strong>${escapeHTML(e.kind.replaceAll('_',' '))}</strong><p>${escapeHTML(e.detail)}</p></div></div>`).join(''):empty('No events yet','Recorder connections and interruptions will appear here.');
  setText('last-refresh','Updated '+time(s.now));
}
async function refreshStatus() {
  try { renderStatus(await api('/api/status')); if(activeTab==='overview') $('recent-plays').innerHTML=playTable((await api('/api/plays')).items.slice(0,6)); }
  catch(e) { setText('connection','Backend unavailable'); $('connection').className='pill bad'; $('alerts').innerHTML=`<div class="alert">Unable to refresh: ${escapeHTML(e.message)}. Displayed information may be stale.</div>`; }
}
let libraryRequest=0;
async function loadLibrary() {
  const request=++libraryRequest;
  const params=new URLSearchParams({offset:libraryOffset,state:$('filter').value,q:$('search').value});
  const result=await api('/api/chunks?'+params);
  if(request!==libraryRequest) return;
  libraryTotal=result.total;
  $('chunk-table').innerHTML=result.items.length?`<table><thead><tr><th>Captured at</th><th>Recording</th><th>State</th><th>Review</th></tr></thead><tbody>${result.items.map(c=>`<tr><td class="time-cell">${escapeHTML(date(c.started))}<div class="track-artist">${duration(c.duration)}${c.recovered?' · recovered':''}</div></td><td><div class="track-name">${escapeHTML(c.title||'Unidentified audio')}</div><div class="track-artist">${escapeHTML(c.artist || (c.error?'Recognition unavailable':'Waiting for a confident match'))}</div></td><td><span class="state-label ${escapeHTML(c.state)}">${escapeHTML(c.deleted_at?'audio deleted':c.state)}</span>${c.review!=='auto'?`<div class="track-artist">${c.review==='keep'?'Kept':'Released'}</div>`:''}</td><td><div class="actions">${!c.deleted_at?`<button class="play" data-audio="${c.id}" data-title="${escapeHTML(c.title||'Preserved recording')}" data-start="${c.started}" aria-label="Play recording">▶</button><button data-review="keep" data-id="${c.id}" class="${c.review==='keep'?'selected':''}">Keep</button><button data-review="release" data-id="${c.id}">Release</button><button data-retry="${c.id}" title="Queue another recognition attempt">Retry</button>`:''}<button data-evidence="${c.id}">Details</button></div></td></tr>`).join('')}</tbody></table>`:empty('No recordings in this view','New recordings will appear as capture chunks finish. Try another filter or refresh.');
  setText('page-info',result.total?`${libraryOffset+1}–${Math.min(libraryOffset+50,result.total)} of ${number(result.total)} recordings`:'0 recordings');
  $('previous').disabled=libraryOffset===0; $('next').disabled=libraryOffset+50>=result.total;
}
async function loadHistory() {
  const metadata=$('history-source').value==='metadata'; $('days-label').hidden=metadata;
  $('history-previous').hidden=metadata; $('history-next').hidden=metadata;
  if(metadata) {
    const {items}=await api('/api/metadata');
    setText('history-note','Last 200 reported metadata changes in the past 7 days. These labels are not verified against audio.');
    $('history-table').innerHTML=items.length?`<table><thead><tr><th>Observed at</th><th>Stream label</th></tr></thead><tbody>${items.map(m=>`<tr><td class="time-cell">${escapeHTML(date(m.observed))}</td><td>${escapeHTML([m.artist,m.title].filter(Boolean).join(' — ')||'Unparsed metadata')}</td></tr>`).join('')}</tbody></table>`:empty('No metadata yet','Metadata polling runs independently of audio recognition.');
    setText('history-page-info',items.length+' changes');
  } else {
    setText('history-note','Times are estimated capture times, not exact song boundaries. Unknown intervals split runs; recorder restarts do not split a continuous match. Counts may overstate full song plays.');
    const since=Date.now()/1000-Number($('history-days').value)*86400;
    const result=await api(`/api/plays?since=${since}&offset=${historyOffset}`);
    $('history-table').innerHTML=playTable(result.items);
    setText('history-page-info',result.items.length?`Runs ${historyOffset+1}–${historyOffset+result.items.length}`:'No runs');
    $('history-previous').disabled=historyOffset===0; $('history-next').disabled=result.items.length<100;
  }
}
document.addEventListener('error', event=>{
  if(event.target.classList?.contains('album-cover')) event.target.remove();
}, true);
document.addEventListener('click',async event=>{
  const button=event.target.closest('button'); if(!button) return;
  try {
    if(button.dataset.tab) await selectTab(button.dataset.tab);
    if(button.dataset.audio) play(button.dataset.audio,button.dataset.title,Number(button.dataset.start));
    if(button.dataset.review) {
      if(button.dataset.review==='release'&&!confirm('Release this recording? After the grace period, its audio can be permanently deleted.')) return;
      await api(`/api/chunks/${button.dataset.id}/review`,{review:button.dataset.review}); toast(button.dataset.review==='keep'?'Recording protected.':'Recording released for cleanup after the grace period.'); await loadLibrary();
    }
    if(button.dataset.retry) { await api(`/api/chunks/${button.dataset.retry}/retry`,{}); toast('Queued for another recognition attempt.'); await loadLibrary(); }
    if(button.dataset.evidence) { const result=await api(`/api/chunks/${button.dataset.evidence}/evidence`); $('evidence-content').textContent=JSON.stringify(result,null,2); $('evidence-dialog').showModal(); }
  } catch(e) { toast(e.message); }
});
$('close-evidence').onclick=()=>$('evidence-dialog').close();
$('close-player').onclick=()=>{ $('audio').pause(); $('audio').removeAttribute('src'); $('audio').load(); $('player').hidden=true; };
$('audio').onended=async()=>{ if(!$('continue-playback').checked || !playingId) return; try { const {next}=await api(`/api/chunks/${playingId}/next`); if(next) play(next.id,next.title,next.started); else toast('End of contiguous available audio.'); } catch(e) { toast(e.message); } };
$('audio').onerror=()=>toast('Audio unavailable. Check recording details and server storage.');
$('refresh-library').onclick=()=>loadLibrary().catch(e=>toast(e.message));
$('delete-unidentified').onclick=async()=>{
  const button=$('delete-unidentified');
  if(button.disabled || !confirm('Permanently delete ALL unidentified samples that are not kept? This applies across all pages and filters, deletes audio immediately without the grace period, and cannot be undone. Kept samples, pending samples, recognition errors, and matched audio are preserved. Records and recognition evidence remain.')) return;
  button.disabled=true;
  try {
    const result=await api('/api/chunks/delete-unidentified',{});
    libraryOffset=0;
    toast(result.deleted ? `Deleted ${number(result.deleted)} unidentified sample${result.deleted===1?'':'s'}.` : 'No unkept unidentified samples to delete.');
    await Promise.all([loadLibrary(),refreshStatus()]);
  } catch(e) { toast(e.message); }
  finally { button.disabled=false; }
};
$('filter').onchange=()=>{ libraryOffset=0; loadLibrary().catch(e=>toast(e.message)); };
$('search').oninput=()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>{ libraryOffset=0; loadLibrary().catch(e=>toast(e.message)); },300); };
$('previous').onclick=()=>{ libraryOffset=Math.max(0,libraryOffset-50); loadLibrary().catch(e=>toast(e.message)); };
$('next').onclick=()=>{ if(libraryOffset+50<libraryTotal) libraryOffset+=50; loadLibrary().catch(e=>toast(e.message)); };
for(const id of ['history-source','history-days']) $(id).onchange=()=>{ historyOffset=0; loadHistory().catch(e=>toast(e.message)); };
$('history-previous').onclick=()=>{historyOffset=Math.max(0,historyOffset-100);loadHistory().catch(e=>toast(e.message));};
$('history-next').onclick=()=>{historyOffset+=100;loadHistory().catch(e=>toast(e.message));};
function clock(){ setText('clock',new Date().toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'})+' · '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})); }
clock(); setInterval(clock,1000);
async function poll() { await refreshStatus(); setTimeout(poll,5000); }
poll();
