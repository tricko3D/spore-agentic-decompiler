import type {ReactNode} from 'react';

/** An architecture map, not a live process monitor.
 * Every node names an actual subsystem and keeps its notes inside its card.
 */
function Node({kind,title,source,children}:{
 kind:'ui'|'gateway'|'service'|'workers'|'data';
 title:string;source:string;children:ReactNode;
}){
 return <article className={'infra-node infra-'+kind}>
  <div className="infra-node-title"><strong>{title}</strong><span>{kind}</span></div>
  <p className="infra-node-description">{children}</p>
  <code title={source}>{source}</code>
 </article>;
}
function Arrow({label,reverse=false}:{label:string;reverse?:boolean}){
 return <div className={'infra-flow-arrow'+(reverse?' reverse':'')} aria-label={label}>
  <span>{label}</span><i aria-hidden="true"/>
 </div>;
}
export default function InfraDocs(){
 return <section className="infra-view view-body">
  <div className="infra-diagram-scroll infra-diagram-only" aria-label="SAD infrastructure diagram">
   <div className="infra-flow-canvas">
    <Node kind="ui" title="React / TypeScript dashboard" source="dashboard_web/src/">
     Agents, Brain and Infra Docs display state and submit only permitted actions. Closing the page does not stop workers.
    </Node>
    <Arrow label="Local HTTP"/>
    <Node kind="gateway" title="Python localhost API" source="dashboard/server.py">
     Serves paginated JSON and validates control requests with token and Origin checks on 127.0.0.1:8765.
    </Node>
    <div className="infra-flow-junction" aria-hidden="true"><span/><span/></div>
    <div className="infra-branches">
     <div className="infra-lane">
      <div className="infra-lane-heading"><strong>AGENT EXECUTION</strong><span>Operational data flow</span></div>
      <div className="infra-lane-flow">
       <Node kind="service" title="Launch / stop controller" source="spore_dashboard_control.py">
        Checks module scope, worker count and spend limits before starting or stopping a run.
       </Node>
       <Arrow label="Schedules"/>
       <Node kind="workers" title="Decompiler workers + checker" source="spore_parallel_sqlite.py">
        Runs compiler experiments, compares retail bytes and records matched or failed attempts.
       </Node>
       <Arrow label="Writes"/>
       <Node kind="data" title="Operational SQLite" source="work/scratch/">
        Persists function outcomes, attempt history, byte differences and worker activity.
       </Node>
      </div>
     </div>
     <div className="infra-lane">
      <div className="infra-lane-heading"><strong>COLLECTIVE MEMORY</strong><span>Research and consolidation</span></div>
      <div className="infra-lane-flow">
       <Node kind="service" title="Knowledge API + retrieval" source="dashboard/knowledge.py">
        Reads paginated evidence, techniques and findings using FTS5 and optional local embeddings.
       </Node>
       <Arrow label="Reads" reverse/>
       <Node kind="data" title="Knowledge SQLite" source="memory/knowledge.sqlite3">
        Holds original findings, source-checked evidence links, extracted techniques and annotations.
       </Node>
       <Arrow label="Writes" reverse/>
       <Node kind="workers" title="DeepSeek consolidator" source="spore_technique_consolidator.py">
        Separate systemd service, independently budgeted; extracts source-backed observations with retries.
       </Node>
      </div>
     </div>
    </div>
   </div>
  </div>
 </section>;
}
