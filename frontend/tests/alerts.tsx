import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { useNewAlertMessages, type AlertMessage } from '../src/hooks/useAlertsSocket';

function Harness() {
  const [messages, setMessages] = useState<AlertMessage[]>([]);
  const [seen, setSeen] = useState<string[]>([]);
  useNewAlertMessages(messages, message => setSeen(previous => [...previous, message.type]));
  return <>
    <button onClick={() => setMessages(previous => [{ type: 'first', receivedAt: 1 }, ...previous])}>First</button>
    <button onClick={() => setMessages(previous => [{ type: 'third', receivedAt: 3 }, { type: 'second', receivedAt: 2 }, ...previous])}>Burst</button>
    <button onClick={() => setMessages([])}>Reset</button>
    <output>{JSON.stringify(seen)}</output>
  </>;
}
createRoot(document.getElementById('root')!).render(<Harness />);
