const DEFAULT_API = "https://radeon-global.anruicloud.com/instances/hf-354-1e66b8a2/proxy/8000";
let API = localStorage.getItem('perturbo_api') || DEFAULT_API;
let FORCE_DEMO = localStorage.getItem('perturbo_force_demo')==='1';
let ONLINE=false;         // backend /health reachable
let LIVE=false;           // ONLINE and not forced into demo -> use the real agent
let NETDATA=null;         // coupling networks (live network.json, else DEMO.network)

const pmode=()=>document.getElementById('pmode');
const statuspill=()=>document.getElementById('statuspill');

function setStatus(){
  LIVE = ONLINE && !FORCE_DEMO;
  const p=statuspill();
  p.classList.remove('live','demo');
  if(LIVE){ p.classList.add('live'); pmode().textContent='live'; p.title='Connected to agent · click to configure'; }
  else { p.classList.add('demo'); pmode().textContent=FORCE_DEMO?'demo (forced)':'demo'; p.title=(ONLINE?'Demo mode forced on':'Agent unreachable — using built-in demo')+' · click to configure'; }
}

async function checkHealth(){
  try{
    const c=new AbortController(); const to=setTimeout(()=>c.abort(),6000);
    const r=await fetch(API+'/health',{cache:'no-store',signal:c.signal}); clearTimeout(to);
    const d=await r.json(); ONLINE=!!d.ok;
  }catch(e){ ONLINE=false; }
  setStatus();
  // load the real coupling network when live; otherwise use the embedded copy
  NETDATA = DEMO.network;
  if(LIVE){ try{ const r=await fetch(API+'/network',{cache:'no-store'}); NETDATA=await r.json(); }catch(e){ NETDATA=DEMO.network; } }
}

// endpoint helpers — resolve to live URL when connected, else embedded data-URI
function mapUrl(section,pop){
  if(LIVE) return `${API}/map?section=${encodeURIComponent(section)}&population=${encodeURIComponent(pop)}`;
  return DEMO.maps[section+'|'+pop] || '';
}
function mapFallback(section,pop){ return DEMO.maps[section+'|'+pop] || ''; }

// pick a saved analysis for demo/offline replay by matching the user's intent
function pickDemoScript(text){
  const t=(text||'').toLowerCase();
  if(DEMO.scripts.knowledge && /\b(druggable|drug|database|databases|known|evidence|literature|trial|trials|clinical|approved|inhibitor)\b/.test(t)) return DEMO.scripts.knowledge;
  if(/\b(eval|evaluate|score|triage|consider|proposed|my genes|gene list|ccn2|egfr|sparc|bgn)\b/.test(t)) return DEMO.scripts.evaluate;
  if(/\b(compare|comparison|versus|vs|difference|between)\b/.test(t)) return DEMO.scripts.compare;
  if(/\b(primary|t11)\b/.test(t) && !/\b(meta|metasta|liver|hm11)\b/.test(t)) return DEMO.scripts.primary;
  return DEMO.scripts.discover;
}

// apply saved light/dark before anything paints
if(localStorage.getItem('perturbo_mode')!=='dark') document.body.dataset.mode='light';  // default = light
// apply saved chat-panel width
(function(){const w=localStorage.getItem('perturbo_railw'); if(w) document.querySelector('.app').style.setProperty('--railw',w+'px');})();

// persistent intro — original logo + draw animation; user scrolls or clicks to enter
let introEntered=false;
function enterApp(){ if(introEntered)return; introEntered=true;
  document.getElementById('intro').classList.add('gone');
  document.getElementById('app').classList.add('in');
}
(function(){const el=document.getElementById('intro');
  el.addEventListener('click',enterApp);
  el.addEventListener('wheel',enterApp,{passive:true});
  el.addEventListener('touchmove',enterApp,{passive:true});
  window.addEventListener('keydown',e=>{if(!introEntered&&(e.key==='Enter'||e.key===' '||e.key==='ArrowDown'||e.key==='PageDown'))enterApp();});
})();
checkHealth();

const stream=document.getElementById('stream'), body=document.getElementById('body');
const stitle=document.getElementById('stitle'), sk=document.getElementById('sk'), shead=document.getElementById('shead');
let history=[], curGrid=null, targetHost=null, CHARTS=[], CHARTSEQ=0;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function fmt(t){return (t||'').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');}
function addMsg(who,cls){const m=document.createElement('div');m.className='msg '+cls;
  m.innerHTML=`<div class="who">${who}</div><div class="bubble"></div>`;
  stream.appendChild(m);stream.scrollTop=stream.scrollHeight;return m.querySelector('.bubble');}
function thinking(){const m=document.createElement('div');m.className='msg bot';
  m.innerHTML=`<div class="who">PerTurbo</div><div class="bubble"><span class="think"><span class="dot"></span><span class="dot"></span><span class="dot"></span> analysing spatial couplings…</span></div>`;
  stream.appendChild(m);stream.scrollTop=stream.scrollHeight;return m;}

// typewriter — types text into a bubble, char by char
async function type(bubble, text, speed=14){
  bubble.innerHTML=''; const html=fmt(text);
  // type plain text but keep <strong> formatting by typing raw then formatting at end
  let plain=text; let i=0;
  return new Promise(res=>{
    const iv=setInterval(()=>{
      i+=2; bubble.textContent=plain.slice(0,i);
      stream.scrollTop=stream.scrollHeight;
      if(i>=plain.length){clearInterval(iv); bubble.innerHTML=html; res();}
    }, speed);
  });
}

async function ask(text){
  addMsg('You','user').textContent=text; document.getElementById('q').value='';
  const t=thinking(); document.getElementById('send').disabled=true;
  if(LIVE){
    try{
      const r=await fetch(API+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:text,history})});
      const d=await r.json(); t.remove();
      history=(d.history||[]).slice(-8);
      if(d.script && d.script.length){ await playScript(d.script, text); }
      else { const b=addMsg('PerTurbo','bot'); await type(b, d.reply||'(no response)'); }
    }catch(e){
      t.remove(); ONLINE=false; setStatus();          // lost the agent mid-session -> fall back
      await playDemo(text, true);
    }
  } else {
    t.remove();
    await playDemo(text, !FORCE_DEMO);                  // note only when it's an unexpected offline, not a chosen demo
  }
  document.getElementById('send').disabled=false;
}

// play a saved analysis (offline / demo / mid-demo fallback)
async function playDemo(text, note){
  if(note){ const n=addMsg('PerTurbo','note');
    await type(n,'The live agent is unreachable right now — playing a saved analysis so you can still explore. Reconnect any time from the status pill.',9); }
  history=[];
  await playScript(pickDemoScript(text), text);
}

// ===== choreography: play steps one by one =====
let ACTSEQ=0;
let GRP=0, curTargetGroup=null, PENDING_STEPS=null;
function tagGroup(cardEl){const gid='g'+(++GRP);if(cardEl)cardEl.dataset.group=gid;return gid;}
async function playScript(steps, query){
  PENDING_STEPS=steps;
  const top=findTopTarget(steps); let bannerDone=false;
  for(const step of steps){
    const b=addMsg('PerTurbo','bot');
    const msgEl=b.closest('.msg'); msgEl.classList.add('act');
    await type(b, step.say);                                  // narrate this part
    const info=await doAction(step.action, query);            // perform + get {el, group, isPiece}
    if(info && info.el){
      const actId='a'+(++ACTSEQ);
      msgEl.dataset.act=actId;
      if(info.group) msgEl.dataset.group=info.group;          // which "act" (card) this line belongs to
      info.el.classList.add('act-src');
      if(info.isPiece) info.el.dataset.act=actId;             // a row -> piece-level 1:1 link
    }
    if(top && !bannerDone && curGrid && step.action && step.action.type==='title'){ renderFinding(curGrid, top); bannerDone=true; }
    await sleep(420);
  }
}
// item 5: hover a whole act -> highlight its whole narration; hover a piece (row/gene) -> its 1-2 lines
function clearActHi(){document.querySelectorAll('.msg.linked').forEach(m=>m.classList.remove('linked'));
  document.querySelectorAll('.act-hi').forEach(x=>x.classList.remove('act-hi'));}
function scrollMsgCentered(m){ if(m) m.scrollIntoView({behavior:'smooth',block:'center'}); }
function setupActLinks(){
  const hist=document.getElementById('history'), strm=document.getElementById('stream');
  hist.addEventListener('mouseover',e=>{
    const row=e.target.closest('.trow[data-act]');
    const card=e.target.closest('.card[data-group]');
    if(!row && !card){ clearActHi(); return; }
    clearActHi();
    if(row){ const m=document.querySelector('.msg[data-act="'+row.dataset.act+'"]');
      if(m){ m.classList.add('linked'); row.classList.add('act-hi'); scrollMsgCentered(m); } }
    else { const msgs=[...document.querySelectorAll('.msg[data-group="'+card.dataset.group+'"]')];
      msgs.forEach(m=>m.classList.add('linked')); card.classList.add('act-hi'); scrollMsgCentered(msgs[0]); }
  });
  hist.addEventListener('mouseleave',clearActHi);
  strm.addEventListener('mouseover',e=>{
    const m=e.target.closest('.msg[data-act]'); if(!m){clearActHi();return;}
    clearActHi(); m.classList.add('linked');
    let tgt=document.querySelector('.trow[data-act="'+m.dataset.act+'"]');
    if(!tgt && m.dataset.group) tgt=document.querySelector('.card[data-group="'+m.dataset.group+'"]');
    if(tgt){ tgt.classList.add('act-hi'); tgt.scrollIntoView({behavior:'smooth',block:'nearest'}); }
  });
  strm.addEventListener('mouseleave',clearActHi);
}
setupActLinks();
function findTopTarget(steps){
  for(const s of (steps||[])){const a=s.action||{};
    if(a.type==='target' && (a.top || (a.target&&a.target.rank===1))) return a.target;}
  return null;
}
// which populations are ranked in a given saved section (for cross-section credibility)
function popSet(steps){const s={};(steps||[]).forEach(st=>{const a=st.action||{};if(a.type==='target'&&a.target)s[a.target.population]=a.target.rank;});return s;}
function inBothSections(pop){
  try{const a=popSet(DEMO.scripts.discover), b=popSet(DEMO.scripts.primary); return (pop in a)&&(pop in b);}catch(e){return false;}
}

