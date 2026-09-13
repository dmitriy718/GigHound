import { useEffect, useRef, useState } from 'react';
import { request } from '../api/client';
import { ErrorBanner } from '../components/common';

type Value = string | number | boolean;
type Data = Record<string, Value>;
type RecordRow = { id: number; version: number; data: Data; created_at: string };
type Field = { key: string; label: string; type?: 'number' | 'datetime-local' | 'checkbox'; choices?: string[]; optional?: boolean };
const FORMS: Record<string, { label: string; help: string; fields: Field[] }> = {
  writing_draft: {label:'Hiring posts & service ads',help:'Turn your brief into an editable draft using your saved writing voice. Review the facts and any [confirm …] placeholders before copying to the marketplace. Publishing is manual.',fields:[
    {key:'title',label:'Post title'}, {key:'purpose',label:'Post purpose',choices:['hiring_post','service_ad']},
    {key:'platform',label:'Post platform',choices:['upwork','fiverr','freelancer','peopleperhour','guru','linkedin','indeed']},
    {key:'brief',label:'Brief: deliverables, skills, audience, budget and timing'},
    {key:'text',label:'Post draft',optional:true}, {key:'state',label:'Post state',choices:['draft','reviewed','manually_published']}]},
  evidence: { label: 'Proof library', help: 'Link each reusable claim to a source you can show a reviewer. Your attestation is recorded separately from independent verification.', fields: [
    {key:'title',label:'Evidence title'}, {key:'claim',label:'Exact supported claim'}, {key:'source',label:'Source URL or document reference'}, {key:'portfolio_id',label:'Portfolio item ID (optional)',type:'number',optional:true}, {key:'verified_by_user',label:'I checked that this source supports the claim',type:'checkbox'}]},
  feedback: {label:'Fit coach',help:'Record why you pursue or skip a job. Skipped jobs leave your daily brief; the underlying quality score remains visible.',fields:[{key:'job_id',label:'Job ID',type:'number'},{key:'decision',label:'Decision',choices:['pursue','skip']},{key:'reason',label:'Reason'}]},
  conversation: {label:'Conversation inbox',help:'Import a message you are authorized to use, draft a reply, and track the next action. Sending is manual on the original platform.',fields:[{key:'title',label:'Conversation title'},{key:'job_id',label:'Job ID (optional)',type:'number',optional:true},{key:'message',label:'Imported message'},{key:'reply_draft',label:'Reply draft',optional:true},{key:'due_at',label:'Next action due',type:'datetime-local',optional:true},{key:'state',label:'State',choices:['needs_reply','drafted','manually_sent','closed']}]},
  client: {label:'Client relationships',help:'Keep notes and reminders against a stable provider client ID. Similar names, locations and budgets do not establish identity.',fields:[{key:'platform',label:'Platform'},{key:'client_id',label:'Stable provider client ID'},{key:'title',label:'Client name'},{key:'notes',label:'Relationship notes'},{key:'due_at',label:'Reminder',type:'datetime-local',optional:true}]},
  experiment: {label:'Experiment lab',help:'Define human-approved variants before use. Record each exposure against a confirmed submitted proposal. Reports show sample size and uncertainty, without claiming causal lift.',fields:[{key:'title',label:'Experiment title'},{key:'hypothesis',label:'Hypothesis'},{key:'variant_a',label:'Variant A'},{key:'variant_b',label:'Variant B'},{key:'minimum_per_variant',label:'Minimum sample per variant (at least 30)',type:'number'},{key:'approved',label:'I approve both variants for this experiment',type:'checkbox'}]},
  exposure: {label:'Record exposure',help:'Record which approved variant was used for an actual confirmed submission. Each proposal can be recorded only once per experiment.',fields:[{key:'experiment_id',label:'Experiment ID',type:'number'},{key:'proposal_id',label:'Confirmed proposal ID',type:'number'},{key:'variant',label:'Variant',choices:['a','b']}]},
  revenue: {label:'Revenue attribution',help:'Record actual receipts and associated costs. A unique receipt reference prevents duplicate entries. Totals keep currencies separate and exclude unreported costs.',fields:[{key:'title',label:'Receipt title'},{key:'proposal_id',label:'Confirmed proposal ID',type:'number'},{key:'reference',label:'Unique receipt reference'},{key:'amount',label:'Realized revenue',type:'number'},{key:'cost',label:'Attributed cost',type:'number'},{key:'effort_hours',label:'Effort hours',type:'number'},{key:'currency',label:'Currency (three uppercase letters)'},{key:'received_at',label:'Receipt date',type:'datetime-local'}]},
};
const SCOPE: Field[] = [{key:'title',label:'Project title'},{key:'deliverables',label:'Deliverables'},{key:'assumptions',label:'Assumptions and acceptance conditions'},{key:'currency',label:'Currency (three uppercase letters)'},...['hours_low','hours_high','cost_per_hour','expenses','margin_percent','available_hours'].map(key => ({key,label:key.replace(/_/g,' '),type:'number' as const}))];

