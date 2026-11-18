import {useEffect,useState} from 'react';
import {controlsEnabled,money,post} from '../api';
import {Drawer,ErrorText,Panel} from './Panel';
import type {ConsolidatorState} from '../types';

/** Persistent consolidation controls, independent of the decompiler fleet. */
export default function KnowledgeDock({state,onRefresh}:{state:ConsolidatorState|null;onRefresh:()=>void}){
 const [editing,setEditing]=useState(false),[budget,setBudget]=useState(.25);
 const [batch,setBatch]=useState(3),[interval,setIntervalValue]=useState(180);
 const [busy,setBusy]=useState(false),[message,setMessage]=useState('');
 useEffect(()=>{
  if(!state||editing)return;
  setBudget(state.settings.daily_usd);
  setBatch(state.settings.batch);
  setIntervalValue(state.settings.interval);
 },[state,editing]);
 const available=controlsEnabled&&Boolean(state?.controls_available);
 async function control(action:'start'|'stop'){
  if(!available||busy)return;
  setBusy(true);setMessage('');
  try{
   await post('/api/knowledge/service',{action});
   setMessage(action==='start'?'Consolidator start requested':'Consolidator pause requested');
   onRefresh();
  }catch(e){setMessage((e as Error).message)}
  finally{setBusy(false)}
 }
 async function save(){
  if(!available||busy)return;
  setBusy(true);setMessage('');
  try{
   await post('/api/knowledge/settings',{daily_usd:budget,batch,interval});
   setMessage('Settings saved for the next consolidation cycle.');
   setEditing(false);onRefresh();
  }catch(e){setMessage((e as Error).message)}
  finally{setBusy(false)}
 }
 return <>
  <Panel title="Knowledge consolidation" subtitle="Independent DeepSeek service" className="knowledge-service-panel">
   <div className="knowledge-service-body">
    <div className="knowledge-dock-state">
    <span className={'status-dot '+(state?.active?'online':'offline')}/>
    <div><strong>Knowledge</strong><small>{state?.active?'ONLINE':state?.installed?'OFFLINE':'NOT CONFIGURED'}</small></div>
   </div>
   <span className="knowledge-dock-limit" title="Daily consolidation budget">{state?money(state.settings.daily_usd)+'/day':'No service'}</span>
   <div className="knowledge-dock-actions">
    <button type="button" disabled={!available||busy||Boolean(state?.active)}
     onClick={()=>void control('start')}>Start</button>
    <button type="button" disabled={!available||busy||!state?.active}
     onClick={()=>void control('stop')}>Pause</button>
    <button type="button" disabled={!available||busy}
     onClick={()=>setEditing(true)}>Configure</button>
   </div>
   </div>
   {message&&<p className="subtle" role="status">{message}</p>}
  </Panel>
  {editing&&<Drawer title="Knowledge consolidation" subtitle={state?.mode||'Independent DeepSeek service'} onClose={()=>setEditing(false)}>
   <p>Settings apply to the next consolidation cycle. Knowledge remains independent of agent launches.</p>
   <div className="settings-grid">
    <label>Daily USD cap<input type="number" min=".01" max="10" step=".01"
     value={budget} disabled={!available||busy} onChange={e=>setBudget(Number(e.target.value))}/></label>
    <label>Batch<input type="number" min="1" max="20"
     value={batch} disabled={!available||busy} onChange={e=>setBatch(Number(e.target.value))}/></label>
    <label>Interval (seconds)<input type="number" min="30" max="7200"
     value={interval} disabled={!available||busy} onChange={e=>setIntervalValue(Number(e.target.value))}/></label>
   </div>
   <div className="inline-actions"><button type="button" onClick={()=>setEditing(false)}>Cancel</button>
    <button type="button" className="primary" disabled={!available||busy} onClick={()=>void save()}>{busy?'Saving…':'Save settings'}</button>
   </div><ErrorText error={message}/>
  </Drawer>}
 </>;
}
