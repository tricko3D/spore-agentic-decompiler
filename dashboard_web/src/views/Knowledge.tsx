import {memo,useCallback,useEffect,useState} from 'react';
import {controlsEnabled,money,post,pretty,query} from '../api';
import {usePoll} from '../hooks';
import {Drawer,ErrorText,Tag} from '../components/Panel';
import GraphViewer from '../components/GraphViewer';
import BrainSearch from '../components/BrainSearch';
import KnowledgeDock from '../components/KnowledgeDock';
import ErrorModal from '../components/ErrorModal';
import type {BrainGraph as Graph,Finding,FindingDetail,
  KnowledgeOverview,Paginated,QueueError,Technique,TechniqueDetail} from '../types';

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

/** Brain is a bounded research workspace: summary, search, a single
 * scrollable evidence list, and short operational status widgets.
 * Nothing below inserts extra document height or grows after interaction.
 */
function Knowledge({active,overview,summaryError,refreshOverview}:{
 active:boolean;overview:KnowledgeOverview|null;summaryError:string;refreshOverview:()=>void
}){
 const [mode,setMode]=useState<'techniques'|'findings'>('techniques');
 const [draft,setDraft]=useState(''),[search,setSearch]=useState('');
 const [category,setCategory]=useState<Category>('all'),[status,setStatus]=useState('');
 const [offset,setOffset]=useState(0);
 const [selection,setSelection]=useState<Selection|null>(null);
 const [selectedError,setSelectedError]=useState<QueueError|null>(null);
 const [showGraph,setShowGraph]=useState(false);
 // The graph is fetched only when actually opened, not in the main list.
 const {value:graph}=usePoll<Graph>('/api/knowledge/graph?limit=35',45000,active&&showGraph);
 const openTechnique=useCallback((id:number)=>setSelection({kind:'technique',id}),[]);
 const openFinding=useCallback((id:number)=>setSelection({kind:'finding',id}),[]);
 useEffect(()=>{
  const timer=window.setTimeout(()=>{setSearch(draft);setOffset(0)},300);
  return ()=>window.clearTimeout(timer);
 },[draft]);
 const endpoint=mode==='techniques'?'/api/knowledge/techniques?'+query({
  search,category:category==='all'?'':category,offset,limit:30
 }):'/api/knowledge/findings?'+query({search,status,offset,limit:30});
 const {value:list,error:listError,loading:listLoading}=usePoll<Paginated<Technique|Finding>>(endpoint,20000,active);
 const queue=overview?.queue||{};
 const waiting=(queue.pending||0)+(queue.claimed||0);
 const queueTotal=(queue.done||0)+(queue.pending||0)+(queue.claimed||0)+(queue.failed||0);
 const currentTotal=list?.total||0;
 const from=currentTotal?offset+1:0;
 const to=Math.min(offset+30,currentTotal);
 const isTechniques=mode==='techniques';
 return <section className="knowledge-view brain-workspace" aria-label="Collective Memory">
  <div className="brain-kpis" aria-label="Knowledge statistics">
   <div className="brain-kpi" title={pretty(overview?.verified_matches)+' matched source functions'}>
    <span>Investigations</span><strong>{pretty(overview?.findings)}</strong><small>{pretty(overview?.verified_matches)} matched</small>
   </div>
   <div className="brain-kpi" title={pretty(overview?.links)+' source evidence links'}>
    <span>Techniques</span><strong>{pretty(overview?.techniques)}</strong><small>{pretty(overview?.links)} links</small>
   </div>
   <div className="brain-kpi" title="Pending and claimed findings awaiting extraction">
    <span>In queue</span><strong>{pretty(waiting)}</strong><small>{pretty(queue.done)} processed</small>
   </div>
   <div className="brain-kpi" title="Separate from agent inference spending">
    <span>Today's spend</span><strong>{money(overview?.daily_spend)}</strong><small>Consolidation only</small>
   </div>
  </div>
  <div className="brain-stage">
   <section className="brain-list-pane" aria-label="Investigations and techniques">
    <div className="brain-list-toolbar">
     <div className="brain-list-tabs" role="group" aria-label="Evidence type">
      <button type="button" className={isTechniques?'selected':''} aria-pressed={isTechniques}
       onClick={()=>{setMode('techniques');setOffset(0)}}>Techniques</button>
      <button type="button" className={!isTechniques?'selected':''} aria-pressed={!isTechniques}
       onClick={()=>{setMode('findings');setOffset(0)}}>Investigations</button>
     </div>
     <input type="search" className="brain-list-filter" aria-label="Filter evidence list"
      placeholder={isTechniques?'Filter techniques…':'Filter addresses and findings…'}
      value={draft} onChange={event=>setDraft(event.target.value)}/>
     {isTechniques
      ?<select className="brain-list-select" value={category} aria-label="Compiler category"
       onChange={event=>{setCategory(event.target.value as Category);setOffset(0)}}>
       <option value="all">All categories</option>
       {overview?.categories?.map(x=><option value={x.category} key={x.category}>
        {x.category.replaceAll('_',' ')} ({x.count})
       </option>)}
      </select>
      :<select className="brain-list-select" value={status} aria-label="Investigation outcome"
       onChange={event=>{setStatus(event.target.value);setOffset(0)}}>
       <option value="">All outcomes</option>
       <option value="MATCH">MATCH</option><option value="FAIL">FAIL</option>
       <option value="DEFER">DEFER</option><option value="SKIP">SKIP</option>
      </select>}
     <BrainSearch onTechnique={openTechnique} onFinding={openFinding}/>
     <button type="button" className="brain-map-button" onClick={()=>setShowGraph(true)}
      title="Explore linked evidence with pan and zoom">Evidence map <span aria-hidden="true">↗</span></button>
    </div>
    <div className="brain-list-columns" aria-hidden="true">
     <span>{isTechniques?'Technique / compiler finding':'Investigation / evidence'}</span>
     <span>{isTechniques?'Category':'Pass'}</span>
     <span>{isTechniques?'Evidence':'Result'}</span>
     <span>{isTechniques?'Sources':'Links'}</span>
    </div>
    <div className="brain-list-scroll" aria-live="polite">
     {isTechniques&&list?.items?.map(row=>{
      const x=row as Technique;
      return <button type="button" className="brain-data-row" key={'technique-'+x.id}
       onClick={()=>setSelection({kind:'technique',id:x.id})}
       aria-label={'Inspect technique '+x.id+': '+x.title}>
       <span className="brain-row-main">
        <strong title={x.title}><span className="brain-row-id mono">#{x.id}</span>{x.title}</strong>
        <small title={x.problem}>{x.problem||'No description recorded'}</small>
       </span>
       <span className="brain-row-category" title={x.category}>{(x.category||'other').replaceAll('_',' ')}</span>
       <span><Tag value={x.evidence_level}/></span>
       <span className="brain-row-count mono">{x.source_count??0}</span>
      </button>;
     })}
     {!isTechniques&&list?.items?.map(row=>{
      const x=row as Finding;
      return <button type="button" className="brain-data-row" key={'finding-'+x.id}
       onClick={()=>setSelection({kind:'finding',id:x.id})}
       aria-label={'Inspect investigation '+x.va}>
       <span className="brain-row-main">
        <strong><span className="brain-row-id mono">{x.va}</span>{x.title||'Investigation #'+x.id}</strong>
        <small title={x.summary}>{x.summary||'No summary recorded'}</small>
       </span>
       <span className="brain-row-category mono">{x.pass_number}</span>
       <span><Tag value={x.status}/></span>
       <span className="brain-row-count mono">{x.technique_count??0}</span>
      </button>;
     })}
     {listLoading&&<p className="brain-list-placeholder">Loading evidence…</p>}
     {!listLoading&&!list?.items?.length&&
      <p className="brain-list-placeholder">{listError?'Could not load evidence':'No matching evidence found'}</p>}
    </div>
    <div className="brain-list-footer">
     <span className="brain-list-message">{listError||summaryError||''}</span>
     <span className="brain-result-count mono">{pretty(from)}–{pretty(to)} of {pretty(currentTotal)}</span>
     <button type="button" disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-30))}>Previous</button>
     <button type="button" disabled={offset+30>=currentTotal} onClick={()=>setOffset(offset+30)}>Next</button>
    </div>
   </section>
   <aside className="brain-sidebar" aria-label="Consolidation status">
    <KnowledgeDock state={overview?.consolidator||null} onRefresh={refreshOverview}/>
    <section className="brain-widget brain-queue-widget" aria-label="Extraction queue">
     <div className="brain-widget-heading"><strong>Extraction queue</strong>
      <small title="The consolidator extracts techniques independently of the agents">Source-backed</small>
     </div>
     <div className="brain-queue-grid">
      {(['done','claimed','pending','failed'] as const).map(name=>
       <div className="brain-queue-item" key={name}><span>{name}</span><strong className="mono">{pretty(queue[name]||0)}</strong></div>
      )}
     </div>
     <div className="brain-queue-track" role="img"
      aria-label={pretty(queue.done)+' processed of '+pretty(queueTotal)+' queued findings'}>
      <span style={{width:(queueTotal?100*(queue.done||0)/queueTotal:0)+'%'}}/>
     </div>
     <div className="brain-queue-note">
      <span>Embedding backlog</span><strong className="mono">{pretty(overview?.pending_embeddings)}</strong>
     </div>
    </section>
    <section className="brain-widget brain-errors-widget" aria-label="Recent consolidation errors">
     <div className="brain-widget-heading"><strong>Recent errors</strong>
      <small>{pretty(overview?.errors?.length||0)} recorded</small>
     </div>
     <div className="brain-errors-list">
      {overview?.errors?.map(e=>
       <button type="button" className="brain-error-row" key={e.finding_id}
        title={e.error} onClick={()=>setSelectedError(e)}>
        <span className="brain-error-top"><strong className="mono">{e.va}</strong><Tag value={e.state}/></span>
        <span className="brain-error-summary">{e.error||'No detail recorded'}</span>
       </button>
      )}
      {!overview?.errors?.length&&<p className="brain-errors-empty">No consolidation errors</p>}
     </div>
    </section>
   </aside>
  </div>
  {selection&&<EvidenceInspector selection={selection} onChoose={setSelection}
   onClose={()=>setSelection(null)} refresh={refreshOverview}/>}
  {selectedError&&<ErrorModal error={selectedError} onClose={()=>setSelectedError(null)}/>}
  {showGraph&&<GraphViewer graph={graph} onTechnique={openTechnique}
   onFinding={openFinding} onClose={()=>setShowGraph(false)}/>}
 </section>;
}
export default memo(Knowledge);
