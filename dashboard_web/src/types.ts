export type Status='MATCH'|'FAIL'|'DEFER'|'SKIP'|'PENDING';
export type View='operations'|'knowledge'|'infra';
export interface AgentEvent {kind:string;message:string}
export interface Worker {worker:number;alive:boolean;phase?:string;current?:string;last_function?:string;pid?:number;bytes?:number|null;stream?:AgentEvent[]}
export interface RunInfo {id?:string;state?:string;finished_at?:string;workers?:number;live_workers?:number;targets?:number;completed?:number;bytes_completed?:number;bytes_target?:number;valid_attempts?:number;outcomes?:Record<string,number>;outcome_bytes?:Record<string,number>}
export interface ControllerInfo {running:boolean;pid?:number|null;external?:boolean;returncode?:number|null;error?:string;exit_reason?:string;settings?:Record<string,unknown>}
export interface Snapshot {
 generated_utc:string;counters:Record<string,number>;run:RunInfo;
 budget:{spend?:number|null;run_spend?:number|null;run_limit?:number|null;inflight?:number};
 adaptive:{concurrency?:number;ceiling?:number;cooldown_seconds?:number;provider_failures?:number};
 workers:Worker[];modules:string[];
 costs?:Record<string,{functions:number;cost:number;covered:number;attempts:number;per_function:number}>;
 effort?:Record<string,number>;effort_bytes?:Record<string,number>;
 bytes?:Record<string,number>;controller?:ControllerInfo;
}
export interface ByteAgreement {kind?:string;matched?:number|null;total?:number;compiled_size?:number;retail_size?:number}
export interface FunctionRow {va:string;last_activity:string|null;size:number;agreement?:ByteAgreement|null;status:Status;effort:string;attempts:number;reason:string;file:string}
export interface Paginated<T> {total:number;offset:number;items:T[];bytes?:number}
export interface FunctionDetail {va:string;size:number;file:string;current:boolean;reason:string;diff_status:string;agreement?:ByteAgreement|null;checker?:{summary:string[]};history:{pass:number;status:string;time:string;effort:string;note:string;cost:{cost?:number;steps?:number}}[]}
export interface AgentDetail {worker:number;va:string|null;bytes:number|null;events:AgentEvent[];log_status:string[]}
export interface AllowList {path:string;revision:string;content:string;exists:boolean;count:number;valid:boolean;validation_error?:string}
export interface Claimant {name:string;count?:number}
export interface Claimants {users:Claimant[];source?:string;updated?:string}
export interface Plan {eligible:number;workers:number;module:string;provisional:boolean;allowlist_active:boolean;allowlist_count:number;claim_count?:number;allowlist_breakdown?:Record<string,unknown>}
export interface Technique {id:number;title:string;category:string;problem:string;conditions:string;intervention:string;observed_effect:string;evidence_level:'hypothesis'|'observed';updated_utc?:string;source_count?:number}
export interface TechniqueSource {finding_id:number;va:string;pass_number:number;status:string;verified_match:number;evidence_path:string;evidence_quote:string;observation:string;created_utc:string}
export interface TechniqueDetail extends Technique {sources:TechniqueSource[];annotation:{note:string;pinned:boolean;updated_utc:string|null}}
export interface Finding {id:number;va:string;pass_number:number;status:Status;verified_match:number;title:string;summary:string;created_utc:string;evidence_path:string;technique_count?:number}
export interface FindingDetail extends Finding {details:string;experiments:{ordinal:number;flags:string;size_compiled:number|null;size_retail:number|null;diff_bytes:number|null;result:string}[];techniques:{id:number;title:string;category:string;observation:string}[]}
export interface CategoryCount {category:string;count:number}
export interface QueueError {va:string;finding_id:number;state:string;error:string;failures:number;next_retry_utc:string|null}
export interface ConsolidatorState {installed:boolean;mode:string;active:boolean;enabled:boolean;controls_available:boolean;unit:string;hint?:string;settings:{daily_usd:number;batch:number;interval:number}}
export interface KnowledgeOverview {installed:boolean;findings:number;verified_matches:number;techniques:number;links:number;pending_embeddings:number;daily_spend:number;queue:Record<string,number>;categories:CategoryCount[];recent_techniques:Technique[];errors:QueueError[];consolidator:ConsolidatorState}
export interface BrainGraph {techniques:Pick<Technique,'id'|'title'|'category'|'evidence_level'>[];findings:{id:number;va:string;status:string;verified_match:number}[];links:{technique_id:number;finding_id:number}[]}
