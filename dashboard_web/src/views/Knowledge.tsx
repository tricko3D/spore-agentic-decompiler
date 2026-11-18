import {useEffect,useState} from 'react';
import {controlsEnabled,money,post,pretty,query} from '../api';
import {usePoll} from '../hooks';
import {Drawer,ErrorText,Metric,Panel,Tag} from '../components/Panel';
import BrainGraph from '../components/BrainGraph';
import BrainSearch from '../components/BrainSearch';
import type {BrainGraph as Graph,ConsolidatorState,Finding,FindingDetail,
  KnowledgeOverview,Paginated,Technique,TechniqueDetail} from '../types';

type Selection={kind:'technique'|'finding';id:number};
type Category='all'|'calling_convention'|'register_allocation'|'stack_layout'|'compiler_flags'|'optimization'|'control_flow'|'layout_types'|'templates'|'link_time'|'other';

function EvidenceInspector({selection,onClose,onChoose,refresh}:{selection:Selection;onClose:()=>void;onChoose:(next:Selection)=>void;refresh:()=>void}){
 const url='/api/knowledge/'+selection.kind+'?id='+selection.id;
 const {value,error,loading,refresh:reload}=usePoll<TechniqueDetail|FindingDetail>(url,8000,true);
 const technique=selection.kind==='technique'?value as TechniqueDetail|null:null;
 const finding=selection.kind==='finding'?value as FindingDetail|null:null;
 const [note,setNote]=useState(''),[pinned,setPinned]=useState(false);
 const [dirty,setDirty]=useState(false),[message,setMessage]=useState('');
 useEffect(()=>{
  if(technique&&!dirty){setNote(technique.annotation?.note||'');setPinned(Boolean(technique.annotation?.pinned))}
 },[technique,dirty]);
 useEffect(()=>{setDirty(false);setMessage('')},[selection.kind,selection.id]);
 async function save(){
  if(!technique)return;
  try{await post('/api/knowledge/annotation',{id:technique.id,note,pinned});
    setDirty(false);setMessage('Annotation saved. Source evidence remains unchanged.');void reload();refresh();}
  catch(e){setMessage((e as Error).message)}
 }
 return <Drawer title={technique?.title||(finding?'Function '+finding.va:'Evidence')}
   subtitle={technique?'Technique #'+technique.id+' · '+technique.category:
    finding?'Finding #'+finding.id+' · pass '+finding.pass_number:''} onClose={onClose}>
  <ErrorText error={error}/>
  {loading&&<p>Loading evidence…</p>}
  {technique&&<div className="knowledge-detail">
   <div className="row-between"><Tag value={technique.evidence_level}/><span className="subtle">{technique.sources.length} current supporting investigations</span></div>
   <div className="section-block"><h3>Compiler problem</h3><p>{technique.problem}</p></div>
   <div className="section-block"><h3>Applicable conditions</h3><p>{technique.conditions}</p></div>
   <div className="section-block"><h3>Attempted intervention</h3><p>{technique.intervention}</p></div>
   <div className="section-block"><h3>Observed effect</h3><p>{technique.observed_effect}</p></div>
   <p className="subtle">These are model-extracted observations, never checker-verified technique claims.</p>
   <h3>Linked source evidence</h3>
   <div className="stack-list">{technique.sources.map(x=><button className="evidence-source" key={x.finding_id}
    onClick={()=>onChoose({kind:'finding',id:x.finding_id})}>
    <div className="row-between"><strong className="mono">{x.va}</strong><Tag value={x.status}/></div>
    <small>Pass {x.pass_number} · {x.verified_match?'Byte-exact source function':'Unmatched observation'}</small>
    <blockquote>{x.evidence_quote}</blockquote><p>{x.observation}</p>
   </button>)}</div>
   <h3>Personal research note</h3>
   <p className="subtle">Notes and pins are stored separately from compiler evidence and never affect verification.</p>
   <textarea rows={5} maxLength={1500} value={note} disabled={!controlsEnabled}
    onChange={e=>{setNote(e.target.value);setDirty(true)}} placeholder="Record an interpretation, experiment idea, or correction…"/>
   <div className="row-between">
    <label className="check-row"><input type="checkbox" checked={pinned} disabled={!controlsEnabled}
      onChange={e=>{setPinned(e.target.checked);setDirty(true)}}/> Pin for review</label>
    <button className="primary" disabled={!controlsEnabled||!dirty} onClick={()=>void save()}>Save note</button>
   </div><p className="subtle" role="status">{message}</p>
  </div>}
  {finding&&<div className="knowledge-detail">
    <div className="row-between"><Tag value={finding.status}/><span className="subtle">{finding.verified_match?'Verified function MATCH':'Historical investigation, not proven'}</span></div>
    <h3>Summary</h3><p>{finding.summary}</p>
    <h3>Detailed research notes</h3><pre className="code-panel">{finding.details||'No additional notes'}</pre>
    <p className="subtle mono">Evidence path: {finding.evidence_path}</p>
    <h3>Independent compiler experiments</h3>
    <div className="stack-list">{finding.experiments?.map(x=><article className="history-item" key={x.ordinal}>
      <div className="row-between"><strong>Check {x.ordinal}</strong><span className="mono">
       {x.size_compiled!=null&&x.size_retail!=null?x.size_compiled+'/'+x.size_retail+' bytes':'Size unknown'}
      </span></div>
      <p>{x.result}</p><small>{x.flags||'Flags not captured'}</small>
      <small>{x.diff_bytes!=null?x.diff_bytes+' differing bytes reported':'Differing byte count unavailable'}</small>
    </article>)}
    {!finding.experiments?.length&&<p className="subtle">No independent compiler checks were extracted for this finding.</p>}</div>
    <h3>Reusable techniques linked to this finding</h3>
    <div className="stack-list">{finding.techniques?.map(x=><button className="evidence-source" key={x.id}
      onClick={()=>onChoose({kind:'technique',id:x.id})}><strong>{x.title}</strong><p>{x.observation}</p></button>)}
      {!finding.techniques?.length&&<p className="subtle">No consolidated techniques currently linked.</p>}</div>
  </div>}
 </Drawer>;
}