function renderFinding(g, t){
  const sec=t._section||''; const site=sec==='T11'?'primary tumour':(sec==='HM11'?'liver metastasis':(sec||'sample'));
  const both=inBothSections(t.population);
  const drivers=(t.top_target_genes||[]).filter(x=>x.priority_target).slice(0,3).map(x=>x.gene).join(', ');
  const chips=[];
  if(both) chips.push('<span class="fchip">✓&nbsp; ranked #1 in <b>both</b> sections</span>');
  if(t.well_powered) chips.push(`<span class="fchip">✓&nbsp; well-powered · <b>${(+t.n_spots).toLocaleString()}</b> spots</span>`);
  if(t.clean_cross_compartment) chips.push('<span class="fchip">✓&nbsp; clean cross-compartment</span>');
  if(drivers) chips.push(`<span class="fchip">drivers · <b>${drivers}</b></span>`);
  const el=document.createElement('div'); el.className='finding';
  el.innerHTML=`<div class="fmain">
      <div class="fk"><span class="lead"></span>Lead predicted target · ${site}</div>
      <h2><em>${t.population}</em> is the strongest predicted target</h2>
      <div class="fsub">${both
        ? 'The only population ranked <b>#1</b> in <b>both</b> the primary tumour and the liver metastasis — the most reproducible signal in the dataset.'
        : 'Top-ranked cell population by predicted tumour-suppression effect in this sample.'}</div>
      <div class="fchips">${chips.join('')}</div>
    </div>
    <div class="fnum"><div class="v">${fx(t.predicted_tumour_effect)}</div><div class="l">predicted effect</div>
      <div class="cav">⚠ hypothesis · needs validation</div></div>`;
  g.insertBefore(el, g.firstChild);
  const vEl=el.querySelector('.fnum .v');
  if(vEl){ el.classList.add('counting'); animNum(vEl, +t.predicted_tumour_effect, 5, ()=>el.classList.remove('counting')); }
}
// wow: count a number up from 0 with an ease-out, matching fx() formatting
function animNum(el, target, dec, done){
  const dur=1150, st=performance.now(); const fmt=v=>(v<0?'':'+')+v.toFixed(dec);
  function tick(now){const p=Math.min(1,(now-st)/dur); const e=1-Math.pow(1-p,3);
    el.textContent=fmt(target*e); if(p<1) requestAnimationFrame(tick); else {el.textContent=fmt(target); done&&done();}}
  requestAnimationFrame(tick);
}
function animRatio(el, ratio){
  const dur=1100, st=performance.now();
  function tick(now){const p=Math.min(1,(now-st)/dur); const e=1-Math.pow(1-p,3); const v=ratio*e;
    el.textContent='≈'+(v<10?v.toFixed(1):Math.round(v))+'×'; if(p<1) requestAnimationFrame(tick);
    else el.textContent='≈'+(ratio<10?ratio.toFixed(1):Math.round(ratio))+'×';}
  requestAnimationFrame(tick);
}

async function doAction(a, query){
  if(!a||a.type==='none'||a.type==='summary'){return null;}
  if(a.type==='title'){ curGrid=setStage(a.kicker,a.title,query); targetHost=null; curTargetGroup=null; await sleep(200); return null; }
  if(a.type==='chart'){
    if(isCompareChart(a)){ const c=await renderCompare(curGrid, a); return {el:c,group:tagGroup(c)}; }
    const c=card(curGrid,'Predicted effect  ·  ×10⁻³','01',0);
    const cid='chart'+(++CHARTSEQ);   // unique id — history keeps multiple analyses, so 'main' would collide
    const w=document.createElement('div');w.className='chartwrap';w.innerHTML=`<canvas id="${cid}"></canvas>`;c.appendChild(w);
    await sleep(60); drawBar(cid,a.labels,a.data,a.colors); await sleep(700); return {el:c,group:tagGroup(c)};
  }
  if(a.type==='open_targets'){ targetHost=card(curGrid,'Ranked targets','02',0); curTargetGroup=tagGroup(targetHost); await sleep(150); return {el:targetHost,group:curTargetGroup}; }
  if(a.type==='target'){ const row=revealTarget(targetHost||curGrid, a.target, a.index, a.top); await sleep(200); return {el:row,group:curTargetGroup,isPiece:true}; }
  if(a.type==='map'){ const c=await flashMap(curGrid, a.section, a.population, a.hero); return {el:c,group:tagGroup(c)}; }
  if(a.type==='gallery'){ const c=await renderGallery(curGrid, a.maps); return {el:c,group:tagGroup(c)}; }
  if(a.type==='network'){ const c=await renderNetwork(curGrid, a.section, a.population); return {el:c,group:tagGroup(c)}; }
  if(a.type==='genes'){ return null; }
  if(a.type==='table'){ const c=renderTable(curGrid, a.results); await sleep(400); return {el:c,group:tagGroup(c)}; }
  if(a.type==='knowledge'){ const c=renderKnowledge(curGrid, a.knowledge); await sleep(400); return {el:c,group:tagGroup(c)}; }
  return null;
}

// ===== external evidence (Knowledge) — grounded public-DB dossier, kept visually
// distinct from PerTurbo's in-silico prediction. Handles the live /knowledge shape
// (drug_detail[], drugs[], top_papers[], trials[]) and any {available:false} source. =====
const TRACT={AB:'Antibody',SM:'Small molecule',PR:'PROTAC / degrader',OC:'Other modality',AOC:'Oligonucleotide',UE:'Enzyme','small molecule':'Small molecule',antibody:'Antibody'};
function kesc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function knum(v){const n=Number(v);return isFinite(n)?n.toLocaleString():kesc(v);}
// fetch a dossier: live -> POST /knowledge, else the embedded demo copy (or null)
async function getKnowledge(gene){
  if(LIVE){ try{
    const r=await fetch(API+'/knowledge',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({gene,disease:'pancreatic cancer'})});
    const d=await r.json(); if(d&&!d.error&&d.sources) return d;
  }catch(e){} }
  return (DEMO.knowledge&&DEMO.knowledge[gene])||null;
}
// per-gene "ⓘ evidence" button -> its own analysis (title · knowledge · summary), like the live chat flow
async function askKnowledge(gene,pop,sec){
  addMsg('You','user').textContent='External evidence · '+gene;
  const t=thinking(); document.getElementById('send').disabled=true;
  const k=await getKnowledge(gene); t.remove();
  if(!k){ const b=addMsg('PerTurbo','note');
    await type(b,`External evidence for ${gene} is pulled live from public databases (Open Targets, DGIdb, Europe PMC, ClinicalTrials.gov) — reconnect to the agent from the status pill to fetch it.`,9);
    document.getElementById('send').disabled=false; return; }
  const site=secName(sec);
  const script=[
    {say:`Pulling external evidence on **${gene}** from public databases — this is prior knowledge from the literature, kept separate from PerTurbo's in-silico prediction.`,
      action:{type:'title',kicker:'External evidence',title:gene+(site?(' · '+site):'')}},
    {say:k.summary||('Known biology for '+gene+'.'), action:{type:'knowledge',knowledge:k}},
    {say:k.disclaimer||'Sourced live from public databases; presence of a drug or trial does not imply efficacy for this target.', action:{type:'summary'}}
  ];
  await playScript(script, 'External evidence · '+gene);
  document.getElementById('send').disabled=false;
}
function renderKnowledge(g,k){
  k=k||{}; const S=k.sources||{};
  const c=card(g,'External evidence  ·  '+kesc(k.gene||''),'ⓘ',0);
  c.classList.add('kcard');
  const al=(k.aliases_queried||[]).join(' / ');
  const head=document.createElement('div'); head.className='khead';
  head.innerHTML=`<span class="kbadge">◇ external evidence · public databases</span>
    <div class="ksum">${kesc(k.summary||'Known biology assembled from public sources.')}</div>
    <div class="kmeta">Queried${al?` as <b>${kesc(al)}</b>`:''}${k.disease_context?` · context <b>${kesc(k.disease_context)}</b>`:''} — this is what the world already knows, <b>not</b> PerTurbo's prediction.</div>`;
  c.appendChild(head);
  const grid=document.createElement('div'); grid.className='kgrid';

  // Open Targets — druggability
  const ot=S.open_targets||{}; let otH;
  if(ot.available){
    const tags=(ot.tractability||[]).map(t=>`<span class="ktag">${kesc(TRACT[t]||t)}</span>`).join('');
    otH=`<div class="kbig">${(ot.tractability||[]).length?'Druggable':'Profiled'}</div>
      <div class="ktags">${tags||'<span class="kdim">no tractability flags</span>'}</div>
      ${ot.name?`<div class="ksmall">${kesc(ot.name)}${ot.biotype?` · ${kesc(ot.biotype)}`:''}</div>`:''}
      ${ot.ensembl_id?`<a class="klink" href="https://platform.opentargets.org/target/${kesc(ot.ensembl_id)}" target="_blank" rel="noopener">Open Targets ↗</a>`:''}`;
  } else otH='<div class="knone">No Open Targets record.</div>';

  // DGIdb — drug interactions
  const di=S.drug_interactions||{}; let diH;
  if(di.available){
    const det=(di.drug_detail&&di.drug_detail.length)?di.drug_detail:(di.drugs||[]).map(d=>({drug:d}));
    const n=(di.n_drug_interactions!=null)?di.n_drug_interactions:det.length;
    const list=det.slice(0,6).map(d=>`<div class="kdrug"><span class="kd-n">${kesc(d.drug)}</span>${d.type?`<span class="kd-t">${kesc(d.type)}</span>`:''}</div>`).join('');
    diH=`<div class="kbig">${knum(n)}<span class="ku">interaction${n==1?'':'s'}</span></div>
      ${list||'<div class="kdim">drug names not returned</div>'}${det.length>6?`<div class="kmore">+${det.length-6} more in DGIdb</div>`:''}`;
  } else diH='<div class="knone">No drug interactions found.</div>';

  // Europe PMC — literature
  const lit=S.literature||{}; let litH;
  if(lit.available){
    const p=(lit.top_papers||[])[0];
    litH=`<div class="kbig">${knum(lit.total_hits!=null?lit.total_hits:0)}<span class="ku">papers</span></div>`
      +(p?`<div class="kpaper"><div class="kp-t">${kesc(p.title||'')}</div>
        <div class="kp-m">${[p.journal,p.year,(p.cited!=null?'cited '+p.cited:'')].filter(Boolean).map(kesc).join(' · ')}</div>
        ${p.id?`<a class="klink" href="https://europepmc.org/abstract/MED/${kesc(p.id)}" target="_blank" rel="noopener">Europe PMC ↗</a>`:''}</div>`
      :'<div class="kdim">top paper not returned</div>');
  } else litH='<div class="knone">No literature returned.</div>';

  // ClinicalTrials.gov — trials
  const ct=S.clinical_trials||{}; let ctH;
  if(ct.available){
    const n=(ct.total_trials!=null)?ct.total_trials:((ct.trials||[]).length);
    const list=(ct.trials||[]).slice(0,3).map(t=>`<div class="ktrial"><div class="kt-t">${kesc(t.title||t.nct||'')}</div><div class="kt-m">${[t.phase,t.status,t.nct].filter(Boolean).map(kesc).join(' · ')}</div></div>`).join('');
    ctH=`<div class="kbig">${knum(n)}<span class="ku">trial${n==1?'':'s'}</span></div>${(+n>0)?(list||''):'<div class="kdim">none registered for this gene · disease</div>'}`;
  } else ctH='<div class="knone">No trials returned.</div>';

  const tile=(cls,src,label,inner)=>`<div class="ktile ${cls}"><div class="kt-src">${src}</div><h5>${label}</h5>${inner}</div>`;
  grid.innerHTML=tile('t-ot','Open Targets','Druggability',otH)
    +tile('t-dg','DGIdb','Drug interactions',diH)
    +tile('t-pmc','Europe PMC','Literature',litH)
    +tile('t-ct','ClinicalTrials.gov','Clinical trials',ctH);
  c.appendChild(grid);

  const disc=document.createElement('div'); disc.className='kdisc';
  disc.innerHTML=`<span>⚠ ${kesc(k.disclaimer||'Sourced live from public databases (Open Targets, DGIdb, Europe PMC, ClinicalTrials.gov). Real records shown as-is; presence of a drug or trial does not imply efficacy for this target.')}</span>`;
  c.appendChild(disc);
  return c;
}

// ===== results history: every analysis is kept (tabs + stacked views) =====
let ANALYSES=[], activeId=null, ANSEQ=0, viewMode='tabs';
const stageEl=()=>document.getElementById('stage');
const histWrap=()=>document.getElementById('history');
const histBar=()=>document.getElementById('histbar');

