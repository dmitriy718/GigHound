from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from worker.handlers import upwork_proposal as h
from worker.platforms import platform_config
cfg=platform_config('upwork'); form=cfg['proposal_form']
page=MagicMock()
page.query_selector.return_value=None
ctx=SimpleNamespace(browser=MagicMock(),client=MagicMock(),external_write_started=False)
task=SimpleNamespace(id=991,user_id=991,payload={'job_url':'https://www.upwork.com/jobs/~synthetic','proposal_text':'Reviewed synthetic text','on_behalf_of':'Reviewed member','bid_amount':123})
with ExitStack() as stack:
    stack.enter_context(patch.object(h,'fetch_page',return_value=page))
    for name in ['mouse_wiggle','human_delay','raise_if_challenge','type_with_plan']:
        stack.enter_context(patch.object(h,name))
    h.handle_submit_upwork_proposal(task,ctx)
assert any(c.args==(form['submit'],) for c in page.click.call_args_list)
print('P7 CONFIRMED: Upwork handler clicks Submit despite missing bid, agency and member controls')
page.query_selector.side_effect=lambda selector: object() if selector in [cfg['submit_success'][0],cfg['submit_failure'][0]] else None
result=h._verify_submission(page,cfg)
assert result['submitted'] is True
print('P8 CONFIRMED: simultaneous success and rejection markers classify as successful submission')
