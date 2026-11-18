import {lazy,Suspense,useCallback,useEffect,useState} from 'react';
import {usePoll} from './hooks';
import AgentDock from './components/AgentDock';
import type {KnowledgeOverview,Snapshot,View} from './types';

// Only the selected workspace mounts. The bottom command bar never unmounts.
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
 const connection=snapshotError?'disconnected':snapshot?'connected':'connecting';
 return <div className="game-shell">
  <main className="view-content">
   <Suspense fallback={<div className="panel">Loading dashboard…</div>}>
    {view==='operations'
      ?<Operations active snapshot={snapshot} snapshotError={snapshotError}/>
      :<Knowledge active overview={overview} summaryError={summaryError} refreshOverview={refreshOverview}/>}
   </Suspense>
  </main>
  <footer className="bottom-hud" aria-label="SAD dashboard command bar">
   <div className="hud-brand"><span className="mini-orb">S</span><strong>SAD</strong></div>
   <nav className="hud-nav" aria-label="Dashboard views">
    <button type="button" className={view==='operations'?'selected':''}
     aria-current={view==='operations'?'page':undefined} onClick={()=>navigate('operations')}>Agents</button>
    <button type="button" className={view==='knowledge'?'selected':''}
     aria-current={view==='knowledge'?'page':undefined} onClick={()=>navigate('knowledge')}>Brain</button>
   </nav>
   <div className="hud-spacer" aria-hidden="true"/>
   <AgentDock snapshot={snapshot} onUpdated={refreshSnapshot}/>
   <div className="hud-spacer hud-spacer-end" aria-hidden="true"/>
   <div className={'hud-connection '+connection} role="status" title={snapshotError||'Dashboard backend connection'}>
    <span className={'status-dot '+(connection==='connected'?'online':'offline')}/>
    <strong>{connection==='connected'?'CONNECTED':connection==='disconnected'?'DISCONNECTED':'CONNECTING'}</strong>
   </div>
  </footer>
 </div>;
}
