"""Audit-only reproduction of Workbench's persisted UTC/input format mismatch."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<input type="datetime-local" id="date">')
    result = page.eval_on_selector(
        '#date',
        'el => {el.value="2026-09-13T16:00:00Z"; return el.value;}',
    )
    assert result == ''
    print('P10 CONFIRMED: persisted UTC ISO value renders empty in datetime-local')
    browser.close()