function setStage(kicker,title,query){
  document.querySelector('#history .empty')?.remove();
  ACTNUM=0;   // restart act numbering for each analysis
  const id='an'+(++ANSEQ);
  const sec=document.createElement('section'); sec.className='analysis'; sec.dataset.id=id;
  const q=(query||'').replace(/</g,'&lt;');
  sec.innerHTML=`<div class="ahead"><div><div class="ak">${kicker||'Analysis'}</div><h4>${title||''}</h4></div>${q?`<div class="aq">“${q}”</div>`:''}</div>`;
  const g=document.createElement('div'); g.className='grid'; sec.appendChild(g);
  histWrap().appendChild(sec);
  ANALYSES.push({id,kicker,title,query:query||'',steps:PENDING_STEPS});
  sk.textContent=kicker; stitle.textContent=title;
  shead.classList.remove('show');void shead.offsetWidth;shead.classList.add('show');
  setActive(id); rebuildHist();
  return g;
}
function setActive(id){
  activeId=id;
  document.querySelectorAll('#history .analysis').forEach(s=>s.classList.toggle('active',s.dataset.id===id));
  document.querySelectorAll('#histbar .htab').forEach(t=>t.classList.toggle('active',t.dataset.id===id));
  const a=ANALYSES.find(x=>x.id===id);
  if(a && viewMode==='tabs'){ sk.textContent=a.kicker; stitle.textContent=a.title; }
  if(viewMode==='tabs'){ const s=document.querySelector(`#history .analysis[data-id="${id}"]`); if(s) s.scrollIntoView({block:'nearest'}); stageEl().scrollTop=0; }
}
function closeAnalysis(id,ev){
  if(ev) ev.stopPropagation();
  const s=document.querySelector(`#history .analysis[data-id="${id}"]`); if(s) s.remove();
  ANALYSES=ANALYSES.filter(x=>x.id!==id);
  if(activeId===id) activeId=ANALYSES.length?ANALYSES[ANALYSES.length-1].id:null;
  if(!ANALYSES.length){ histBar().classList.add('hide'); }
  rebuildHist(); if(activeId) setActive(activeId);
}
function rebuildHist(){
  const bar=histBar(); if(!ANALYSES.length){ bar.classList.add('hide'); bar.innerHTML=''; return; }
  bar.classList.remove('hide');
  const tabs=ANALYSES.map(a=>{
    const lbl=(a.title||a.query||'Analysis'); const short=lbl.length>26?lbl.slice(0,25)+'…':lbl;
    return `<div class="htab${a.id===activeId?' active':''}" data-id="${a.id}" title="${(a.query||a.title||'').replace(/"/g,'&quot;')}">
      <span class="htx">${short}</span><span class="hclose" data-close="${a.id}">✕</span></div>`;
  }).join('');
  bar.innerHTML=tabs+`<div class="hview">
      <button data-view="tabs" class="${viewMode==='tabs'?'on':''}">Tabs</button>
      <button data-view="stacked" class="${viewMode==='stacked'?'on':''}">Stacked</button></div>`;
  bar.querySelectorAll('.htab').forEach(t=>t.onclick=()=>setActive(t.dataset.id));
  bar.querySelectorAll('[data-close]').forEach(c=>c.onclick=(e)=>closeAnalysis(c.dataset.close,e));
  bar.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));
}
function setView(m){
  viewMode=m; stageEl().classList.toggle('tabs',m==='tabs'); stageEl().classList.toggle('stacked',m==='stacked');
  rebuildHist(); if(m==='tabs'&&activeId) setActive(activeId);
}
function fx(e){return (e<0?'':'+')+Number(e).toFixed(5);}
function ec(e){return e<0?'neg':'pos';}
let ACTNUM=0;
function card(g,title,idx,delay){const c=document.createElement('div');c.className='card';
  c.style.animationDelay=(delay||0)+'s';
  const n=String(++ACTNUM).padStart(2,'0');   // sequential act number per analysis (01, 02, 03…)
  c.innerHTML=`<h3><span class="idx">${n}</span>${title}</h3>`;g.appendChild(c);
  addActExport(c); return c;}
// per-act export button (item: "export buttons in each act")
function addActExport(cardEl){
  const btn=document.createElement('button'); btn.className='actexport'; btn.title='Export this act as an image';
  btn.setAttribute('data-html2canvas-ignore','true'); btn.innerHTML='⤓';
  btn.onclick=(e)=>{e.stopPropagation(); exportCardPNG(cardEl, btn);};
  cardEl.appendChild(btn);
}
function exportCardPNG(cardEl, btn){
  if(typeof html2canvas==='undefined'){toast('Image export needs an internet connection.',true);return;}
  if(!cardEl.offsetParent){toast('Open this analysis first, then export the act.',true);return;}
  const old=btn?btn.innerHTML:''; if(btn)btn.innerHTML='…';
  html2canvas(cardEl,{backgroundColor:getComputedStyle(document.body).backgroundColor,scale:2,useCORS:true}).then(cv=>{
    cv.toBlob(b=>{ if(btn)btn.innerHTML=old; if(!b){toast('Image export failed.',true);return;}
      const u=URL.createObjectURL(b);const l=document.createElement('a');l.href=u;l.download='perturbo_act.png';l.click();
      setTimeout(()=>URL.revokeObjectURL(u),2000);toast('Act exported as PNG.');});
  }).catch(()=>{if(btn)btn.innerHTML=old;toast('Image export failed.',true);});
}

