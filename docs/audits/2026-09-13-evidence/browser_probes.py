from types import SimpleNamespace
from unittest.mock import patch
from worker.handlers import upwork_proposal as up, manual_assist as ma
from worker.platforms import platform_config
from worker.tests.test_stealth_phase2 import _FakePage, _FakeBrowser, _FakeClient
from worker.handlers.base import HandlerContext
from worker.config import Config
form=platform_config('upwork')['proposal_form']
page=_FakePage({form['cover_letter']:object()})
ctx=HandlerContext(config=Config(worker_token='synthetic',allow_submit=False),client=_FakeClient(),browser=_FakeBrowser(page))
task=SimpleNamespace(id=1,user_id=1,platform='upwork',payload={'job_url':'https://www.upwork.com/jobs/1234','proposal_text':'Approved','bid_amount':321,'on_behalf_of':'approved-member-uid'})
with patch.object(up,'fetch_page',return_value=page),patch.object(up,'human_delay'),patch.object(up,'mouse_wiggle'),patch.object(up,'raise_if_challenge'),patch.object(up,'type_with_plan'):
    result=up.handle_submit_upwork_proposal(task,ctx)
assert form['submit'] in page.clicked
print('W01 CONFIRMED: Upwork clicks final submit when agency/member/bid fields are absent and allow_submit=False')
form=platform_config('fiverr')['offer_form']
price=SimpleNamespace(fill=lambda v: (_ for _ in ()).throw(AssertionError('Unexpected price fill')))
page=_FakePage({form['message']:object(),form['price']:price},url='https://www.fiverr.com/brief')
ctx=HandlerContext(config=Config(worker_token='synthetic',allow_submit=True),client=_FakeClient(),browser=_FakeBrowser(page))
task=SimpleNamespace(id=2,user_id=1,platform='fiverr',task_type='submit_fiverr_offer',payload={'job_url':'https://www.fiverr.com/brief','proposal_text':'Approved','bid_amount':321})
with patch.object(ma,'fetch_page',return_value=page),patch.object(ma,'human_delay'),patch.object(ma,'raise_if_challenge'),patch.object(ma,'type_with_plan'),patch.object(up,'_verify_submission',return_value={'submitted':True}):
    result=ma.handle_submit_fiverr_offer(task,ctx)
assert form['submit_do_not_click'] in page.clicked
print('W02 CONFIRMED: Fiverr final submit is clicked without filling the approved bid amount')
# Browser standards proof of Workbench edit-date representation.
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    page=b.new_page()
    page.set_content('<input type="datetime-local" id="date">')
    value=page.evaluate("""() => {const el=document.querySelector('#date');el.value='2026-09-13T12:00:00Z';return el.value;}""")
    assert value==''
    print('W03 CONFIRMED: API ISO date with Z renders blank in datetime-local edit input')
    b.close()
