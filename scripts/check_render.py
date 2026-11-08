#!/usr/bin/env python3
"""检查从零推导 HTML 产物的常见问题。

用法：
    python3 check_render.py <html路径> [--viewport 390x844]

两项检查：
  1. 静态扫描：公式内裸写的 `<` 后跟字母（HTML 解析器的坑，最常见）
  2. 浏览器检查：逐章残留 `$`、KaTeX 元素数、导航容器、横向溢出、console 错误

只有装了 playwright 才跑第二项；没装就只报静态结果。
"""

import argparse
import json
import re
import sys
from pathlib import Path

BLOCK_RE = re.compile(r'\$\$(.*?)\$\$', re.S)
INLINE_RE = re.compile(r'\$(?!\$)([^$\n]{1,400}?)\$(?!\$)')
BAD_LT_RE = re.compile(r'<[a-zA-Z]')


def static_scan(html: str):
    """找 $...$ 与 $$...$$ 里 < 后面跟字母的位置。"""
    issues = []
    for pattern, label in ((BLOCK_RE, 'block'), (INLINE_RE, 'inline')):
        for m in pattern.finditer(html):
            body = m.group(1)
            for hit in BAD_LT_RE.finditer(body):
                pos = m.start(1) + hit.start()
                line = html.count('\n', 0, pos) + 1
                ctx = body[max(0, hit.start() - 12): hit.start() + 12]
                issues.append({
                    'kind': 'bare-lt',
                    'math': label,
                    'line': line,
                    'context': ctx,
                    'fix': ctx.replace('<', '&lt;', 1),
                })
    return issues


def browser_scan(html_path: Path, viewport: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'skipped': 'playwright 未安装，跳过浏览器检查'}

    w, h = (int(x) for x in viewport.split('x'))
    result = {'sections': [], 'overflow': None, 'console': []}

    with sync_playwright() as p:
        launch = {}
        for candidate in ('/usr/local/bin/chromium', '/usr/bin/chromium'):
            if Path(candidate).exists():
                launch['executable_path'] = candidate
                break
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={'width': w, 'height': h})
        page.on('console', lambda msg: result['console'].append(msg.text)
                if msg.type == 'error' else None)
        page.goto('file://' + str(html_path.resolve()), wait_until='domcontentloaded')
        page.wait_for_timeout(5000)

        sec_ids = page.evaluate("""() => Array.from(document.querySelectorAll('section[id]')).map(s => s.id)""")
        has_show = page.evaluate("() => typeof window.showChapter === 'function'")

        for sid in sec_ids:
            if has_show:
                page.evaluate("id => window.showChapter(id)", sid)
                page.wait_for_timeout(350)
            info = page.evaluate("""sid => {
                const sec = document.getElementById(sid);
                if (!sec) return {id: sid, missing: true};
                let left = 0, walker = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT);
                let n, samples = [];
                while (n = walker.nextNode()) {
                    if (n.textContent.includes('$')) {
                        left++;
                        if (samples.length < 3) samples.push(n.textContent.slice(0, 60));
                    }
                }
                return {
                    id: sid,
                    katex: sec.querySelectorAll('.katex').length,
                    leftover: left,
                    samples: samples,
                    breadcrumb: !!sec.querySelector('.breadcrumb'),
                    chapterNav: !!sec.querySelector('.chapter-nav'),
                };
            }""", sid)
            result['sections'].append(info)

        result['overflow'] = page.evaluate("""() => ({
            scrollW: document.documentElement.scrollWidth,
            clientW: document.documentElement.clientWidth
        })""")
        result['viewport'] = f'{w}x{h}'
        browser.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('html', help='待检查的 HTML 文件')
    ap.add_argument('--viewport', default='390x844', help='移动端视口，默认 390x844')
    ap.add_argument('--json', action='store_true', help='只输出 JSON')
    args = ap.parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        print(f'找不到文件：{html_path}', file=sys.stderr)
        return 2
    html = html_path.read_text(encoding='utf-8')

    static = static_scan(html)
    dynamic = browser_scan(html_path, args.viewport)

    if args.json:
        print(json.dumps({'static': static, 'dynamic': dynamic}, ensure_ascii=False, indent=2))
    else:
        print(f'文件：{html_path.name}（{html_path.stat().st_size / 1024:.0f} KB）')

        print(f'\n[静态] 公式内裸 < 后跟字母：{len(static)} 处')
        for it in static[:20]:
            print(f'  第 {it["line"]} 行 {it["math"]}：{it["context"]!r}')

        if 'skipped' in dynamic:
            print(f'\n[浏览器] {dynamic["skipped"]}')
        else:
            print(f'\n[浏览器] 视口 {dynamic.get("viewport")}')
            bad = 0
            for s in dynamic['sections']:
                if s.get('missing'):
                    print(f'  {s["id"]}: section 不存在')
                    bad += 1
                    continue
                flags = []
                if s['leftover']:
                    flags.append(f'残留 $ {s["leftover"]} 处 {s["samples"]}')
                if not s['breadcrumb']:
                    flags.append('缺面包屑')
                if not s['chapterNav']:
                    flags.append('缺上下章容器')
                if flags:
                    bad += 1
                    print(f'  {s["id"]}: KaTeX {s["katex"]}，' + '；'.join(flags))
            if bad == 0:
                print(f'  {len(dynamic["sections"])} 个章节全部通过')
            o = dynamic['overflow']
            if o and o['scrollW'] > o['clientW'] + 2:
                print(f'  横向溢出：scrollWidth {o["scrollW"]} > clientWidth {o["clientW"]}')
            else:
                print(f'  无横向溢出（{o["scrollW"]}px）')
            errs = [c for c in dynamic['console'] if 'font' not in c.lower() or '404' not in c]
            if errs:
                print(f'  console 错误 {len(errs)} 条：{errs[:3]}')

    ok = not static and (isinstance(dynamic, dict) and 'skipped' in dynamic
                         or (not any(s.get('leftover') or s.get('missing') or
                                     (s.get('breadcrumb') is False) or (s.get('chapterNav') is False)
                                     for s in dynamic.get('sections', []))
                             and not (dynamic.get('overflow') or {}).get('scrollW', 0) >
                             (dynamic.get('overflow') or {}).get('clientW', 9999) + 2))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
