import {memo,useMemo} from 'react';
import type {BrainGraph as Graph} from '../types';

/** Browsable evidence graph, not an invented neural network visualization.
 * Each edge is a real, current source-hash-validated technique/finding link.
 */
export type GraphViewport={x:number;y:number;width:number;height:number;pixelWidth:number;pixelHeight:number};
function BrainGraph({graph,onTechnique,onFinding,viewport}:{
 graph:Graph|null;onTechnique:(id:number)=>void;onFinding:(id:number)=>void;
 viewport?:GraphViewport;
}){
 const layout=useMemo(()=>{
  const techniques=graph?.techniques?.slice(0,22)||[];
  const findings=graph?.findings?.slice(0,30)||[];
  const left=techniques.map((item,i)=>({...item,x:270,y:44+i*38}));
  const right=findings.map((item,i)=>({...item,x:715,y:44+i*28}));
  const max=Math.max(left.at(-1)?.y||0,right.at(-1)?.y||0,300)+50;
  const found=new Map(right.map(x=>[x.id,x]));
  const choices=new Map(left.map(x=>[x.id,x]));
  const lines=(graph?.links||[]).filter(x=>choices.has(x.technique_id)&&found.has(x.finding_id))
   .map(x=>({key:x.technique_id+':'+x.finding_id,
    a:choices.get(x.technique_id)!,b:found.get(x.finding_id)!}));
  return {left,right,lines,height:max};
 },[graph]);
 if(!layout.left.length)return <div className="empty">The graph will appear as DeepSeek extracts reusable techniques.</div>;
 return <div className={'graph-wrap'+(viewport?' graph-svg-viewport':'')}><svg className="brain-svg"
  width={viewport?.pixelWidth??980} height={viewport?.pixelHeight??layout.height}
  viewBox={viewport?[viewport.x,viewport.y,viewport.width,viewport.height].join(' '):'0 0 980 '+layout.height}
  role="img"
  aria-label="Interactive graph connecting consolidated compiler techniques to their original investigations">
  <text x="26" y="22" className="graph-heading">COMPILER TECHNIQUES</text>
  <text x="692" y="22" className="graph-heading">SOURCE FINDINGS</text>
  {layout.lines.map(x=><path key={x.key} className="graph-edge"
    d={'M '+x.a.x+' '+x.a.y+' C 460 '+x.a.y+', 535 '+x.b.y+', '+x.b.x+' '+x.b.y}/>)}
  {layout.left.map(x=><g key={x.id} className="graph-item" onClick={()=>onTechnique(x.id)} tabIndex={0}
    role="button" aria-label={'Inspect '+x.title} onKeyDown={e=>{if(e.key==='Enter')onTechnique(x.id)}}>
    <circle cx={x.x} cy={x.y} r="9" className="graph-technique"/>
    <text x={x.x-19} y={x.y+4} textAnchor="end">{(x.title||'Untitled technique').length>35?(x.title||'Untitled technique').slice(0,34)+'…':(x.title||'Untitled technique')}</text>
    <title>{x.title}</title>
   </g>)}
  {layout.right.map(x=><g key={x.id} className="graph-item" onClick={()=>onFinding(x.id)} tabIndex={0}
    role="button" aria-label={'Inspect finding '+x.va} onKeyDown={e=>{if(e.key==='Enter')onFinding(x.id)}}>
    <circle cx={x.x} cy={x.y} r="7" className={x.verified_match?'graph-match':'graph-finding'}/>
    <text x={x.x+15} y={x.y+4}>{x.va}</text>
   </g>)}
 </svg></div>;
}
export default memo(BrainGraph);
