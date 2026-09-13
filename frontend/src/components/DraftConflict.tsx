import type { ProposalQueueItem } from '../types';

export function DraftConflict({ item, onDiscard, onKeep }: {
  item: ProposalQueueItem;
  onDiscard: () => void;
  onKeep: () => void;
}) {
  return <div className="panel" role="alert">
    <h4>This proposal changed while you were editing</h4>
    <p>Your unsaved edits are still in the editor. Compare them with the saved version below before choosing what to keep.</p>
    <label htmlFor={`saved-proposal-${item.id}`}>Current saved proposal</label>
    <textarea id={`saved-proposal-${item.id}`} rows={5} readOnly value={item.humanized_text || item.proposal_text} />
    <p>Saved bid: {item.bid_amount ?? 'unset'} {item.job?.currency ?? ''} · Delivery: {item.bid_period_days ?? 'unset'} days · Account: {item.platform_account_id ?? 'not selected'}</p>
    <div className="form-row">
      <button className="btn secondary" onClick={onDiscard}>Discard my edits and use saved version</button>
      <button className="btn secondary" onClick={onKeep}>Keep my edits for fresh review</button>
    </div>
    <p className="muted">Neither choice approves or submits the proposal. Review the resulting text, price and account before approving.</p>
  </div>;
}