function BrainSettings({state,onRefresh}:{state:ConsolidatorState|null;onRefresh:()=>void}){
 const [editing,setEditing]=useState(false),[budget,setBudget]=useState(0.25),
  [batch,setBatch]=useState(3),[interval,setIntervalValue]=useState(180),
  [busy,setBusy]=useState(false),[message,setMessage]=useState('');
 useEffect(()=>{
  if(!state||editing)return;
  setBudget(state.settings.daily_usd);
  setBatch(state.settings.batch);
  setIntervalValue(state.settings.interval);
 },[state,editing]);
 async function control(action:'start'|'stop'){
  setBusy(true);
  try{await post('/api/knowledge/service',{action});setMessage(action==='stop'?'Consolidator paused':'Consolidator running');onRefresh()}
  catch(e){setMessage((e as Error).message)}
  finally{setBusy(false)}
 }
 async function save(){
  setBusy(true);
  try{await post('/api/knowledge/settings',{daily_usd:budget,batch,interval});
   setMessage('Settings saved, effective at the next consolidation cycle.');setEditing(false);onRefresh()}
  catch(e){setMessage((e as Error).message)}
  finally{setBusy(false)}
 }
 return <Panel title="Knowledge consolidation" subtitle="Independent systemd service · DeepSeek V4.1 Flash">
  <div className="row-between"><div className="state-line">
   <span className={'status-dot '+(state?.active?'online':'offline')}></span>
   <strong>{state?.active?'ONLINE':state?.installed?'OFFLINE':'NOT CONFIGURED'}</strong>
   <span className="subtle">{state?.mode||'Unavailable'}</span>
  </div>
   <div className="inline-actions">
    <button disabled={!controlsEnabled||!state?.controls_available||busy||state.active}
      onClick={()=>void control('start')}>Start</button>
    <button disabled={!controlsEnabled||!state?.controls_available||busy||!state.active}
      onClick={()=>void control('stop')}>Pause</button>
   </div>
  </div>
  {state?.hint&&<p className="subtle">{state.hint}</p>}
  <div className="settings-grid">
   <label>Daily USD cap<input type="number" min=".01" max="10" step=".01"
    value={budget} disabled={!controlsEnabled||!editing} onChange={e=>setBudget(Number(e.target.value))}/></label>
   <label>Batch<input type="number" min="1" max="20" value={batch} disabled={!controlsEnabled||!editing}
    onChange={e=>setBatch(Number(e.target.value))}/></label>
   <label>Interval (s)<input type="number" min="30" max="7200" value={interval}
    disabled={!controlsEnabled||!editing} onChange={e=>setIntervalValue(Number(e.target.value))}/></label>
  </div>
  <div className="row-between"><p className="subtle">A local admission cap, not a provider-enforced credit limit.</p>
   <div className="inline-actions">{editing?<><button onClick={()=>setEditing(false)}>Cancel</button>
    <button className="primary" disabled={busy} onClick={()=>void save()}>Save</button></>:
    <button disabled={!controlsEnabled} onClick={()=>setEditing(true)}>Configure</button>}</div>
  </div>
  {message&&<p className="dock-message" role="status">{message}</p>}
 </Panel>;
}

