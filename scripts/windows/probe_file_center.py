from __future__ import annotations

from playwright.sync_api import sync_playwright


JS = r"""
(needle) => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none' &&
      Number(style.opacity || 1) > 0;
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const roots = Array.from(document.querySelectorAll(
    '.river-notification-center_notification, .notification-center, .notification-drawer-container, #notification-center'
  ));
  if (!roots.length) roots.push(document);
  const selectors = [
    'li[id^="fileTask"]',
    '[id^="fileTask"]',
    '[id*="fileTask"]',
    '[role="row"]',
    '.next-table-row',
    '.next-list-item',
    '.file-task-item',
    '[class*="fileTask"]',
    '[class*="file-task"]',
    '[class*="task-item"]',
    '.file-item',
    '[class*="file-item"]'
  ];
  const rows = [];
  const seen = new Set();
  for (const root of roots) {
    for (const selector of selectors) {
      for (const row of root.querySelectorAll(selector)) {
        if (seen.has(row)) continue;
        seen.add(row);
        const text = textOf(row);
        if (text.includes(needle)) rows.push({row, text, selector});
      }
    }
  }
  return rows.slice(0, 8).map(({row, text, selector}) => ({
    selector,
    rowTag: row.tagName,
    rowClass: row.className,
    rowVisible: visible(row),
    rowRect: (() => {
      const rect = row.getBoundingClientRect();
      return {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)};
    })(),
    text: text.slice(0, 240),
    links: Array.from(row.querySelectorAll('a, button, [role="button"], [title], [aria-label], span, div'))
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          tag: el.tagName,
          cls: String(el.className || '').slice(0, 80),
          text: textOf(el).slice(0, 80),
          title: el.getAttribute('title') || '',
          aria: el.getAttribute('aria-label') || '',
          role: el.getAttribute('role') || '',
          visible: visible(el),
          rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
        };
      })
      .filter((item) => item.text.includes('下载') || item.title.includes('下载') || item.aria.includes('下载') || item.role === 'button')
      .slice(0, 20)
  }));
}
"""


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9221", timeout=5000)
        contexts = browser.contexts
        pages = [page for context in contexts for page in context.pages]
        for page in pages:
            print(f"PAGE {page.url}")
            for frame in page.frames:
                try:
                    rows = frame.evaluate(JS, "PO明细确认分页导出")
                except Exception as exc:
                    print(f"  FRAME {frame.url[:120]} ERR {exc}")
                    continue
                if rows:
                    print(f"  FRAME {frame.url[:120]}")
                    for row in rows:
                        print(row)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
