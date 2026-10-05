"""
Browser automation via Playwright.

Uses a persistent browser profile (stored in .browser_profile/) so logins
survive across runs — log into a site once manually in the automation
browser, and future agent runs are already signed in.

Design: read_page() enumerates every interactive element on the current
page and tags each with a stable data-mars-index attribute. All subsequent
actions (click, fill, select) address elements by that index, so the model
never has to guess selectors or match duplicate text.
"""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
_PROFILE_DIR = _HERE / ".browser_profile"
_SCREENSHOT_DIR = _HERE / "screenshots"

# Module-level state — one browser, one page per process. Sync API so
# this composes cleanly with the rest of the agent's synchronous flow.
_pw = None
_context = None
_page = None
_last_elements = []  # cache of the last read_page() result, indexed the same way
_last_title = ""
_last_url = ""


def _ensure_browser():
    """Lazily start the persistent browser on first use."""
    global _pw, _context, _page
    if _page is not None and not _page.is_closed():
        return
    _PROFILE_DIR.mkdir(exist_ok=True)
    _pw = sync_playwright().start()
    _context = _pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=False,  # visible so you can watch and intervene
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    _page = _context.pages[0] if _context.pages else _context.new_page()


def _element_scan_js():
    """
    Runs in the browser. Tags every visible interactive element with a
    data-mars-index attribute and returns a compact description of each.
    """
    return """
    () => {
        const SELECTOR = 'a[href], button, input, textarea, select, ' +
                         '[role=button], [role=link], [role=checkbox], ' +
                         '[role=menuitem], [contenteditable=true]';
        const els = Array.from(document.querySelectorAll(SELECTOR));
        const visible = els.filter(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return false;
            const s = getComputedStyle(el);
            if (s.visibility === 'hidden' || s.display === 'none') return false;
            if (el.type === 'hidden') return false;
            return true;
        });
        visible.forEach((el, i) => el.setAttribute('data-mars-index', String(i)));
        return visible.map((el, i) => {
            const text = (el.innerText || el.value ||
                          el.getAttribute('aria-label') ||
                          el.placeholder || el.name || '').trim().slice(0, 120);
            return {
                i: i,
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                text: text,
                name: el.name || '',
                placeholder: el.placeholder || '',
                aria: el.getAttribute('aria-label') || '',
                href: el.href ? el.href.slice(0, 100) : '',
                checked: el.checked === true,
            };
        });
    }
    """


def _format_elements(elements, title, url):
    if not elements:
        return (f"[Page: {title} — {url}]\n\n"
                "No interactive elements found. The page may still be loading — "
                "call browser_read_page again after a moment.")
    lines = [f"[Page: {title} — {url}]", ""]
    for e in elements:
        parts = [f"{e['i']:>3}", e["tag"]]
        if e["type"]:
            parts[1] = f"{e['tag']}[{e['type']}]"
        if e["text"]:
            parts.append(f'"{e["text"]}"')
        if e["placeholder"] and e["placeholder"] not in e["text"]:
            parts.append(f"placeholder={e['placeholder']!r}")
        if e["href"]:
            parts.append(f"href={e['href']}")
        if e["tag"] in ("input",) and e["type"] == "checkbox":
            parts.append(f"checked={e['checked']}")
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


def _refresh():
    """Re-scans the current page and updates module state."""
    global _last_elements, _last_title, _last_url
    _ensure_browser()
    try:
        _last_title = _page.title()
        _last_url = _page.url
        _last_elements = _page.evaluate(_element_scan_js())
    except Exception as e:
        _last_elements = []
        return f"[Could not read page: {e}]"
    return _format_elements(_last_elements, _last_title, _last_url)


def browser_open(url):
    _ensure_browser()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        _page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Give SPA-rendered pages a moment to hydrate
        _page.wait_for_timeout(800)
    except Exception as e:
        return f"[Navigation failed: {e}]"
    return _refresh()


