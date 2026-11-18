import {memo,useMemo,useState} from 'react';
import type {BrainGraph as Graph} from '../types';

/**
 * Evidence map, not a decorative neural network.
 *
 * The previous full-width SVG scaled its labels with the viewport and
 * repainted hundreds of paths on scroll. This bounded, responsive adjacency
 * explorer renders only the selected technique's *actual* source links.
 * No canvas, SVG line animation, zoom transform or nested scroll container.
 */
function BrainGraph({graph,onTechnique,onFinding}:{
 graph:Graph|null;
 onTechnique:(id:number)=>void;
 onFinding:(id:number)=>void;
}){
 const [selectedId,setSelectedId]=useState<number|null>(null);
 const [showAll,setShowAll]=useState(false);
 const [showAllSources,setShowAllSources]=useState(false);

 const techniques=graph?.techniques||[];
 const selected=techniques.find(x=>x.id===selectedId)||techniques[0];
 const connections=useMemo(()=>{
  if(!graph||!selected)return [];
  const linked=new Set(graph.links.filter(x=>x.technique_id===selected.id)
    .map(x=>x.finding_id));
  return graph.findings.filter(x=>linked.has(x.id));
 },[graph,selected]);

 if(!techniques.length){
  return <div className="empty">The evidence map fills in as reusable compiler
    techniques are linked to real investigations.</div>;
 }

 const techniqueRows=showAll?techniques:techniques.slice(0,7);
 const sourceRows=showAllSources?connections:connections.slice(0,6);
 return <div className="evidence-map" aria-label="Compiler technique and original investigation evidence explorer">
  <div className="evidence-index">
   <div className="evidence-map-caption">TECHNIQUES <span>{techniques.length}</span></div>
   <div className="evidence-index-items">
    {techniqueRows.map(x=>{
     const active=x.id===selected.id;
     const count=graph?.links.filter(link=>link.technique_id===x.id).length||0;
     return <button type="button" key={x.id}
      className={'evidence-node'+(active?' active':'')}
      aria-pressed={active} onClick={()=>{setSelectedId(x.id);setShowAllSources(false)}}>
      <span className={'evidence-node-dot '+(x.evidence_level==='observed'?'observed':'hypothesis')}/>
      <span className="evidence-node-body"><strong title={x.title}>{x.title}</strong>
       <small>#{x.id} · {x.category.replaceAll('_',' ')}</small></span>
      <span className="evidence-node-count" title={count+' linked investigations'}>{count}</span>
     </button>;
    })}
   </div>
   {techniques.length>7&&<button type="button" className="evidence-map-more"
    onClick={()=>setShowAll(v=>!v)}>{showAll?'Show fewer techniques':'Show all '+techniques.length+' techniques'}</button>}
  </div>
  <div className="evidence-map-connection" aria-hidden="true"><span/><b>→</b></div>
  <div className="evidence-sources">
   <div className="row-between evidence-map-caption"><span>LINKED INVESTIGATIONS <small>{connections.length}</small></span>
    <button type="button" className="evidence-open"
      onClick={()=>onTechnique(selected.id)}>Inspect technique #{selected.id}</button>
   </div>
   <div className="evidence-current-title" title={selected.title}>{selected.title}</div>
   <div className="evidence-source-items">
    {sourceRows.map(x=><button type="button" className="evidence-source-row"
      key={x.id} onClick={()=>onFinding(x.id)}>
      <span className={'evidence-source-dot '+(x.verified_match?'match':'unmatched')}/>
      <span className="mono">{x.va}</span>
      <span className={'evidence-source-status '+(x.verified_match?'match':'unmatched')}>
       {x.verified_match?'MATCH':x.status}
      </span>
     </button>)}
    {!connections.length&&<p className="subtle evidence-empty">No current linked source in this graph sample.
     The full technique detail contains its evidence history.</p>}
   </div>
   {connections.length>6&&<button type="button" className="evidence-map-more"
      onClick={()=>setShowAllSources(v=>!v)}>
      {showAllSources?'Show fewer findings':'Show all '+connections.length+' linked findings'}</button>}
   <p className="evidence-map-footnote">Links come from current Collective Memory source hashes. A matched function does not verify the technique itself.</p>
  </div>
 </div>;
}
export default memo(BrainGraph);