function exportText(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
}

function WorkbenchResult({ value }: { value: unknown }) {
  const r = value as Record<string, unknown>;
  if (r.writing_generated) return <p role="status">Draft generated below. Review and save it when ready.</p>;
  if (r.writing_copied) return <p role="status">Post text copied. Publish it on the marketplace after reviewing it.</p>;
  if (typeof r.statement_of_work === 'string') return <>
    <h2>Scope estimate</h2><p style={{fontSize:24}}>{String(r.currency)} {String(r.price_low)}–{String(r.price_high)}</p>
    <p role={r.capacity_exceeded ? 'alert' : undefined}>{r.capacity_exceeded ? 'The high effort estimate exceeds your available capacity.' : 'The estimate fits your stated capacity.'}</p>
    <pre style={{whiteSpace:'pre-wrap',fontFamily:'inherit'}}>{r.statement_of_work}</pre>
  </>;
  if (Array.isArray(r.matches)) return <><h2>Evidence links</h2><p>{String(r.warning)}</p>{r.matches.length ? r.matches.map((m: Record<string,unknown>)=><article key={String(m.id)}><h3>{String(m.claim)}</h3><p>Source: {String(m.source)}</p><p>{m.verified_by_user?'Attested by you':'Awaiting your verification'}</p></article>):<p>No exact library claims matched this draft.</p>}</>;
  if (r.currencies && typeof r.currencies === 'object') return <><h2>Realized value report</h2><p>{String(r.basis)}</p><table><thead><tr><th>Currency</th><th>Receipts</th><th>Reported costs</th><th>Net</th><th>Hours</th></tr></thead><tbody>{Object.entries(r.currencies as Record<string, Record<string,string>>).map(([c,v])=><tr key={c}><td>{c}</td><td>{v.revenue}</td><td>{v.cost}</td><td>{v.net}</td><td>{v.hours}</td></tr>)}</tbody></table><p>{String(r.records)} receipt records included.</p></>;
  if (Array.isArray(r.items)) return <><h2>Your daily brief</h2><p>{String(r.basis)}</p>{r.items.length ? r.items.map((item: Record<string,unknown>)=><article key={String(item.job_id)}><h3>{String(item.title)}</h3><p>{String(item.platform)} · quality {String(item.score)} · {String(item.review_minutes)} minutes to review · job #{String(item.job_id)}</p></article>):<p>No eligible opportunities fit this review window.</p>}</>;
  if (Array.isArray(r.accounts)) return <><WorkerHealth value={r.worker_health} /><h2>Connection and recovery checks</h2><p>{String(r.synthetic_check)}</p><p>{String(r.uncertain_submissions)} uncertain submissions · {String(r.generation_exhausted)} exhausted generation attempts · {String(r.evidence_items)} proof-library items</p>{r.accounts.map((a: Record<string,unknown>)=><article key={String(a.id)}><h3>{String(a.platform)}</h3><p>{a.enabled?'Enabled':'Disabled'} · {a.credential_reference_present?'Credentials enrolled':'Credentials needed'}</p><p>{String(a.next_action)}</p></article>)}</>;
  if (r.variants && typeof r.variants === 'object') return <><h2>Experiment observations</h2><p>{r.enough_samples?'Minimum sample threshold reached.':'More exposures are needed before the minimum sample threshold is reached.'}</p>{Object.entries(r.variants as Record<string,{hired:number;exposures:number;interval_95:number[]}>).map(([k,v])=><p key={k}>Variant {k.toUpperCase()}: {v.hired} hired / {v.exposures} exposures; 95% interval {(100*v.interval_95[0]).toFixed(1)}–{(100*v.interval_95[1]).toFixed(1)}%.</p>)}<p>{String(r.interpretation)}</p></>;
  return <p role="status">{r.deleted ? 'Record deleted.' : 'Record saved.'}</p>;
}

function WorkerHealth({value}: {value: unknown}) {
  if (!value || typeof value !== 'object') return null;
  const health = value as {pending: number; workers: {worker_id: string; heartbeat: {at: string} | null}[]};
  return <section><h2>Browser workers</h2><p>{health.pending} pending tasks. Heartbeats expire after three minutes.</p>{health.workers.length ? health.workers.map(w=><p key={w.worker_id}>{w.worker_id}: {w.heartbeat ? `last seen ${new Date(w.heartbeat.at).toLocaleString()}` : 'No recent heartbeat; check the worker before dispatching'}</p>) : <p>No worker has claimed work for this account yet. An idle or unassigned worker is not verified by this check.</p>}</section>;
}

function GenerationRecovery() {
  type Work = {job_id:number;state:string;attempts:number;error:string;lease_until:string|null};
  const [items,setItems] = useState<Work[]>([]);
  const [next,setNext] = useState<number|null>(null);
  const [message,setMessage] = useState('');
  const [busy,setBusy] = useState(false);
  const load = async (after=0) => {
    const result = await request<{items:Work[];next:number|null}>(`/api/workbench/generation?after=${after}`);
    setItems(previous=>after ? [...previous,...result.items] : result.items);setNext(result.next);
  };
  const run = async (action:()=>Promise<void>) => {
    setBusy(true);setMessage('');
    try {await action();} catch(e) {setMessage((e as Error).message);} finally {setBusy(false);}
  };
  return <section><h2>Generation recovery</h2>
    <p>Inspect unfinished drafts, including jobs that failed before a proposal was created. Active generation and reviewed proposals cannot be replaced.</p>
    <button disabled={busy} onClick={()=>run(()=>load())}>Inspect unfinished generation</button>
    {message&&<p role="status">{message}</p>}
    {items.map(item=><article key={item.job_id}><h3>Job #{item.job_id}</h3>
      <p>{item.state} · {item.attempts} attempts · {item.error || 'No error recorded'}</p>
      {item.lease_until&&<p>Current lease expires: {new Date(item.lease_until).toLocaleString()}</p>}
      <button disabled={busy} onClick={()=>run(async()=>{
        const result=await request<{delivery:string}>(`/api/workbench/generation/${item.job_id}/retry`,{method:'POST'});
        await load();setMessage(result.delivery==='waiting_for_broker'?'Retry saved; delivery will resume when the broker recovers.':'Retry queued.');
      })}>Retry generation</button></article>)}
    {next!==null&&<button disabled={busy} onClick={()=>run(()=>load(next))}>Load more unfinished jobs</button>}
  </section>;
}

function editableRecord(data: Data): Data {
  const result = {...data};
  for (const key of ['due_at','received_at']) {
    if (typeof data[key] !== 'string' || !data[key]) continue;
    const date = new Date(data[key]);
    if (Number.isFinite(date.getTime())) {
      result[key] = new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,23);
    }
  }
  return result;
}

export default function Workbench() {
  const [tab,setTab] = useState('brief');
  const [rows,setRows] = useState<RecordRow[]>([]);
  const [hasMore,setHasMore] = useState(false);
  const [form,setForm] = useState<Data>({});
  const [editing,setEditing] = useState<RecordRow|null>(null);
  const [result,setResult] = useState<unknown>(null);
  const [error,setError] = useState<string|null>(null);
  const [busy,setBusy] = useState(false);
  const serial = useRef(0);
  const load = async (after=0) => {
    const id = ++serial.current;
    try {
      const data = await request<RecordRow[]>(`/api/workbench/records?kind=${tab}&limit=200&after=${after}`);
      if (id === serial.current) {setRows(previous=>after ? [...previous,...data] : data);setHasMore(data.length===200);}
    } catch(e) { if (id === serial.current) setError((e as Error).message); }
  };
  useEffect(() => {
    setForm({}); setEditing(null); setResult(null); setError(null); setRows([]); setHasMore(false);
    if (FORMS[tab]) void load();
    return () => { serial.current++; };
  },[tab]);
  const fields = tab === 'scope' ? SCOPE : FORMS[tab]?.fields ?? [];
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { setResult(await action()); } catch(e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const save = () => run(async () => {
    const data: Record<string, unknown> = tab === 'scope' ? {} : {kind:tab};
    const originalForm = editing ? editableRecord(editing.data) : {};
    fields.forEach(f => {
      const value = form[f.key];
      if (f.optional && (value == null || value === '')) return;
      data[f.key] = f.type === 'checkbox' ? Boolean(value) : f.type === 'number' ? Number(value) : f.type === 'datetime-local' ? (editing && value === originalForm[f.key] ? editing.data[f.key] : new Date(String(value)).toISOString()) : value ?? f.choices?.[0] ?? '';
    });
    if (tab === 'scope') return request('/api/workbench/scope',{method:'POST',body:JSON.stringify(data)});
    const saved = await request<RecordRow>(`/api/workbench/records${editing ? `/${editing.id}` : ''}`, {method:editing?'PUT':'POST', body:JSON.stringify({data,...(editing?{expected_version:editing.version}:{})})});
    setEditing(null); setForm({}); await load(); return {saved_record:saved.id};
  });
  return <section>
    <h1>Business workbench</h1>
    <p>Plan your day, ground your proposals, and track client work and realized value.</p>
    <div className="row" style={{flexWrap:'wrap',gap:8}}>
      {[['brief','Daily brief'],['scope','Scope & pricing'],['studio','Evidence check'],['doctor','Connection doctor'],...Object.entries(FORMS).map(([key,value])=>[key,value.label]),['roi','ROI report']].map(([key,label])=><button key={key} disabled={busy} className={`btn ${tab===key?'':'secondary'}`} onClick={()=>{if(!Object.keys(form).length || window.confirm('Discard unsaved form changes?')) setTab(key);}}>{label}</button>)}
    </div>
    <ErrorBanner error={error}/>
    {FORMS[tab] && <p>{FORMS[tab].help}</p>}
    {tab==='brief' && <><p>Choose time available for reviewing opportunities. Delivery capacity and expected revenue still require your assessment.</p><label>Review capacity in hours <input type="number" min="0" max="168" value={String(form.capacity??2)} onChange={e=>setForm({capacity:e.target.value})}/></label><button className="btn" disabled={busy} onClick={()=>run(()=>request(`/api/workbench/brief?capacity_hours=${Number(form.capacity??2)}`))}>Build my brief</button></>}
    {tab==='doctor' && <><p>Inspect account enrollment, exhausted generation and uncertain submissions. These local checks do not certify a live provider connection.</p><button className="btn" disabled={busy} onClick={()=>run(()=>request('/api/workbench/doctor'))}>Run local checks</button><GenerationRecovery/></>}
    {tab==='roi' && <><p>This redacted report contains currency totals, costs and effort from your recorded receipts.</p><button className="btn" disabled={busy} onClick={()=>run(()=>request('/api/workbench/roi'))}>Build report</button></>}
    {tab==='studio' && <><label htmlFor="studio-text">Proposal text</label><textarea id="studio-text" rows={8} value={String(form.text??'')} onChange={e=>setForm({text:e.target.value})}/><button className="btn" disabled={busy} onClick={()=>run(()=>request('/api/workbench/evidence-check',{method:'POST',body:JSON.stringify({text:form.text??''})}))}>Link proof library claims</button></>}
    {fields.length>0 && <form onSubmit={e=>{e.preventDefault();void save();}} style={{maxWidth:850,marginTop:20}}>
      {editing && <p>Editing record #{editing.id}, version {editing.version}. Changes in another session will require a reload.</p>}
      <fieldset disabled={busy} style={{border:0,padding:0}}>
      {fields.map(f=><div className="field" key={f.key}><label htmlFor={`workbench-${f.key}`}>{f.label}</label>
        {f.choices?<select id={`workbench-${f.key}`} value={String(form[f.key]??f.choices[0])} onChange={e=>setForm({...form,[f.key]:e.target.value})}>{f.choices.map(c=><option key={c}>{c}</option>)}</select>:
        f.type==='checkbox'?<input id={`workbench-${f.key}`} type="checkbox" checked={Boolean(form[f.key])} onChange={e=>setForm({...form,[f.key]:e.target.checked})}/>:
        (f.type || !['claim','reason','message','reply_draft','notes','hypothesis','variant_a','variant_b','deliverables','assumptions','brief','text'].includes(f.key))?<input id={`workbench-${f.key}`} type={f.type ?? 'text'} step="any" required={!f.optional} value={String(form[f.key]??'')} onChange={e=>setForm({...form,[f.key]:e.target.value})}/>:
        <textarea id={`workbench-${f.key}`} rows={2} maxLength={10000} required={!f.optional} value={String(form[f.key]??'')} onChange={e=>setForm({...form,[f.key]:e.target.value})}/>}
      </div>)}
      {tab==='writing_draft' && <>
        <button className="btn secondary" type="button" onClick={()=>{
          if (form.text && !window.confirm('Replace this draft with a new generation? Save a copy first if you want to keep it.')) return;
          void run(async()=>{
            const draft=await request<{text:string;model:string;provider:string}>('/api/workbench/writing-drafts/generate',{method:'POST',body:JSON.stringify({title:form.title??'',brief:form.brief??'',purpose:form.purpose??'hiring_post',platform:form.platform??'upwork'})});
            setForm({...form,text:draft.text,state:'draft'}); return {writing_generated:true};
          });
        }}>Generate post draft</button>{' '}
        <button className="btn secondary" type="button" disabled={!form.text} onClick={()=>{
          void run(async()=>{await navigator.clipboard.writeText(String(form.text));return {writing_copied:true};});
        }}>Copy post text</button>{' '}
      </>}
      <button className="btn" disabled={busy}>{busy?'Working…':tab==='scope'?'Calculate & draft scope':editing?'Save changes':'Create record'}</button>
      </fieldset>
    </form>}
    {result!==null && <div className="card" data-testid="workbench-result" style={{marginTop:20}}><WorkbenchResult value={result}/><button className="btn secondary" onClick={()=>exportText(`gighound-${tab}.txt`,String((result as Record<string,unknown>).statement_of_work??JSON.stringify(result,null,2)))}>Export result</button></div>}
    {rows.map(row=><article className="card" key={row.id} style={{marginTop:16}}><h2>#{row.id} {String(row.data.title??row.data.reason??row.data.kind)}</h2>
      <dl>{Object.entries(row.data).filter(([k])=>k!=='kind'&&k!=='title').map(([k,v])=><div key={k}><dt>{k.replace(/_/g,' ')}</dt><dd style={{whiteSpace:'pre-wrap'}}>{String(v??'')}</dd></div>)}</dl>
      {!['revenue','exposure'].includes(tab)&&<button className="btn secondary" onClick={()=>{setEditing(row);setForm(editableRecord(row.data));}}>Edit</button>}
      {tab==='experiment'&&<button className="btn secondary" onClick={()=>run(()=>request(`/api/workbench/experiments/${row.id}/report`))}>Sample & uncertainty report</button>}
      <button className="btn secondary" onClick={()=>exportText(`gighound-${tab}-${row.id}.txt`,JSON.stringify(row.data,null,2))}>Export</button>
      <button className="btn secondary" disabled={busy} onClick={()=>{if(window.confirm('Delete this record? An audit entry will be retained.')) void run(async()=>{await request(`/api/workbench/records/${row.id}?expected_version=${row.version}`,{method:'DELETE'});await load();return {deleted:row.id};});}}>Delete</button>
    </article>)}
    {hasMore&&<button className="btn secondary" disabled={busy} onClick={()=>run(async()=>{await load(rows[rows.length-1].id);return {loaded:true};})}>Load more records</button>}
  </section>;
}
