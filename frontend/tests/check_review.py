"""Synthetic manual review journey; credentials belong to check_ui's temporary DB."""
import os
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1100})
    errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog', lambda d:d.accept())
    page.goto('http://127.0.0.1:8059/?view=proposals')
    page.get_by_label('Email', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_EMAIL'])
    page.get_by_label('Password', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_PASSWORD'])
    page.get_by_role('button',name='Sign in',exact=True).last.click()
    page.get_by_text('Synthetic manual review',exact=True).click()
    page.screenshot(path='/tmp/astra-review-debug.png',full_page=True)
    page.locator('textarea').first.fill('Reviewed synthetic scope, no external action')
    page.get_by_role('button',name='Approve',exact=True).click()
    expect(page.get_by_text('choose a platform account before approving this proposal', exact=False)).to_be_visible()
    page.get_by_label('Submission account',exact=True).select_option(label='Synthetic second · second')
    selected_account = int(page.get_by_label('Submission account',exact=True).input_value())
    # A second authenticated review changes the same owned synthetic row.
    def other_review(text):
        page.evaluate("""async ({accountId,text}) => {
            const headers = {Authorization: `Bearer ${localStorage.getItem('gighound_token')}`, 'Content-Type':'application/json'};
            const request = async (path, body) => {
                const r = await fetch(path,{headers,...(body ? {method:'POST',body:JSON.stringify(body)} : {})});
                if (!r.ok) throw new Error(`Synthetic review fixture failed: ${r.status}`);
                return r.json();
            };
            const item = (await request('/api/proposals?status=pending_review')).items[0];
            await request(`/api/proposals/${item.id}/approve`,{expected_revision:item.revision,reviewer:'Other synthetic session',proposal_text:text,platform_account_id:accountId,save_as_template:false});
            await request(`/api/proposals/${item.id}/return-to-review`,{});
        }""", {'accountId':selected_account,'text':text})
    other_review('Changed saved scope from another session')
    page.get_by_role('button',name='Refresh',exact=True).click()
    expect(page.get_by_label('Current saved proposal',exact=True)).to_have_value('Changed saved scope from another session')
    page.reload()
    page.get_by_text('Synthetic manual review',exact=True).click()
    expect(page.get_by_label('Current saved proposal',exact=True)).to_have_value('Changed saved scope from another session')
    expect(page.locator('textarea').last).to_have_value('Reviewed synthetic scope, no external action')
    page.get_by_role('button',name='Keep my edits for fresh review',exact=True).click()
    expect(page.get_by_label('Current saved proposal',exact=True)).to_have_count(0)
    page.get_by_role('button',name='Approve',exact=True).click()
    page.locator('select').first.select_option('approved')
    expect(page.get_by_text('Synthetic manual review',exact=True)).to_be_visible()
    if not page.get_by_label('Submission account',exact=True).is_visible():
        page.get_by_text('Synthetic manual review',exact=True).click()
    expect(page.get_by_label('Submission account',exact=True).locator('option:checked')).to_have_text('Synthetic second · second')
    page.get_by_role('button',name='Return to review',exact=True).click()
    page.locator('select').first.select_option('pending_review')
    expect(page.get_by_text('Synthetic manual review',exact=True)).to_be_visible()
    if not page.get_by_label('Submission account',exact=True).is_visible():
        page.get_by_text('Synthetic manual review',exact=True).click()
    page.locator('textarea').first.fill('Local edits to discard')
    other_review('Reviewed synthetic scope, no external action')
    page.get_by_role('button',name='Refresh',exact=True).click()
    expect(page.get_by_label('Current saved proposal',exact=True)).to_be_visible()
    page.get_by_role('button',name='Discard my edits and use saved version',exact=True).click()
    expect(page.locator('textarea').first).to_have_value('Reviewed synthetic scope, no external action')
    page.get_by_title('Approve with current text').click()
    page.locator('select').first.select_option('approved')
    if not page.locator('textarea').first.is_visible():
        page.get_by_text('Synthetic manual review',exact=True).click()
    expect(page.locator('textarea').first).to_have_value('Reviewed synthetic scope, no external action')
    page.get_by_role('button',name='Mark as submitted',exact=True).click()
    page.locator('select').first.select_option('submitted')
    expect(page.get_by_text('Synthetic manual review',exact=True)).to_be_visible()
    page.goto('http://127.0.0.1:8059/?view=accounts')
    page.get_by_role('row').filter(has_text='Synthetic agency-second').click()
    expect(page.get_by_role('heading',name='Unassigned legacy roster',exact=True)).to_be_visible()
    page.get_by_role('button',name='Assign reviewed legacy roster to this account',exact=True).click()
    expect(page.get_by_role('heading',name='Unassigned legacy roster',exact=True)).to_have_count(0)
    expect(page.get_by_text('synthetic-legacy-member',exact=False)).to_be_visible()
    page.get_by_role('dialog').get_by_role('button',name='Close',exact=True).click()
    page.get_by_role('row').filter(has_text='Synthetic agency-first').click()
    expect(page.get_by_text('synthetic-legacy-member',exact=False)).to_have_count(0)
    page.get_by_role('dialog').get_by_role('button',name='Close',exact=True).click()
    page.get_by_role('row').filter(has_text='Synthetic polling account').click()
    page.get_by_role('button',name='Refresh polling status',exact=True).click()
    expect(page.get_by_text('Last successful check: Not polled yet',exact=True)).to_be_visible()
    assert not errors, errors
    print('PASS: explicit second-account selection, stale edit reload/comparison/rebase/discard, revision approval, renewed review, synthetic manual submission tracking, one-account legacy roster assignment and polling freshness')
    browser.close()
