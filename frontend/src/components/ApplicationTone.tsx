import { useEffect, useId, useState } from 'react';
import { request } from '../api/client';

type Tone = {id:string;label:string;description:string};
export default function ApplicationTone({jobId,disabled=false,onBusy}: {jobId:number;disabled?:boolean;onBusy?:(value:boolean)=>void}) {
  const selectId=useId();
  const [tones,setTones] = useState<Tone[]>([]);
  const [tone,setTone] = useState('');
  const [busy,setBusy] = useState(true);
  const [error,setError] = useState('');
  useEffect(()=>{
    let active=true;
    setBusy(true); onBusy?.(true);
    Promise.all([request<{tones:Tone[]}>('/api/profiles/application-tones'),request<{tone:string}>(`/api/profiles/jobs/${jobId}/writing-style`)])
      .then(([catalog,style])=>{if(active){setTones(catalog.tones);setTone(style.tone);}})
      .catch((e:Error)=>{if(active)setError(e.message);})
      .finally(()=>{if(active){setBusy(false);onBusy?.(false);}});
    return()=>{active=false;};
  },[jobId,onBusy]);
  const change = async (value:string) => {
    setBusy(true);onBusy?.(true);setError('');
    try {const saved=await request<{tone:string}>(`/api/profiles/jobs/${jobId}/writing-style`,{method:'PUT',body:JSON.stringify({tone:value})});setTone(saved.tone);}
    catch(e){setError((e as Error).message);} finally{setBusy(false);onBusy?.(false);}
  };
  return <div className="field">
    <label htmlFor={selectId}>Application tone</label>
    <select id={selectId} value={tone} disabled={disabled||busy||!tones.length} onChange={e=>void change(e.target.value)}>
      {!tone&&<option value="">Loading tones…</option>}
      {tones.map(option=><option value={option.id} key={option.id}>{option.label}</option>)}
    </select>
    <p className="muted">{tones.find(option=>option.id===tone)?.description}</p>
    <p className="muted">Saved for this job’s new drafts and follow-ups. Existing text changes only when you request a new draft and review it. These presets are informed by writing guidance; no individual tone has a proven hiring advantage.</p>
    {error&&<p role="alert">{error}</p>}
  </div>;
}