function revealTarget(host,t,i,top){
  const row=document.createElement('div');row.className='trow'+(top?' top':'');
  const tag=t.is_self_signal?'<span class="tagd t-self">self-signal</span>':
    (t.clean_cross_compartment?'<span class="tagd t-clean">clean cross-compartment</span>':'<span class="tagd t-flag">shared-gene flagged</span>');
  const genes=(t.top_target_genes||[]).slice(0,5).map(gn=>{
    let b='';if(gn.network_hub)b+='<span class="b hub">hub</span>';if(gn.pathway_upstream)b+='<span class="b up">up</span>';
    const da=`data-gene="${gn.gene}" data-sec="${t._section||''}" data-hub="${gn.network_hub?1:0}" data-up="${gn.pathway_upstream?1:0}" data-pri="${gn.priority_target?1:0}" data-deg="${gn.degree!=null?gn.degree:''}" data-eff="${gn.effect!=null?gn.effect:''}"`;
    return `<span class="gene${gn.priority_target?' pri':''}" ${da}>${gn.gene}${b}</span>`;}).join('');
  const pesc=(t.population||'').replace(/'/g,"\\'");
  const mapb=t.has_spatial_map?`<span class="mapbtn" onclick="showMap('${t._section||''}','${pesc}')">◱ spatial map</span>`:'';
  // evidence chips from real fields — turns "hypothesis" into visible rigor
  const powered=t.well_powered
    ? `<span class="evi good">✓ ${(+t.n_spots).toLocaleString()} spots</span>`
    : `<span class="evi">${(+t.n_spots).toLocaleString()} spots</span>`;
  const both=inBothSections(t.population)?'<span class="evi good">✓ both sections</span>':'';
  const hubc=(t.top_target_genes||[]).some(g=>g.network_hub)?'<span class="evi">network-central</span>':'';
  row.innerHTML=`<div class="rk">${t.rank||i+1}</div>
    <div class="tinfo"><div class="pname">${t.population}</div>
      <div class="pmeta">${tag}${powered}${both}${hubc}${mapb}</div>
      <div class="genes">${genes}</div></div>
    <div class="eff"><div class="v ${ec(t.predicted_tumour_effect)}">${fx(t.predicted_tumour_effect)}</div><div class="l">effect</div></div>`;
  host.appendChild(row);
  row.scrollIntoView({behavior:'smooth',block:'nearest'});
  return row;
}

// ===== item 6: rich hover tooltips on gene chips =====
function netNode(gene,section){
  try{const s=(NETDATA&&NETDATA.sections&&NETDATA.sections[section])||null; if(!s)return null;
    return (s.nodes||[]).find(n=>n.id===gene)||null;}catch(e){return null;}
}
function geneReport(g,node,eff){
  const parts=[];
  if(g.hub==='1'&&node) parts.push(`One of the most connected genes here (degree <b>${node.degree}</b>) — a hub in the coupling network, so a plausible control point.`);
  else if(node&&node.degree) parts.push(`Coupling degree <b>${node.degree}</b> in this population's network.`);
  if(node&&node.pos_links!=null){const dom=node.pos_links>=node.neg_links?'supportive':'antagonistic';
    parts.push(`Its couplings are predominantly <b>${dom}</b> (+${node.pos_links}/−${node.neg_links}).`);}
  if(node&&node.theme&&node.theme!=='other') parts.push(`Associated with the <b>${node.theme}</b> programme.`);
  if(eff!=null) parts.push(`Predicted knockout effect on the tumour: <b>${(eff<0?'':'+')+(+eff).toFixed(5)}</b>.`);
  if(g.up==='1') parts.push(`Flagged upstream from literature priors — direction is imported, not learned; the couplings themselves are undirected associations.`);
  if(!parts.length) parts.push('Co-varies with the tumour programme in this population.');
  return parts.join(' ');
}
function showGeneTip(elm){
  const g=elm.dataset, node=netNode(g.gene,g.sec);
  const eff=(g.eff!==''&&g.eff!=null)?(+g.eff):(node&&node.effect!=null?node.effect:null);
  const badges=[]; if(g.hub==='1')badges.push('<span class="gt-b hub">network hub</span>');
  if(g.up==='1')badges.push('<span class="gt-b up">pathway-upstream</span>');
  if(g.pri==='1')badges.push('<span class="gt-b pri">priority target</span>');
  const rows=[];
  if(node&&node.degree) rows.push(`<div class="gt-row"><span>coupling degree</span><b>${node.degree}</b></div>`);
  if(node&&node.pos_links!=null) rows.push(`<div class="gt-row"><span>signed links</span><b>+${node.pos_links} / −${node.neg_links}</b></div>`);
  if(node&&node.theme&&node.theme!=='other') rows.push(`<div class="gt-row"><span>programme</span><b>${node.theme}</b></div>`);
  if(eff!=null) rows.push(`<div class="gt-row"><span>gene effect</span><b>${(eff<0?'':'+')+(+eff).toFixed(5)}</b></div>`);
  const trow=elm.closest('.trow'); const pop=trow?((trow.querySelector('.pname')||{}).textContent||''):'';
  const tip=document.getElementById('gtip'); tip.style.pointerEvents='auto';
  tip.innerHTML=`<div class="gt-name">${g.gene}${eff!=null?`<span class="gt-eff ${eff<0?'neg':'pos'}">${(eff<0?'':'+')+(+eff).toFixed(5)}</span>`:''}</div>
    ${badges.length?`<div class="gt-badges">${badges.join('')}</div>`:''}${rows.join('')}
    <div class="gt-report">${geneReport(g,node,eff)}</div>
    <div class="gt-btns">
      <button class="gt-ask" data-ask="${g.gene}" data-pop="${pop.replace(/"/g,'&quot;')}" data-sec="${g.sec}">Ask PerTurbo about ${g.gene} →</button>
      <button class="gt-ev" data-ev="${g.gene}" data-sec="${g.sec}" title="Pull real records from public databases">ⓘ external evidence</button>
    </div>`;
  tip.classList.add('show'); anchorTip(elm);
}
function anchorTip(elm){const tip=document.getElementById('gtip');const r=elm.getBoundingClientRect();const w=tip.offsetWidth,h=tip.offsetHeight;
  let x=r.left, y=r.bottom+8; if(x+w>innerWidth-10)x=innerWidth-w-10; if(x<10)x=10;
  if(y+h>innerHeight-10)y=r.top-h-8; if(y<10)y=10; tip.style.left=x+'px'; tip.style.top=y+'px';}
function moveTip(e){const tip=document.getElementById('gtip');const w=tip.offsetWidth,h=tip.offsetHeight;
  let x=e.clientX+16,y=e.clientY+16; if(x+w>innerWidth-10)x=e.clientX-w-16; if(y+h>innerHeight-10)y=e.clientY-h-16;
  tip.style.left=x+'px';tip.style.top=y+'px';}
function hideTip(){const t=document.getElementById('gtip');t.classList.remove('show');t.style.pointerEvents='none';}
let tipHideT=null; const tipHideSoon=()=>{clearTimeout(tipHideT);tipHideT=setTimeout(hideTip,220);}; const tipCancel=()=>clearTimeout(tipHideT);
(function(){const HIST=document.getElementById('history'), GT=document.getElementById('gtip');
  HIST.addEventListener('mouseover',e=>{const g=e.target.closest('.gene[data-gene]');if(g){tipCancel();showGeneTip(g);}});
  HIST.addEventListener('mouseout',e=>{const g=e.target.closest('.gene[data-gene]');if(!g)return;
    const to=e.relatedTarget; if(to&&to.closest&&(to.closest('.gene[data-gene]')||to.closest('#gtip')))return; tipHideSoon();});
  GT.addEventListener('mouseenter',tipCancel); GT.addEventListener('mouseleave',hideTip);
  GT.addEventListener('click',e=>{
    const ev=e.target.closest('.gt-ev');
    if(ev){ hideTip(); askKnowledge(ev.dataset.ev,'',ev.dataset.sec); return; }
    const b=e.target.closest('.gt-ask');if(!b)return;
    const gene=b.dataset.ask,pop=b.dataset.pop,site=secName(b.dataset.sec); hideTip();
    ask(`Tell me more about ${gene}${pop?(' in '+pop):''}${site?(' ('+site+')'):''} — its role in the coupling network, the supporting evidence, and whether it is a plausible drug target.`);});
})();

function secName(s){return s==='HM11'?'liver metastasis':(s==='T11'?'primary tumour':(s||''));}
function mapExists(section,pop){return LIVE || !!(DEMO.maps&&DEMO.maps[section+'|'+pop]);}
// build one framed microscope specimen
function specimen(section,pop,hero){
  const d=document.createElement('div');d.className='specimen scan focus'+(hero?' hero':'');
  const img=new Image();
  img.onload=()=>{img.classList.add('loaded'); d.classList.add('pulse'); setTimeout(()=>d.classList.remove('focus'),120);};
  img.onerror=()=>{const fb=mapFallback(section,pop); if(fb&&img.src!==fb){img.src=fb;} else {d.classList.remove('scan','focus'); img.classList.add('loaded');}};
  img.src=mapUrl(section,pop); d.appendChild(img);
  const chrome=document.createElement('div');chrome.className='mchrome';
  chrome.innerHTML=`<span>${secName(section)} · ${section}</span><span class="mag">Visium · knockout</span>`;
  d.appendChild(chrome);
  const sb=document.createElement('div');sb.className='scalebar';sb.innerHTML='<i></i><span>spatial spots</span>';d.appendChild(sb);
  const cap=document.createElement('div');cap.className='cap';
  cap.innerHTML=`<b>${pop}</b> — predicted knockout effect across the tissue. Blue = suppression.`;
  d.appendChild(cap);
  const zh=document.createElement('div');zh.className='zoomhint';zh.textContent='◱ click to zoom';d.appendChild(zh);
  d.onclick=()=>openLb(mapUrl(section,pop),pop,section);
  return d;
}
// a dark, elevated map panel that floats on the glass card — no text overlaps the plot
function mapPanel(section,pop){
  const d=document.createElement('div'); d.className='mapstage scan focus';
  const img=new Image();
  img.onload=()=>{img.classList.add('loaded'); setTimeout(()=>d.classList.remove('focus'),120);};
  img.onerror=()=>{const fb=mapFallback(section,pop); if(fb&&img.src!==fb){img.src=fb;} else {d.classList.remove('scan','focus'); img.classList.add('loaded');}};
  img.src=mapUrl(section,pop); d.appendChild(img);
  const zh=document.createElement('div'); zh.className='mapzoom'; zh.textContent='◱ zoom'; d.appendChild(zh);
  d.onclick=()=>openLb(mapUrl(section,pop),pop,section);
  return d;
}
async function flashMap(g, section, pop, hero){
  const c=card(g,'Spatial effect map  ·  '+pop,'◱',0);
  const layout=document.createElement('div'); layout.className='maplayout'; c.appendChild(layout);
  const other=section==='HM11'?'T11':'HM11';
  const canCompare = hero && mapExists(other,pop);
  let split=false;
  function build(){
    layout.className='maplayout'+(split?' compare':'');
    layout.innerHTML='';
    if(!split){
      const wrap=document.createElement('div'); wrap.className='mapstagewrap'; wrap.appendChild(mapPanel(section,pop));
      const side=document.createElement('div'); side.className='mapside';
      side.innerHTML=`<div class="ms-sec">${secName(section)} · ${section}</div>
        <div class="ms-pop">${pop}</div>
        <div class="ms-desc">Predicted knockout effect across the tissue. <b class="blue">Blue = suppression</b>, <b class="red">red = activation</b>.</div>
        <div class="ms-scale"><i></i><span>spatial spots · Visium</span></div>`;
      if(canCompare){const btn=document.createElement('button');btn.className='cmpbtn';btn.textContent='⇄ compare sections';btn.onclick=()=>{split=true;build();};side.appendChild(btn);}
      const hint=document.createElement('div'); hint.className='ms-hint'; hint.textContent='◱ click the map to zoom & pan'; side.appendChild(hint);
      layout.appendChild(wrap); layout.appendChild(side);
    } else {
      const head=document.createElement('div'); head.className='mapheader';
      head.innerHTML=`<div class="mh-pop">${pop}</div><div class="mh-sub">${secName(section)} vs ${secName(other)} — predicted knockout effect · <b class="blue">blue = suppression</b>, <b class="red">red = activation</b></div>`;
      const wrap=document.createElement('div'); wrap.className='mapstagewrap two';
      [[section,secName(section)],[other,secName(other)]].forEach(([s,nm],i)=>{const w=document.createElement('div');w.className='twin';w.style.animationDelay=(0.14+i*0.14)+'s';
        w.innerHTML=`<div class="tlab">${nm}</div>`; w.appendChild(mapPanel(s,pop)); wrap.appendChild(w);});
      const btn=document.createElement('button'); btn.className='cmpbtn single-btn'; btn.textContent='◱ back to single view'; btn.onclick=()=>{split=false;build();};
      layout.appendChild(head); layout.appendChild(wrap); layout.appendChild(btn);
    }
  }
  build();
  await sleep(hero?1100:900);
  return c;
}

async function renderGallery(g, maps){
  const c=card(g,'Spatial atlas  ·  all target populations','◱',0);
  const grid=document.createElement('div');grid.className='mapgrid';c.appendChild(grid);
  for(let i=0;i<maps.length;i++){
    const m=maps[i];
    const cell=document.createElement('div');cell.className='specimen mini';cell.style.animationDelay=(i*.1)+'s';
    const url=mapUrl(m.section,m.population);
    cell.onclick=()=>openLb(mapUrl(m.section,m.population),m.population,m.section);
    const img=new Image();img.onload=()=>img.classList.add('loaded');
    img.onerror=()=>{const fb=mapFallback(m.section,m.population); if(fb&&img.src!==fb)img.src=fb;};
    img.src=url;cell.appendChild(img);
    const cap=document.createElement('div');cap.className='cap mini';cap.innerHTML=`<b>${m.population}</b>`;cell.appendChild(cap);
    grid.appendChild(cell);
    await sleep(140);
  }
  await sleep(300);
  return c;
}

// ===== interactive coupling network \u2014 our logo, our strongest point =====
async function renderNetwork(g, section, pop){
  const c=card(g,'Coupling network \u2014 '+ (pop||'') ,'\u2b21',0);
  if(!NETDATA||!NETDATA.sections||!NETDATA.sections[section]){
    const p=document.createElement('div');p.style.cssText='color:var(--mut);font-size:12.5px';
    p.textContent='Network data not loaded (run perturbo_network_export.py \u2192 network.json).';
    c.appendChild(p);await sleep(300);return c;
  }
  const net=NETDATA.sections[section];
  const rep=net.report||{};
  const themes=[...new Set((net.nodes||[]).map(n=>n.theme).filter(t=>t&&t!=='other'))];
  const themeCol={fibrotic:'#5B9DFF',immune:'#5EE0B0',epithelial:'#B99BFF',metabolic:'#57C7E8',other:'#7C8896'};
  const bar=document.createElement('div');bar.className='netbar';
  bar.innerHTML=`
    <div class="netgrp"><span class="ntl">view</span>
      <button class="nt active" data-view="und">undirected \u00b7 signed</button>
      <button class="nt" data-view="dir">directed \u00b7 pathway</button>
      <button class="nt" data-view="both">both</button></div>
    <div class="netgrp"><span class="ntl">layout</span>
      <button class="nt lay active" data-lay="force">force</button>
      <button class="nt lay" data-lay="circle">circle</button>
      <button class="nt lay" data-lay="grid">grid</button></div>
    <div class="netgrp"><span class="ntl">edges</span>
      <button class="nt sg active" data-sg="all">all</button>
      <button class="nt sg" data-sg="pos">supportive</button>
      <button class="nt sg" data-sg="neg">antagonistic</button></div>
    <input class="netsearch" id="netsearch" placeholder="find gene\u2026">`;
  c.appendChild(bar);
  // theme filter + focus indicator row
  const trow=document.createElement('div');trow.className='netbar';
  trow.innerHTML=`<div class="netgrp"><span class="ntl">programme</span>
     <span class="themechips" id="themechips">
       <span class="themechip active" data-theme="all">all</span>
       ${themes.map(t=>`<span class="themechip" data-theme="${t}"><i style="background:${themeCol[t]||'#7C8896'}"></i>${t}</span>`).join('')}
     </span>
     <span class="netfocus" id="netfocus">focused on <b id="focusgene"></b> \u00b7 <span id="focusclear" style="cursor:pointer;color:var(--mut)">clear \u2715</span></span></div>`;
  c.appendChild(trow);
  const wrap=document.createElement('div');wrap.className='netwrap';
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 900 560');svg.classList.add('netsvg');wrap.appendChild(svg);c.appendChild(wrap);
  const leg=document.createElement('div');leg.className='netfoot';
  let conc = rep.signed_overlap
    ? `couplings encode <b>undirected association</b> (symmetric model) \u2014 directional regulation is supplied by literature priors, not learned. <span style="color:var(--mut)">signed-edge sign match ${rep.concordant}/${rep.signed_overlap}, ~chance, as expected.</span>`
    : 'pathway arrows show literature direction; coupling sign shown by line colour';
  leg.innerHTML=`<div class="netleg">
      <div class="lgroup"><span class="lgt">nodes</span>
        <span class="lg"><i class="dot" style="background:#F5923E;box-shadow:0 0 7px rgba(245,146,62,.8)"></i>hub</span>
        <span class="lg"><i class="dot" style="border:2px solid #B99BFF;background:transparent"></i>upstream</span>
        <span class="lg"><i class="szdot"></i>size = degree</span></div>
      <div class="lgroup"><span class="lgt">couplings</span>
        <span class="lg"><i class="ln" style="background:#FFB454"></i>supportive</span>
        <span class="lg"><i class="ln" style="background:#57C7E8"></i>antagonistic</span></div>
      <div class="lgroup"><span class="lgt">overlay</span>
        <span class="lg"><i class="ln arw" style="background:#B99BFF"></i>pathway direction</span></div>
    </div>
    <div class="netnote">${conc} <span style="color:var(--mut)">\u00b7 priors: ${rep.prior_source||'curated'} \u00b7 click a gene to focus its neighbourhood</span></div>`;
  c.appendChild(leg);

  const st={view:'und',lay:'force',sign:'all',search:'',theme:'all',focus:null,section};
  st.onFocus=()=>{const f=document.getElementById('netfocus');if(st.focus){document.getElementById('focusgene').textContent=st.focus;f.classList.add('show');}else f.classList.remove('show');};
  const redraw=()=>simNetwork(svg,net,st);
  bar.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{bar.querySelectorAll('[data-view]').forEach(x=>x.classList.remove('active'));b.classList.add('active');st.view=b.dataset.view;redraw();});
  bar.querySelectorAll('[data-lay]').forEach(b=>b.onclick=()=>{bar.querySelectorAll('[data-lay]').forEach(x=>x.classList.remove('active'));b.classList.add('active');st.lay=b.dataset.lay;redraw();});
  bar.querySelectorAll('[data-sg]').forEach(b=>b.onclick=()=>{bar.querySelectorAll('[data-sg]').forEach(x=>x.classList.remove('active'));b.classList.add('active');st.sign=b.dataset.sg;redraw();});
  trow.querySelectorAll('[data-theme]').forEach(b=>b.onclick=()=>{trow.querySelectorAll('[data-theme]').forEach(x=>x.classList.remove('active'));b.classList.add('active');st.theme=b.dataset.theme;redraw();});
  document.getElementById('focusclear').onclick=()=>{st.focus=null;st.onFocus();redraw();};
  bar.querySelector('#netsearch').addEventListener('input',e=>{st.search=e.target.value.trim().toUpperCase();redraw();});
  await sleep(120);redraw();await sleep(500);
  return c;
}

