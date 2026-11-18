import {useEffect,useState} from 'react';
import {controlsEnabled} from './api';
import type {View} from './types';
import Operations from './views/Operations';
import Knowledge from './views/Knowledge';

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
  <header className="app-header">
   <div className="brand"><span className="orb-mark">S</span><div><strong>SAD <span>MISSION CONTROL</span></strong>
    <p>Agentic Spore decompilation and shared compiler intelligence</p></div></div>
   <nav className="view-switch" aria-label="Dashboard views">
    <button className={view==='operations'?'selected':''} aria-current={view==='operations'?'page':undefined} onClick={()=>navigate('operations')}>◈ Operations</button>
    <button className={view==='knowledge'?'selected':''} aria-current={view==='knowledge'?'page':undefined} onClick={()=>navigate('knowledge')}>◎ Knowledge</button>
   </nav>
   <div className="mode-note"><span className={'status-dot '+(controlsEnabled?'online':'idle')}></span>{controlsEnabled?'Controls enabled':'Read-only'}</div>
  </header>
  <div className="view-content">
   <div hidden={view!=='operations'}><Operations active={view==='operations'}/></div>
   <div hidden={view!=='knowledge'}><Knowledge active={view==='knowledge'}/></div>
  </div>
  <footer className="app-footer"><span>SAD · Localhost only</span><span>Operations and knowledge run independently · <a href="/legacy">Legacy interface</a></span></footer>
 </div>;
}
