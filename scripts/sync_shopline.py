#!/usr/bin/env python3
"""
BardShop x Shopline Sync Tool
Compares Shopline products with internal database, generates diff report,
and optionally applies spec name changes.
"""
import json, os, re, sys, urllib.request, urllib.error, base64

SHOPLINE_API = 'https://open.shopline.io/v1'
SHOPLINE_TOKEN = os.environ.get('SHOPLINE_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
REPO = 'bardshop/bardshop.github.io'
MODE = os.environ.get('SYNC_MODE', 'report')  # 'report' or 'apply'
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
                f'{SHOPLINE_API}/products?per_page=100&page={page}',
                {'Authorization': f'Bearer {SHOPLINE_TOKEN}', 'Accept': 'application/json', 'User-Agent': 'BardShop Sync'}
            )
        except urllib.error.HTTPError as e:
            print(f'[ERROR] Shopline API error: {e.code} {e.reason}')
            sys.exit(1)
        items = data.get('items', data.get('data', []))
        if not items:
            break
        products.extend(items)
        if len(items) < 100:
            break
        page += 1
    return products

def fetch_index_html():
    html = api_get_text(
        f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3.raw'}
    )
    return html

def fetch_index_sha():
    data = api_get(
        f'https://api.github.com/repos/{REPO}/contents/index.html',
        {'Authorization': f'token {GITHUB_TOKEN}'}
    )
    return data['sha']

def parse_products(html):
    idx = html.index('const PRODUCTS')
    ob = html.index('[', idx)
    depth = 0
    cb = -1
    for i in range(ob, len(html)):
        if html[i] == '[':
            depth += 1
        elif html[i] == ']':
            depth -= 1
            if depth == 0:
                cb = i
                break
    return json.loads(html[ob:cb+1]), ob, cb

def rebuild_html(html, ob, cb, products):
    new_array = json.dumps(products, ensure_ascii=False, indent=2)
    new_array = new_array.replace('\n', '\n ')
    return html[:ob] + new_array + html[cb+1:]

def extract_shopline_specs(product):
    """Extract variant spec names from a Shopline product."""
    specs = set()
    # Shopline API uses 'variations' (not 'variants')
    for v in product.get('variations', product.get('variants', [])):
        parts = []
        for ov in v.get('option_values', []):
            val = ov.get('value', '')
            if val:
                parts.append(val)
        if parts:
            specs.add(' / '.join(parts))
    return specs

def get_shopline_slug(product):
    """Extract slug from Shopline product using multiple field fallbacks."""
    # Try handle first
    handle = product.get('handle', '')
    if handle:
        return handle
    # Try permalink (e.g. "/products/mini-charm" or full URL)
    permalink = product.get('permalink', '')
    if permalink:
        return permalink.rstrip('/').split('/')[-1]
    # Try slug field
    slug = product.get('slug', '')
    if slug:
        return slug
    # Try extracting from link field (Shopline returns full URL or path)
    link = product.get('link', '')
    if link:
        # Remove query params and fragments
        link_clean = link.split('?')[0].split('#')[0]
        last_seg = link_clean.rstrip('/').split('/')[-1]
        if last_seg and last_seg not in ('products', ''):
            return last_seg
    # Try url/product_url fields
    for field in ('url', 'product_url'):
        val = product.get(field, '')
        if val:
            val_clean = val.split('?')[0].split('#')[0]
            last_seg = val_clean.rstrip('/').split('/')[-1]
            if last_seg and last_seg not in ('products', ''):
                return last_seg
    return ''

