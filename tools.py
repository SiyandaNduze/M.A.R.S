import os
import json
import time
import webbrowser
import platform
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
import subprocess
import ctypes
import smtplib
from email.message import EmailMessage
from datetime import datetime
import browser as _browser
import desktop as _desktop

from memory import remember_fact, recall_memories_text, list_all_memories, forget_fact

TODO_FILE = Path(__file__).resolve().parent / "todos.json"
TOOL_LOG_FILE = Path(__file__).resolve().parent / "tool_log.jsonl"
# Tools that pause and ask the user before running
CONFIRM_TOOLS = {"delete_todo", "send_email", "browser_upload_file"}


def _log_tool_call(name, args, result, elapsed_seconds, error=None):
    """
    Appends one line of JSON per tool call to tool_log.jsonl — a durable,
    searchable record for post-session review, independent of terminal
    scrollback. Logging failures are swallowed on purpose: a broken log
    must never break the actual tool call it's trying to record.
    """
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "tool": name,
        "args": args,
        "result_preview": (str(result)[:300] if result is not None else None),
        "elapsed_ms": round(elapsed_seconds * 1000),
        "error": error,
    }
    try:
        with TOOL_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

# ---------- To-do list (persisted locally in todos.json) ----------

def _load_todos():
    if TODO_FILE.exists():
        return json.loads(TODO_FILE.read_text())
    return []


def _save_todos(todos):
    TODO_FILE.write_text(json.dumps(todos, indent=2))


def add_todo(task):
    todos = _load_todos()
    todos.append({"task": task, "done": False})
    _save_todos(todos)
    return f"Added: {task}"


def list_todos():
    todos = _load_todos()
    if not todos:
        return "No tasks yet."
    lines = []
    for i, t in enumerate(todos):
        status = "x" if t["done"] else " "
        lines.append(f"{i}. [{status}] {t['task']}")
    return "\n".join(lines)


def complete_todo(index):
    todos = _load_todos()
    try:
        todos[index]["done"] = True
        _save_todos(todos)
        return f"Marked done: {todos[index]['task']}"
    except (IndexError, TypeError):
        return f"No task at index {index}. Use list_todos to see valid numbers."

def delete_todo(index):
    todos = _load_todos()
    try:
        removed = todos.pop(index)
        _save_todos(todos)
        return f"Deleted: {removed['task']}"
    except (IndexError, TypeError):
        return f"No task at index {index}. Use list_todos to see valid numbers."


def edit_todo(index, new_task):
    todos = _load_todos()
    try:
        old = todos[index]["task"]
        todos[index]["task"] = new_task
        _save_todos(todos)
        return f"Updated task {index}: '{old}' -> '{new_task}'"
    except (IndexError, TypeError):
        return f"No task at index {index}. Use list_todos to see valid numbers."


def uncomplete_todo(index):
    todos = _load_todos()
    try:
        todos[index]["done"] = False
        _save_todos(todos)
        return f"Marked not done: {todos[index]['task']}"
    except (IndexError, TypeError):
        return f"No task at index {index}. Use list_todos to see valid numbers."

# ---------- File reading ----------

def read_file(path):
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"File not found: {path}"

        suffix = p.suffix.lower()

        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return "PDF support requires pypdf. Run: py -m pip install pypdf"

            reader = PdfReader(str(p))
            chunks = []
            total = 0
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                chunks.append(t)
                total += len(t)
                if total > 8000:
                    break
            text = "\n".join(chunks).strip()
            if not text:
                return "PDF has no extractable text (may be a scanned image)."
            if len(text) > 8000:
                text = text[:8000] + "\n...[truncated, PDF is longer]"
            return text

        # Plain text / code / markdown etc.
        text = p.read_text(errors="ignore")
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated, file is longer]"
        return text
    except Exception as e:
        return f"Error reading file: {e}"


# ---------- Web search (Tavily) ----------

def web_search(query):
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return "Web search unavailable: TAVILY_API_KEY is not set in .env"

    from tavily import TavilyClient

    try:
        client = TavilyClient(api_key=api_key)
        result = client.search(query, max_results=4)
        results = result.get("results", [])
        if not results:
            return "No results found."
        lines = []
        for r in results:
            snippet = r.get("content", "")[:300]
            lines.append(f"- {r.get('title', 'Untitled')}: {snippet} ({r.get('url', '')})")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