function simNetwork(svg, net, st){
  const W=900,H=560,NS='http://www.w3.org/2000/svg';svg.innerHTML='';try{hideTip();}catch(e){}
  let ue=(st.view==='dir')?[]:(net.undirected_edges||[]).slice();
  let de=(st.view==='und')?[]:(net.directed_edges||[]).slice();
  if(st.sign==='pos')ue=ue.filter(e=>e.sign==='pos');
  if(st.sign==='neg')ue=ue.filter(e=>e.sign==='neg');
  const involved=new Set();ue.forEach(e=>{involved.add(e.source);involved.add(e.target);});de.forEach(e=>{involved.add(e.source);involved.add(e.target);});
  let nodes=net.nodes.filter(n=>involved.has(n.id));
  if(st.view!=='dir')net.nodes.filter(n=>n.is_hub).slice(0,20).forEach(n=>{if(!involved.has(n.id))nodes.push(n);});
  if(st.theme!=='all')nodes=nodes.filter(n=>n.theme===st.theme);
  // focus: restrict to the clicked gene's ego network
  if(st.focus){
    const nb=new Set([st.focus]);
    [...ue,...de].forEach(e=>{if(e.source===st.focus)nb.add(e.target);if(e.target===st.focus)nb.add(e.source);});
    nodes=nodes.filter(n=>nb.has(n.id));
  }
  nodes=nodes.slice(0,55);
  const idset=new Set(nodes.map(n=>n.id));
  ue=ue.filter(e=>idset.has(e.source)&&idset.has(e.target));
  de=de.filter(e=>idset.has(e.source)&&idset.has(e.target));
  const maxdeg=Math.max(...nodes.map(n=>n.degree||1),1);
  const adj={};nodes.forEach(n=>adj[n.id]=new Set());
  [...ue,...de].forEach(e=>{if(adj[e.source])adj[e.source].add(e.target);if(adj[e.target])adj[e.target].add(e.source);});
  const P={};
  if(st.lay==='circle'){
    nodes.forEach((n,i)=>{const a=i/nodes.length*Math.PI*2;P[n.id]={x:W/2+Math.cos(a)*Math.min(W,H)*0.4,y:H/2+Math.sin(a)*Math.min(W,H)*0.4};});
  } else if(st.lay==='grid'){
    const cols=Math.ceil(Math.sqrt(nodes.length));const cw=W/(cols+1),rh=H/(Math.ceil(nodes.length/cols)+1);
    nodes.forEach((n,i)=>{P[n.id]={x:cw*((i%cols)+1),y:rh*(Math.floor(i/cols)+1)};});
  } else {
    const es=[...ue,...de];
    nodes.forEach((n,i)=>{const a=i/nodes.length*Math.PI*2;P[n.id]={x:W/2+Math.cos(a)*230,y:H/2+Math.sin(a)*180,vx:0,vy:0};});
    const REP=8000,LINK=90,SPR=0.02;
    for(let it=0;it<340;it++){
      for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
        const A=P[nodes[i].id],B=P[nodes[j].id];let dx=A.x-B.x,dy=A.y-B.y,d2=dx*dx+dy*dy||1,d=Math.sqrt(d2),f=REP/d2;
        A.vx+=dx/d*f;A.vy+=dy/d*f;B.vx-=dx/d*f;B.vy-=dy/d*f;}
      es.forEach(e=>{const A=P[e.source],B=P[e.target];if(!A||!B)return;let dx=B.x-A.x,dy=B.y-A.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-LINK)*SPR;A.vx+=dx/d*f;A.vy+=dy/d*f;B.vx-=dx/d*f;B.vy-=dy/d*f;});
      nodes.forEach(n=>{const p=P[n.id];p.vx+=(W/2-p.x)*0.004;p.vy+=(H/2-p.y)*0.004;p.x+=p.vx*=0.86;p.y+=p.vy*=0.86;p.x=Math.max(40,Math.min(W-40,p.x));p.y=Math.max(40,Math.min(H-40,p.y));});
    }
  }
  const defs=document.createElementNS(NS,'defs');
  defs.innerHTML='<marker id="arrp" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M1 1L9 5L1 9" fill="#B99BFF"/></marker>'
    +'<filter id="nglow" x="-70%" y="-70%" width="240%" height="240%"><feGaussianBlur stdDeviation="4.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
  svg.appendChild(defs);
  const edgeEls=[];
  function epath(A,B){const dx=B.x-A.x,dy=B.y-A.y,len=Math.hypot(dx,dy)||1,off=Math.min(46,len*0.14);
    return `M${A.x} ${A.y} Q ${(A.x+B.x)/2-dy/len*off} ${(A.y+B.y)/2+dx/len*off} ${B.x} ${B.y}`;}
  ue.forEach(e=>{const A=P[e.source],B=P[e.target];if(!A||!B)return;const pn=document.createElementNS(NS,'path');
    pn.setAttribute('d',epath(A,B));pn.setAttribute('fill','none');pn.classList.add('netedge');
    pn.setAttribute('stroke',e.sign==='neg'?'#57C7E8':'#FFB454');pn.setAttribute('stroke-width',Math.min(3.6,0.8+Math.abs(e.weight)*3.5));
    pn.setAttribute('stroke-opacity','0.4');svg.appendChild(pn);edgeEls.push({el:pn,s:e.source,t:e.target});});
  de.forEach(e=>{const A=P[e.source],B=P[e.target];if(!A||!B)return;const pn=document.createElementNS(NS,'path');
    pn.setAttribute('d',epath(A,B));pn.setAttribute('fill','none');pn.classList.add('netedge');
    pn.setAttribute('stroke','#B99BFF');pn.setAttribute('stroke-width','2');pn.setAttribute('stroke-opacity','0.85');
    pn.setAttribute('marker-end','url(#arrp)');svg.appendChild(pn);edgeEls.push({el:pn,s:e.source,t:e.target,dir:1});});
  const nodeEls={};
  const updateEdges=()=>edgeEls.forEach(l=>{const A=P[l.s],B=P[l.t];if(A&&B)l.el.setAttribute('d',epath(A,B));});
  nodes.forEach((n,idx)=>{const p=P[n.id];const r=7+((n.degree||1)/maxdeg)*16;
    const gr=document.createElementNS(NS,'g');gr.setAttribute('transform',`translate(${p.x},${p.y})`);gr.classList.add('netnode');
    gr.style.opacity='0';gr.style.transition='opacity .45s ease';setTimeout(()=>{gr.style.opacity='1';},20+idx*10);
    const match=st.search&&n.id.toUpperCase().includes(st.search);
    if(n.is_upstream){const ring=document.createElementNS(NS,'circle');ring.setAttribute('r',r+4);ring.setAttribute('fill','none');ring.setAttribute('stroke','#B99BFF');ring.setAttribute('stroke-width','1.8');gr.appendChild(ring);}
    const cir=document.createElementNS(NS,'circle');cir.setAttribute('r',r);
    cir.setAttribute('fill',n.is_hub?'#F5923E':'#57C7E8');cir.setAttribute('stroke',match?'#fff':(n.is_hub?'#FFC48A':'#8FD8EE'));cir.setAttribute('stroke-width',match?'2.6':'1.2');
    if(n.is_hub||n.id===st.focus)cir.setAttribute('filter','url(#nglow)');
    gr.appendChild(cir);
    const tx=document.createElementNS(NS,'text');tx.setAttribute('y',r+13);tx.setAttribute('text-anchor','middle');
    tx.setAttribute('fill', n.is_hub?'#CFE0FF':'#A7B6D0');tx.setAttribute('font-size', n.is_hub?'10.5':'9.5');tx.setAttribute('font-family','IBM Plex Mono, monospace');tx.textContent=n.id;gr.appendChild(tx);
    gr.style.cursor='pointer';
    nodeEls[n.id]={gr,cir};
    // hover: neighbour-highlight + rich tooltip
    gr.addEventListener('mouseenter',ev=>{
      const keep=adj[n.id]||new Set();
      nodes.forEach(m=>{if(m.id!==n.id&&!keep.has(m.id))nodeEls[m.id].gr.classList.add('net-dim');});
      edgeEls.forEach(l=>{if(l.s!==n.id&&l.t!==n.id)l.el.classList.add('net-dimline');});
      showNodeTip(n,st.section,keep.size);moveTip(ev);
    });
    gr.addEventListener('mousemove',ev=>{if(document.getElementById('gtip').classList.contains('show'))moveTip(ev);});
    gr.addEventListener('mouseleave',()=>{Object.values(nodeEls).forEach(o=>o.gr.classList.remove('net-dim'));edgeEls.forEach(l=>l.el.classList.remove('net-dimline'));hideTip();});
    // click: focus this gene's neighbourhood
    let moved=false;
    gr.addEventListener('mousedown',ev=>{ev.stopPropagation();moved=false;const R=svg.getBoundingClientRect();
      const mv=e=>{moved=true;const x=(e.clientX-R.left)/R.width*W,y=(e.clientY-R.top)/R.height*H;p.x=x;p.y=y;gr.setAttribute('transform',`translate(${x},${y})`);updateEdges();};
      const up=()=>{window.removeEventListener('mousemove',mv);window.removeEventListener('mouseup',up);
        if(!moved){st.focus=(st.focus===n.id?null:n.id);if(st.onFocus)st.onFocus();simNetwork(svg,net,st);}};
      window.addEventListener('mousemove',mv);window.addEventListener('mouseup',up);});
    svg.appendChild(gr);});
}
function showNodeTip(n,section,conn){
  const badges=[];if(n.is_hub)badges.push('<span class="gt-b hub">network hub</span>');if(n.is_upstream)badges.push('<span class="gt-b up">pathway-upstream</span>');
  const rows=[`<div class="gt-row"><span>coupling degree</span><b>${n.degree}</b></div>`];
  if(n.pos_links!=null)rows.push(`<div class="gt-row"><span>signed links</span><b>+${n.pos_links} / \u2212${n.neg_links}</b></div>`);
  if(n.theme&&n.theme!=='other')rows.push(`<div class="gt-row"><span>programme</span><b>${n.theme}</b></div>`);
  if(conn!=null)rows.push(`<div class="gt-row"><span>neighbours in view</span><b>${conn}</b></div>`);
  const tip=document.getElementById('gtip'); tip.style.pointerEvents='none';
  tip.innerHTML=`<div class="gt-name">${n.id}</div>${badges.length?`<div class="gt-badges">${badges.join('')}</div>`:''}${rows.join('')}
    <div class="gt-desc">Node size = coupling degree (data-derived centrality). Click to focus its neighbourhood. Predicted association, not identified causation.</div>`;
  tip.classList.add('show');
}

