import {memo,useState,type FormEvent} from 'react';
import {get,query} from '../api';
import {Drawer,Tag} from './Panel';

interface SearchResults {
 mode:'hybrid'|'lexical';
 techniques:{id:number;title:string;problem:string;category:string}[];
 findings:{id:number;va:string;pass:number;summary:string;status:string;verified_match:boolean}[];
}
/** On-demand semantic retrieval. Never occupies permanent page space. */
function BrainSearch({onTechnique,onFinding}:{
 onTechnique:(id:number)=>void;onFinding:(id:number)=>void
}){
 const [open,setOpen]=useState(false),[text,setText]=useState('');
 const [querying,setQuerying]=useState(false),[result,setResult]=useState<SearchResults|null>(null);
 const [error,setError]=useState('');
 async function search(event:FormEvent<HTMLFormElement>){
  event.preventDefault();
  if(querying||text.trim().length<2)return;
  setQuerying(true);setError('');setResult(null);
  try{setResult(await get<SearchResults>('/api/knowledge/search?'+query({text:text.trim()})))}
  catch(e){setError(e instanceof Error?e.message:String(e))}
  finally{setQuerying(false)}
 }
 function chooseTechnique(id:number){setOpen(false);onTechnique(id)}
 function chooseFinding(id:number){setOpen(false);onFinding(id)}
 return <>
  <button type="button" className="brain-search-trigger" onClick={()=>{
   setOpen(true);setError('');
  }} title="Search Collective Memory with FTS5 and local semantic retrieval">
   Search memory
  </button>
  {open&&<Drawer title="Search Collective Memory"
   subtitle="Compiler techniques, findings and original checker evidence"
   onClose={()=>setOpen(false)}>
   <form className="brain-search-dialog-form" role="search" onSubmit={event=>void search(event)}>
    <input type="search" placeholder="Why is MSVC putting this in EBX instead of ESI?"
     value={text} onChange={event=>setText(event.target.value)} maxLength={180}
     aria-label="Semantic search of Collective Memory"/>
    <button className="primary" disabled={querying||text.trim().length<2}>
     {querying?'Searching…':'Search'}
    </button>
   </form>
   {error&&<p className="brain-search-dialog-error" role="alert">{error}</p>}
   {result&&<div className="brain-search-results">
    <div className="row-between">
     <span className="subtle">Matching investigations and compiler techniques</span>
     <Tag value={result.mode==='hybrid'?'semantic + lexical':'lexical fallback'}/>
    </div>
    <div className="brain-match-grid">
     <div><h3>Reusable techniques</h3>
      {result.techniques.map(x=><button className="brain-match" key={x.id}
       onClick={()=>chooseTechnique(x.id)}>
       <span className="mono accent">#{x.id} · {(x.category||'other').replaceAll('_',' ')}</span>
       <strong>{x.title}</strong><small>{x.problem}</small>
      </button>)}
      {!result.techniques.length&&<p className="subtle">No matching techniques.</p>}
     </div>
     <div><h3>Original findings</h3>
      {result.findings.map(x=><button className="brain-match" key={x.id}
       onClick={()=>chooseFinding(x.id)}>
       <span className="row-between">
        <strong className="mono accent">{x.va} · pass {x.pass}</strong>
        <Tag value={x.status}/>
       </span><small>{x.summary}</small>
      </button>)}
      {!result.findings.length&&<p className="subtle">No matching investigations.</p>}
     </div>
    </div>
   </div>}
  </Drawer>}
 </>;
}
export default memo(BrainSearch);