NOTES_FILE = Path(__file__).resolve().parent / "notes.md"


def get_datetime():
    now = datetime.now()
    return now.strftime("It is %I:%M %p on %A, %d %B %Y.")


def system_status():
    drive = Path.home().anchor or "C:\\"
    total, used, free = shutil.disk_usage(drive)
    return (
        f"OS: {platform.system()} {platform.release()}\n"
        f"Machine: {platform.machine()}\n"
        f"Processor: {platform.processor()}\n"
        f"Drive {drive}: {free / 1e9:.1f} GB free of {total / 1e9:.1f} GB"
    )


def open_url(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url} in your browser."


def add_note(note):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now():%Y-%m-%d %H:%M} — {note}\n")
    return f"Saved note: {note}"


def list_notes():
    if not NOTES_FILE.exists():
        return "No notes yet."
    text = NOTES_FILE.read_text(encoding="utf-8", errors="ignore")
    return text[-4000:] if text else "No notes yet."


def get_weather(city):
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8").strip()
    except Exception as e:
        return f"Weather lookup failed: {e}"

# ---------- App launcher ----------

APP_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "vscode": "code",
    "vs code": "code",
    "code": "code",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
}

def launch_app(app_name):
    name = app_name.strip().lower()
    target = APP_ALIASES.get(name)

    if not target:
        # Try os.startfile with the literal name — no shell
        try:
            os.startfile(app_name)
            return f"Opened {app_name}."
        except Exception as e:
            return f"Unknown app '{app_name}'. Known: {', '.join(sorted(APP_ALIASES))} ({e})"

    if target == "spotify:":
        return open_spotify()

    try:
        subprocess.Popen([target])  # list form, no shell
        return f"Launched {app_name}."
    except FileNotFoundError:
        return f"'{app_name}' not found on PATH."
    except Exception as e:
        return f"Could not launch {app_name}: {e}"


# ---------- Spotify (media keys, no API needed) ----------

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

def _press_media_key(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP

def open_spotify():
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "spotify:"], shell=True)
        return "Opened Spotify."
    except Exception:
        exe = Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe"
        if exe.exists():
            subprocess.Popen([str(exe)])
            return "Opened Spotify."
        return "Could not find Spotify. Is it installed?"

def spotify_control(action):
    action = action.strip().lower()
    if action in ("play", "pause", "playpause", "toggle"):
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        return f"Sent {action} to media player."
    if action in ("next", "skip"):
        _press_media_key(VK_MEDIA_NEXT_TRACK)
        return "Skipped to next track."
    if action in ("prev", "previous", "back"):
        _press_media_key(VK_MEDIA_PREV_TRACK)
        return "Went to previous track."
    if action == "stop":
        _press_media_key(VK_MEDIA_STOP)
        return "Stopped playback."
    return f"Unknown Spotify action: {action}"

# ---------- File search ----------

def search_files(query, max_results=20):
    # Match each word separately (AND), not the whole phrase as one substring —
    # "siyanda cv" should match "Siyanda's CV.pdf" even though that exact
    # phrase never appears literally in the filename.
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return "No search terms provided."
    roots = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads",
        Path.home() / "Pictures",
        Path.home() / "Music",
        Path.home() / "Videos",
    ]
    skip_dirs = {"AppData", "node_modules", ".git", "__pycache__", "venv", ".venv", "Windows"}
    results = []
 
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if any(part in skip_dirs for part in path.parts):
                continue
            name_lower = path.name.lower()
            if all(t in name_lower for t in terms):
                results.append(str(path))
                if len(results) >= max_results:
                    return "\n".join(results)
 
    return "\n".join(results) if results else "No matching files found."

# ---------- Email ----------

def send_email(to, subject, body):
    addr = os.environ.get("EMAIL_ADDRESS")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    if not addr or not password:
        return "Email not configured. Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env"

    msg = EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(addr, password)
            smtp.send_message(msg)
        return f"Email sent to {to}."
    except Exception as e:
        return f"Email failed: {e}"

