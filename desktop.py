"""
Windows desktop app automation via pywinauto's UIA backend.

Same read-then-act pattern as browser.py: list_windows() shows what's
open, get_window_controls() enumerates the control tree of one window,
then click/type address controls by index. Uses Windows' own accessibility
API — no pixel guessing, no vision model needed.

Limitations: works with apps that expose a proper accessibility tree
(most standard Windows software — Notepad, Explorer, older business apps).
Struggles with apps built on custom-drawn frameworks or games.
"""

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pywinauto")

# Force STA mode at import time so we don't get the "Revert to STA" warning
# on every call. Must happen before any pywinauto import.
try:
    import sys
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
except Exception:
    pass

import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCREENSHOT_DIR = _HERE / "screenshots"

# Module-level state so click/type can reference the last-read controls
_last_controls = []
_last_window_title = ""


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def list_windows():
    """Returns every visible top-level window, formatted for the model."""
    try:
        d = _desktop()
    except Exception as e:
        return f"[pywinauto not available: {e}]"
    try:
        windows = d.windows(visible_only=True)
    except Exception as e:
        return f"[Could not list windows: {e}]"
    if not windows:
        return "No visible windows."
    lines = ["[Open windows]", ""]
    for w in windows:
        try:
            title = (w.window_text() or "").strip()
            if not title:
                continue
            rect = w.rectangle()
            size = f"{rect.width()}x{rect.height()}"
            lines.append(f"  - {title!r}  ({size})")
        except Exception:
            continue
    return "\n".join(lines) if len(lines) > 2 else "No titled windows."


def _find_window(title_substring):
    try:
        d = _desktop()
    except Exception as e:
        return None, f"[pywinauto not available: {e}]"
    needle = title_substring.lower()
    try:
        windows = d.windows(visible_only=True)
    except Exception as e:
        return None, f"[Could not list windows: {e}]"
    for w in windows:
        try:
            title = (w.window_text() or "")
            if needle in title.lower():
                return w, None
        except Exception:
            continue
    return None, f"No open window matched {title_substring!r}."


def get_window_controls(title_substring):
    """
    Enumerates every control in the matched window and caches them for
    subsequent click/type calls. Focuses the window first so it's in the
    foreground (some apps won't accept input when backgrounded).
    """
    global _last_controls, _last_window_title
    w, err = _find_window(title_substring)
    if err:
        return err
    try:
        w.set_focus()
    except Exception:
        pass
    try:
        controls = w.descendants()
    except Exception as e:
        return f"[Could not read controls: {e}]"

    _last_controls = []
    _last_window_title = w.window_text()
    lines = [f"[Controls in window: {_last_window_title!r}]", ""]

    for c in controls:
        try:
            info = c.element_info
        except Exception:
            continue
        ctype = (getattr(info, "control_type", "") or "").strip() or "?"
        name = (getattr(info, "name", "") or "").strip()
        auto_id = (getattr(info, "automation_id", "") or "").strip()
        try:
            rect = c.rectangle()
            if rect.width() < 2 or rect.height() < 2:
                continue
        except Exception:
            pass

        # Only keep controls that are actually actionable or readable
        if ctype not in (
            "Button", "Edit", "ComboBox", "CheckBox", "RadioButton",
            "MenuItem", "TabItem", "ListItem", "Hyperlink", "Text",
            "Document", "TreeItem", "Slider", "Spinner",
        ):
            continue

        idx = len(_last_controls)
        _last_controls.append(c)

        parts = [f"{idx:>3}", ctype]
        if name:
            parts.append(f'"{name[:80]}"')
        if auto_id:
            parts.append(f"id={auto_id}")
        lines.append("  " + " ".join(parts))

    if len(_last_controls) == 0:
        return (f"[Window {_last_window_title!r} has no accessible controls. "
                f"This app may not expose an accessibility tree.]")
    return "\n".join(lines)


def _resolve_control(index):
    if not _last_controls:
        return None, "No controls cached. Call get_window_controls first."
    if index < 0 or index >= len(_last_controls):
        return None, (f"Control index {index} out of range "
                      f"(valid: 0..{len(_last_controls) - 1}). "
                      f"Call get_window_controls to refresh.")
    return _last_controls[index], None


def desktop_click(index):
    ctrl, err = _resolve_control(index)
    if err:
        return err
    try:
        # click_input() moves the real mouse — more reliable for apps that
        # ignore the UIA Invoke pattern.
        ctrl.click_input()
    except Exception:
        try:
            ctrl.click()
        except Exception as e:
            return f"[Click failed on control {index}: {e}]"
    time.sleep(0.3)
    return f"Clicked control {index}."


def desktop_type(index, text):
    ctrl, err = _resolve_control(index)
    if err:
        return err
    try:
        ctrl.set_focus()
        ctrl.type_keys(text, with_spaces=True, with_newlines=True,
                       set_foreground=True, pause=0.02)
    except Exception as e:
        return f"[Type failed on control {index}: {e}]"
    return f"Typed into control {index}: {text!r}"


def desktop_set_text(index, text):
    """Sets a text field's value directly (faster than typing). Different
    backends expose different methods — tries UIA's set_value, then falls
    back to typing."""
    ctrl, err = _resolve_control(index)
    if err:
        return err
    # Try UIA's set_value first (works for Edit, Document, ComboBox)
    try:
        ctrl.set_value(text)
        return f"Set control {index} to {text!r}"
    except Exception:
        pass
    # Some UIA controls don't expose set_value — try typing instead
    try:
        ctrl.set_focus()
        ctrl.type_keys(text, with_spaces=True, with_newlines=True,
                       set_foreground=True, pause=0.02)
        return f"Set control {index} to {text!r} (via typing)"
    except Exception as e:
        return f"[set_text failed on control {index}: {e}. Try desktop_type instead.]"


def desktop_send_keys(keys):
    """
    Sends keystrokes to whatever currently has focus. Supports pywinauto's
    mini-language: '{ENTER}', '{TAB}', '^s' (Ctrl+S), '%{F4}' (Alt+F4), etc.
    """
    try:
        from pywinauto.keyboard import send_keys as sk
    except Exception as e:
        return f"[pywinauto not available: {e}]"
    try:
        sk(keys)
    except Exception as e:
        return f"[send_keys failed: {e}]"
    return f"Sent keys: {keys}"


def desktop_focus_window(title_substring):
    w, err = _find_window(title_substring)
    if err:
        return err
    try:
        w.set_focus()
    except Exception as e:
        return f"[Focus failed: {e}]"
    return f"Focused window {w.window_text()!r}"

def desktop_clear_control(index):
    """Clears a text control's contents, then leaves focus there."""
    ctrl, err = _resolve_control(index)
    if err:
        return err
    try:
        ctrl.set_focus()
        # Select all then delete — works for Edit, Document, ComboBox
        ctrl.type_keys("^a{BACKSPACE}", set_foreground=True, pause=0.02)
        return f"Cleared control {index}."
    except Exception as e:
        return f"[Clear failed on control {index}: {e}]"

def desktop_screenshot(title_substring):
    w, err = _find_window(title_substring)
    if err:
        return err
    _SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = _SCREENSHOT_DIR / f"window_{int(time.time())}.png"
    try:
        img = w.capture_as_image()
        img.save(str(path))
    except Exception as e:
        return f"[Screenshot failed: {e}]"
    return f"Saved screenshot to {path}"