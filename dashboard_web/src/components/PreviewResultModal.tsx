import {useEffect,useState} from 'react';
import {createPortal} from 'react-dom';
import type {Plan} from '../types';

/** Full, copyable raw Preview error. Successful previews use the same modal. */
export default function PreviewResultModal({plan,error,onClose}:{
 plan:Plan|null;error:string|null;onClose:()=>void
}){
 const [copied,setCopied]=useState(false);
 useEffect(()=>{
  const onKey=(event:KeyboardEvent)=>{if(event.key==='Escape')onClose()};
  document.addEventListener('keydown',onKey);
  return ()=>document.removeEventListener('keydown',onKey);
 },[onClose]);
 const hasError=error!==null;
 const fullText=hasError?error||'Unknown preview error':JSON.stringify(plan,null,2);
 async function copy(){
  try{await navigator.clipboard.writeText(fullText);setCopied(true)}
  catch{setCopied(false)}
 }
 return createPortal(<div className="preview-modal-backdrop"
  onMouseDown={event=>{if(event.target===event.currentTarget)onClose()}}>
  <section className="preview-result-modal" role="dialog" aria-modal="true"
   aria-label={hasError?'Preview failed':'Agent launch preview'}>
   <header className="preview-modal-header">
    <div><span className="eyebrow">AGENT CONTROL / PREVIEW</span>
     <h2>{hasError?'Preview failed':'Launch preview'}</h2></div>
    <button type="button" onClick={onClose}>Close</button>
   </header>
   {hasError
    ?<p className="preview-modal-description">The request returned an error. The complete response is preserved below for inspection and copying.</p>
    :<div className="preview-modal-facts">
      <span><strong>{plan?.eligible??0}</strong> eligible functions</span>
      <span><strong>{plan?.workers??0}</strong> workers</span>
      <span>{plan?.provisional?'Provisional scope':'Defined scope'}</span>
     </div>}
   <div className="preview-modal-label">{hasError?'Full error':'Complete preview response'}</div>
   <pre className="preview-modal-content">{fullText}</pre>
   <footer className="preview-modal-actions">
    <span role="status" className="subtle">{copied?'Copied to clipboard':''}</span>
    <button type="button" className="primary" onClick={()=>void copy()}>{copied?'Copied':'Copy '+(hasError?'error':'preview')}</button>
   </footer>
  </section>
 </div>,document.body);
}