function renderTable(g, results){
  const c=card(g,'Proposed targets — triage','01',0);
  let rows=(results||[]).map((r,i)=>{
    const vc=r.verdict&&r.verdict.startsWith('PRIOR')?'v-pri':(r.profiled?'v-con':'v-no');
    return `<tr style="animation-delay:${.05+i*.08}s"><td style="font-weight:600">${r.gene}</td>
      <td><span class="verdict ${vc}">${r.verdict||'—'}</span></td>
      <td class="num ${r.predicted_tumour_effect<0?'neg':'pos'}">${r.predicted_tumour_effect!=null?fx(r.predicted_tumour_effect):'—'}</td>
      <td style="color:var(--sub)">${r.population||'—'}</td></tr>`;}).join('');
  c.innerHTML+=`<table><thead><tr><th>Gene</th><th>Verdict</th><th>Effect</th><th>Population</th></tr></thead><tbody>${rows}</tbody></table>`;
  const d=document.createElement('div');d.className='disc';
  d.innerHTML='<span>Predicted, causal-given-model — prioritized in-silico hypotheses requiring experimental validation.</span>';
  c.appendChild(d);
  return c;
}

// lightbox with zoom+pan
let zscale=1,zx=0,zy=0,drag=false,sx,sy;
function openLb(src,title,sub){
  const lb=document.getElementById('lb'),img=document.getElementById('lbimg');
  img.src=src;document.getElementById('lbtitle').textContent=title;
  document.getElementById('lbsub').textContent=(sub==='HM11'?'liver metastasis':(sub==='T11'?'primary tumour':''));
  zscale=1;zx=0;zy=0;applyZoom();lb.classList.add('show');
}
function closeLb(e){if(e)e.stopPropagation();document.getElementById('lb').classList.remove('show');}
function applyZoom(){document.getElementById('lbimg').style.transform=`translate(${zx}px,${zy}px) scale(${zscale})`;}
function showMap(section,pop){openLb(mapUrl(section,pop),pop,section);}
document.getElementById('lb').addEventListener('click',e=>{if(e.target.id==='lb')closeLb();});
document.getElementById('lbimg').addEventListener('wheel',e=>{e.preventDefault();
  zscale=Math.min(5,Math.max(1,zscale+(e.deltaY<0?.25:-.25)));if(zscale<=1){zx=0;zy=0;}applyZoom();});
document.getElementById('lbimg').addEventListener('mousedown',e=>{if(zscale>1){drag=true;sx=e.clientX-zx;sy=e.clientY-zy;e.target.style.cursor='grabbing';}});
window.addEventListener('mousemove',e=>{if(drag){zx=e.clientX-sx;zy=e.clientY-sy;applyZoom();}});
window.addEventListener('mouseup',()=>{drag=false;const i=document.getElementById('lbimg');if(i)i.style.cursor='grab';});

// direct value labels at bar tips (Chart.js inline plugin — no external dep)
const barLabels={id:'barLabels',afterDatasetsDraw(c){
  const meta=c.getDatasetMeta(0); if(!meta||!meta.data) return; const raw=c.data.datasets[0].data;
  const horiz=c.options.indexAxis==='y';
  c.ctx.save(); c.ctx.font='600 11px "IBM Plex Mono", monospace'; c.ctx.fillStyle=document.body.dataset.mode==='light'?'#42536F':'#A7B6D0';
  meta.data.forEach((el,i)=>{const v=raw[i]; const t=(v>0?'+':'')+v;
    if(horiz){ c.ctx.textBaseline='middle'; c.ctx.textAlign=v<0?'right':'left'; c.ctx.fillText(t, el.x+(v<0?-8:8), el.y); }
    else { c.ctx.textAlign='center'; c.ctx.textBaseline=v<0?'top':'bottom'; c.ctx.fillText(t, el.x, el.y+(v<0?6:-6)); }
  }); c.ctx.restore();
}};
function niceStep(x){x=Math.abs(x)||1;const e=Math.floor(Math.log10(x));const f=x/Math.pow(10,e);const nf=f<1.5?1:f<3?2:f<7?5:10;return nf*Math.pow(10,e);}
// automatic axis range — keep 0 on the scale + ~2 tick-steps of headroom on the side opposite the bars.
// data-driven (never hardcoded): all-negative → positive headroom; all-positive → negative headroom.
function axisRange(data){
  const dmin=Math.min(...data), dmax=Math.max(...data);
  const allNeg=dmax<=1e-12, allPos=dmin>=-1e-12;
  const span=(Math.max(dmax,0)-Math.min(dmin,0))||Math.abs(dmax||dmin||1);
  const step=niceStep(span/4);
  let min,max;
  if(allNeg){ max=2*step; min=Math.floor(dmin/step)*step; }
  else if(allPos){ min=-2*step; max=Math.ceil(dmax/step)*step; }
  else { min=Math.floor(dmin/step)*step; max=Math.ceil(dmax/step)*step; }
  return {min,max,step};
}
function drawBar(id,labels,data,colors){
  const ctx=document.getElementById(id);if(!ctx)return;
  const horiz=labels.length>5;
  // emphasis: the strongest-magnitude population is the story -> amber; the rest are context -> teal
  const maxi=data.reduce((m,v,i)=>Math.abs(v)>Math.abs(data[m])?i:m,0);
  const bg=data.map((v,i)=>i===maxi?'#5B9DFF':'#57C7E8');
  // scriptable colors so chart.update() recolours on light/dark toggle (fixes Act-1 in light mode)
  const L=()=>document.body.dataset.mode==='light';
  const grid=()=>L()?'rgba(120,150,190,.28)':'rgba(28,42,68,.6)';
  const tick=()=>L()?'#42536F':'#A7B6D0', tickB=()=>L()?'#0B1B33':'#EAF1FB', axis=()=>L()?'#CBD8EC':'#1C2A44';
  const R=axisRange(data), vk=horiz?'x':'y';   // value axis gets the auto range
  const scales={
    x:{grid:{color:grid},ticks:{color:tick,font:{size:11}},border:{color:axis}},
    y:{grid:{color:grid},ticks:{color:tickB,font:{size:12}},border:{color:axis}}
  };
  scales[vk].min=R.min; scales[vk].max=R.max; scales[vk].ticks.stepSize=R.step;
  CHARTS.push(new Chart(ctx,{type:'bar',data:{labels,datasets:[{data,backgroundColor:bg,borderRadius:6,
    barThickness:'flex',maxBarThickness:38}]},
    plugins:[barLabels],
    options:{indexAxis:horiz?'y':'x',responsive:true,maintainAspectRatio:false,
      layout:{padding:{top:16,right:horiz?42:8}},
      animation:{duration:1000,easing:'easeOutQuart',delay:(c)=>c.dataIndex*80},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#101C31',borderColor:'#293A5A',borderWidth:1,
        titleColor:'#EAF1FB',bodyColor:'#A7B6D0',padding:11,cornerRadius:8,
        callbacks:{label:(c)=>` ${c.raw} ×10⁻³`}}},
      scales}}));
}

// ===== signature primary-vs-metastasis comparison =====
function isCompareChart(a){ return (a.labels||[]).length===2 && (a.labels||[]).some(l=>/primary|metasta|liver|tumour|tumor/i.test(l)); }
// suppression (negative) = blue→red · positive = blue→green
function cmpGrad(v){return v<0?'linear-gradient(90deg,#2E6FE0,#E5484D)':'linear-gradient(90deg,#2E6FE0,#2FA968)';}
async function renderCompare(g, a){
  const c=card(g,'Predicted effect across samples','01',0);
  const data=(a.data||[]).map(Number), labels=a.labels||[];
  const mags=data.map(Math.abs), maxm=Math.max(...mags,1e-9), maxi=mags.indexOf(Math.max(...mags)), minm=Math.min(...mags);
  const ratio=(minm>0)?maxm/minm:null;
  const wrap=document.createElement('div'); wrap.className='cmp';
  const rows=labels.map((lb,i)=>{const pct=Math.max(3,Math.round(mags[i]/maxm*100)), real=data[i]/1000;
    const grad=cmpGrad(real);
    return `<div class="row"><div class="lab"><b>${lb}</b>myCAF</div>
      <div class="track"><div class="fill" data-pct="${pct}" style="background:${grad}"></div></div>
      <div class="val">${real<0?'−':'+'}${Math.abs(real).toFixed(4)}</div></div>`;}).join('');
  const rtxt=ratio?`<div class="ratio"><span class="big">≈${ratio<10?ratio.toFixed(1):Math.round(ratio)}×</span>
      <span class="txt">stronger predicted suppression in the <b>${labels[maxi]}</b>. myCAF's canonical fibrotic programme (BGN, CCN2, collagens) is far more active in the primary — yet the signal persists into the metastasis, which is what makes it a durable target. Both sites are clean cross-compartment signals.</span></div>`:'';
  wrap.innerHTML=rows+rtxt; c.appendChild(wrap);
  await sleep(80);
  wrap.querySelectorAll('.fill').forEach((f,i)=>setTimeout(()=>{f.style.width=f.dataset.pct+'%';},100+i*180));
  const bigEl=wrap.querySelector('.ratio .big'); if(bigEl&&ratio) setTimeout(()=>animRatio(bigEl,ratio),260);
  await sleep(1200);
  return c;
}

document.getElementById('send').onclick=()=>{const q=document.getElementById('q').value.trim();if(q)ask(q);};
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter'){const q=e.target.value.trim();if(q)ask(q);}});
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>ask(c.dataset.q));

// ===== toast =====
let toastT=null;
function toast(msg,warn){const el=document.getElementById('toast');el.textContent=msg;el.classList.toggle('warn',!!warn);
  el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),3200);}

// ===== endpoint config modal =====
const cfg=document.getElementById('cfg');
function openCfg(){document.getElementById('cfgurl').value=API;document.getElementById('cfgdemo').checked=FORCE_DEMO;
  document.getElementById('cfgmsg').textContent='';document.getElementById('cfgmsg').className='cfgmsg';cfg.classList.add('show');
  setTimeout(()=>document.getElementById('cfgurl').focus(),100);}
function closeCfg(){cfg.classList.remove('show');}
statuspill().onclick=openCfg;
cfg.addEventListener('click',e=>{if(e.target===cfg)closeCfg();});

async function testUrl(url){
  const m=document.getElementById('cfgmsg');m.className='cfgmsg';m.textContent='Testing…';
  try{const c=new AbortController();const to=setTimeout(()=>c.abort(),6000);
    const r=await fetch(url.replace(/\/$/,'')+'/health',{cache:'no-store',signal:c.signal});clearTimeout(to);
    const d=await r.json();
    if(d.ok){m.className='cfgmsg ok';m.textContent='Connected — sections: '+(d.sections||[]).join(', ')+(d.has_llm?' · LLM ready':'');return true;}
    m.className='cfgmsg err';m.textContent='Reachable but not healthy.';return false;
  }catch(e){m.className='cfgmsg err';m.textContent='Unreachable — the built-in demo will be used.';return false;}
}
document.getElementById('cfgtest').onclick=()=>testUrl(document.getElementById('cfgurl').value.trim());
document.getElementById('cfgsave').onclick=async()=>{
  API=document.getElementById('cfgurl').value.trim()||DEFAULT_API;
  FORCE_DEMO=document.getElementById('cfgdemo').checked;
  localStorage.setItem('perturbo_api',API);
  localStorage.setItem('perturbo_force_demo',FORCE_DEMO?'1':'0');
  await checkHealth();
  closeCfg();
  toast(LIVE?'Connected to agent — running live.':(FORCE_DEMO?'Demo mode on — using the built-in saved analysis.':'Agent unreachable — using the built-in demo.'), !LIVE);
};