export default function Knowledge({active}:{active:boolean}){
 const {value:overview,error:summaryError,refresh}=usePoll<KnowledgeOverview>('/api/knowledge/overview',10000,active);
 const {value:graph}=usePoll<Graph>('/api/knowledge/graph?limit=35',45000,active);
 const [mode,setMode]=useState<'techniques'|'findings'>('techniques');
 const [draft,setDraft]=useState(''),[search,setSearch]=useState(''),[category,setCategory]=useState<Category>('all');
 const [status,setStatus]=useState(''),[offset,setOffset]=useState(0),[selection,setSelection]=useState<Selection|null>(null);
 useEffect(()=>{const t=window.setTimeout(()=>{setSearch(draft);setOffset(0)},300);return ()=>clearTimeout(t)},[draft]);
 const endpoint=mode==='techniques'?'/api/knowledge/techniques?'+query({
   search,category:category==='all'?'':category,offset,limit:30
 }):'/api/knowledge/findings?'+query({search,status,offset,limit:30});
 const {value:list,error:listError}=usePoll<Paginated<Technique|Finding>>(endpoint,20000,active);
 const queue=overview?.queue||{},waiting=(queue.pending||0)+(queue.claimed||0);
 return <section className="knowledge-view view-body">
  <div className="knowledge-intro"><div><p className="eyebrow">COLLECTIVE INTELLIGENCE</p>
   <h1>Compiler Knowledge</h1>
   <p>Explore what the fleet learned, trace each claim back to its checker evidence, and steer consolidation independently of the workers.</p>
  </div><div className="brain-state"><span className={'status-dot '+(overview?.consolidator?.active?'online':'offline')}></span>
   {overview?.consolidator?.active?'Brain consolidating':'Consolidator idle'}</div></div>
  <div className="metrics knowledge-metrics">
   <Metric label="Historical findings" value={pretty(overview?.findings)} detail={pretty(overview?.verified_matches)+' matched functions'}/>
   <Metric label="Reusable techniques" value={pretty(overview?.techniques)} tone="tone-match" detail={pretty(overview?.links)+' evidence links'}/>
   <Metric label="Awaiting extraction" value={pretty(waiting)} detail={pretty(queue.done)+' processed'}/>
   <Metric label="Failed extraction" value={pretty(queue.failed||0)} tone={queue.failed?'tone-fail':''} detail="Inspectable below"/>
   <Metric label="Embedding backlog" value={pretty(overview?.pending_embeddings)} detail="Local ONNX model"/>
   <Metric label="Today’s spend" value={money(overview?.daily_spend)} detail="Separate from worker spend"/>
  </div><ErrorText error={summaryError}/>
  <BrainSearch onTechnique={id=>setSelection({kind:'technique',id})}
   onFinding={id=>setSelection({kind:'finding',id})}/>
  <div className="knowledge-layout">
   <div className="knowledge-main">
    <Panel title="Evidence graph" subtitle="Click a technique or function; every edge represents a stored evidence link"
      action={<span className="subtle">Current source hashes only</span>}>
     <BrainGraph graph={graph} onTechnique={id=>setSelection({kind:'technique',id})}
       onFinding={id=>setSelection({kind:'finding',id})}/>
    </Panel>
    <Panel title="Explore the brain" subtitle="Search consolidated compiler techniques and individual investigations" className="explorer">
     <div className="tabs"><button className={mode==='techniques'?'selected':''}
      onClick={()=>{setMode('techniques');setOffset(0)}}>Compiler techniques</button>
      <button className={mode==='findings'?'selected':''}
      onClick={()=>{setMode('findings');setOffset(0)}}>Raw investigations</button></div>
     <div className="toolbar">
      <input className="knowledge-search" type="search" value={draft}
       placeholder="ABI, EBX, stack frame, optimization, function address…"
       onChange={e=>setDraft(e.target.value)}/>
      {mode==='techniques'?<select value={category} aria-label="Technique category" onChange={e=>{setCategory(e.target.value as Category);setOffset(0)}}>
       <option value="all">All categories</option>
       {overview?.categories?.map(x=><option key={x.category} value={x.category}>{x.category.replaceAll('_',' ')} ({x.count})</option>)}
       </select>:<select value={status} aria-label="Finding status" onChange={e=>{setStatus(e.target.value);setOffset(0)}}>
         <option value="">All statuses</option><option value="MATCH">MATCH</option>
         <option value="FAIL">FAIL</option><option value="DEFER">DEFER</option><option value="SKIP">SKIP</option>
       </select>}
      <span className="subtle">{pretty(list?.total)} results</span>
     </div><ErrorText error={listError}/>
     <div className="knowledge-results">
      {mode==='techniques'&&list?.items?.map(row=>{
       const x=row as Technique;
       return <button className="knowledge-card" key={x.id} onClick={()=>setSelection({kind:'technique',id:x.id})}>
        <div className="row-between"><span className="mono accent">TECHNIQUE #{x.id}</span><Tag value={x.evidence_level}/></div>
        <h3>{x.title}</h3><p>{x.problem}</p>
        <div className="row-between"><small>{x.category.replaceAll('_',' ')}</small><small>{x.source_count||0} evidence links</small></div>
       </button>;
      })}
      {mode==='findings'&&list?.items?.map(row=>{
       const x=row as Finding;
       return <button className="knowledge-card" key={x.id} onClick={()=>setSelection({kind:'finding',id:x.id})}>
        <div className="row-between"><span className="mono accent">{x.va} · pass {x.pass_number}</span><Tag value={x.status}/></div>
        <h3>{x.title||'Investigation '+x.id}</h3><p>{x.summary}</p>
        <div className="row-between"><small>{x.verified_match?'Independently verified MATCH':'Experiment / hypothesis'}</small>
         <small>{x.technique_count||0} extracted techniques</small></div>
       </button>;
      })}
      {!list?.items?.length&&<p className="empty">No results. Raw findings remain available while techniques are still being consolidated.</p>}
     </div>
     <div className="pager"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-30))}>Previous</button>
      <span>{pretty(offset+1)}–{pretty(Math.min(offset+30,list?.total||0))} of {pretty(list?.total)}</span>
      <button disabled={offset+30>=(list?.total||0)} onClick={()=>setOffset(offset+30)}>Next</button></div>
    </Panel>
   </div>
   <div className="knowledge-side">
    <BrainSettings state={overview?.consolidator||null} onRefresh={()=>void refresh()}/>
    <Panel title="Extraction pipeline" subtitle="Status of the independent, evidence-backed queue">
     <div className="queue-bars">
      {['done','claimed','pending','failed'].map(x=><div key={x} className="queue-row">
       <span>{x}</span><strong>{pretty(queue[x]||0)}</strong>
       <div className="mini-bar"><span className={x} style={{width:(overview?.findings?100*(queue[x]||0)/overview.findings:0)+'%'}}/></div>
      </div>)}
     </div>
     <p className="subtle">An extracted technique is observed or hypothetical, not automatically proven by byte-exact matches.</p>
    </Panel>
    <Panel title="Recent consolidation errors" subtitle="Failures have backoff, so one bad response does not block other findings">
     <div className="stack-list">{overview?.errors?.map(x=><button key={x.finding_id} className="error-record"
      onClick={()=>setSelection({kind:'finding',id:x.finding_id})}>
      <div className="row-between"><strong className="mono">{x.va}</strong><Tag value={x.state}/></div>
      <p>{x.error}</p><small>{x.failures} failures · Next retry {x.next_retry_utc||'not scheduled'}</small></button>)}
      {!overview?.errors?.length&&<p className="subtle">No recorded consolidation errors.</p>}</div>
    </Panel>
    <Panel title="How knowledge is formed" subtitle="Source-grounded, never model-verified">
     <div className="pipeline-flow"><span>Checker evidence</span><b>↓</b>
      <span>Collective Memory</span><b>↓</b><span>DeepSeek consolidator</span><b>↓</b>
      <span>Verbatim evidence validation</span><b>↓</b><span>Reusable technique + source links</span></div>
    </Panel>
   </div>
  </div>
  {selection&&<EvidenceInspector selection={selection} onChoose={setSelection}
    onClose={()=>setSelection(null)} refresh={()=>void refresh()}/>}
 </section>;
}
