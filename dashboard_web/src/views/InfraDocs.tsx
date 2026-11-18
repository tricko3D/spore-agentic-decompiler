import type {ReactNode} from 'react';

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
  <div className="infra-diagram-scroll infra-diagram-only" aria-label="SAD infrastructure diagram">

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
  </div>
 </section>;
}