// ===== chat-panel resize handle (item 10) =====
(function(){
  const rz=document.getElementById('resizer'), app=document.querySelector('.app'); if(!rz)return;
  let dragging=false;
  rz.addEventListener('mousedown',e=>{dragging=true;rz.classList.add('drag');document.body.style.userSelect='none';e.preventDefault();});
  window.addEventListener('mousemove',e=>{if(!dragging)return;
    const w=Math.max(300,Math.min(640,e.clientX-app.getBoundingClientRect().left));
    app.style.setProperty('--railw',w+'px'); app.dataset.railw=w;});
  window.addEventListener('mouseup',()=>{if(!dragging)return;dragging=false;rz.classList.remove('drag');document.body.style.userSelect='';
    if(app.dataset.railw)localStorage.setItem('perturbo_railw',app.dataset.railw);});
})();

// ===== light / dark toggle (item 11) =====
// apple-style light/dark switch (checked = dark)
const modetoggle=document.getElementById('modetoggle');
function syncMode(){ modetoggle.checked = (document.body.dataset.mode!=='light'); }
syncMode();
modetoggle.addEventListener('change',()=>{
  if(modetoggle.checked){ delete document.body.dataset.mode; localStorage.setItem('perturbo_mode','dark'); }
  else { document.body.dataset.mode='light'; localStorage.setItem('perturbo_mode','light'); }
  CHARTS.forEach(c=>{try{c.update();}catch(e){}});
});

// ===== credits (item 12) =====
const TEAM=[
  {n:'Team member',r:'ML / agent engineering'},
  {n:'Team member',r:'Spatial-omics / method'},
  {n:'Team member',r:'Front-end / product'},
  {n:'Team member',r:'Pitch / biology'}
];
document.getElementById('cteam').innerHTML=TEAM.map(m=>`<div class="cmember"><div class="cav">${(m.n[0]||'P')}</div><div><div class="cn">${m.n}</div><div class="cr">${m.r}</div></div></div>`).join('');
const cred=document.getElementById('cred');
function openCred(){cred.classList.add('show');}
function closeCred(){cred.classList.remove('show');}
document.getElementById('creditsbtn').onclick=openCred;
cred.addEventListener('click',e=>{if(e.target===cred)closeCred();});

// ===== export (T10) =====
const exp=document.getElementById('exp');
function openExport(){ if(!activeId){toast('Run an analysis first, then export.',true);return;}
  document.getElementById('expmsg').textContent=''; exp.classList.add('show'); }
function closeExport(){exp.classList.remove('show');}
document.getElementById('exportbtn').onclick=openExport;
exp.addEventListener('click',e=>{if(e.target===exp)closeExport();});
exp.querySelectorAll('.expcard').forEach(b=>b.onclick=()=>doExport(b.dataset.fmt));

