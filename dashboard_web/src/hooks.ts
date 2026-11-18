import {useCallback,useEffect,useRef,useState} from 'react';
import {get} from './api';

/**
 * Serial, visibility-aware polling. Each response settles before scheduling
 * the next one. A changed route/view aborts in-flight work, so stale
 * responses cannot repaint the old view or overtake newer query results.
 */
export function usePoll<T>(url:string,interval:number,active=true){
 const [value,setValue]=useState<T|null>(null);
 const [error,setError]=useState('');
 const [loading,setLoading]=useState(true);
 const inFlight=useRef<AbortController|null>(null);
 const requestId=useRef(0);

 const refresh=useCallback(async(signal?:AbortSignal)=>{
  // Abort an older request when the user explicitly refreshes.
  inFlight.current?.abort();
  const controller=new AbortController();
  inFlight.current=controller;
  const id=++requestId.current;
  const onAbort=()=>controller.abort();
  signal?.addEventListener('abort',onAbort,{once:true});
  if(signal?.aborted)controller.abort();
  try{
   const next=await get<T>(url,controller.signal);
   if(!controller.signal.aborted&&id===requestId.current){
    setValue(next);setError('');
   }
  }catch(e){
   if(!controller.signal.aborted&&id===requestId.current){
    setError((e as Error).message);
   }
  }finally{
   signal?.removeEventListener('abort',onAbort);
   if(inFlight.current===controller)inFlight.current=null;
   if(!controller.signal.aborted&&id===requestId.current)setLoading(false);
  }
 },[url]);

 useEffect(()=>{
  if(!active)return;
  let disposed=false;
  let timer:number|undefined;
  const signal=new AbortController();
  async function loop(){
   if(disposed||signal.signal.aborted)return;
   if(document.visibilityState!=='hidden')await refresh(signal.signal);
   if(!disposed)timer=window.setTimeout(()=>void loop(),interval);
  }
  const onVisibility=()=>{
   if(document.visibilityState==='visible'&&!disposed){
    window.clearTimeout(timer);
    timer=window.setTimeout(()=>void loop(),50);
   }
  };
  void loop();
  document.addEventListener('visibilitychange',onVisibility);
  return ()=>{
   disposed=true;
   signal.abort();
   inFlight.current?.abort();
   window.clearTimeout(timer);
   document.removeEventListener('visibilitychange',onVisibility);
  };
 },[refresh,interval,active]);

 return {value,error,loading,refresh};
}
