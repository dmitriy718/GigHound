"""Run with worker/.venv/bin/python while Vite serves 127.0.0.1:4179."""
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:4179/tests/alerts.html')
        output = page.locator('output')
        expect(output).to_have_text('[]')
        page.get_by_role('button', name='First', exact=True).click()
        expect(output).to_have_text('["first"]')
        page.get_by_role('button', name='Burst', exact=True).click()
        expect(output).to_have_text('["first","second","third"]')
        page.get_by_role('button', name='Reset', exact=True).click()
        page.get_by_role('button', name='First', exact=True).click()
        expect(output).to_have_text('["first","second","third","first"]')
        assert not errors, errors
        print('PASS: first event, burst ordering, no replay on rerender, session reset')
    finally:
        browser.close()