function analysisData(a){
  const steps=a.steps||[]; const targets=[]; let table=null,compare=null,chart=null,section='',summary='',knowledge=null;
  steps.forEach(s=>{const ac=s.action||{};
    if(ac.type==='target'&&ac.target){targets.push(ac.target); if(ac.target._section)section=ac.target._section;}
    if(ac.type==='table') table=ac.results;
    if(ac.type==='chart'){ if(isCompareChart(ac)) compare={labels:ac.labels,data:ac.data}; else chart={labels:ac.labels,data:ac.data}; }
    if(ac.type==='knowledge'&&ac.knowledge) knowledge=ac.knowledge;
    if(ac.type==='summary'&&s.say) summary=s.say;   // the agent's own conclusion
  });
  if(!section&&compare) section=/primary/i.test((a.title||'')+(a.query||''))?'T11':'HM11';
  return {title:a.title,kicker:a.kicker,query:a.query,section,targets,table,compare,chart,summary,knowledge};
}
function csvCell(v){v=(v==null?'':String(v));return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
function toCSV(d){let rows=[];
  if(d.targets&&d.targets.length){rows.push(['rank','population','predicted_tumour_effect','n_spots','clean_cross_compartment','well_powered','top_genes']);
    d.targets.forEach(t=>rows.push([t.rank,t.population,t.predicted_tumour_effect,t.n_spots,t.clean_cross_compartment,t.well_powered,(t.top_target_genes||[]).map(g=>g.gene).join(' ')]));}
  else if(d.table){rows.push(['gene','verdict','predicted_tumour_effect','population']);
    d.table.forEach(r=>rows.push([r.gene,r.verdict,r.predicted_tumour_effect,r.population]));}
  else if(d.compare){rows.push(['sample','predicted_effect_x1e-3']);d.compare.labels.forEach((l,i)=>rows.push([l,d.compare.data[i]]));}
  return rows.map(r=>r.map(csvCell).join(',')).join('\n');
}
function download(fn,content,type){const b=new Blob([content],{type});const u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download=fn;a.click();setTimeout(()=>URL.revokeObjectURL(u),2000);}
function printReport(){
  const w=window.open('','_blank'); if(!w){toast('Allow pop-ups to export a PDF.',true);return;}
  const doc=buildFullReport().replace('</body>','<scr'+'ipt>onload=function(){setTimeout(function(){print();},450);}</scr'+'ipt></body>');
  w.document.write(doc); w.document.close();
}
function exportPNG(a,base,m){
  const el=document.querySelector('.analysis[data-id="'+a.id+'"]'); if(!el){toast('Nothing to capture.',true);return;}
  if(typeof html2canvas==='undefined'){m.className='cfgmsg err';m.textContent='Image export needs an internet connection (html2canvas).';return;}
  m.className='cfgmsg';m.textContent='Rendering image…';
  html2canvas(el,{backgroundColor:getComputedStyle(document.body).backgroundColor,scale:2,useCORS:true}).then(cv=>{
    cv.toBlob(b=>{if(!b){m.className='cfgmsg err';m.textContent='Image export failed.';return;}
      const u=URL.createObjectURL(b);const l=document.createElement('a');l.href=u;l.download=base+'.png';l.click();
      setTimeout(()=>URL.revokeObjectURL(u),2000);m.className='cfgmsg ok';m.textContent='PNG downloaded.';});
  }).catch(()=>{m.className='cfgmsg err';m.textContent='Image export failed.';});
}
function doExport(fmt){
  const m=document.getElementById('expmsg');
  if(!ANALYSES.length){toast('Run an analysis first.',true);return;}
  try{
    if(fmt==='html'){ download('perturbo_report.html', buildFullReport(), 'text/html;charset=utf-8');
      m.className='cfgmsg ok';m.textContent='Full HTML report downloaded ('+ANALYSES.length+' analysis'+(ANALYSES.length===1?'':'es')+'). Open it in any browser.'; }
    else if(fmt==='pdf'){ printReport();
      m.className='cfgmsg ok';m.textContent='Opened the report — choose “Save as PDF” in the print dialog.'; }
  }catch(e){ m.className='cfgmsg err';m.textContent='Export failed: '+e.message; }
}

// ===== full self-contained HTML report (FastQC-style) — all analyses this session =====
function collectMaps(a){const seen={},out=[];(a.steps||[]).forEach(s=>{const ac=s.action||{};
  if(ac.type==='map'){const k=ac.section+'|'+ac.population;if(!seen[k]){seen[k]=1;out.push({section:ac.section,population:ac.population});}}
  if(ac.type==='gallery'){(ac.maps||[]).forEach(m=>{const k=m.section+'|'+m.population;if(!seen[k]){seen[k]=1;out.push(m);}});}});return out;}
function mapDataURI(section,pop){return (DEMO.maps&&DEMO.maps[section+'|'+pop])||(LIVE?`${API}/map?section=${encodeURIComponent(section)}&population=${encodeURIComponent(pop)}`:'');}
function networkHubs(section){try{const s=NETDATA&&NETDATA.sections&&NETDATA.sections[section];if(!s)return[];
  return (s.nodes||[]).filter(n=>n.is_hub).sort((x,y)=>y.degree-x.degree).slice(0,10);}catch(e){return[];}}
function mdToHtml(t){
  const lines=(t||'').split('\n'); const out=[]; let tbl=[];
  const flush=()=>{ if(tbl.length){ out.push('<table class="mdt">'+tbl.map((r,i)=>'<tr>'+r.map(c=>`<${i===0?'th':'td'}>${c}</${i===0?'th':'td'}>`).join('')+'</tr>').join('')+'</table>'); tbl=[]; } };
  lines.forEach(ln=>{const s=ln.trim();
    if(/^\|.*\|$/.test(s)){ if(/^\|[\s:\-|]+\|$/.test(s))return; tbl.push(s.replace(/^\||\|$/g,'').split('|').map(c=>c.trim())); }
    else { flush(); if(s) out.push('<p>'+s+'</p>'); } });
  flush();
  return out.join('').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
}
function reportSection(a){
  const d=analysisData(a); const top=(d.targets||[]).find(t=>t.rank===1)||(d.targets||[])[0];
  let h=`<section class="rs" id="an-${a.id}"><h2>${d.title||'Analysis'}</h2><div class="meta">${d.kicker||''}${d.query?` · query: “${d.query}”`:''}</div>`;
  if(top) h+=`<div class="rfind"><span class="fbig">${top.population}</span><span class="fnum">${fx(top.predicted_tumour_effect)}</span><span class="flab">lead predicted target</span></div>`;
  if(d.summary) h+=`<div class="agent-say"><span class="asl">◆ Agent's answer</span>${mdToHtml(d.summary)}</div>`;
  if(d.targets&&d.targets.length){h+=`<h3>Ranked target populations</h3><table><thead><tr><th>#</th><th>Population</th><th>Predicted effect</th><th>Spots</th><th>Flags</th><th>Top genes</th></tr></thead><tbody>`;
    d.targets.forEach(t=>{const fl=[t.clean_cross_compartment?'clean cross-compartment':'',t.well_powered?'well-powered':'',t.is_self_signal?'self-signal':''].filter(Boolean).join(', ');
      h+=`<tr><td>${t.rank}</td><td><b>${t.population}</b></td><td class="num">${fx(t.predicted_tumour_effect)}</td><td>${(+t.n_spots).toLocaleString()}</td><td>${fl}</td><td>${(t.top_target_genes||[]).slice(0,6).map(g=>g.gene+(g.network_hub?' (hub)':'')+(g.pathway_upstream?' (up)':'')).join(', ')}</td></tr>`;});
    h+=`</tbody></table>`;}
  if(d.table){h+=`<h3>Proposed targets — triage</h3><table><thead><tr><th>Gene</th><th>Verdict</th><th>Effect</th><th>Population</th></tr></thead><tbody>`;
    d.table.forEach(r=>h+=`<tr><td><b>${r.gene}</b></td><td>${r.verdict||''}</td><td class="num">${r.predicted_tumour_effect!=null?fx(r.predicted_tumour_effect):'—'}</td><td>${r.population||''}</td></tr>`);h+=`</tbody></table>`;}
  if(d.compare){const dd=d.compare.data.map(Number),mg=dd.map(Math.abs),maxm=Math.max(...mg,1e-9),mi=mg.indexOf(Math.max(...mg)),ratio=maxm/Math.min(...mg);
    h+=`<h3>Predicted effect across samples</h3><div class="cmpfig">`;
    d.compare.labels.forEach((l,i)=>{const pct=Math.max(3,Math.round(mg[i]/maxm*100)),real=dd[i]/1000;
      h+=`<div class="cmprow"><div class="cmplab">${l}<span>myCAF</span></div><div class="cmptrack"><div class="cmpfill" style="width:${pct}%;background:${cmpGrad(real)}"></div></div><div class="cmpval">${real<0?'−':'+'}${Math.abs(real).toFixed(4)}</div></div>`;});
    h+=`</div><p class="ratio">≈${ratio<10?ratio.toFixed(1):Math.round(ratio)}× stronger predicted suppression in the <b>${d.compare.labels[mi]}</b>.</p>`;}
  const maps=collectMaps(a);
  if(maps.length){h+=`<h3>Spatial effect maps</h3><div class="maps">`;
    maps.forEach(mp=>{const src=mapDataURI(mp.section,mp.population);if(src)h+=`<figure><img src="${src}" alt="${mp.population}"><figcaption>${mp.population} · ${secName(mp.section)}</figcaption></figure>`;});h+=`</div>`;}
  const netEl=document.querySelector('.analysis[data-id="'+a.id+'"] .netsvg');
  if(netEl){ const clone=netEl.cloneNode(true); clone.querySelectorAll('text').forEach(t=>t.setAttribute('fill','#CBD8EC'));
    h+=`<h3>Coupling network${d.section?' · '+secName(d.section):''}</h3><div class="netfig">${clone.outerHTML}</div><div class="netleg2"><span><i class="d" style="background:#F5923E"></i>hub</span><span><i class="d" style="border:2px solid #B99BFF"></i>upstream</span><span><i class="l" style="background:#FFB454"></i>supportive</span><span><i class="l" style="background:#57C7E8"></i>antagonistic</span><span><i class="l" style="background:#B99BFF"></i>pathway</span></div>`; }
  const hubs=networkHubs(d.section);
  if(hubs.length)h+=`<h3>Network hubs${d.section?' · '+secName(d.section):''}</h3><table><thead><tr><th>Gene</th><th>Degree</th><th>+ / −</th><th>Programme</th></tr></thead><tbody>${hubs.map(n=>`<tr><td><b>${n.id}</b></td><td>${n.degree}</td><td>+${n.pos_links}/−${n.neg_links}</td><td>${n.theme&&n.theme!=='other'?n.theme:''}</td></tr>`).join('')}</tbody></table>`;
  if(d.knowledge) h+=knowledgeReport(d.knowledge);
  return h+`</section>`;
}
// external-evidence block for the report — clearly separated from PerTurbo's prediction
function knowledgeReport(k){
  const S=k.sources||{}, e=kesc;
  const ot=S.open_targets||{}, di=S.drug_interactions||{}, lit=S.literature||{}, ct=S.clinical_trials||{};
  const det=(di.drug_detail&&di.drug_detail.length)?di.drug_detail:(di.drugs||[]).map(x=>({drug:x}));
  const rows=[];
  rows.push(['Open Targets · druggability', ot.available?((ot.tractability||[]).map(t=>e(TRACT[t]||t)).join(', ')||'profiled, no tractability flags'):'no record']);
  rows.push(['DGIdb · drug interactions', di.available?`${knum(di.n_drug_interactions!=null?di.n_drug_interactions:det.length)} — ${det.slice(0,5).map(x=>e(x.drug)).join(', ')||'names not returned'}`:'none']);
  rows.push(['Europe PMC · literature', lit.available?`${knum(lit.total_hits!=null?lit.total_hits:0)} papers${(lit.top_papers||[])[0]?` — top: “${e(lit.top_papers[0].title)}” (${e(lit.top_papers[0].year||'')})`:''}`:'none']);
  rows.push(['ClinicalTrials.gov', ct.available?`${knum(ct.total_trials!=null?ct.total_trials:(ct.trials||[]).length)} registered${(ct.trials||[]).slice(0,2).map(t=>` · ${e(t.nct||t.title||'')} (${e(t.phase||t.status||'')})`).join('')}`:'none']);
  return `<h3>External evidence · ${e(k.gene||'')} <span style="font-weight:400;color:#7C8AA6;font-size:12px">(public databases — not a PerTurbo prediction)</span></h3>`
    +`<div class="agent-say" style="border-left-color:#7A5AF8;background:#F6F5FF"><span class="asl" style="color:#7A5AF8">◇ Known biology${k.aliases_queried?` · queried as ${e((k.aliases_queried||[]).join(' / '))}`:''}</span>${e(k.summary||'')}</div>`
    +`<table><tbody>${rows.map(r=>`<tr><td style="width:200px;color:#7C8AA6">${r[0]}</td><td>${r[1]}</td></tr>`).join('')}</tbody></table>`
    +`<p class="foot" style="margin-top:8px">⚠ ${e(k.disclaimer||'Real records from public databases; presence of a drug or trial does not imply efficacy for this target.')}</p>`;
}
function buildFullReport(){
  const secs=ANALYSES.map(reportSection).join('');
  const toc=ANALYSES.map(a=>`<li><a href="#an-${a.id}">${a.title||'Analysis'}</a></li>`).join('');
  const now=new Date().toLocaleString();
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PerTurbo — results report</title><style>
    body{font-family:-apple-system,BlinkMacSystemFont,Inter,Arial,sans-serif;color:#0B1B33;margin:0;background:#F3F7FD}
    header{background:linear-gradient(120deg,#0B57D0,#0E86C4);color:#fff;padding:30px 40px}
    header .rhdr{display:flex;align-items:center;gap:16px}header .rlogo{width:50px;height:50px;flex:none}
    header h1{font-size:28px;margin:0;letter-spacing:-.5px}header .sub{opacity:.9;font-size:13px;margin-top:5px}
    .wrap{max-width:960px;margin:0 auto;padding:0 24px 60px}
    nav{background:#fff;border:1px solid #E4EAF5;border-radius:12px;padding:16px 20px;margin:22px 0}
    nav b{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#7C8AA6}nav ul{margin:8px 0 0;padding-left:18px}nav a{color:#0B57D0;text-decoration:none}nav a:hover{text-decoration:underline}
    section.rs{background:#fff;border:1px solid #E4EAF5;border-radius:14px;padding:24px 28px;margin:18px 0;box-shadow:0 6px 20px rgba(11,27,51,.05)}
    section.rs h2{font-size:21px;margin:0 0 4px}.meta{color:#7C8AA6;font-size:12px;margin-bottom:14px}
    .rfind{display:flex;align-items:baseline;gap:16px;background:linear-gradient(120deg,rgba(11,87,208,.08),#fff);border:1px solid rgba(11,87,208,.2);border-radius:12px;padding:14px 18px;margin-bottom:6px}
    .rfind .fbig{font-size:22px;font-weight:700;color:#0B57D0}.rfind .fnum{font-family:ui-monospace,monospace;font-size:22px;color:#C2610C}.rfind .flab{color:#7C8AA6;font-size:11px;text-transform:uppercase;letter-spacing:1px}
    h3{font-size:14px;margin:20px 0 8px}table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #EDF1F8}th{color:#7C8AA6;font-size:11px;text-transform:uppercase;letter-spacing:.5px}.num{font-family:ui-monospace,monospace}
    .maps{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin:8px 0}figure{margin:0;border:1px solid #E4EAF5;border-radius:10px;overflow:hidden;background:#0A1826}img{width:100%;display:block}figcaption{padding:9px 12px;font-size:11px;color:#EAF6FF}
    .ratio{font-size:14px;color:#0B57D0;font-weight:600}.cav{margin:22px 0;padding:14px 16px;background:#FFF6E9;border:1px solid #F3D9A8;border-radius:10px;font-size:12px;color:#7A4C15}.foot{color:#7C8AA6;font-size:11px;margin-top:22px}
    .agent-say{background:#F5F9FE;border-left:3px solid #0B57D0;border-radius:0 10px 10px 0;padding:12px 16px;margin:6px 0 4px;font-size:13.5px;line-height:1.6;color:#33435C}
    .agent-say .asl{display:block;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#0B57D0;font-weight:700;margin-bottom:6px}
    .agent-say p{margin:6px 0}.agent-say strong{color:#0B1B33}.mdt{width:auto;margin:8px 0}.mdt th,.mdt td{padding:6px 12px}
    .cmpfig{display:flex;flex-direction:column;gap:10px;margin:8px 0}
    .cmprow{display:flex;align-items:center;gap:14px}.cmplab{width:150px;flex:none;text-align:right;font-size:13px;font-weight:600;color:#0B1B33}.cmplab span{display:block;font-size:11px;color:#7C8AA6;font-weight:400}
    .cmptrack{flex:1;height:24px;background:#EDF1F8;border-radius:7px;overflow:hidden}.cmpfill{height:100%;border-radius:7px}
    .cmpval{width:86px;flex:none;font-family:ui-monospace,monospace;font-size:13px;color:#C2610C}
    .netfig{background:#0A1826;border-radius:12px;padding:12px;margin:8px 0}.netfig svg{width:100%;height:auto;display:block}
    .netleg2{display:flex;gap:16px;flex-wrap:wrap;font-size:11px;color:#7C8AA6;margin:6px 0 4px}.netleg2 span{display:flex;align-items:center;gap:6px}.netleg2 .d{width:11px;height:11px;border-radius:50%}.netleg2 .l{width:16px;height:3px;border-radius:2px}
  </style></head><body>
    <header><div class="rhdr">
      <svg class="rlogo" viewBox="0 0 100 100"><g stroke-width="3.5" fill="none">
        <line x1="50" y1="50" x2="82" y2="26" stroke="#EF8F6E"/><line x1="50" y1="50" x2="86" y2="58" stroke="#5BB3A6"/>
        <line x1="50" y1="50" x2="64" y2="86" stroke="#EF8F6E"/><line x1="50" y1="50" x2="18" y2="70" stroke="#5BB3A6"/>
        <line x1="50" y1="50" x2="24" y2="30" stroke="#EF8F6E"/></g>
        <circle cx="82" cy="26" r="6" fill="#DBAE5F"/><circle cx="86" cy="58" r="5" fill="#84B6AB"/>
        <circle cx="64" cy="86" r="5" fill="#C98AC0"/><circle cx="18" cy="70" r="5" fill="#84B6AB"/>
        <circle cx="24" cy="30" r="5" fill="#C98AC0"/><circle cx="50" cy="50" r="15" fill="#E6A85C"/></svg>
      <div><h1>PerTurbo</h1><div class="sub">Results report · spatial target discovery · kimi-k2 on AMD Instinct MI300X · generated ${now}</div></div>
    </div></header>
    <div class="wrap"><nav><b>Contents</b><ul>${toc||'<li>No analyses yet</li>'}</ul></nav>
      ${secs||'<section class="rs"><p>No analyses to report — run a question first.</p></section>'}
      <div class="cav">⚠ Every result here is <b>predicted, causal-given-model</b> — prioritized in-silico hypotheses requiring experimental validation, never proven targets. Couplings are undirected associations; direction is overlaid from literature, not learned.</div>
      <div class="foot">PerTurbo · data GSE272362 (Khaliq et al.) · self-contained report.</div></div></body></html>`;
}

// ===== lightbox / modal keyboard =====
window.addEventListener('keydown',e=>{
  if(e.key==='Escape'){ if(cfg.classList.contains('show'))closeCfg();
    else if(cred.classList.contains('show'))closeCred();
    else if(exp.classList.contains('show'))closeExport();
    else if(document.getElementById('lb').classList.contains('show'))closeLb(); }
});
