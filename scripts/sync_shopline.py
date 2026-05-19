#!/usr/bin/env python3
"""
BardShop x Shopline Sync Tool
Compares Shopline products with internal database, generates diff report,
and optionally applies spec name changes.
"""
import json, os, re, sys, urllib.request, urllib.error, urllib.parse, base64

SHOPLINE_API = 'https://open.shoplineapp.com/v1'
SHOPLINE_TOKEN = os.environ.get('SHOPLINE_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
REPO = 'bardshop/bardshop.github.io'
MODE = os.environ.get('SYNC_MODE', 'report')
LINE_TOKEN = os.environ.get('LINE_NOTIFY_TOKEN', '')

def api_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode('utf-8'))

def api_get_text(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return r.read().decode('utf-8')

def fetch_shopline_products():
    products = []
    page = 1
    while True:
        try:
            data = api_get(
                f'{SHOPLINE_API}/products?per_page=250&page={page}&status=active',
                {'Authorization': f'Bearer {SHOPLINE_TOKEN}', 'Content-Type': 'application/json'}
            )
        except urllib.error.HTTPError as e:
            print(f'[ERROR] Shopline API error: {e.code} {e.reason}')
            sys.exit(1)
        items = data.get('items', data.get('data', []))
        if not items: break
        products.extend(items)
        if len(items) < 250: break
        page += 1
    return products

def fetch_index_html():
    return api_get_text(
        f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3.raw'}
    )

def fetch_index_sha():
    data = api_get(f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}'})
    return data['sha']

def parse_products(html):
    idx = html.index('const PRODUCTS')
    ob = html.index('[', idx)
    depth, cb = 0, -1
    for i in range(ob, len(html)):
        if html[i] == '[': depth += 1
        elif html[i] == ']':
            depth -= 1
            if depth == 0: cb = i; break
    return json.loads(html[ob:cb+1]), ob, cb

def extract_shopline_specs(product):
    specs = set()
    for v in product.get('variants', []):
        parts = [ov.get('value','') for ov in v.get('option_values',[]) if ov.get('value')]
        if parts: specs.add(' / '.join(parts))
    return specs

def match_products(sl_list, int_list):
    by_slug = {}
    for p in int_list:
        url = p.get('url','')
        if url: by_slug[url.rstrip('/').split('/')[-1]] = p
    matches, unmatched_sl = [], []
    for sp in sl_list:
        h = sp.get('handle','')
        if h in by_slug: matches.append((sp, by_slug.pop(h)))
        else: unmatched_sl.append(sp)
    return matches, unmatched_sl, list(by_slug.values())

def validate_price(price):
    if not isinstance(price,(int,float)): return False,'not a number'
    if price < 0: return False,'negative'
    if price > 50000: return False,'suspiciously high'
    return True,'ok'

def compare_all(matches):
    diffs = []
    for sl, internal in matches:
        sl_specs = extract_shopline_specs(sl)
        int_specs = set(internal.get('_pricing',{}).get('sizes',{}).keys())
        if sl_specs and int_specs and sl_specs != int_specs:
            only_sl = sl_specs - int_specs
            only_int = int_specs - sl_specs
            if only_sl or only_int:
                diffs.append({'product_id':internal['id'],'product_name':internal['name'],
                    'type':'spec_mismatch','shopline_only':sorted(only_sl),
                    'internal_only':sorted(only_int),'shopline_all':sorted(sl_specs),
                    'internal_all':sorted(int_specs)})
        sl_imgs = [img.get('original_url',img.get('url','')).split('?')[0]
                   for img in sl.get('images',[]) if img.get('original_url') or img.get('url')]
        if sl_imgs and not internal.get('_imgs',[]):
            diffs.append({'product_id':internal['id'],'product_name':internal['name'],
                'type':'missing_image','shopline_images':sl_imgs[:3]})
    return diffs

def generate_report(diffs, unmatched_sl, unmatched_int):
    lines = ['# BardShop Shopline Sync Report\n']
    from datetime import datetime, timezone, timedelta
    tw = timezone(timedelta(hours=8))
    lines.append(f'Generated: {datetime.now(tw).strftime("%Y-%m-%d %H:%M")} (UTC+8)\n')
    if not diffs and not unmatched_sl:
        lines.append('## All synced! No differences found.\n')
        return '\n'.join(lines)
    if diffs:
        lines.append(f'## Found {len(diffs)} difference(s)\n')
        for d in diffs:
            lines.append(f'### {d["product_name"]} (\x60{d["product_id"]}\x60)')
            if d['type']=='spec_mismatch':
                lines.append('**Type:** Spec name mismatch')
                if d['shopline_only']: lines.append(f'- Shopline has: {", ".join(d["shopline_only"])}')
                if d['internal_only']: lines.append(f'- Internal DB has: {", ".join(d["internal_only"])}')
            elif d['type']=='missing_image':
                lines.append(f'**Type:** Missing image ({len(d["shopline_images"])} available on Shopline)')
            lines.append('')
    if unmatched_sl:
        lines.append(f'## {len(unmatched_sl)} Shopline product(s) not in internal DB\n')
        for sp in unmatched_sl:
            title = next(iter(sp.get('title_translations',{}).values()),'') or sp.get('handle','?')
            lines.append(f'- **{title}** (\x60{sp.get("handle","")}\x60)')
        lines.append('')
    return '\n'.join(lines)

def create_github_issue(title, body):
    data = json.dumps({'title':title,'body':body,'labels':['sync-report']}).encode()
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/issues',
        data=data, headers={'Authorization':f'token {GITHUB_TOKEN}','Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get('html_url','')

def send_line_notify(message):
    if not LINE_TOKEN: return
    try:
        data = urllib.parse.urlencode({'message':message}).encode()
        req = urllib.request.Request('https://notify-api.line.me/api/notify',
            data=data, headers={'Authorization':f'Bearer {LINE_TOKEN}'})
        urllib.request.urlopen(req)
    except Exception as e: print(f'[WARN] LINE Notify failed: {e}')

def main():
    print('=== BardShop Shopline Sync ===')
    print(f'Mode: {MODE}')
    print('[1/4] Fetching Shopline products...')
    sl_products = fetch_shopline_products()
    print(f'  Found {len(sl_products)} Shopline products')
    print('[2/4] Fetching internal database...')
    html = fetch_index_html()
    int_products, ob, cb = parse_products(html)
    print(f'  Found {len(int_products)} internal products')
    print('[3/4] Comparing...')
    matches, unmatched_sl, unmatched_int = match_products(sl_products, int_products)
    print(f'  Matched: {len(matches)}, Shopline-only: {len(unmatched_sl)}, Internal-only: {len(unmatched_int)}')
    diffs = compare_all(matches)
    print(f'  Differences found: {len(diffs)}')
    report = generate_report(diffs, unmatched_sl, unmatched_int)
    print('\n' + report)
    print('[4/4] Creating report...')
    if diffs or unmatched_sl:
        issue_url = create_github_issue('[Sync] Shopline comparison report', report)
        print(f'  Issue created: {issue_url}')
        summary = f'\n🔄 BardShop Sync\n差異: {len(diffs)}項 / 新商品: {len(unmatched_sl)}項\n詳情: {issue_url}'
        send_line_notify(summary)
    else:
        print('  All synced!')
        send_line_notify('\n✅ BardShop Sync: 全部一致')
    warnings = []
    for p in int_products:
        for sn, sd in p.get('_pricing',{}).get('sizes',{}).items():
            for pr in sd.get('prices',[]):
                ok, reason = validate_price(pr)
                if not ok: warnings.append(f'{p["name"]}/{sn}: {pr} ({reason})')
    if warnings:
        print(f'\nPrice warnings ({len(warnings)}):')
        for w in warnings[:10]: print(f'  - {w}')
    print('\n=== Done ===')

if __name__ == '__main__':
    main()