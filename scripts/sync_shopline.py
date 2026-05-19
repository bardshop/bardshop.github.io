#!/usr/bin/env python3
"""
BardShop x Shopline Sync Tool v4
Compares Shopline products with internal database, generates diff report,
auto-creates new product entries, and optionally applies spec name changes.
"""
import json, os, re, sys, urllib.request, urllib.error, urllib.parse, base64

SHOPLINE_API = 'https://open.shopline.io/v1'
SHOPLINE_TOKEN = os.environ.get('SHOPLINE_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
REPO = 'bardshop/bardshop.github.io'
MODE = os.environ.get('SYNC_MODE', 'report')  # 'report' or 'apply'
LINE_TOKEN = os.environ.get('LINE_NOTIFY_TOKEN', '')
SHOP_DOMAIN = 'https://www.bardshoptw.com'

# --- Category mapping from Shopline category names to internal categories ---
CATEGORY_MAP = {
    '壓克力': '壓克力商品',
    '鑰匙圈': '壓克力商品',
    '胸章': '特殊貼紙/胸章',
    '徽章': '特殊貼紙/胸章',
    '貼紙': '特殊貼紙/胸章',
    '杯墊': '杯墊/皂墊/地墊',
    '地墊': '杯墊/皂墊/地墊',
    '馬克杯': '馬克杯/保溫水瓶',
    '保溫': '馬克杯/保溫水瓶',
    '水瓶': '馬克杯/保溫水瓶',
    '掛軸': '掛軸/海報/畫類',
    '海報': '掛軸/海報/畫類',
    '卡片': '電子票證/卡片',
    '票證': '電子票證/卡片',
    '悠遊卡': '電子票證/卡片',
    'NFC': '電子票證/卡片',
    '行動電源': '數位週邊',
    '充電': '數位週邊',
    'USB': '數位週邊',
    '滑鼠墊': '數位週邊',
    '手機': '數位週邊',
    'T恤': '衣著服飾',
    '帽': '衣著服飾',
    '襪': '衣著服飾',
    '圍裙': '衣著服飾',
    '背心': '衣著服飾',
    '提袋': '提袋/束口袋',
    '束口袋': '提袋/束口袋',
    '帆布袋': '提袋/束口袋',
    '收納袋': '提袋/束口袋',
    'PVC': '提袋/束口袋',
    '抱枕': '抱枕/玩偶',
    '玩偶': '抱枕/玩偶',
    '娃': '抱枕/玩偶',
    '毛巾': '織品布類',
    '旗幟': '織品布類',
    '布': '織品布類',
    '木': '木製商品',
    '皮革': '皮革製品',
    '金屬': '金屬製品',
    '包裝': '包裝/配件',
    '紙袋': '包裝/配件',
    '代工': '客製化代工/服務',
}

# --- Blacklist: Shopline slugs/titles that are NOT real products ---
SLUG_BLACKLIST = {
    '每滿50件現折100元',
    '商品總覽圖',
}

# Title patterns that indicate non-product entries (promotions, catalog pages, etc.)
NON_PRODUCT_PATTERNS = [
    r'^每滿\d+件',       # 每滿50件現折100元 (volume discount promo)
    r'總覽圖$',          # 商品總覽圖 (catalog overview)
    r'^滿\d+件',         # 滿XX件... promotions
    r'免運',             # 免運 promotions
]

def is_real_product(shopline_product):
    """Check if a Shopline product is a real product (not a promo/catalog page)."""
    slug = get_shopline_slug(shopline_product)
    title = get_shopline_title(shopline_product)

    # Check slug blacklist
    if slug in SLUG_BLACKLIST:
        return False

    # Check title patterns
    for pattern in NON_PRODUCT_PATTERNS:
        if re.search(pattern, title):
            return False

    return True

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
    handle = product.get('handle', '')
    if handle:
        return handle
    permalink = product.get('permalink', '')
    if permalink:
        return permalink.rstrip('/').split('/')[-1]
    slug = product.get('slug', '')
    if slug:
        return slug
    link = product.get('link', '')
    if link:
        link_clean = link.split('?')[0].split('#')[0]
        last_seg = link_clean.rstrip('/').split('/')[-1]
        if last_seg and last_seg not in ('products', ''):
            return last_seg
    for field in ('url', 'product_url'):
        val = product.get(field, '')
        if val:
            val_clean = val.split('?')[0].split('#')[0]
            last_seg = val_clean.rstrip('/').split('/')[-1]
            if last_seg and last_seg not in ('products', ''):
                return last_seg
    return ''

def get_shopline_title(product):
    """Extract best title from Shopline product."""
    for lang in ('zh-hant', 'zh-TW', 'zh', 'en'):
        t = product.get('title_translations', {}).get(lang, '')
        if t:
            return t
    for t in product.get('title_translations', {}).values():
        if t:
            return t
    return product.get('title', product.get('handle', ''))

def get_shopline_images(product):
    """Extract image URLs from Shopline product."""
    imgs = []
    for img in product.get('images', product.get('medias', [])):
        src = img.get('original_url', img.get('url', img.get('src', '')))
        if src:
            imgs.append(src.split('?')[0])
    return imgs

def guess_category(title):
    """Guess internal category from product title."""
    for keyword, cat in CATEGORY_MAP.items():
        if keyword in title:
            return cat
    return '新品企劃'

def extract_moq_from_description(product):
    """Try to extract MOQ from Shopline product description."""
    for lang_desc in product.get('description_translations', {}).values():
        if not lang_desc:
            continue
        # Common patterns: "500件起訂", "100件起訂", "單圖300件起訂"
        m = re.search(r'(\d+)\s*件起訂', lang_desc)
        if m:
            return f'{m.group(1)}件起訂'
    return ''

def build_new_product(shopline_product):
    """Build an internal PRODUCTS entry from a Shopline product."""
    slug = get_shopline_slug(shopline_product)
    title = get_shopline_title(shopline_product)
    images = get_shopline_images(shopline_product)
    specs = extract_shopline_specs(shopline_product)
    moq = extract_moq_from_description(shopline_product)
    cat = guess_category(title)

    # Build _pricing structure from specs
    sizes = {}
    for spec_name in sorted(specs):
        sizes[spec_name] = {
            'prices': [],
            'sample': 0
        }

    # Extract print method from variations if available
    print_methods = set()
    for v in shopline_product.get('variations', shopline_product.get('variants', [])):
        for ov in v.get('option_values', []):
            val = ov.get('value', '')
            if '印刷' in val or '印' in val:
                print_methods.add(val)

    product = {
        'id': slug or shopline_product.get('id', ''),
        'name': title,
        'url': f'{SHOP_DOMAIN}/products/{slug}' if slug else '',
        'cat': cat,
        'hasSpec': len(specs) > 0,
        'moq': moq,
        'material': '',
        'size': '',
        'print': sorted(print_methods) if print_methods else [''],
        'leadtime': '',
        'notes': '',
        'qa': [],
        'newArrival': True,
        'newArrivalOrder': 0,
        '_imgs': images[:5],
        '_pricing': {
            'tiers': [],
            'sizes': sizes
        },
        '_partNumbers': {},
        '_autoAdded': True  # Flag for auto-added products
    }
    return product

def match_products(shopline_list, internal_list):
    """Match products by Shopline slug <-> internal URL slug, with name fallback."""
    if shopline_list:
        p0 = shopline_list[0]
        print(f'  [DEBUG] Shopline product keys: {sorted(p0.keys())}')
        print(f'  [DEBUG] Sample handle={p0.get("handle","")!r} permalink={p0.get("permalink","")!r} slug={p0.get("slug","")!r}')
        print(f'  [DEBUG] Sample link={p0.get("link","")!r}')
        print(f'  [DEBUG] Resolved slug: {get_shopline_slug(p0)!r}')

    # Build slug index
    internal_by_slug = {}
    for p in internal_list:
        url = p.get('url', '')
        if url:
            slug = url.rstrip('/').split('/')[-1]
            internal_by_slug[slug] = p

    # Build name index for secondary matching
    internal_by_name = {}
    for p in internal_list:
        name = p.get('name', '').strip()
        if name:
            # Normalize: remove "客製｜" or "客製|" prefix for matching
            clean = re.sub(r'^客製[｜|]\s*', '', name).strip()
            internal_by_name[clean] = p

    matches = []
    unmatched_sl = []
    matched_internal_ids = set()

    for sp in shopline_list:
        sl_slug = get_shopline_slug(sp)
        matched = False

        # Primary match: by slug
        if sl_slug and sl_slug in internal_by_slug:
            p = internal_by_slug.pop(sl_slug)
            matches.append((sp, p))
            matched_internal_ids.add(p['id'])
            matched = True
        else:
            # Secondary match: by title
            sl_title = get_shopline_title(sp)
            clean_title = re.sub(r'^客製[｜|]\s*', '', sl_title).strip()
            if clean_title in internal_by_name:
                p = internal_by_name[clean_title]
                if p['id'] not in matched_internal_ids:
                    matches.append((sp, p))
                    matched_internal_ids.add(p['id'])
                    # Remove from slug index too
                    url = p.get('url', '')
                    if url:
                        s = url.rstrip('/').split('/')[-1]
                        internal_by_slug.pop(s, None)
                    matched = True

        if not matched:
            unmatched_sl.append(sp)

    unmatched_int = [p for p in internal_by_slug.values() if p['id'] not in matched_internal_ids]
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
        sl_specs = extract_shopline_specs(sl)
        int_specs = set(internal.get('_pricing', {}).get('sizes', {}).keys())

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

def generate_report(diffs, unmatched_sl, unmatched_int, auto_added=None):
    """Generate markdown report."""
    auto_added = auto_added or []
    lines = ['# BardShop Shopline Sync Report\n']
    lines.append(f'Generated: {os.popen("date").read().strip()}\n')

    if not diffs and not unmatched_sl and not auto_added:
        lines.append('## ✅ All synced! No differences found.\n')
        return '\n'.join(lines)

    if auto_added:
        lines.append(f'## 🆕 Auto-added {len(auto_added)} new product(s) to internal DB\n')
        for p in auto_added:
            specs = list(p.get('_pricing', {}).get('sizes', {}).keys())
            spec_str = ', '.join(specs[:5]) if specs else '無規格'
            lines.append(f'- **{p["name"]}** (`{p["id"]}`)')
            lines.append(f'  - 分類: {p["cat"]}')
            lines.append(f'  - 規格: {spec_str}')
            lines.append(f'  - ⚠️ 需補充: 報價、料號、材質、MOQ 等')
        lines.append('')

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
        lines.append(f'## 📋 {len(unmatched_sl)} Shopline product(s) still unmatched\n')
        lines.append('These could not be matched by URL slug or name:\n')
        for sp in unmatched_sl[:20]:
            title = get_shopline_title(sp)
            slug = get_shopline_slug(sp)
            lines.append(f'- **{title}** (`{slug}`)')
        if len(unmatched_sl) > 20:
            lines.append(f'- ... and {len(unmatched_sl) - 20} more')
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
    print('=== BardShop Shopline Sync v4 ===')
    print(f'Mode: {MODE}')

    # 1. Fetch data
    print('[1/5] Fetching Shopline products...')
    sl_products = fetch_shopline_products()
    print(f'  Found {len(sl_products)} Shopline products')

    print('[2/5] Fetching internal database...')
    html = fetch_index_html()
    int_products, ob, cb = parse_products(html)
    print(f'  Found {len(int_products)} internal products')

    # 2. Match & Compare
    print('[3/5] Comparing...')
    matches, unmatched_sl, unmatched_int = match_products(sl_products, int_products)
    print(f'  Matched: {len(matches)}, Shopline-only: {len(unmatched_sl)}, Internal-only: {len(unmatched_int)}')

    diffs = compare_all(matches)
    print(f'  Differences found: {len(diffs)}')

    # 3. Auto-create new products from unmatched Shopline products
    print('[4/5] Checking for new products to auto-add...')
    existing_ids = {p['id'] for p in int_products}
    existing_urls = set()
    for p in int_products:
        url = p.get('url', '')
        if url:
            existing_urls.add(url.rstrip('/').split('/')[-1])

    auto_added = []
    still_unmatched = []
    skipped_non_products = []
    for sp in unmatched_sl:
        slug = get_shopline_slug(sp)
        title = get_shopline_title(sp)

        # Skip non-product entries (promotions, catalog pages, etc.)
        if not is_real_product(sp):
            skipped_non_products.append(title)
            print(f'    [SKIP] Non-product: {title} ({slug})')
            continue

        # Skip if slug already exists (edge case)
        if slug in existing_urls or slug in existing_ids:
            still_unmatched.append(sp)
            continue
        # Skip products without a slug (can't build proper entry)
        if not slug:
            still_unmatched.append(sp)
            continue

        new_product = build_new_product(sp)

        # Verify we're not creating a duplicate by ID
        if new_product['id'] in existing_ids:
            still_unmatched.append(sp)
            continue

        int_products.append(new_product)
        existing_ids.add(new_product['id'])
        existing_urls.add(slug)
        auto_added.append(new_product)

    if skipped_non_products:
        print(f'  Skipped {len(skipped_non_products)} non-product entries: {", ".join(skipped_non_products)}')

    if auto_added:
        print(f'  Auto-added {len(auto_added)} new products:')
        for p in auto_added:
            print(f'    + {p["name"]} ({p["id"]})')

        # Assign newArrivalOrder
        max_order = max((p.get('newArrivalOrder', 0) for p in int_products), default=0)
        for i, p in enumerate(auto_added):
            p['newArrivalOrder'] = max_order + 1 + i

        # Push updated HTML
        new_html = rebuild_html(html, ob, cb, int_products)
        names = ', '.join(p['name'] for p in auto_added[:3])
        if len(auto_added) > 3:
            names += f' 等{len(auto_added)}個'
        push_sha = push_to_github(new_html, f'[Sync] 自動新增 {len(auto_added)} 個商品: {names}')
        print(f'  Pushed to GitHub (SHA: {push_sha[:8]})')
    else:
        print('  No new products to add.')

    # 4. Generate report
    report = generate_report(diffs, still_unmatched, unmatched_int, auto_added)
    print('\n' + report)

    # 5. Create GitHub Issue
    print('[5/5] Creating report...')
    if diffs or still_unmatched or auto_added:
        issue_url = create_github_issue(
            f'[Sync] Shopline comparison report',
            report
        )
        print(f'  Issue created: {issue_url}')

        summary = f'\n🔄 BardShop Sync Report\n'
        if auto_added:
            summary += f'✅ 自動新增: {len(auto_added)} 個商品\n'
        summary += f'差異: {len(diffs)} 項\n'
        if still_unmatched:
            summary += f'未配對: {len(still_unmatched)} 項\n'
        if auto_added:
            for p in auto_added[:3]:
                summary += f'  + {p["name"]}\n'
        summary += f'\n詳情: {issue_url}'
        send_line_notify(summary)
    else:
        print('  No differences - no issue created.')
        send_line_notify('\n✅ BardShop Sync: 全部一致，無需更新')

    # Price validation
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