def open_file(path):
    """
    Open a local file with its default application (Windows).
    Accepts plain paths, Windows paths, or file:/// URLs.
    """
    if not path:
        return "No file path provided."

    p = path.strip().strip('"').strip("'")

    # Strip file:// or file:/// prefix
    if p.lower().startswith("file://"):
        p = p[7:]
        # On Windows file:///C:/... becomes /C:/... after stripping; drop leading slash
        if platform.system() == "Windows" and p.startswith("/") and len(p) > 2 and p[2] == ":":
            p = p[1:]

    # Decode %20 etc.
    p = urllib.parse.unquote(p)

    # Normalize slashes
    p = p.replace("/", os.sep) if platform.system() == "Windows" else p

    target = Path(p).expanduser()

    if not target.exists():
        return f"File not found: {target}"

    try:
        if platform.system() == "Windows":
            os.startfile(str(target))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return f"Opened {target}"
    except Exception as e:
        return f"Could not open file: {e}"

def open_url(url):
    if not url:
        return "No URL provided."

    # Route file:// URLs to open_file
    if url.lower().startswith("file://"):
        return open_file(url)

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    webbrowser.open(url)
    return f"Opened {url} in your browser."

TOOLS_SCHEMA = [
    # ---------- To-do list ----------
    {"type": "function", "function": {
        "name": "add_todo",
        "description": "Add a task to the to-do list.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "list_todos",
        "description": "List all to-do tasks with their index and status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "complete_todo",
        "description": "Mark a task done by its index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "delete_todo",
        "description": "Delete a task by its index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "edit_todo",
        "description": "Change a task's text by index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "new_task": {"type": "string"}}, "required": ["index", "new_task"]}}},
    {"type": "function", "function": {
        "name": "uncomplete_todo",
        "description": "Mark a task not-done by its index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},

    # ---------- File ----------
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a local file's contents. Supports text and PDFs.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "open_file",
        "description": "Open a local file with its default app. Use instead of open_url for files on disk.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Search for files by name in Desktop/Documents/Downloads/etc.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"}}, "required": ["query"]}}},

    # ---------- Web / info ----------
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current info. Not for live numbers (weather/prices).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Open an http/https URL in the default browser (no interaction).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "get_datetime",
        "description": "Current local date and time.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "system_status",
        "description": "OS, machine, and free disk space.",
        "parameters": {"type": "object", "properties": {}}}},

    # ---------- Notes ----------
    {"type": "function", "function": {
        "name": "add_note",
        "description": "Append a note to notes.md.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"}}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "list_notes",
        "description": "Show recent notes.",
        "parameters": {"type": "object", "properties": {}}}},

    # ---------- Apps / media ----------
    {"type": "function", "function": {
        "name": "launch_app",
        "description": "Launch a desktop app by name (notepad, chrome, vscode, explorer, etc.).",
        "parameters": {"type": "object", "properties": {
            "app_name": {"type": "string"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {
        "name": "open_spotify",
        "description": "Open Spotify.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "spotify_control",
        "description": "Spotify playback: play/pause/next/previous/stop.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}}, "required": ["action"]}}},

    # ---------- Email ----------
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email. Requires EMAIL_ADDRESS/EMAIL_APP_PASSWORD in .env.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"}}, "required": ["to", "subject", "body"]}}},

    # ---------- Memory ----------
    {"type": "function", "function": {
        "name": "remember_fact",
        "description": "Save a durable fact about the user (preferences, projects, key details).",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string"}}, "required": ["fact"]}}},
    {"type": "function", "function": {
        "name": "recall_memories",
        "description": "Search memory for facts matching a query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_all_memories",
        "description": "List every stored fact.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "forget_fact",
        "description": "Delete a stored fact matching a substring.",
        "parameters": {"type": "object", "properties": {
            "fact_substring": {"type": "string"}}, "required": ["fact_substring"]}}},

    # ---------- Browser automation ----------
    {"type": "function", "function": {
        "name": "browser_open",
        "description": "Open a URL in the automation browser for interaction (forms, clicks). Returns the page's interactive elements. Use web_search for plain reading.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "browser_read_page",
        "description": "Re-scan the current page. Only needed if the page changed on its own — action tools already return refreshed state.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "browser_click",
        "description": "Click an element by index from the last page read.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "browser_fill",
        "description": "Fill a text input by index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "value": {"type": "string"}}, "required": ["index", "value"]}}},
    {"type": "function", "function": {
        "name": "browser_select",
        "description": "Choose an option in a <select> dropdown by index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "value": {"type": "string"}}, "required": ["index", "value"]}}},
    {"type": "function", "function": {
        "name": "browser_upload_file",
        "description": "Upload a local file to a file input by index.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "file_path": {"type": "string"}}, "required": ["index", "file_path"]}}},
    {"type": "function", "function": {
        "name": "browser_press_key",
        "description": "Press a key on the page (Enter, Tab, Escape).",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}}, "required": ["key"]}}},
    {"type": "function", "function": {
        "name": "browser_go_back",
        "description": "Navigate back.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "browser_screenshot",
        "description": "Save a screenshot of the page to disk.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "browser_current_url",
        "description": "Current page URL and title.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "browser_close",
        "description": "Close the automation browser.",
        "parameters": {"type": "object", "properties": {}}}},

    # ---------- Desktop automation ----------
    {"type": "function", "function": {
        "name": "list_windows",
        "description": "List visible desktop windows.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_window_controls",
        "description": "Enumerate controls inside a window (matches by title substring). Required before desktop_click/desktop_type.",
        "parameters": {"type": "object", "properties": {
            "window_title": {"type": "string"}}, "required": ["window_title"]}}},
    {"type": "function", "function": {
        "name": "desktop_click",
        "description": "Click a control by index from get_window_controls.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "desktop_type",
        "description": "Type text into a control by index (simulated keystrokes).",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "text": {"type": "string"}}, "required": ["index", "text"]}}},
    {"type": "function", "function": {
        "name": "desktop_set_text",
        "description": "Set a control's text directly (faster than desktop_type).",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "text": {"type": "string"}}, "required": ["index", "text"]}}},
    {"type": "function", "function": {
        "name": "desktop_send_keys",
        "description": "Send keystrokes to the focused window (pywinauto syntax: '{ENTER}', '^s', '%{F4}').",
        "parameters": {"type": "object", "properties": {
            "keys": {"type": "string"}}, "required": ["keys"]}}},
    {"type": "function", "function": {
        "name": "desktop_focus_window",
        "description": "Bring a window to the foreground (matches by title substring).",
        "parameters": {"type": "object", "properties": {
            "window_title": {"type": "string"}}, "required": ["window_title"]}}},
    {"type": "function", "function": {
        "name": "desktop_screenshot",
        "description": "Save a screenshot of a window.",
        "parameters": {"type": "object", "properties": {
            "window_title": {"type": "string"}}, "required": ["window_title"]}}},
    {"type": "function", "function": {
    "name": "desktop_clear_control",
    "description": "Clear a text control's existing contents before typing.",
    "parameters": {"type": "object", "properties": {
        "index": {"type": "integer"}}, "required": ["index"]}}},
]

_FUNCTIONS = {
    "add_todo": lambda args: add_todo(args["task"]),
    "list_todos": lambda args: list_todos(),
    "complete_todo": lambda args: complete_todo(args["index"]),
    "read_file": lambda args: read_file(args["path"]),
    "web_search": lambda args: web_search(args["query"]),
    "get_datetime": lambda args: get_datetime(),
    "system_status": lambda args: system_status(),
    "open_url": lambda args: open_url(args["url"]),
    "add_note": lambda args: add_note(args["note"]),
    "list_notes": lambda args: list_notes(),
    "get_weather": lambda args: get_weather(args["city"]),
    "launch_app": lambda args: launch_app(args["app_name"]),
    "open_spotify": lambda args: open_spotify(),
    "spotify_control": lambda args: spotify_control(args["action"]),
    "search_files": lambda args: search_files(args["query"], args.get("max_results", 20)),
    "send_email": lambda args: send_email(args["to"], args["subject"], args["body"]),
    "open_file": lambda args: open_file(args["path"]),
    "delete_todo": lambda args: delete_todo(args["index"]),
    "edit_todo": lambda args: edit_todo(args["index"], args["new_task"]),
    "uncomplete_todo": lambda args: uncomplete_todo(args["index"]),
    "remember_fact": lambda args: remember_fact(args["fact"]),
    "recall_memories": lambda args: recall_memories_text(args["query"]),
    "list_all_memories": lambda args: list_all_memories(),
    "forget_fact": lambda args: forget_fact(args["fact_substring"]),
        
    # Browser automation
    "browser_open": lambda args: _browser.browser_open(args["url"]),
    "browser_read_page": lambda args: _browser.browser_read_page(),
    "browser_click": lambda args: _browser.browser_click(args["index"]),
    "browser_fill": lambda args: _browser.browser_fill(args["index"], args["value"]),
    "browser_select": lambda args: _browser.browser_select(args["index"], args["value"]),
    "browser_upload_file": lambda args: _browser.browser_upload_file(args["index"], args["file_path"]),
    "browser_press_key": lambda args: _browser.browser_press_key(args["key"]),
    "browser_go_back": lambda args: _browser.browser_go_back(),
    "browser_screenshot": lambda args: _browser.browser_screenshot(),
    "browser_current_url": lambda args: _browser.browser_current_url(),
    "browser_close": lambda args: _browser.browser_close(),

    # Desktop app automation
    "list_windows": lambda args: _desktop.list_windows(),
    "get_window_controls": lambda args: _desktop.get_window_controls(args["window_title"]),
    "desktop_click": lambda args: _desktop.desktop_click(args["index"]),
    "desktop_type": lambda args: _desktop.desktop_type(args["index"], args["text"]),
    "desktop_set_text": lambda args: _desktop.desktop_set_text(args["index"], args["text"]),
    "desktop_send_keys": lambda args: _desktop.desktop_send_keys(args["keys"]),
    "desktop_focus_window": lambda args: _desktop.desktop_focus_window(args["window_title"]),
    "desktop_screenshot": lambda args: _desktop.desktop_screenshot(args["window_title"]),
    "desktop_clear_control": lambda args: _desktop.desktop_clear_control(args["index"]),
}


_SUBMIT_KEYWORDS = {
    "submit", "send", "apply", "confirm", "pay", "purchase", "buy",
    "delete", "remove", "unsubscribe", "publish", "post", "order",
    "checkout", "complete", "finalize",
}


def _looks_like_submit(args):
    """Return a description of the click target if it looks like a
    consequential action (form submit, payment, post, etc.), else None."""
    text = ""
    for v in args.values():
        if isinstance(v, str):
            text += " " + v.lower()
    for kw in _SUBMIT_KEYWORDS:
        if kw in text:
            return kw
    return None


def _default_confirm(name, args, submit_kw):
    """Keyboard-based confirmation — the original behavior, used whenever
    the caller doesn't supply a confirm_fn (i.e. typed/non-voice turns)."""
    if submit_kw:
        print(f"\n  [!] This click looks consequential: {submit_kw}")
    preview = ", ".join(f"{k}={v!r}" for k, v in args.items())
    try:
        answer = input(f"  [confirm] {name}({preview}) - allow? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return answer == "y"


def execute_tool(name, args, confirm_fn=None):
    """
    confirm_fn, if given, replaces the default keyboard-based confirmation
    prompt. It's called as confirm_fn(name, args, submit_kw) -> bool. This
    is how voice turns route confirmation through speech instead of
    blocking on keyboard input mid-conversation — see main.py's
    _voice_confirm. Every call (allowed, cancelled, or errored) is logged
    to tool_log.jsonl regardless of which confirm path was used.
    """
    func = _FUNCTIONS.get(name)
    if not func:
        _log_tool_call(name, args, None, 0.0, error="unknown_tool")
        return f"Unknown tool: {name}"

    must_confirm = name in CONFIRM_TOOLS
    submit_kw = None

    # For clicks: only prompt when the click text looks consequential.
    if name in ("browser_click", "desktop_click"):
        # Look at the cached element the index points to
        try:
            idx = args.get("index", -1)
            text = ""
            if name == "browser_click" and _browser._last_elements:
                if 0 <= idx < len(_browser._last_elements):
                    text = _browser._last_elements[idx].get("text", "")
            elif name == "desktop_click" and _desktop._last_controls:
                if 0 <= idx < len(_desktop._last_controls):
                    text = _desktop._last_controls[idx].element_info.name or ""
            for kw in _SUBMIT_KEYWORDS:
                if kw in text.lower():
                    submit_kw = f"{kw} (target: {text!r})"
                    must_confirm = True
                    break
        except Exception:
            pass

    if must_confirm:
        confirm = confirm_fn or _default_confirm
        allowed = confirm(name, args, submit_kw)
        if not allowed:
            _log_tool_call(name, args, "Cancelled by user", 0.0, error="cancelled")
            return "Cancelled by user."

    start = time.time()
    try:
        result = func(args)
        _log_tool_call(name, args, result, time.time() - start)
        return result
    except Exception as e:
        _log_tool_call(name, args, None, time.time() - start, error=str(e))
        return f"Error running {name}: {e}"