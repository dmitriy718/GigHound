import {useEffect, useRef, useState} from 'react';
import {request} from '../api/client';
import {PLATFORMS, type Platform} from '../types';
import {ErrorBanner} from './common';

type Circuit = {state:'open'|'closed'|'half_open';reason:string;manual_stop?:boolean;global_stop?:boolean};
export function CircuitControls() {
  const [platform,setPlatform]=useState<Platform>('freelancer');
  const [state,setState]=useState<Circuit|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const serial=useRef(0);
  const update=async(action?:'open'|'closed')=>{
    const id=++serial.current;setBusy(true);setError(null);
    try {
      const value=await request<Circuit>(`/api/gigs/circuit/${platform}`,action?{
        method:'POST',body:JSON.stringify({state:action,reason:action==='open'?'Paused by account owner':'Account owner reviewed and resumed automation'})
      }:undefined);
      if(id===serial.current)setState(value);
    } catch(e) {if(id===serial.current)setError((e as Error).message);}
    finally {if(id===serial.current)setBusy(false);}
  };
  useEffect(()=>{setState(null);void update();return()=>{serial.current++;};},[platform]);
  return <section className="panel"><h2>Automation safety</h2>
    <p>Review the pause reason and account configuration before resuming. Resuming does not retry uncertain submissions.</p>
    <label htmlFor="circuit-platform">Automation platform</label>
    <select id="circuit-platform" disabled={busy} value={platform} onChange={e=>setPlatform(e.target.value as Platform)}>
      {PLATFORMS.map(p=><option key={p} value={p}>{p}</option>)}
    </select>
    <ErrorBanner error={error}/>
    {state&&<p role="status" data-testid="circuit-status">{state.state==='closed'?'Automation enabled':state.state==='half_open'?'One trial permitted':'Automation paused'} — {state.reason||'No stop recorded'}{state.global_stop?' (deployment-wide control; contact the operator)':''}</p>}
    <button className="btn secondary" disabled={busy} onClick={()=>update()}>Refresh automation status</button>
    <button className="btn secondary" disabled={busy||!state} onClick={()=>update('open')}>Pause automation</button>
    <button className="btn" disabled={busy||!state||state.state==='closed'||state.global_stop} onClick={()=>{
      if(window.confirm(`Resume ${platform} automation after reviewing the account and pause reason? Uncertain submissions still need reconciliation.`))void update('closed');
    }}>Resume reviewed automation</button>
  </section>;
}