def browser_read_page():
    """Re-scan the current page — call this after any action that might
    have changed the page (click that opens a modal, navigation, etc.)."""
    if _page is None or _page.is_closed():
        return ("No browser session is open. Do NOT call any other browser_* "
                "tool. If the user's request involves a website, call browser_open first. "
                "Otherwise, use a different tool entirely.")
    try:
        _page.wait_for_timeout(300)
    except Exception:
        pass
    return _refresh()


def _resolve(index):
    if not _last_elements:
        return None, "No elements cached. Call browser_read_page first."
    if index < 0 or index >= len(_last_elements):
        return None, (f"Element index {index} out of range "
                      f"(valid: 0..{len(_last_elements) - 1}). "
                      f"Call browser_read_page to refresh.")
    sel = f'[data-mars-index="{index}"]'
    el = _page.locator(sel).first
    if el.count() == 0:
        return None, (f"Element {index} is no longer on the page. "
                      f"Call browser_read_page to refresh.")
    return el, None


def browser_click(index):
    el, err = _resolve(index)
    if err:
        return err
    desc = _last_elements[index]
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        el.click(timeout=5000)
        _page.wait_for_timeout(600)
    except Exception as e:
        # Fallback: try a JS click — works when overlays intercept real clicks
        try:
            el.evaluate("node => node.click()")
            _page.wait_for_timeout(600)
        except Exception as e2:
            return f"[Click failed on element {index}: {e} / {e2}]"
    label = desc["text"] or desc["tag"]
    return f"Clicked {index} ({label}).\n\n" + _refresh()


def browser_fill(index, value):
    el, err = _resolve(index)
    if err:
        return err
    desc = _last_elements[index]
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        el.fill("", timeout=5000)   # clear existing content
        el.fill(value, timeout=5000)
    except Exception as e:
        return f"[Fill failed on element {index}: {e}]"
    label = desc["text"] or desc["placeholder"] or desc["name"] or desc["tag"]
    return f"Filled {index} ({label}) with {value!r}."


def browser_select(index, value):
    el, err = _resolve(index)
    if err:
        return err
    try:
        el.select_option(label=value, timeout=5000)
    except Exception:
        try:
            el.select_option(value=value, timeout=5000)
        except Exception as e:
            return f"[Select failed on element {index}: {e}]"
    return f"Selected {value!r} on element {index}."


def browser_upload_file(index, file_path):
    el, err = _resolve(index)
    if err:
        return err
    p = Path(file_path).expanduser()
    if not p.exists():
        return f"File not found: {p}"
    try:
        el.set_input_files(str(p), timeout=10000)
    except Exception as e:
        return f"[Upload failed on element {index}: {e}]"
    return f"Uploaded {p.name} to element {index}."


def browser_press_key(key):
    """Press a keyboard key in the current page. Useful for Enter, Tab,
    Escape, or arrow keys. Examples: 'Enter', 'Tab', 'Escape'."""
    if _page is None or _page.is_closed():
        return "No browser is open."
    try:
        _page.keyboard.press(key)
        _page.wait_for_timeout(400)
    except Exception as e:
        return f"[Key press failed: {e}]"
    return f"Pressed {key}.\n\n" + _refresh()


def browser_go_back():
    if _page is None or _page.is_closed():
        return "No browser is open."
    try:
        _page.go_back(wait_until="domcontentloaded", timeout=15000)
        _page.wait_for_timeout(600)
    except Exception as e:
        return f"[Back navigation failed: {e}]"
    return "Went back.\n\n" + _refresh()


def browser_screenshot():
    if _page is None or _page.is_closed():
        return "No browser is open."
    _SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = _SCREENSHOT_DIR / f"page_{int(time.time())}.png"
    try:
        _page.screenshot(path=str(path), full_page=False)
    except Exception as e:
        return f"[Screenshot failed: {e}]"
    return f"Saved screenshot to {path}"


def browser_close():
    global _context, _pw, _page
    try:
        if _context:
            _context.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _context = None
    _pw = None
    _page = None
    return "Browser closed."


def browser_current_url():
    """Returns the current page URL — useful for the agent to check
    whether a navigation or form submission succeeded."""
    if _page is None or _page.is_closed():
        return "No browser is open."
    return f"Current URL: {_page.url}\nTitle: {_page.title()}"