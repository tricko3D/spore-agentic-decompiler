/** The one browser/network gateway. No filesystem, SQLite or process access. */
declare global {interface Window {__SAD_TOKEN__?:string;__SAD_CONTROL__?:boolean}}
export const controlsEnabled=Boolean(window.__SAD_CONTROL__);
export function query(params:Record<string,string|number|undefined>):string {
 const args=new URLSearchParams();
 Object.entries(params).forEach(([key,value])=>{if(value!==undefined&&value!=='')args.set(key,String(value))});
 return args.toString();
}
export async function get<T>(route:string,signal?:AbortSignal):Promise<T> {
 const response=await fetch(route,{cache:'no-store',signal,credentials:'same-origin'});
 const data=await response.json() as T&{error?:string};
 if(!response.ok||data.error)throw Error(data.error||'HTTP '+response.status);
 return data;
}
export async function post<T>(route:string,payload:object):Promise<T> {
 if(!controlsEnabled)throw Error('Read-only dashboard. Restart with --control.');
 const response=await fetch(route,{method:'POST',headers:{
  'Content-Type':'application/json','X-Spore-Token':window.__SAD_TOKEN__||''
 },credentials:'same-origin',body:JSON.stringify(payload)});
 const data=await response.json() as T&{error?:string};
 if(!response.ok||data.error)throw Error(data.error||'HTTP '+response.status);
 return data;
}
export const money=(v:number|null|undefined)=>typeof v==='number'?'$'+v.toFixed(v<.01?5:3):'—';
export const pretty=(v:number|null|undefined)=>typeof v==='number'?v.toLocaleString('en-US'):'—';
export const bytes=(v:number|null|undefined)=>typeof v==='number'?pretty(v)+' B':'—';
