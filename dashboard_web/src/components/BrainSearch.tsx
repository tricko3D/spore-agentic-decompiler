import {useState,type FormEvent} from 'react';
import {get,query} from '../api';
import {Panel,Tag} from './Panel';

interface SearchResults {
 mode:'hybrid'|'lexical';
 techniques:{id:number;title:string;problem:string;category:string}[];
 findings:{id:number;va:string;pass:number;summary:string;status:string;verified_match:boolean}[];
}
export default function BrainSearch({onTechnique,onFinding}:{
 onTechnique:(id:number)=>void;onFinding:(id:number)=>void
}){
 const [text,setText]=useState(''),[querying,setQuerying]=useState(false),
 [result,setResult]=useState<SearchResults|null>(null),[error,setError]=useState('');
 async function search(event:FormEvent<HTMLFormElement>){
  event.preventDefault();
  if(text.trim().length<2)return;
  setQuerying(true);setError('');
  try{setResult(await get<SearchResults>('/api/knowledge/search?'+query({text:text.trim()})))}
  catch(e){setError((e as Error).message)}
  finally{setQuerying(false)}
 }
 return <Panel title="Search Collective Memory" subtitle="Ask about compiler behavior, registers, calling conventions or specific mismatches; retrieval is local and free"
  action={<span className="subtle">FTS5 + 384D similarity</span>}>
  <form className="brain-search" onSubmit={e=>void search(e)}>
   <input type="search" placeholder="Why is MSVC putting this in EBX instead of ESI?"
    value={text} onChange={e=>setText(e.target.value)} maxLength={180}
    aria-label="Search Collective Memory"/>
   <button className="primary" disabled={querying||text.trim().length<2}>{querying?'Searching…':'Search brain'}</button>
  </form>
  {error&&<p className="error-text">{error}</p>}
  {result&&<div className="brain-search-results">
   <div className="row-between"><span className="subtle">Matched original investigations and reusable techniques</span>
    <Tag value={result.mode==='hybrid'?'semantic + lexical':'lexical fallback'}/></div>
   <div className="brain-match-grid">
    <div><h3>Reusable techniques</h3>
     {result.techniques.map(x=><button className="brain-match" key={x.id} onClick={()=>onTechnique(x.id)}>
      <span className="mono accent">#{x.id} · {x.category.replaceAll('_',' ')}</span>
      <strong>{x.title}</strong><small>{x.problem}</small>
     </button>)}
     {!result.techniques.length&&<p className="subtle">No applicable consolidated techniques yet.</p>}
    </div>
    <div><h3>Original findings</h3>
     {result.findings.map(x=><button className="brain-match" key={x.id} onClick={()=>onFinding(x.id)}>
      <span className="row-between"><strong className="mono accent">{x.va} · pass {x.pass}</strong>
        <Tag value={x.status}/></span><small>{x.summary}</small>
     </button>)}
     {!result.findings.length&&<p className="subtle">No matching investigations.</p>}
    </div>
   </div>
  </div>}
 </Panel>;
}
