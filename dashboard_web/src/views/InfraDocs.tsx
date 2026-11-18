import type {ReactNode} from 'react';
import {Panel} from '../components/Panel';

/** Read-only architecture description, not a simulated service monitor. */
function Block({kind,title,source,children}:{kind:string;title:string;source:string;children:ReactNode}){
 return <div className={'infra-node infra-'+kind}>
  <div className="infra-node-title"><strong>{title}</strong><span>{kind}</span></div>
  <p className="infra-node-description">{children}</p>
  <code>{source}</code>
 </div>;
}
function Connection({label}:{label:string}){
 return <div className="infra-link"><span>{label}</span></div>;
}
export default function InfraDocs(){
 return <section className="infra-view view-body">
  <div className="infra-heading">
   <div><h1>Infrastructure</h1><p>Actual SAD components and their responsibility boundaries</p></div>
   <span className="infra-heading-tag">LOCAL FIRST · SOURCE GROUNDED</span>
  </div>
  <div className="infra-layout">
   <Panel className="infra-topology" title="System topology" subtitle="Control plane, worker pipeline and evidence-backed memory">
    <div className="infra-diagram-scroll">
     <div className="infra-top-node">
      <Block kind="ui" title="React / TypeScript dashboard" source="dashboard_web/src/">
       Agents, Brain and Infra Docs share a persistent command bar. The UI reads data and submits explicitly permitted control actions.
      </Block>
     </div>
     <Connection label="Same-origin JSON / HTTP on 127.0.0.1:8765"/>
     <div className="infra-top-node">
      <Block kind="gateway" title="Python localhost API" source="dashboard/server.py">
       Threaded HTTP server, paginated read endpoints and narrow control routes with token and Origin checks. No arbitrary shell execution.
      </Block>
     </div>
     <div className="infra-fork" aria-hidden="true"/>
     <div className="infra-branches">
      <div className="infra-branch">
       <div className="infra-branch-heading">
        <strong>AGENT EXECUTION</strong><span>Operational pipeline</span>
       </div>
       <Block kind="service" title="Launch / stop controller" source="spore_dashboard_control.py">
        Validates launch scope, worker count, budgets and permissions before starting or stopping a run.
       </Block>
       <Connection label="Schedules worker attempts"/>
       <Block kind="workers" title="Decompilation workers + checker" source="spore_parallel_sqlite.py">
        Parallel function investigations and compiler experiments. Independent checking records matched, failed and deferred outcomes.
       </Block>
       <Connection label="Persists attempts and byte agreement"/>
       <Block kind="data" title="Operational SQLite + build evidence" source="work/scratch/">
        Worker events, match status, compiled bytes, differences and attempts feed the Agents dashboard.
       </Block>
      </div>
      <div className="infra-branch">
       <div className="infra-branch-heading">
        <strong>COLLECTIVE MEMORY</strong><span>Research consolidation pipeline</span>
       </div>
       <Block kind="service" title="Knowledge API + local retrieval" source="dashboard/knowledge.py">
        Read-only evidence queries, paginated techniques and findings, FTS5 and optional local embedding lookup.
       </Block>
       <Connection label="Source hash checked links"/>
       <Block kind="data" title="Knowledge SQLite" source="memory/knowledge.sqlite3">
        Original findings, compiler techniques, evidence links, retry state and separate research annotations.
       </Block>
       <Connection label="Consolidates source-backed findings"/>
       <Block kind="workers" title="DeepSeek technique consolidator" source="spore_technique_consolidator.py">
        Independent systemd user service with daily budget, batching and retry handling. Extracted techniques remain observations or hypotheses.
       </Block>
      </div>
     </div>
     <div className="infra-diagram-foot">
      <p><b>External inference:</b> Worker and consolidator processes make their respective model calls, never the browser.</p>
      <p><b>Trust boundary:</b> The frontend cannot directly read SQLite, mutate verified outcomes, or execute arbitrary commands.</p>
     </div>
    </div>
   </Panel>
   <Panel className="infra-notes-panel" title="Design commentary" subtitle="What the separation buys us">
    <div className="infra-notes">
     <article><strong>1. Presentation is disposable</strong>
      <p>Closing or reloading the dashboard does not stop decompilation or consolidation. They are separate, process-owned services.</p></article>
     <article><strong>2. Operational data is not knowledge</strong>
      <p>Checked bytes and attempt history live in the operational store. Techniques and research notes live in Collective Memory, linked to original investigations.</p></article>
     <article><strong>3. Provenance before promotion</strong>
      <p>Evidence links are validated against current source hashes. An extracted pattern is never automatically promoted to a compiler-verified fact.</p></article>
     <article><strong>4. Search stays local</strong>
      <p>FTS5 works without paid inference. An optional local embedding daemon improves retrieval when available.</p></article>
     <article><strong>5. Two independent budgets</strong>
      <p>Decompilation controls govern worker launches. The consolidator has its own daily USD cap and is managed by its existing systemd user unit.</p></article>
     <article><strong>6. A narrow API boundary</strong>
      <p>Only known launch, stop, annotation and settings actions are permitted. Source investigations remain read-only in the browser.</p></article>
    </div>
   </Panel>
  </div>
 </section>;
}
