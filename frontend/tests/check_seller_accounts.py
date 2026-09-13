"""Synthetic local seller selection; never contacts a marketplace."""
import os
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    api = page.request
    origin = 'http://127.0.0.1:8059'
    login = api.post(origin + '/api/auth/login', data={
        'email': os.environ['GIGHOUND_UI_REVIEW_EMAIL'],
        'password': os.environ['GIGHOUND_UI_REVIEW_PASSWORD']})
    assert login.ok
    headers = {'Authorization': 'Bearer ' + login.json()['access_token']}
    ids = []
    for principal in ('seller-first', 'seller-second'):
        r = api.post(origin + '/api/accounts', headers=headers, data={
            'platform': 'fiverr', 'label': principal, 'principal': principal,
            'mode': 'stealth', 'enabled': True, 'settings': {}})
        assert r.ok, r.text()
        ids.append(r.json()['id'])
    r = api.post(origin + '/api/gigs/templates', headers=headers, data={
        'platform': 'fiverr', 'name': 'Synthetic seller selection',
        'template_json': {'title': 'Synthetic seller draft', 'description': {
            key: 'Synthetic example' for key in ('hook', 'what_you_get', 'why_me', 'cta')}}})
    assert r.ok, r.text()
    tpl_id = r.json()['id']
    page.goto(origin + '/?view=gigs')
    page.get_by_label('Email', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_EMAIL'])
    page.get_by_label('Password', exact=True).fill(os.environ['GIGHOUND_UI_REVIEW_PASSWORD'])
    page.get_by_role('button', name='Sign in', exact=True).last.click()
    page.get_by_role('button', name='Template Builder', exact=True).click()
    page.get_by_text('Synthetic seller selection', exact=False).click()
    create = page.get_by_role('button', name='Create Gig from Template', exact=True)
    expect(create).to_be_disabled()
    page.get_by_label('Draft seller account', exact=True).select_option(str(ids[1]))
    expect(create).to_be_enabled()
    with page.expect_request(lambda req: f'/templates/{tpl_id}/create-gig' in req.url) as observed:
        create.click()
    assert f'account_id={ids[1]}' in observed.value.url
    listing = api.post(origin + '/api/gigs', headers=headers, data={
        'platform': 'fiverr', 'title': 'Synthetic metrics listing', 'url': 'https://example.test/listing'})
    assert listing.ok and listing.json()['account_id'] is None
    page.get_by_role('button', name='Gigs', exact=True).click()
    page.get_by_role('cell', name='Synthetic metrics listing', exact=True).click()
    page.get_by_label('Metrics seller account', exact=True).select_option(str(ids[1]))
    with page.expect_response(lambda response: f"/gigs/{listing.json()['id']}/account" in response.url) as saved:
        page.get_by_role('button', name='Save seller assignment', exact=True).click()
    assert saved.value.ok and saved.value.json()['account_id'] == ids[1]
    page.reload()
    page.get_by_role('cell', name='Synthetic metrics listing', exact=True).click()
    expect(page.get_by_label('Metrics seller account', exact=True)).to_have_value(str(ids[1]))
    browser.close()
print('PASS: ambiguous seller selection blocked; second draft account transmitted; explicit metrics seller assignment survives reload')
