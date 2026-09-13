import { useEffect, useState } from 'react';
import { request } from '../api/client';
import { ErrorBanner } from '../components/common';

type Team = {id:number;name:string;role:string;accepted:boolean};
type Draft = {id:number;title:string;text:string;destination:string;version:number;status:string;assignee_id:number|null;review_note:string};
type Detail = Team & {owner_id:number;members:{user_id:number;role:string;accepted:boolean}[];drafts:Draft[];events:{actor_id:number;action:string;detail:unknown;at:string}[]};
export default function Teams() {
  const [teams,setTeams]=useState<Team[]>([]),[team,setTeam]=useState<Detail|null>(null);
  const [error,setError]=useState<string|null>(null),[busy,setBusy]=useState(false);
  const [name,setName]=useState(''),[email,setEmail]=useState(''),[role,setRole]=useState('contributor');
  const [draft,setDraft]=useState<Partial<Draft>>({});
  const load=()=>request<Team[]>('/api/teams').then(setTeams);
  useEffect(()=>{void load().catch(e=>setError(e.message));},[]);
  const run=async(fn:()=>Promise<unknown>)=>{setBusy(true);setError(null);try{await fn();await load();}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
  const open=async(id:number)=>setTeam(await request<Detail>(`/api/teams/${id}`));
  return <section><h1>Team workspace</h1><p>Share selected drafts with accepted members, assign work and review exact versions. Approval here permits export; the account owner reviews the draft again in their proposal queue before platform submission. Credentials and private job records are not shared.</p><ErrorBanner error={error}/>
    <form onSubmit={e=>{e.preventDefault();void run(async()=>{await request('/api/teams',{method:'POST',body:JSON.stringify({name})});setName('');});}}><div className="field"><label htmlFor="team-workspace-name">Workspace name </label><input id="team-workspace-name" required maxLength={200} value={name} onChange={e=>setName(e.target.value)}/></div><button className="btn" disabled={busy}>Create workspace</button></form>
    <div className="row" style={{gap:8,marginTop:16}}>{teams.map(t=><button key={t.id} className="btn secondary" disabled={busy} onClick={()=>run(async()=>{if(!t.accepted)await request(`/api/teams/${t.id}/accept`,{method:'POST'});await open(t.id);setDraft({});})}>{t.accepted?'Open':'Accept invitation to'} {t.name} ({t.role})</button>)}</div>
    {team&&<><h2>{team.name}</h2><p>Owner: user #{team.owner_id}. Your role: {team.role}.</p>
      {team.role==='owner'&&<form onSubmit={e=>{e.preventDefault();void run(async()=>{await request(`/api/teams/${team.id}/members`,{method:'POST',body:JSON.stringify({email,role})});setEmail('');await open(team.id);});}}><div className="field"><label htmlFor="team-registered-member-email">Registered member email </label><input id="team-registered-member-email" type="email" required value={email} onChange={e=>setEmail(e.target.value)}/></div><div className="field"><label htmlFor="team-role">Role </label><select id="team-role" value={role} onChange={e=>setRole(e.target.value)}><option value="contributor">Contributor</option><option value="reviewer">Reviewer</option></select></div><button className="btn" disabled={busy}>Invite inside GigHound</button><p>No email is sent. The member accepts from their Team workspace page.</p></form>}
      <ul>{team.members.map(m=><li key={m.user_id}>User #{m.user_id}: {m.role}, {m.accepted?'accepted':'awaiting acceptance'} {team.role==='owner'&&<button className="btn secondary small" disabled={busy} onClick={()=>run(async()=>{await request(`/api/teams/${team.id}/members/${m.user_id}`,{method:'DELETE'});await open(team.id);})}>Remove</button>}</li>)}</ul>
      <h2>{draft.id?'Edit shared draft':'Share a draft'}</h2><form onSubmit={e=>{e.preventDefault();void run(async()=>{await request(`/api/teams/${team.id}/drafts${draft.id?`/${draft.id}`:''}`,{method:draft.id?'PUT':'POST',body:JSON.stringify({title:draft.title,text:draft.text,destination:draft.destination,assignee_id:draft.assignee_id??null,...(draft.id?{expected_version:draft.version}:{})})});setDraft({});await open(team.id);});}}>
        <div className="field"><label htmlFor="team-title">Title</label><input id="team-title" required value={draft.title??''} onChange={e=>setDraft({...draft,title:e.target.value})}/></div>
        <div className="field"><label htmlFor="team-destination-url-or-reference">Destination URL or reference</label><input id="team-destination-url-or-reference" required value={draft.destination??''} onChange={e=>setDraft({...draft,destination:e.target.value})}/></div>
        <div className="field"><label htmlFor="team-exact-proposal-text">Exact proposal text</label><textarea id="team-exact-proposal-text" rows={8} required value={draft.text??''} onChange={e=>setDraft({...draft,text:e.target.value})}/></div>
        <div className="field"><label htmlFor="team-assign-to">Assign to</label><select id="team-assign-to" value={draft.assignee_id??''} onChange={e=>setDraft({...draft,assignee_id:e.target.value?Number(e.target.value):null})}><option value="">Unassigned</option><option value={team.owner_id}>Owner</option>{team.members.filter(m=>m.accepted).map(m=><option key={m.user_id} value={m.user_id}>User #{m.user_id} ({m.role})</option>)}</select></div>
        <button className="btn" disabled={busy}>{draft.id?'Save and require fresh review':'Share for review'}</button>
      </form>
      {team.drafts.map(d=><article className="card" key={d.id} style={{marginTop:20}}><h2>{d.title}</h2><p>{d.status.replace(/_/g,' ')} · version {d.version} · {d.assignee_id?`assigned to user #${d.assignee_id}`:'unassigned'}</p><p>{d.destination}</p><p style={{whiteSpace:'pre-wrap'}}>{d.text}</p>{d.review_note&&<p>Review note: {d.review_note}</p>}<button className="btn secondary" onClick={()=>setDraft(d)}>Edit</button>
        {d.status==='pending_review'&&team.role!=='contributor'&&['approved','changes_requested'].map(decision=><button key={decision} className="btn" disabled={busy} onClick={()=>run(async()=>{const note=window.prompt('Review note (optional)')??'';await request(`/api/teams/${team.id}/drafts/${d.id}/review`,{method:'POST',body:JSON.stringify({expected_version:d.version,decision,note})});await open(team.id);})}>{decision==='approved'?'Approve this version':'Request changes'}</button>)}
        {d.status==='approved'&&<button className="btn secondary" onClick={()=>{const url=URL.createObjectURL(new Blob([`${d.title}\n${d.destination}\n\n${d.text}`],{type:'text/plain'}));const a=document.createElement('a');a.href=url;a.download=`reviewed-draft-${d.id}-v${d.version}.txt`;a.click();URL.revokeObjectURL(url);}}>Export reviewed draft</button>}
      </article>)}
      <details><summary>Recent team activity</summary><ol>{team.events.map((e,i)=><li key={i}>{new Date(e.at).toLocaleString()} · user #{e.actor_id} · {e.action.replace(/_/g,' ')}</li>)}</ol></details>
    </>}
  </section>;
}
