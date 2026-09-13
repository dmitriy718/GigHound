import { useEffect, useState } from 'react';
import { request } from '../api/client';
import type { Job, User, Platform } from '../types';
import { PLATFORMS } from '../types';
import type { AlertMessage, SocketStatus } from '../hooks/useAlertsSocket';
import { ErrorBanner } from '../components/common';
import Accounts from './Accounts';
import ProfileManager from './ProfileManager';
import ProposalQueue from './ProposalQueue';
import type { ViewKey } from '../App';
import ApplicationTone from '../components/ApplicationTone';

type Guidance = {step:number;always_guided:boolean;job_id:number|null};
const STEPS = [
  ['Connect', 'Choose the marketplaces you use. Enroll each account in Platform Accounts below. An enrolled account still needs its connection checked; you can also track a manual application.'],
  ['Introduce yourself', 'Add your actual work to Portfolio, set your rate card, and save examples under My writing voice. These reusable details help each new proposal sound like you.'],
  ['Choose a job', 'Select one opportunity for this application. Use Job Feed to inspect opportunities and Search Profiles to configure discovery. Each application keeps its own job description and draft.'],
  ['Understand & draft', 'Read the complete brief below. Confirm the scope and required skills, then request a draft. Generation uses this job plus your current portfolio, rate card and writing preferences.'],
  ['Review', 'Open the proposal below. Check each claim, scope, price, delivery estimate and submission account. Edit it in your own words, then approve the exact version you want to use. Approval and sending are separate actions.'],
  ['Submit', 'Use the reviewed proposal’s submission controls below. Supported channels can dispatch the approved version; manual channels need you to send it on the marketplace and record the result. An uncertain result needs reconciliation before another attempt.'],
  ['Follow up & interview', 'For a confirmed submission, use Follow up to create a separate draft or open interview preparation below. Review the follow-up before sending it. Use Business Workbench’s Conversation inbox for messages and next-action reminders.'],
  ['Record the outcome', 'Check the marketplace and record Hired or Not hired on the confirmed proposal below. A guide step is not evidence of a hire. If hired, confirm scope and record actual receipts in Business Workbench. Start another application when you are ready.'],
];

