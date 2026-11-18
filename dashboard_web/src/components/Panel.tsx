import type {ReactNode} from 'react';
export function Panel({title,subtitle,action,children,className=''}:{
 title:string;subtitle?:string;action?:ReactNode;children:ReactNode;className?:string
}){
 return <section className={'panel '+className}>
  <header className="panel-head"><div><h2>{title}</h2>{subtitle&&<p className="subtle">{subtitle}</p>}</div>{action}</header>
  {children}
 </section>;
}
export function Metric({label,value,detail,tone=''}:{
 label:string;value:string|number;detail?:string;tone?:string
}){
 return <div className={'metric '+tone}><span>{label}</span><strong>{value}</strong><small>{detail||'\u00a0'}</small></div>;
}
export function Tag({value,tone}: {value:string;tone?:string}){
 return <span className={'tag '+(tone||value.toLowerCase().replace(/[^a-z0-9]+/g,'-'))}>{value}</span>;
}
export function Drawer({title,subtitle,onClose,children}:{title:string;subtitle?:string;onClose:()=>void;children:ReactNode}){
 return <div className="overlay" onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}>
   <aside className="detail-drawer" role="dialog" aria-modal="true" aria-label={title}>
    <header className="drawer-head"><div><h2>{title}</h2>{subtitle&&<p className="subtle mono">{subtitle}</p>}</div>
     <button type="button" onClick={onClose}>Close</button></header>
    <div className="drawer-content">{children}</div>
   </aside>
  </div>;
}
export function ErrorText({error}:{error?:string|null}){return error?<p className="error-text" role="alert">{error}</p>:null}
