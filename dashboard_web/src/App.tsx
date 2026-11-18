import {lazy,Suspense,useCallback,useEffect,useState} from 'react';
import {usePoll} from './hooks';
import AgentDock from './components/AgentDock';
import KnowledgeDock from './components/KnowledgeDock';
import type {KnowledgeOverview,Snapshot,View} from './types';

// Views mount independently. Both control bubbles always stay mounted.
const Operations=lazy(()=>import('./views/Operations'));
const Knowledge=lazy(()=>import('./views/Knowledge'));
function initialView():View{return window.location.hash==='#knowledge'?'knowledge':'operations'}
export default function App(){
 const [view,setView]=useState<View>(initialView);
 const {value:snapshot,error:snapshotError,refresh:refreshSnapshot}=usePoll<Snapshot>('/api/snapshot',3000);
 const {value:overview,error:summaryError,refresh:refreshOverview}=usePoll<KnowledgeOverview>('/api/knowledge/overview',10000);
 useEffect(()=>{
  const update=()=>setView(initialView());
  window.addEventListener('hashchange',update);
  return ()=>window.removeEventListener('hashchange',update);
 },[]);
 const navigate=useCallback((next:View)=>{
  window.location.hash=next==='knowledge'?'knowledge':'operations';
  setView(next);
 },[]);
 const toggle=useCallback(()=>navigate(view==='knowledge'?'operations':'knowledge'),[view,navigate]);
 return <div className="game-shell">
  <main className="view-content">
   <Suspense fallback={<div className="panel">Loading dashboard…</div>}>
    {view==='operations'
      ?<Operations active snapshot={snapshot} snapshotError={snapshotError}/>
      :<Knowledge active overview={overview} summaryError={summaryError} refreshOverview={refreshOverview}/>}
   </Suspense>
  </main>
  <div className="persistent-docks" aria-label="Persistent dashboard controls">
   <AgentDock snapshot={snapshot} onUpdated={refreshSnapshot} view={view} onViewSwitch={toggle}/>
   <KnowledgeDock state={overview?.consolidator||null} onRefresh={refreshOverview}/>
  </div>
 </div>;
}
