import {useCallback,useEffect,useState} from 'react';
import {controlsEnabled,get,post,query} from '../api';
import {Drawer,ErrorText} from './Panel';
import PreviewResultModal from './PreviewResultModal';
import type {AllowList,Claimants,Plan,Snapshot} from '../types';

type Launch={module:string;workers:number;max_inflight:number;passes:number;minutes:number;budget:number;claim_user:string};
const START:Launch={module:'SPGonzago',workers:16,max_inflight:16,passes:3,minutes:20,budget:20,claim_user:''};

function AllowListEditor({onClose}:{onClose:()=>void}){
 const [doc,setDoc]=useState<AllowList|null>(null),[content,setContent]=useState('');
 const [error,setError]=useState(''),[saving,setSaving]=useState(false);
 const reload=useCallback(async()=>{
  try{const d=await get<AllowList>('/api/allowlist');setDoc(d);setContent(d.content);setError('')}
  catch(e){setError((e as Error).message)}
 },[]);
 useEffect(()=>{void reload()},[reload]);
 const save=async()=>{
  if(!doc||!controlsEnabled)return;
  setSaving(true);
  try{const d=await post<AllowList>('/api/allowlist',{content,revision:doc.revision});
   setDoc(d);setContent(d.content);setError('Saved '+d.count+' addresses.')}
  catch(e){setError((e as Error).message)}
  finally{setSaving(false)}
 };
 return <Drawer title="Retry allowlist" subtitle={doc?.path} onClose={onClose}>
  <p>The validated retry scope for all-modules and allowlist launches. Saving is blocked while workers run.</p>
  <div className="row-between"><span className="subtle">{doc?.count??0} addresses · {doc?.valid?'Valid':'Not validated'}</span>
   <button onClick={()=>void reload()}>Reload</button></div>
  <textarea className="json-editor mono" spellCheck={false} value={content} onChange={e=>setContent(e.target.value)}/>
  <ErrorText error={error||doc?.validation_error}/>
  <button className="primary" disabled={!controlsEnabled||saving||!doc} onClick={()=>void save()}>{saving?'Saving…':'Save allowlist'}</button>
 </Drawer>;
}
export default function AgentDock({snapshot,onUpdated}:{snapshot:Snapshot|null;onUpdated:()=>void}){
 const [form,setForm]=useState<Launch>(START),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
 const [preview,setPreview]=useState<Plan|null>(null),[claimants,setClaimants]=useState<Claimants|null>(null),[editor,setEditor]=useState(false);
 const [previewError,setPreviewError]=useState<string|null>(null),[showPreview,setShowPreview]=useState(false);
 const running=Boolean(snapshot?.controller?.running||snapshot?.run?.state==='running');
 const update=<K extends keyof Launch>(field:K,value:Launch[K])=>{setForm(old=>({...old,[field]:value}));setPreview(null)};
 useEffect(()=>{
  if(form.module!=='__claimed__')return;
  let alive=true;void get<Claimants>('/api/claimants')
   .then(d=>{if(alive)setClaimants(d)}).catch(e=>{if(alive)setNotice((e as Error).message)});
  return ()=>{alive=false};
 },[form.module]);
 async function toggle(){
  if(!controlsEnabled||busy)return;
  setBusy(true);setNotice('');
  try{
   if(running){
    await post('/api/control/stop',{});setNotice('Stop requested; waiting for workers to exit.');
   }else{
    if(form.module==='__claimed__'&&!form.claim_user)throw Error('Choose a claimant before launching');
    await post('/api/control/start',{module:form.module,workers:form.workers,
      max_inflight:form.max_inflight,passes:form.passes,minutes:form.minutes,
      budget:form.budget,claim_user:form.claim_user||undefined});
    setNotice('Decompilation launched.');
   }
   onUpdated();
  }catch(e){setNotice((e as Error).message)}
  finally{setBusy(false)}
 }
 const getPlan=async()=>{
  if(busy)return;
  setBusy(true);setNotice('');setPreview(null);setPreviewError(null);
  try{
   const plan=await get<Plan>('/api/plan?'+query({
    module:form.module,workers:form.workers,claim_user:form.module==='__claimed__'?form.claim_user:undefined
   }));
   setPreview(plan);setShowPreview(true);
  }catch(e){setPreviewError(e instanceof Error?e.message:String(e));setShowPreview(true)}
  finally{setBusy(false)}
 };
 return <><div className="control-dock" aria-label="Agent controls">
  <div className="control-fields">
   <label>Module<select value={form.module} onChange={e=>update('module',e.target.value)}>
    <option value="SPGonzago">SPGonzago</option>
    {snapshot?.modules?.filter(x=>x!=='SPGonzago').map(x=><option key={x} value={x}>{x}</option>)}
    <option value="*">All classified (allowlist)</option><option value="__allowlist__">Retry allowlist</option>
    <option value="__claimed__">Claimed by GitHub user</option>
   </select></label>
   {form.module==='__claimed__'&&<label>Claimant<select value={form.claim_user} onChange={e=>update('claim_user',e.target.value)}>
    <option value="">Choose…</option>{claimants?.users?.map(x=><option key={x.name} value={x.name}>{x.name}{x.count!=null?' ('+x.count+')':''}</option>)}
   </select></label>}
   {([['workers','Workers',1,32],['max_inflight','In flight',1,32],
     ['passes','Passes',1,24],['minutes','Idle min',15,120]] as const).map(([field,label,min,max])=>
     <label key={field}>{label}<input type="number" min={min} max={max} value={form[field]}
      onChange={e=>update(field,Number(e.target.value))}/></label>)}
   <label>Budget $<input className="budget-input" type="number" min=".01" step=".01"
     value={form.budget} onChange={e=>update('budget',Number(e.target.value))}/></label>
  </div>
  <div className="dock-actions">
   <button onClick={()=>void getPlan()} disabled={busy||running}>Preview</button>
   <button onClick={()=>setEditor(true)}>Allowlist</button>
   <button className={running?'danger':'primary'} disabled={!controlsEnabled||busy}
     onClick={()=>void toggle()}>{busy?'Working…':running?'Stop workers':'Launch'}</button>
  </div>
 </div>
  {notice&&<div className="dock-message" role="status">{notice}</div>}
  {showPreview&&<PreviewResultModal plan={preview} error={previewError} onClose={()=>setShowPreview(false)}/>}
  {editor&&<AllowListEditor onClose={()=>setEditor(false)}/>}
 </>;
}
