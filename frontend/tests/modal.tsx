import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Modal } from '../src/components/common';
import '../src/styles.css';
function Harness() {
 const [open,setOpen]=useState(false);
 return <><button onClick={()=>setOpen(true)}>Open dialog</button>{open&&<Modal title="Focus test" onClose={()=>setOpen(false)}><label>Evidence<input aria-label="Evidence"/></label><button>Last control</button></Modal>}</>;
}
createRoot(document.getElementById('root')!).render(<Harness/>);
