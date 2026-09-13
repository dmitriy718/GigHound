"""Only the synthetic Vite harness; no real accounts or provider calls."""
from playwright.sync_api import sync_playwright, expect
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://127.0.0.1:4179/tests/drafts.html')
    page.get_by_role('button', name='Edit locally').click()
    page.get_by_role('button', name='Refresh newer server revision').click()
    expect(page.locator('output')).to_have_text('2')
    page.wait_for_function("JSON.parse(sessionStorage.getItem('gighound:drafts:999'))['1'].edits.text === 'My unsaved edit'")
    stored = page.evaluate("JSON.parse(sessionStorage.getItem('gighound:drafts:999'))['1']")
    assert stored['revision'] == 1, stored
    assert stored['base'] == 'Original server text', stored
    page.reload()
    page.get_by_role('button', name='Refresh newer server revision').click()
    expect(page.get_by_test_id('draft-text')).to_have_text('My unsaved edit')
    expect(page.get_by_test_id('draft-revision')).to_have_text('1')
    print('PASS: server refresh and reload preserve unsaved draft origin and text')
    browser.close()
