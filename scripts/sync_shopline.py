#!/usr/bin/env python3
"""BardShop x Shopline Sync Tool"""
import json, os, sys, urllib.request, urllib.error, urllib.parse, base64
from datetime import datetime, timezone, timedelta

SHOPLINE_API = 'https://open.shoplineapp.com/v1'
SHOPLINE_TOKEN = os.environ.get('SHOPLINE_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
REPO = 'bardshop/bardshop.github.io'
MODE = os.environ.get('SYNC_MODE', 'report')

def api_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read().decode('utf-8')
            if not body.strip():
                print(f'[WARN] Empty response from {url}')
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f'[ERROR] HTTP {e.code} from {url}')
        print(f'  Response: {body}')
        raise
    except json.JSONDecodeError as e:
        print(f'[ERROR] Invalid JSON from {url}: {e}')
        print(f'  Body preview: {body[:200] if body else "(empty)"}')
        raise

def api_get_text(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return r.read().decode('utf-8')

def fetch_shopline_products():
    products = []
    page = 1
    while True:
        url = f'{SHOPLINE_API}/products?per_page=250&page={page}'
        print(f'  Fetching: {url}')
        try:
            data = api_get(url, {
                'Authorization': f'Bearer {SHOPLINE_TOKEN}',
                'Accept': 'application/json'
            })
        except Exception as e:
            print(f'[ERROR] Failed to fetch Shopline products: {e}')
            if not products:
                sys.exit(1)
            break
        items = data.get('items', data.get('data', []))
        if not items: break
        products.extend(items)
        print(f'  Page {page}: got {len(items)} products')
        if len(items) < 250: break
        page += 1
    return products

def fetch_index_html():
    return api_get_text(
        f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3.raw'})

def fetch_index_sha():
    data = api_get(f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/json'})
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
                    'internal_only':sorted(only_int)})
        sl_imgs = [img.get('original_url',img.get('url','')).split('?')[0]
                   for img in sl.get('images',[]) if img.get('original_url') or img.get('url')]
        if sl_imgs and not internal.get('_imgs',[]):
            diffs.append({'product_id':internal['id'],'product_name':internal['name'],
                'type':'missing_image','count':len(sl_imgs)})
    return diffs

def generate_report(diffs, unmatched_sl, unmatched_int, match_count):
    tw = timezone(timedelta(hours=8))
    now = datetime.now(tw).strftime('%Y-%m-%d %H:%M')
    lines = [f'# BardShop Shopline Sync Report', f'Generated: {now} (UTC+8)', f'Matched products: {match_count}', '']
    if not diffs and not unmatched_sl:
        lines.append('## All synced! No differences found.')
        return '\n'.join(lines)
    if diffs:
        lines.append(f'## Found {len(diffs)} difference(s)')
        lines.append('')
        for d in diffs:
            lines.append(f'### {d["product_name"]} ({d["product_id"]})')
            if d['type']=='spec_mismatch':
                lines.append('**Type:** Spec name mismatch')
                if d.get('shopline_only'): lines.append(f'- Shopline has: {", ".join(d["shopline_only"])}')
                if d.get('internal_only'): lines.append(f'- Internal DB has: {", ".join(d["internal_only"])}')
            elif d['type']=='missing_image':
                lines.append(f'**Type:** Missing image ({d["count"]} available on Shopline)')
            lines.append('')
    if unmatched_sl:
        lines.append(f'## {len(unmatched_sl)} Shopline product(s) not in internal DB')
        lines.append('')
        for sp in unmatched_sl:
            title = next(iter(sp.get('title_translations',{}).values()),'') or sp.get('handle','?')
            lines.append(f'- **{title}** ({sp.get("handle","")})')
        lines.append('')
    return '\n'.join(lines)

def create_github_issue(title, body):
    data = json.dumps({'title':title,'body':body,'labels':['sync-report']}).encode()
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/issues',
        data=data, headers={'Authorization':f'token {GITHUB_TOKEN}','Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get('html_url','')

def main():
    print('=== BardShop Shopline Sync ===')
    print(f'Mode: {MODE}')
    print(f'Token present: {bool(SHOPLINE_TOKEN)} (len={len(SHOPLINE_TOKEN)})')
    print(f'GitHub token present: {bool(GITHUB_TOKEN)} (len={len(GITHUB_TOKEN)})')

    print('[1/4] Fetching Shopline products...')
    sl_products = fetch_shopline_products()
    print(f'  Total: {len(sl_products)} Shopline products')

    print('[2/4] Fetching internal database...')
    html = fetch_index_html()
    int_products, ob, cb = parse_products(html)
    print(f'  Total: {len(int_products)} internal products')

    print('[3/4] Comparing...')
    matches, unmatched_sl, unmatched_int = match_products(sl_products, int_products)
    print(f'  Matched: {len(matches)}, Shopline-only: {len(unmatched_sl)}, Internal-only: {len(unmatched_int)}')

    diffs = compare_all(matches)
    print(f'  Differences: {len(diffs)}')

    report = generate_report(diffs, unmatched_sl, unmatched_int, len(matches))
    print('\n--- REPORT ---')
    print(report)
    print('--- END REPORT ---\n')

    print('[4/4] Publishing...')
    if diffs or unmatched_sl:
        issue_url = create_github_issue('[Sync] Shopline comparison report', report)
        print(f'  Issue: {issue_url}')
    else:
        print('  All synced, no issue needed.')

    # Price validation
    warnings = []
    for p in int_products:
        for sn, sd in p.get('_pricing',{}).get('sizes',{}).items():
            for pr in sd.get('prices',[]):
                if not isinstance(pr,(int,float)) or pr < 0 or pr > 50000:
                    warnings.append(f'{p["name"]}/{sn}: {pr}')
    if warnings:
        print(f'\nPrice warnings ({len(warnings)}):')
        for w in warnings[:10]: print(f'  - {w}')

    print('\n=== Done ===')

if __name__ == '__main__':
    main()
