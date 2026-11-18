import {useCallback,useEffect,useRef,useState} from 'react';
import {get} from './api';

/**
 * Serial, visibility-aware polling. Each response settles before scheduling
 * the next one. A changed route/view aborts in-flight work, so stale
 * responses cannot repaint the old view or overtake newer query results.
 */
export function usePoll<T>(url:string,interval:number,active=true){
 // Bind cached data to the exact URL that produced it. Switching between
 // technique and finding tabs must never render old rows as the new type.
 const [response,setResponse]=useState<{url:string;value:T|null;error:string;loading:boolean}>({
  url:'',value:null,error:'',loading:true
 });
 const current=response.url===url;
 const value=current?response.value:null;
 const error=current?response.error:'';
 const loading=current?response.loading:true;
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
    setResponse({url,value:next,error:'',loading:false});
   }
  }catch(e){
   if(!controller.signal.aborted&&id===requestId.current){
    setResponse(prev=>({
     url,value:prev.url===url?prev.value:null,
     error:(e as Error).message,loading:false
    }));
   }
  }finally{
   signal?.removeEventListener('abort',onAbort);
   if(inFlight.current===controller)inFlight.current=null;
   // Success and failure already settle loading together with their URL.

  }
 },[url]);

 useEffect(()=>{
  if(!active)return;
  let disposed=false;
  let running=false;
  let timer:number|undefined;
  const signal=new AbortController();
  async function loop(){
   if(disposed||signal.signal.aborted||running)return;
   running=true;
   try{
    if(document.visibilityState!=='hidden')await refresh(signal.signal);
   }finally{
    running=false;
    if(!disposed)timer=window.setTimeout(()=>void loop(),interval);
   }
  }
  const onVisibility=()=>{
   if(document.visibilityState==='visible'&&!disposed&&!running){
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
