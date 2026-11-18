import {useEffect,useState} from 'react';
import {bytes,money,pretty,query} from '../api';
import {usePoll} from '../hooks';
import {Drawer,ErrorText,Metric,Panel,Tag} from '../components/Panel';
import type {AgentDetail,FunctionDetail,FunctionRow,Paginated,Snapshot,Status} from '../types';

type Detail={kind:'function'|'agent';id:string};
const STATUS:Status[]=['MATCH','FAIL','DEFER','SKIP','PENDING'];
const number=(v:number|undefined)=>v||0;
const agreement=(a:FunctionRow['agreement'],sizeB:number)=>{
 if(!a)return 'unknown / '+bytes(sizeB);
 if(a.matched!=null)return a.matched+'/'+(a.total??sizeB)+' bytes';
 if(a.kind==='size_mismatch')return 'size '+a.compiled_size+'/'+a.retail_size+' B';
 return 'unknown / '+bytes(sizeB);
};
function Details({item,onClose}:{item:Detail;onClose:()=>void}){
 const url=item.kind==='agent'?'/api/agent?worker='+item.id:'/api/function?va='+encodeURIComponent(item.id);
 const {value,error,loading}=usePoll<AgentDetail|FunctionDetail>(url,2000,true);
 const data=value as FunctionDetail|null,worker=value as AgentDetail|null;
 return <Drawer title={item.kind==='agent'?'Agent '+item.id:item.id}
   subtitle={item.kind==='agent'?worker?.va||'Latest activity':data?.file} onClose={onClose}>
  <ErrorText error={error}/>{loading&&<p>Loading recorded evidence…</p>}
  {item.kind==='agent'&&worker&&<><h3>Live agent events</h3>
   <div className="event-list">{worker.events?.map((x,i)=><div className="event" key={i}>
    <span className="mono subtle">{x.kind}</span><span>{x.message}</span></div>)}
    {!worker.events?.length&&<p>No recent agent events</p>}</div>
   <h3>Worker log status</h3><pre className="code-panel">{worker.log_status?.join('\n')||'No saved worker log'}</pre>
  </>}
  {item.kind==='function'&&data&&<><h3>Byte agreement</h3>
   <p className="strong-line">{agreement(data.agreement,data.size)}</p>
   <p className="subtle">Only independently checked or recorded comparison data is shown.</p>
   <h3>Recorded blocker</h3><pre className="code-panel">{data.reason||'No recorded blocker'}</pre>
   <h3>{data.diff_status}</h3><pre className="code-panel">{data.checker?.summary?.join('\n')||'No saved checker diff evidence'}</pre>
   <h3>Attempt history</h3><div className="stack-list">{data.history?.map((x,i)=>
    <article className="history-item" key={i}><div className="row-between"><Tag value={x.status}/><span className="mono">Pass {x.pass} · {x.time}</span></div>
    <p>{x.note}</p><small>{x.effort} · {money(x.cost?.cost)} logged spend · {x.cost?.steps??0} steps</small>
    </article>)}</div>
  </>}
 </Drawer>;
}
export default function Operations({active,snapshot,snapshotError}:{active:boolean;snapshot:Snapshot|null;snapshotError:string}){
 const d=snapshot,error=snapshotError;
 const [status,setStatus]=useState('ALL'),[search,setSearch]=useState(''),[typed,setTyped]=useState(''),[offset,setOffset]=useState(0);
 const [detail,setDetail]=useState<Detail|null>(null);
 useEffect(()=>{const t=window.setTimeout(()=>{setSearch(typed);setOffset(0)},250);return ()=>clearTimeout(t)},[typed]);
 const url='/api/functions?'+query({status,search,offset,limit:75});
 const {value:table,error:tableError,refresh:refreshTable}=usePoll<Paginated<FunctionRow>>(url,15000,active);
 const run=d?.run||{},counts=d?.counters||{},budget=d?.budget||{},sizes=d?.bytes||{},effort=d?.effort||{};
 const key=(x:Status)=>x==='MATCH'?'matches':x==='FAIL'?'fails':x==='DEFER'?'deferred':x==='SKIP'?'skips':'pending';
 const statusTotal=STATUS.reduce((a,x)=>a+number(counts[key(x)]),0);
 const byStatus=STATUS.map(x=>({key:x,count:number(counts[key(x)]),color:x.toLowerCase()}));
 return <section className="operations-view view-body">
  <div className="metrics">
   <Metric label="Matched" value={pretty(counts.matches)} tone="tone-match" detail={bytes(sizes.MATCH)}/>
   <Metric label="Unmatched" value={pretty(counts.fails)} tone="tone-fail" detail={bytes(sizes.FAIL)}/>
   <Metric label="Deferred" value={pretty(counts.deferred)} tone="tone-defer" detail={bytes(sizes.DEFER)}/>
   <Metric label="Pending" value={pretty(counts.pending)} detail={bytes(sizes.PENDING)}/>
   <Metric label="Inference capacity" value={pretty(d?.adaptive?.concurrency)} detail={d?.adaptive?.cooldown_seconds?'Cooldown '+d.adaptive.cooldown_seconds+'s':'Adaptive'}/>
   <Metric label="Run spend" value={money(budget.run_spend)} detail={'Limit '+money(budget.run_limit)}/>
  </div>
  <ErrorText error={error}/>
  <div className="operations-layout">
   <div className="ops-left">
    <Panel title="Evolution progress" subtitle="Cumulative function outcomes and checked bytes">
     <div className="stacked-track" role="img" aria-label="Function outcome distribution">
      {byStatus.map(x=><span key={x.key} className={x.color} style={{width:(statusTotal?100*x.count/statusTotal:0)+'%'}} title={x.key+' '+x.count}/>)}
     </div>
     <div className="legend">{byStatus.map(x=><span key={x.key}><b className={x.color}></b>{x.key} <strong>{x.count}</strong></span>)}</div>
     <div className="subsection-title">Captured inference cost by outcome</div>
     <div className="cost-row">{['MATCH','FAIL','DEFER'].map(x=><div key={x} className="mini-stat">
      <small>{x} · {d?.costs?.[x]?.covered||0}/{d?.costs?.[x]?.attempts||0} priced</small>
      <strong>{money(d?.costs?.[x]?.cost)}</strong>
      <span>{bytes(sizes[x])} · {pretty(d?.costs?.[x]?.functions)} functions</span>
     </div>)}</div>
     <div className="subsection-title">Unmatched effort distribution</div>
     <div className="effort-row">{Object.entries(effort).map(([k,v])=><div key={k}><strong>{k}</strong><span>{v}</span></div>)}</div>
    </Panel>
    <Panel title="Expedition" subtitle={run.id||'No run recorded'} action={<Tag value={run.state||'none'}/>}>
     <div className="run-stats">
      <div><span>Workers active</span><strong>{run.live_workers||0} / {run.workers||0}</strong></div>
      <div><span>Valid attempts</span><strong>{run.valid_attempts||0}</strong></div>
      <div><span>Assigned targets</span><strong>{run.targets||0}</strong></div>
      <div><span>Covered bytes</span><strong>{bytes(run.bytes_completed)} / {bytes(run.bytes_target)}</strong></div>
     </div>
     <div className="row-between"><h3>Targets completed or exhausted</h3><strong>{run.completed||0}/{run.targets||0}</strong></div>
     <div className="progress"><span style={{width:(run.targets?100*(run.completed||0)/run.targets:0)+'%'}}/></div>
     <div className="run-statuses">{STATUS.map(x=><div key={x}><Tag value={x}/><strong>{run.outcomes?.[x]||0}</strong><small>{bytes(run.outcome_bytes?.[x])}</small></div>)}</div>
     <p className="subtle">This expedition is separate from lifetime attempt counts.</p>
     {d?.controller?.error&&<ErrorText error={d.controller.error}/>}
     {d?.controller?.exit_reason&&<ErrorText error={d.controller.exit_reason}/>}
    </Panel>
    <Panel title="Function collection" subtitle="Latest valid attempt first · Select a function to inspect evidence" className="explorer">
     <div className="toolbar">
      <select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0)}} aria-label="Filter functions">
       <option value="ALL">All outcomes</option>{STATUS.map(x=><option key={x} value={x}>{x}</option>)}
      </select>
      <input type="search" aria-label="Search addresses or paths" placeholder="Address or source file…" value={typed} onChange={e=>setTyped(e.target.value)}/>
      <span className="subtle">{pretty(table?.total)} results · {bytes(table?.bytes)}</span>
     </div><ErrorText error={tableError}/>
     <div className="table-scroll"><table><thead><tr>
      <th>Function</th><th>Bytes</th><th>Byte agreement</th><th>Result</th><th>Effort</th><th>Attempts</th><th>Reason</th><th>Source</th>
     </tr></thead><tbody>{table?.items?.map(f=><tr key={f.va} onClick={()=>setDetail({kind:'function',id:f.va})} tabIndex={0}
      onKeyDown={e=>{if(e.key==='Enter')setDetail({kind:'function',id:f.va})}}>
      <td className="mono accent">{f.va}</td><td>{f.size}</td><td className="mono">{agreement(f.agreement,f.size)}</td><td><Tag value={f.status}/></td>
      <td>{f.effort||'—'}</td><td>{f.attempts}</td><td className="truncate-cell" title={f.reason}>{f.reason||'—'}</td>
      <td className="truncate-cell mono" title={f.file}>{f.file}</td></tr>)}
      {!table?.items?.length&&<tr><td colSpan={8} className="empty">No matching functions</td></tr>}</tbody></table></div>
     <div className="pager"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-75))}>Previous</button>
      <span>{pretty(offset+1)}–{pretty(Math.min(offset+75,table?.total||0))} / {pretty(table?.total)}</span>
      <button disabled={offset+75>=(table?.total||0)} onClick={()=>setOffset(offset+75)}>Next</button></div>
    </Panel>
   </div>
   <Panel title="Agent fleet" subtitle="Live workers, current functions and newest recorded events" className="fleet">
    <div className="agent-grid">{d?.workers?.map(w=>{
      const phase=!w.alive?'offline':w.phase==='VERIFIED'?'verified':w.phase==='NO SLOT'?'no-slot':w.phase==='PROBING'?'probing':'idle';
      return <button className={'agent '+phase} key={w.worker} onClick={()=>setDetail({kind:'agent',id:String(w.worker)})}>
       <div className="row-between"><strong>Agent {String(w.worker).padStart(2,'0')}</strong><Tag value={!w.alive?'OFFLINE':w.phase||'IDLE'} tone={phase}/></div>
       <span className="mono accent">{w.current||w.last_function||'Idle / waiting'}</span>
       <small>{bytes(w.bytes)} · PID {w.pid||'—'}</small>
       <div className="agent-events">{w.stream?.slice(-3).map((x,i)=><p key={i}><b>{x.kind}</b> {x.message?.slice(0,130)}</p>)}</div>
      </button>;
     })}
     {!d?.workers?.length&&<div className="empty">No worker activity recorded</div>}</div>
   </Panel>
  </div>
  {detail&&<Details item={detail} onClose={()=>setDetail(null)}/>} 
 </section>;
}
