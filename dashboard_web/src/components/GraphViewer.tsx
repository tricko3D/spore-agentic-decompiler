import {useCallback,useEffect,useMemo,useRef,useState,type PointerEvent} from 'react';
import {createPortal} from 'react-dom';
import BrainGraph from './BrainGraph';
import type {BrainGraph as Graph} from '../types';

type Camera={x:number;y:number;scale:number};
const WIDTH=980;
const minZoom=.2,maxZoom=3.5;
const clamp=(n:number,a:number,b:number)=>Math.min(b,Math.max(a,n));

/** Render a real evidence graph in a portaled viewport with pointer panning,
 * cursor-centered wheel zoom, toolbar zoom and fit-to-view. The graph itself
 * stays mounted in one fixed-size coordinate system, not the page grid.
 */
export default function GraphViewer({graph,onTechnique,onFinding,onClose}:{
 graph:Graph|null;onTechnique:(id:number)=>void;onFinding:(id:number)=>void;onClose:()=>void;
}){
 const viewport=useRef<HTMLDivElement|null>(null);
 const drag=useRef<{x:number;y:number;camera:Camera}|null>(null);
 const moved=useRef(false);
 const [camera,setCamera]=useState<Camera>({x:25,y:25,scale:1});
 const height=useMemo(()=>{
  const countA=Math.min(22,graph?.techniques?.length||0);
  const countB=Math.min(30,graph?.findings?.length||0);
  return Math.max(300,44+Math.max(0,countA-1)*38,44+Math.max(0,countB-1)*28)+50;
 },[graph]);
 const fit=useCallback(()=>{
  const node=viewport.current;
  if(!node)return;
  const width=node.clientWidth,availableHeight=node.clientHeight;
  if(!width||!availableHeight)return;
  const scale=clamp(Math.min((width-56)/WIDTH,(availableHeight-56)/height),minZoom,1.4);
  setCamera({x:(width-WIDTH*scale)/2,y:(availableHeight-height*scale)/2,scale});
 },[height]);
 useEffect(()=>{
  fit();
  const observer=new ResizeObserver(()=>fit());
  if(viewport.current)observer.observe(viewport.current);
  return ()=>observer.disconnect();
 },[fit]);
 useEffect(()=>{
  const onKey=(e:KeyboardEvent)=>{
   if(e.key==='Escape')onClose();
   if(e.key==='0')fit();
  };
  document.addEventListener('keydown',onKey);
  return ()=>document.removeEventListener('keydown',onKey);
 },[onClose,fit]);
 const zoom=useCallback((factor:number,point?:{x:number;y:number})=>{
  const node=viewport.current;if(!node)return;
  const anchor=point||{x:node.clientWidth/2,y:node.clientHeight/2};
  setCamera(c=>{
   const scale=clamp(c.scale*factor,minZoom,maxZoom);
   const ratio=scale/c.scale;
   return {scale,x:anchor.x-(anchor.x-c.x)*ratio,y:anchor.y-(anchor.y-c.y)*ratio};
  });
 },[]);
 useEffect(()=>{
  const node=viewport.current;if(!node)return;
  const wheel=(event:WheelEvent)=>{
   event.preventDefault();
   const rect=node.getBoundingClientRect();
   zoom(Math.exp(-event.deltaY*.00135),{x:event.clientX-rect.left,y:event.clientY-rect.top});
  };
  node.addEventListener('wheel',wheel,{passive:false});
  return ()=>node.removeEventListener('wheel',wheel);
 },[zoom]);
 function down(event:PointerEvent<HTMLDivElement>){
  if(event.button!==0)return;
  drag.current={x:event.clientX,y:event.clientY,camera};
  moved.current=false;
 }
 function move(event:PointerEvent<HTMLDivElement>){
  const active=drag.current;if(!active)return;
  const dx=event.clientX-active.x,dy=event.clientY-active.y;
  if(Math.abs(dx)+Math.abs(dy)>5&&!moved.current){moved.current=true;event.currentTarget.setPointerCapture(event.pointerId)}
  setCamera({scale:active.camera.scale,x:active.camera.x+dx,y:active.camera.y+dy});
 }
 function up(event:PointerEvent<HTMLDivElement>){
  drag.current=null;
  if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);
 }
 const chooseTechnique=(id:number)=>{onClose();onTechnique(id)};
 const chooseFinding=(id:number)=>{onClose();onFinding(id)};
 return createPortal(<div className="graph-viewer-backdrop"
  onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}>
  <section className="graph-viewer" role="dialog" aria-modal="true" aria-label="Interactive evidence graph">
   <div className="graph-viewer-toolbar" aria-label="Graph viewport controls">
    <div className="graph-viewer-actions">
     <span className="graph-zoom-label">{Math.round(camera.scale*100)}%</span>
     <button type="button" aria-label="Zoom out" onClick={()=>zoom(1/1.2)}>−</button>
     <button type="button" aria-label="Zoom in" onClick={()=>zoom(1.2)}>+</button>
     <button type="button" onClick={fit}>Fit</button>
     <button type="button" onClick={onClose}>Close</button>
    </div>
   </div>
   <div ref={viewport} className="graph-viewer-viewport" onPointerDown={down} onPointerMove={move}
    onPointerUp={up} onPointerCancel={up}
    onClickCapture={event=>{if(moved.current){event.stopPropagation();moved.current=false}}}>
    <div className="graph-viewer-world"
     style={{transform:'translate('+camera.x+'px,'+camera.y+'px) scale('+camera.scale+')'}}>
     <BrainGraph graph={graph} onTechnique={chooseTechnique} onFinding={chooseFinding}/>
    </div>
   </div>
   <footer className="graph-viewer-hint">
    Drag to pan · Scroll to zoom around cursor · Click a node to inspect evidence · 0 to fit
   </footer>
  </section>
 </div>,document.body);
}
