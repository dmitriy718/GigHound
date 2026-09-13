"""Real browser save/reload/clear journey for owner writing preferences."""
import os
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8059/?view=profiles')
    page.get_by_label('Email', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_EMAIL'])
    page.get_by_label('Password', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_PASSWORD'])
    page.get_by_role('button', name='Sign in', exact=True).last.click()
    page.get_by_role('button', name='My writing voice', exact=True).click()
    page.get_by_label('My style', exact=True).fill('Direct and friendly. Keep sentences short.')
    page.get_by_role('button', name='Add writing sample', exact=True).click()
    page.get_by_label('Writing sample 1', exact=True).fill('Could you send one example of the output you need?')
    page.get_by_role('button', name='Save writing voice', exact=True).click()
    expect(page.get_by_text('Writing voice saved.', exact=False)).to_be_visible()
    page.reload()
    page.get_by_role('button', name='My writing voice', exact=True).click()
    expect(page.get_by_label('My style', exact=True)).to_have_value('Direct and friendly. Keep sentences short.')
    expect(page.get_by_label('Writing sample 1', exact=True)).to_have_value('Could you send one example of the output you need?')
    page.get_by_role('button', name='Remove sample 1', exact=True).click()
    page.get_by_label('My style', exact=True).fill('')
    page.get_by_role('button', name='Save writing voice', exact=True).click()
    expect(page.get_by_text('Writing voice saved.', exact=False)).to_be_visible()
    page.reload()
    page.get_by_role('button', name='My writing voice', exact=True).click()
    expect(page.get_by_label('My style', exact=True)).to_have_value('')
    expect(page.get_by_label('Writing sample 1', exact=True)).to_have_count(0)
    assert not errors, errors
    browser.close()
print('Writing voice: save, reload and clear passed.')