def match_products(shopline_list, internal_list):
    """Match products by Shopline slug <-> internal URL slug."""
    # Debug: print first product's keys to help diagnose matching issues
    if shopline_list:
        p0 = shopline_list[0]
        print(f'  [DEBUG] Shopline product keys: {sorted(p0.keys())}')
        print(f'  [DEBUG] Sample handle={p0.get("handle","")!r} permalink={p0.get("permalink","")!r} slug={p0.get("slug","")!r}')
        print(f'  [DEBUG] Sample link={p0.get("link","")!r}')
        print(f'  [DEBUG] Resolved slug: {get_shopline_slug(p0)!r}')
        # Print first 3 internal slugs for comparison
        if internal_list:
            sample_urls = [p.get('url','') for p in internal_list[:3]]
            print(f'  [DEBUG] Sample internal URLs: {sample_urls}')

    internal_by_slug = {}
    for p in internal_list:
        url = p.get('url', '')
        if url:
            slug = url.rstrip('/').split('/')[-1]
            internal_by_slug[slug] = p

    matches = []
    unmatched_sl = []
    for sp in shopline_list:
        sl_slug = get_shopline_slug(sp)
        if sl_slug and sl_slug in internal_by_slug:
            matches.append((sp, internal_by_slug.pop(sl_slug)))
        else:
            unmatched_sl.append(sp)

    unmatched_int = list(internal_by_slug.values())
    return matches, unmatched_sl, unmatched_int

def validate_price(price):
    """Check if a price value is sane."""
    if not isinstance(price, (int, float)):
        return False, 'not a number'
    if price < 0:
        return False, 'negative'
    if price > 50000:
        return False, 'suspiciously high'
    return True, 'ok'

def compare_all(matches):
    """Compare matched products and return diffs."""
    diffs = []
    for sl, internal in matches:
        sl_title = ''
        for t in sl.get('title_translations', {}).values():
            sl_title = t
            break
        if not sl_title:
            sl_title = sl.get('title', sl.get('handle', ''))

        sl_specs = extract_shopline_specs(sl)
        int_specs = set(internal.get('_pricing', {}).get('sizes', {}).keys())

        # Spec name comparison
        if sl_specs and int_specs and sl_specs != int_specs:
            only_sl = sl_specs - int_specs
            only_int = int_specs - sl_specs
            if only_sl or only_int:
                diffs.append({
                    'product_id': internal['id'],
                    'product_name': internal['name'],
                    'type': 'spec_mismatch',
                    'shopline_only': sorted(only_sl),
                    'internal_only': sorted(only_int),
                    'shopline_all': sorted(sl_specs),
                    'internal_all': sorted(int_specs)
                })

        # Image comparison
        sl_imgs = []
        for img in sl.get('images', []):
            src = img.get('original_url', img.get('url', ''))
            if src:
                sl_imgs.append(src.split('?')[0])
        int_imgs = [u.split('?')[0] for u in internal.get('_imgs', [])]
        if sl_imgs and not int_imgs:
            diffs.append({
                'product_id': internal['id'],
                'product_name': internal['name'],
                'type': 'missing_image',
                'shopline_images': sl_imgs[:3]
            })

    return diffs

def generate_report(diffs, unmatched_sl, unmatched_int):
    """Generate markdown report."""
    lines = ['# BardShop Shopline Sync Report\n']
    lines.append(f'Generated: {os.popen("date").read().strip()}\n')

    if not diffs and not unmatched_sl:
        lines.append('## ✅ All synced! No differences found.\n')
        return '\n'.join(lines)

    if diffs:
        lines.append(f'## ⚠️ Found {len(diffs)} difference(s)\n')
        for d in diffs:
            lines.append(f'### {d["product_name"]} (`{d["product_id"]}`)')
            if d['type'] == 'spec_mismatch':
                lines.append(f'**Type:** Spec name mismatch')
                if d['shopline_only']:
                    lines.append(f'- Shopline has: {", ".join(d["shopline_only"])}')
                if d['internal_only']:
                    lines.append(f'- Internal DB has: {", ".join(d["internal_only"])}')
                lines.append(f'- Shopline specs: {d["shopline_all"]}')
                lines.append(f'- Internal specs: {d["internal_all"]}')
            elif d['type'] == 'missing_image':
                lines.append(f'**Type:** Missing image in internal DB')
                lines.append(f'- Shopline images available: {len(d["shopline_images"])}')
            lines.append('')

    if unmatched_sl:
        lines.append(f'## 🆕 {len(unmatched_sl)} Shopline product(s) not in internal DB\n')
        for sp in unmatched_sl:
            title = ''
            for t in sp.get('title_translations', {}).values():
                title = t
                break
            lines.append(f'- **{title or sp.get("handle", "?")}** (`{sp.get("handle", "")}`)')
        lines.append('')

    if unmatched_int:
        lines.append(f'## 📦 {len(unmatched_int)} internal product(s) not on Shopline\n')
        for p in unmatched_int[:20]:
            lines.append(f'- {p["name"]} (`{p["id"]}`)')
        if len(unmatched_int) > 20:
            lines.append(f'- ... and {len(unmatched_int) - 20} more')
        lines.append('')

    return '\n'.join(lines)