export default function GuidedApplications({user,messages,status,onNavigate}: {
  user:User|null;messages:AlertMessage[];status:SocketStatus;onNavigate:(view:ViewKey)=>void;
}) {
  const [guide,setGuide] = useState<Guidance|null>(null);
  const [jobs,setJobs] = useState<Job[]>([]);
  const [job,setJob] = useState<Job|null>(null);
  const [offset,setOffset] = useState(0);
  const [total,setTotal] = useState(0);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState<string|null>(null);
  const [notice,setNotice] = useState('');
  const [toneBusy,setToneBusy] = useState(false);
  useEffect(()=>{
    let active=true;
    request<Guidance>('/api/workbench/guidance').then(data=>{if(active)setGuide(data);}).catch((e:Error)=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[]);
  useEffect(()=>{
    let active=true;
    if (!guide?.job_id) {setJob(null);return;}
    request<Job>(`/api/jobs/${guide.job_id}`).then(data=>{if(active)setJob(data);}).catch((e:Error)=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[guide?.job_id]);
  useEffect(()=>{
    if(guide?.step!==2)return;
    let active=true;
    request<{jobs:Job[];total:number}>(`/api/jobs?limit=50&offset=${offset}`).then(data=>{
      if(active){setJobs(data.jobs);setTotal(data.total);}
    }).catch((e:Error)=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[guide?.step,offset]);
  const save = async (next:Guidance, optimistic=false) => {
    const previous=guide;
    setBusy(true);setError(null);setNotice('');
    if(optimistic)setGuide(next);
    try {setGuide(await request<Guidance>('/api/workbench/guidance',{method:'PUT',body:JSON.stringify(next)}));}
    catch(e){if(optimistic)setGuide(previous);setError((e as Error).message);} finally{setBusy(false);}
  };
  const draft = async () => {
    setBusy(true);setError(null);setNotice('');
    try {
      const result=await request<{delivery:string}>(`/api/workbench/guidance/jobs/${guide!.job_id}/draft`,{method:'POST'});
      setNotice(result.delivery==='waiting_for_broker'?'Draft request saved. Generation will resume when the queue is available.':'Draft requested. Continue to Review and refresh the proposal list when generation finishes.');
    } catch(e){setError((e as Error).message);} finally{setBusy(false);}
  };
  if(!guide)return <section><h1>Guided applications</h1><ErrorBanner error={error}/><p>Loading your saved guide…</p></section>;
  return <section>
    <h1>Guided applications</h1>
    <p>One application at a time, from account setup to a recorded outcome. Your progress is saved as you move through the guide.</p>
    <label><input type="checkbox" checked={guide.always_guided} disabled={busy}
      onChange={e=>void save({...guide,always_guided:e.target.checked},true)}/> Start in guided mode when I open GigHound</label>
    <div className="row" style={{gap:8,margin:'16px 0',flexWrap:'wrap'}}>
      <button className="btn secondary" onClick={()=>onNavigate('jobs')}>Use manual workspace</button>
      <button className="btn secondary" disabled={busy} onClick={()=>void save({...guide,job_id:null,step:2})}>Start another application</button>
      <button className="btn secondary" onClick={()=>onNavigate('workbench')}>Hiring posts & business tools</button>
    </div>
    <ErrorBanner error={error}/>{notice&&<p role="status">{notice}</p>}
    <nav aria-label="Application steps" style={{display:'flex',gap:8,flexWrap:'wrap'}}>
      {STEPS.map(([title],index)=><button key={title} className={`btn ${guide.step===index?'':'secondary'}`} aria-current={guide.step===index?'step':undefined}
        disabled={busy||(index>=3&&!guide.job_id)} onClick={()=>void save({...guide,step:index})}>{index+1}. {title}</button>)}
    </nav>
    <article className="panel" style={{marginTop:16}}>
      <h2>Step {guide.step+1} of {STEPS.length}: {STEPS[guide.step][0]}</h2>
      <p>{STEPS[guide.step][1]}</p>
      {job&&<p><strong>Current application:</strong> {job.title} · {job.platform} · Job #{job.id}</p>}
      <div className="row" style={{gap:8}}>
        <button className="btn secondary" disabled={busy||guide.step===0} onClick={()=>void save({...guide,step:guide.step-1})}>Previous step</button>
        <button className="btn" disabled={busy||guide.step===7||(guide.step===2&&!guide.job_id)} onClick={()=>void save({...guide,step:guide.step+1})}>Next step</button>
      </div>
    </article>
    {guide.step===0&&<Accounts status={status}/>}
    {guide.step===1&&<ProfileManager/>}
    {guide.step===2&&<section>
      <button className="btn secondary" onClick={()=>onNavigate('jobs')}>Explore Job Feed</button>{' '}
      <button className="btn secondary" onClick={()=>onNavigate('searchProfiles')}>Set up job discovery</button>
      <ImportGuidedJob onImported={candidate=>{
        setJob(candidate);setJobs(previous=>[candidate,...previous.filter(value=>value.id!==candidate.id)]);
        if(candidate.status==='archived'||candidate.is_duplicate){setNotice('Imported for tracking, but your filters archived it or identified a duplicate. Review it in Job Feed before applying.');return;}
        void save({...guide,job_id:candidate.id,step:3});
      }}/>
      {!jobs.length&&<p>No jobs on this page yet. Paste a listing above or configure discovery, then return to choose an opportunity.</p>}
      {jobs.map(candidate=><article className="card" key={candidate.id}><h3>{candidate.title}</h3><p>{candidate.platform} · {candidate.status} · quality score {candidate.quality_score}</p>
        <button className="btn" disabled={busy||candidate.status==='archived'||candidate.is_duplicate} onClick={()=>void save({...guide,job_id:candidate.id,step:3})}>Guide me through job #{candidate.id}</button></article>)}
      <button disabled={!offset||busy} onClick={()=>setOffset(Math.max(0,offset-50))}>Previous jobs</button>{' '}
      <button disabled={offset+50>=total||busy} onClick={()=>setOffset(offset+50)}>More jobs</button>
    </section>}
    {guide.step===3&&job&&<article className="panel">
      <h3>{job.title}</h3><p style={{whiteSpace:'pre-wrap'}}>{job.description||'No description was supplied. Open the original listing and check its requirements before drafting.'}</p>
      {job.url&&<a href={job.url} target="_blank" rel="noreferrer">Read the original listing</a>}
      <p>Skills requested: {(job.skills||[]).join(', ')||'Not specified'}</p>
      <ApplicationTone jobId={job.id} disabled={busy} onBusy={setToneBusy}/>
      <button className="btn" disabled={busy||toneBusy} onClick={()=>void draft()}>Request a draft for this job</button>
    </article>}
    {guide.step>=4&&guide.job_id&&<ProposalQueue key={guide.job_id} jobId={guide.job_id} user={user} messages={messages} status={status}/>}
  </section>;
}

function ImportGuidedJob({onImported}:{onImported:(job:Job)=>void}) {
  const [platform,setPlatform]=useState<Platform>('upwork');
  const [title,setTitle]=useState('');
  const [externalId,setExternalId]=useState('');
  const [url,setUrl]=useState('');
  const [description,setDescription]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  return <details className="panel"><summary>Import a job listing</summary>
    <p>Copy the listing you want to apply to. Use its original platform job ID so repeated imports can be recognized. You can review pricing before submission.</p>
    <form onSubmit={async e=>{
      e.preventDefault();setBusy(true);setError('');
      try{const imported=await request<Job>('/api/workbench/guidance/import',{method:'POST',body:JSON.stringify({platform,title,external_id:externalId,url,description})});onImported(imported);}
      catch(err){setError((err as Error).message);}finally{setBusy(false);}
    }}>
      <fieldset disabled={busy} style={{border:0,padding:0}}>
        <div className="field"><label htmlFor="guided-platform">Listing platform</label><select id="guided-platform" value={platform} onChange={e=>setPlatform(e.target.value as Platform)}>{PLATFORMS.map(value=><option key={value}>{value}</option>)}</select></div>
        <div className="field"><label htmlFor="guided-external">Platform job ID</label><input id="guided-external" required maxLength={200} value={externalId} onChange={e=>setExternalId(e.target.value)}/></div>
        <div className="field"><label htmlFor="guided-title">Listing title</label><input id="guided-title" required maxLength={200} value={title} onChange={e=>setTitle(e.target.value)}/></div>
        <div className="field"><label htmlFor="guided-url">Original listing URL (optional)</label><input id="guided-url" type="url" value={url} onChange={e=>setUrl(e.target.value)}/></div>
        <div className="field"><label htmlFor="guided-description">Full job description</label><textarea id="guided-description" required minLength={20} maxLength={10000} rows={6} value={description} onChange={e=>setDescription(e.target.value)}/></div>
        <button className="btn">{busy?'Importing…':'Import and select this job'}</button>
      </fieldset>
      {error&&<p role="alert">{error}</p>}
    </form>
  </details>;
}
