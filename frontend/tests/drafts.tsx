import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {useDrafts} from '../src/hooks/useDrafts';
import type {ProposalQueueItem} from '../src/types';
type Edit = {base_revision:number;text:string};
const initial = {id:1,revision:1,status:'pending_review',proposal_text:'Original server text',humanized_text:''} as ProposalQueueItem;
const editsFrom = (p:ProposalQueueItem):Edit=>({base_revision:p.revision,text:p.proposal_text});
const isPristine = (p:ProposalQueueItem,e:Edit)=>e.text===p.proposal_text;
function Test() {
  const [items,setItems]=useState([initial]);
  const [edits,setEdits]=useState<Record<number,Edit>>({});
  useDrafts(999,items,edits,setEdits,editsFrom,isPristine);
  return <><button onClick={()=>setEdits({1:{base_revision:1,text:'My unsaved edit'}})}>Edit locally</button>
    <button onClick={()=>setItems([{...initial,revision:2,proposal_text:'Someone else changed server text'}])}>Refresh newer server revision</button>
    <output>{items[0].revision}</output><p data-testid="draft-text">{edits[1]?.text}</p><p data-testid="draft-revision">{edits[1]?.base_revision}</p></>;
}
createRoot(document.getElementById('root')!).render(<Test/>);