def create_github_issue(title, body):
    """Create a GitHub issue with the report."""
    data = json.dumps({'title': title, 'body': body, 'labels': ['sync-report']}).encode()
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/issues',
        data=data,
        headers={
            'Authorization': f'token {GITHUB_TOKEN}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
        return result.get('html_url', '')

def send_line_notify(message):
    """Send LINE Notify message."""
    if not LINE_TOKEN:
        return
    try:
        data = urllib.parse.urlencode({'message': message}).encode()
        req = urllib.request.Request(
            'https://notify-api.line.me/api/notify',
            data=data,
            headers={'Authorization': f'Bearer {LINE_TOKEN}'}
        )
        urllib.request.urlopen(req)
    except Exception as e:
        print(f'[WARN] LINE Notify failed: {e}')

def push_to_github(html, message):
    """Push updated index.html to GitHub."""
    sha = fetch_index_sha()
    content_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
    data = json.dumps({
        'message': message,
        'content': content_b64,
        'sha': sha
    }).encode()
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/contents/index.html',
        data=data,
        headers={
            'Authorization': f'token {GITHUB_TOKEN}',
            'Content-Type': 'application/json'
        },
        method='PUT'
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
        return result.get('content', {}).get('sha', '')

def main():
    print('=== BardShop Shopline Sync ===')
    print(f'Mode: {MODE}')

    # 1. Fetch data
    print('[1/4] Fetching Shopline products...')
    sl_products = fetch_shopline_products()
    print(f'  Found {len(sl_products)} Shopline products')

    print('[2/4] Fetching internal database...')
    html = fetch_index_html()
    int_products, ob, cb = parse_products(html)
    print(f'  Found {len(int_products)} internal products')

    # 2. Match & Compare
    print('[3/4] Comparing...')
    matches, unmatched_sl, unmatched_int = match_products(sl_products, int_products)
    print(f'  Matched: {len(matches)}, Shopline-only: {len(unmatched_sl)}, Internal-only: {len(unmatched_int)}')

    diffs = compare_all(matches)
    print(f'  Differences found: {len(diffs)}')

    # 3. Generate report
    report = generate_report(diffs, unmatched_sl, unmatched_int)
    print('\n' + report)

    # 4. Create GitHub Issue
    print('[4/4] Creating report...')
    if diffs or unmatched_sl:
        issue_url = create_github_issue(
            f'[Sync] Shopline comparison report',
            report
        )
        print(f'  Issue created: {issue_url}')

        # LINE notification
        summary = f'\n🔄 BardShop Sync Report\n'
        summary += f'差異: {len(diffs)} 項\n'
        summary += f'Shopline新商品: {len(unmatched_sl)} 項\n'
        if diffs:
            for d in diffs[:3]:
                summary += f'- {d["product_name"]}: {d["type"]}\n'
        summary += f'\n詳情: {issue_url}'
        send_line_notify(summary)
    else:
        print('  No differences - no issue created.')
        send_line_notify('\n✅ BardShop Sync: 全部一致，無需更新')

    # Price validation for all internal products
    warnings = []
    for p in int_products:
        sizes = p.get('_pricing', {}).get('sizes', {})
        for size_name, size_data in sizes.items():
            for price in size_data.get('prices', []):
                ok, reason = validate_price(price)
                if not ok:
                    warnings.append(f'{p["name"]} / {size_name}: {price} ({reason})')
    if warnings:
        print(f'\n⚠️ Price validation warnings ({len(warnings)}):')
        for w in warnings[:10]:
            print(f'  - {w}')

    print('\n=== Done ===')

if __name__ == '__main__':
    main()
