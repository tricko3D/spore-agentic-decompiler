import {useCallback,useEffect,useState} from 'react';
import {get} from './api';
export function usePoll<T>(url:string,interval:number,active=true){
 const [value,setValue]=useState<T|null>(null);
 const [error,setError]=useState('');
 const [loading,setLoading]=useState(true);
 const refresh=useCallback(async(signal?:AbortSignal)=>{
  try{const next=await get<T>(url,signal);setValue(next);setError('')}
  catch(e){if((e as Error).name!=='AbortError')setError((e as Error).message)}
  finally{setLoading(false)}
 },[url]);
 useEffect(()=>{
  if(!active)return;
  const controller=new AbortController();
  void refresh(controller.signal);
  const timer=window.setInterval(()=>void refresh(controller.signal),interval);
  return ()=>{controller.abort();window.clearInterval(timer)}
 },[refresh,interval,active]);
 return {value,error,loading,refresh};
}
