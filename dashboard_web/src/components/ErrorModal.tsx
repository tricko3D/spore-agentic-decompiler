import {useEffect,useState} from 'react';
import {createPortal} from 'react-dom';
import type {QueueError} from '../types';

/** Copies the exact stored error, never truncates or transforms its contents. */
export default function ErrorModal({error,onClose}:{error:QueueError;onClose:()=>void}){
 const [copied,setCopied]=useState(false);
 useEffect(()=>{
  const onKey=(e:KeyboardEvent)=>{if(e.key==='Escape')onClose()};
  document.addEventListener('keydown',onKey);
  return ()=>document.removeEventListener('keydown',onKey);
 },[onClose]);
 async function copy(){
  try{await navigator.clipboard.writeText(error.error||'');setCopied(true)}
  catch{setCopied(false)}
 }
 return createPortal(<div className="error-modal-backdrop"
  onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}>
  <section className="error-modal" role="dialog" aria-modal="true" aria-label={'Consolidation error for '+error.va}>
   <header className="error-modal-head">
    <div><span className="eyebrow">CONSOLIDATION ERROR</span><h2>{error.va}</h2></div>
    <button type="button" onClick={onClose}>Close</button>
   </header>
   <div className="error-modal-metadata">
    <span>Status: <strong>{error.state||'unknown'}</strong></span>
    <span>Failures: <strong>{error.failures}</strong></span>
    <span>Next retry: <strong>{error.next_retry_utc||'not scheduled'}</strong></span>
   </div>
   <div className="error-modal-label">Full recorded error</div>
   <pre className="error-modal-code">{error.error||'No detailed error content was recorded.'}</pre>
   <div className="error-modal-actions">
    <span className="subtle">{copied?'Copied to clipboard':''}</span>
    <button type="button" className="primary" onClick={()=>void copy()}>{copied?'Copied':'Copy error'}</button>
   </div>
  </section>
 </div>,document.body);
}
