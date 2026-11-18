import {memo,useState,type FormEvent} from 'react';
import {get,query} from '../api';
import {Drawer,Tag} from './Panel';

interface SearchResults {
 mode:'hybrid'|'lexical';
 techniques:{id:number;title:string;problem:string;category:string}[];
 findings:{id:number;va:string;pass:number;summary:string;status:string;verified_match:boolean}[];
}
function BrainSearch({onTechnique,onFinding}:{
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
 return <><form className="brain-global-search" role="search" onSubmit={e=>void search(e)}>
   <input type="search" placeholder="Search Collective Memory, techniques, findings and compiler behavior…"
    value={text} onChange={e=>setText(e.target.value)} maxLength={180}
    aria-label="Semantic search of Collective Memory"/>
   <button className="primary" disabled={querying||text.trim().length<2}>{querying?'Searching…':'Search memory'}</button>
   {error&&<span className="brain-global-error" role="status" title={error}>{error}</span>}
  </form>
 {result&&<Drawer title="Collective Memory results" subtitle="Traceable compiler techniques and original investigations" onClose={()=>setResult(null)}>
  <div className="brain-search-results">
   <div className="row-between"><span className="subtle">Matched original investigations and reusable techniques</span>
    <Tag value={result.mode==='hybrid'?'semantic + lexical':'lexical fallback'}/></div>
   <div className="brain-match-grid">
    <div><h3>Reusable techniques</h3>
     {result.techniques.map(x=><button className="brain-match" key={x.id} onClick={()=>{setResult(null);onTechnique(x.id)}}>
      <span className="mono accent">#{x.id} · {(x.category||'other').replaceAll('_',' ')}</span>
      <strong>{x.title}</strong><small>{x.problem}</small>
     </button>)}
     {!result.techniques.length&&<p className="subtle">No applicable consolidated techniques yet.</p>}
    </div>
    <div><h3>Original findings</h3>
     {result.findings.map(x=><button className="brain-match" key={x.id} onClick={()=>{setResult(null);onFinding(x.id)}}>
      <span className="row-between"><strong className="mono accent">{x.va} · pass {x.pass}</strong>
        <Tag value={x.status}/></span><small>{x.summary}</small>
     </button>)}
     {!result.findings.length&&<p className="subtle">No matching investigations.</p>}
    </div>
   </div>
  </div></Drawer>}
 </>;
}
export default memo(BrainSearch);
