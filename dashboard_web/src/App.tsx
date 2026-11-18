import {lazy,Suspense,useEffect,useState} from 'react';
import {controlsEnabled} from './api';
import type {View} from './types';
// Inactive views are unmounted and do not poll.
const Operations=lazy(()=>import('./views/Operations'));
const Knowledge=lazy(()=>import('./views/Knowledge'));

function initialView():View {
 return window.location.hash==='#knowledge'?'knowledge':'operations';
}
export default function App(){
 const [view,setView]=useState<View>(initialView);
 useEffect(()=>{
  const update=()=>setView(initialView());
  window.addEventListener('hashchange',update);
  return ()=>window.removeEventListener('hashchange',update);
 },[]);
 const navigate=(next:View)=>{
  window.location.hash=next==='knowledge'?'knowledge':'operations';
  setView(next);
 };
 return <div className="game-shell">
  <main className="view-content">
   <Suspense fallback={<div className="panel">Loading dashboard…</div>}>
    {view==='operations'?<Operations active onKnowledge={()=>navigate('knowledge')}/>:<Knowledge active/>}
   </Suspense>
  </main>
  {view==='knowledge'&&<nav className="knowledge-return-dock" aria-label="Dashboard navigation">
   <span className="dock-id"><span className="mini-orb">S</span><strong>SAD</strong></span>
   <button className="primary" onClick={()=>navigate('operations')}>Operations</button>
   <span className="subtle">Knowledge</span>
  </nav>}
  <footer className="app-footer status-pod">
   <span className={'status-dot '+(controlsEnabled?'online':'idle')}></span>
   <span>{controlsEnabled?'Controls enabled':'Read-only'}</span>
  </footer>
 </div>;
}
