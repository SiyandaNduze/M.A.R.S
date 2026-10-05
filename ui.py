"""
Desktop orb widget — transparent, per-pixel-alpha rendering via Windows
layered windows (UpdateLayeredWindow). This eliminates the AA halo that
occurred with Tk's colorkey transparency.

Original design (concentric rings + rotating tick marks + amplitude-
reactive glowing core). Replies and live transcripts appear in a caption
bubble near the orb.

Important: never call Tk's wm_attributes("-alpha", ...) on the caption
window. That maps to SetLayeredWindowAttributes on Windows, which is an
alternative to UpdateLayeredWindow — calling it switches the window out of
per-pixel-alpha mode and the window becomes a solid black rectangle.
Caption fade is done by scaling the alpha channel of the layered image.
"""

import ctypes
import json
import math
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ---------- ctypes setup ----------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]

user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),
    wintypes.HDC,
    ctypes.POINTER(POINT),
    wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
]


def _hwnd_of(widget):
    """Return the actual top-level HWND for a Tk widget."""
    h = widget.winfo_id()
    parent = user32.GetParent(h)
    return parent if parent else h


def _premultiply_bgra(pil_rgba):
    """Convert RGBA to premultiplied BGRA bytes (what UpdateLayeredWindow wants)."""
    import numpy as np
    arr = np.asarray(pil_rgba, dtype=np.uint8)
    arr = np.ascontiguousarray(arr)
    alpha = arr[:, :, 3:4].astype(np.uint16)
    rgb = arr[:, :, 0:3].astype(np.uint16)
    rgb = (rgb * alpha) // 255
    out = np.empty_like(arr)
    out[:, :, 0] = rgb[:, :, 2]  # B
    out[:, :, 1] = rgb[:, :, 1]  # G
    out[:, :, 2] = rgb[:, :, 0]  # R
    out[:, :, 3] = arr[:, :, 3]  # A
    return out.tobytes()


class LayeredWindow:
    """Wraps a Tk widget's top-level HWND and draws RGBA content onto it
    via UpdateLayeredWindow, giving real per-pixel alpha.

    Takes the Tk widget rather than a pre-fetched HWND, because Tk can
    recreate its HWND when a withdrawn window is deiconified. On each push
    we re-fetch the HWND and re-apply WS_EX_LAYERED.
    """

    def __init__(self, widget):
        self.widget = widget
        self.hwnd = None
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbitmap = None
        self._old_bitmap = None
        self._bits_ptr = None
        self._w = 0
        self._h = 0

    def _refresh_hwnd_and_style(self):
        hwnd = _hwnd_of(self.widget)
        if hwnd != self.hwnd:
            self._teardown_dib()
            self.hwnd = hwnd
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (style & WS_EX_LAYERED):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

    def _setup_dib(self, w, h):
        self._hdc_screen = user32.GetDC(0)
        self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = w * h * 4

        self._bits_ptr = ctypes.c_void_p()
        self._hbitmap = gdi32.CreateDIBSection(
            self._hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(self._bits_ptr), None, 0,
        )
        self._old_bitmap = gdi32.SelectObject(self._hdc_mem, self._hbitmap)
        self._w = w
        self._h = h

    def _teardown_dib(self):
        if self._hdc_mem is None:
            return
        try:
            if self._old_bitmap:
                gdi32.SelectObject(self._hdc_mem, self._old_bitmap)
            if self._hbitmap:
                gdi32.DeleteObject(self._hbitmap)
            gdi32.DeleteDC(self._hdc_mem)
        except Exception:
            pass
        self._hdc_mem = None
        self._hbitmap = None
        self._old_bitmap = None
        self._bits_ptr = None
        self._w = 0
        self._h = 0

    def push(self, pil_rgba):
        self._refresh_hwnd_and_style()
        if self.hwnd is None:
            return

        w, h = pil_rgba.size
        if w != self._w or h != self._h:
            self._teardown_dib()
            self._setup_dib(w, h)

        data = _premultiply_bgra(pil_rgba)
        ctypes.memmove(self._bits_ptr, data, len(data))

        pt_src = POINT(0, 0)
        size = SIZE(w, h)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        user32.UpdateLayeredWindow(
            self.hwnd, self._hdc_screen,
            None,  # pt_dst=None → leave window position alone
            ctypes.byref(size),
            self._hdc_mem, ctypes.byref(pt_src),
            0, ctypes.byref(blend), ULW_ALPHA,
        )

    def destroy(self):
        self._teardown_dib()
        if self._hdc_screen is not None:
            try:
                user32.ReleaseDC(0, self._hdc_screen)
            except Exception:
                pass
            self._hdc_screen = None


# ---------- Layout / style constants ----------

ORB_SIZE = 210
STATUS_HEIGHT = 22
WINDOW_W = ORB_SIZE
WINDOW_H = ORB_SIZE + STATUS_HEIGHT
SUPERSAMPLE = 3

IDLE_COLOR = "#4a5a70"
LISTEN_COLOR = "#7fe6c8"
THINK_COLOR = "#c9a8ff"
SPEAK_COLOR = "#2fd8ff"
STATUS_COLOR = (138, 150, 168, 220)
TRANSCRIPT_COLOR = (170, 186, 210, 255)
REPLY_COLOR = (216, 232, 248, 255)
CAPTION_BG = (22, 27, 34, 240)
CAPTION_OUTLINE = (42, 51, 64, 255)

FPS_MS = 33
CAPTION_MAX_WIDTH = 400
CAPTION_PADDING = 12
CAPTION_RADIUS = 10
CAPTION_FADE_START = 9.0
CAPTION_FADE_DURATION = 1.5

POSITION_FILE = Path(__file__).resolve().parent / "orb_position.json"

FONT_PATH = "C:/Windows/Fonts/segoeui.ttf"

WHEELS = [
    {"radius": 0.92, "speed": 0.35, "direction": 1,  "squash": 1.00, "ticks": 22, "style": "line"},
    {"radius": 0.78, "speed": 0.55, "direction": -1, "squash": 0.62, "ticks": 16, "style": "dot"},
    {"radius": 0.60, "speed": 0.80, "direction": 1,  "squash": 0.80, "ticks": 18, "style": "line"},
    {"radius": 0.42, "speed": 1.15, "direction": -1, "squash": 0.45, "ticks": 12, "style": "dot"},
]


def _hex_to_rgba(hex_color, alpha=255):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


class AssistantOrb:
    def __init__(self, on_toggle_voice=None):
        self.on_toggle_voice = on_toggle_voice or (lambda: None)

        self.events = queue.Queue()
        self.typed_queue = queue.Queue()
        self.exit_flag = threading.Event()

        self.state = "idle"
        self.status_text = ""
        self.envelope = None
        self.envelope_started_at = 0.0
        self.envelope_duration = 0.0

        self._caption_visible = False
        self._caption_is_reply = False
        self._caption_text = ""
        self._caption_shown_at = 0.0
        self._caption_img_full = None       # cached full-alpha caption image
        self._caption_last_fade = None      # last fade pushed, to skip redundant pushes

        self._drag_start = None
        self._dragged = False
        self._type_box = None

        self._status_font = ImageFont.truetype(FONT_PATH, 12)
        self._caption_font = ImageFont.truetype(FONT_PATH, 14)

        # --- Root (orb) window ---
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.configure(bg="black")

        pos = self._load_position()
        if pos is None:
            sw = self.root.winfo_screenwidth()
            x, y = sw - WINDOW_W - 40, 60
        else:
            x, y = pos
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")
        self.root.update_idletasks()

        self._orb_layered = LayeredWindow(self.root)

        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Button-3>", self._on_right_click)

        # --- Caption window ---
        # NOTE: we never call wm_attributes("-alpha", ...) on this window,
        # because that switches the layered window out of per-pixel-alpha
        # mode and it renders as a raw black Tk rectangle.
        self._caption_win = tk.Toplevel(self.root)
        self._caption_win.overrideredirect(True)
        self._caption_win.wm_attributes("-topmost", True)
        self._caption_win.configure(bg="black")
        self._caption_win.geometry("10x10+-10000+-10000")
        self._caption_win.update_idletasks()
        self._caption_win.withdraw()

        self._caption_layered = LayeredWindow(self._caption_win)
        self._caption_win.bind("<Button-1>", lambda e: self._hide_caption())

        self._tick()

    # ---------- Position persistence ----------

    def _load_position(self):
        try:
            data = json.loads(POSITION_FILE.read_text())
            return int(data["x"]), int(data["y"])
        except Exception:
            return None

    def _save_position(self):
        try:
            POSITION_FILE.write_text(json.dumps({
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
            }))
        except Exception:
            pass

    # ---------- Thread-safe API ----------

    def set_state(self, state, status_text=""):
        self.events.put(("state", (state, status_text)))

    def set_transcript(self, text):
        self.events.put(("transcript", text))

    def set_reply(self, text):
        self.events.put(("reply", text))

    def speak_started(self, envelope, duration):
        self.events.put(("speak_start", (envelope, duration)))

    def speak_ended(self):
        self.events.put(("speak_end", None))

    # ---------- Event drain ----------

    def _drain_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                state, status = payload
                self.state = state
                self.status_text = status or ""
                if state != "speaking":
                    self.envelope = None
            elif kind == "speak_start":
                self.state = "speaking"
                self.envelope, self.envelope_duration = payload
                self.envelope_started_at = time.time()
            elif kind == "speak_end":
                if self.state == "speaking":
                    self.state = "idle"
                self.envelope = None
            elif kind == "transcript":
                self._show_caption(payload, is_reply=False)
            elif kind == "reply":
                self._show_caption(payload, is_reply=True)

    # ---------- Caption ----------

    def _wrap_text(self, text, max_width, font):
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            w = font.getbbox(test)[2] - font.getbbox(test)[0]
            if w <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    def _show_caption(self, text, is_reply=False):
        if not text or not text.strip():
            return
        self._caption_text = text
        self._caption_is_reply = is_reply
        self._caption_shown_at = time.time()
        self._caption_visible = True
        self._caption_last_fade = None  # force a fresh push for this caption

        # Render and cache the caption at full alpha. The fade step later
        # scales the alpha channel of this cached image rather than
        # touching any Tk window attribute.
        self._caption_img_full = self._render_caption_image()
        w, h = self._caption_img_full.size

        # Order matters: position via Tk first (safe on a hidden window),
        # then push the layered content while still hidden, then show.
        # Pushing after deiconify would let Tk reset WS_EX_LAYERED first,
        # causing a one-frame flash of the raw black Tk window.
        self._position_caption(w, h)
        self._caption_layered.push(self._caption_img_full)
        self._caption_win.deiconify()
        self._caption_win.lift()

    def _hide_caption(self):
        self._caption_visible = False
        self._caption_img_full = None
        self._caption_last_fade = None
        try:
            self._caption_win.withdraw()
        except Exception:
            pass

    def _render_caption_image(self):
        s = SUPERSAMPLE
        color = REPLY_COLOR if self._caption_is_reply else TRANSCRIPT_COLOR
        lines = self._wrap_text(self._caption_text, CAPTION_MAX_WIDTH, self._caption_font)

        draw_font = ImageFont.truetype(FONT_PATH, 14 * s)

        sample_bbox = self._caption_font.getbbox("Ay")
        line_height = sample_bbox[3] - sample_bbox[1] + 4

        text_w = max(
            (self._caption_font.getbbox(l)[2] for l in lines),
            default=0,
        )
        w = max(120, text_w + CAPTION_PADDING * 2)
        h = len(lines) * line_height + CAPTION_PADDING * 2

        img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")
        draw.rounded_rectangle(
            (0, 0, w * s - 1, h * s - 1),
            radius=CAPTION_RADIUS * s,
            fill=CAPTION_BG,
            outline=CAPTION_OUTLINE,
            width=s,
        )
        y = CAPTION_PADDING * s
        for line in lines:
            draw.text(
                (CAPTION_PADDING * s, y), line,
                fill=color, font=draw_font,
            )
            y += line_height * s

        return img.resize((w, h), Image.LANCZOS)

    def _push_caption_with_fade(self, fade):
        """Re-push the caption image with its alpha channel scaled by
        `fade` (0..1). This is how we fade — no Tk window attributes."""
        if self._caption_img_full is None:
            return
        if fade >= 0.999:
            img = self._caption_img_full
        else:
            r, g, b, a = self._caption_img_full.split()
            a = a.point(lambda v: int(v * fade))
            img = Image.merge("RGBA", (r, g, b, a))
        self._caption_layered.push(img)

    def _position_caption(self, w, h):
        orb_x = self.root.winfo_x()
        orb_y = self.root.winfo_y()
        screen_h = self.root.winfo_screenheight()
        screen_w = self.root.winfo_screenwidth()

        y = orb_y + WINDOW_H + 8
        if y + h > screen_h - 20:
            y = orb_y - h - 8
            if y < 20:
                y = 20

        x = orb_x
        if x + w > screen_w - 20:
            x = screen_w - w - 20
        if x < 20:
            x = 20

        self._caption_win.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- Interaction ----------

    def _on_press(self, event):
        self._drag_start = (event.x_root, event.y_root)
        self._dragged = False

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        if abs(dx) > 6 or abs(dy) > 6:
            self._dragged = True
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")
        self._drag_start = (event.x_root, event.y_root)
        # Keep the caption under the orb while dragging
        if self._caption_visible and self._caption_img_full is not None:
            cw, ch = self._caption_img_full.size
            self._position_caption(cw, ch)

    def _on_release(self, event):
        if not self._dragged and event.y < ORB_SIZE:
            self._toggle_type_box()
        elif self._dragged:
            self._save_position()
        self._drag_start = None

    def _on_right_click(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Toggle voice output", command=self.on_toggle_voice)
        menu.add_command(label="Hide caption", command=self._hide_caption)
        menu.add_separator()
        menu.add_command(label="Exit", command=self._exit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _exit(self):
        self._save_position()
        self.exit_flag.set()
        try:
            self._orb_layered.destroy()
            self._caption_layered.destroy()
        except Exception:
            pass
        self.root.destroy()

    def _toggle_type_box(self):
        if self._type_box is not None:
            self._hide_type_box()
            return
        box = tk.Toplevel(self.root)
        box.overrideredirect(True)
        box.wm_attributes("-topmost", True)
        box.wm_attributes("-alpha", 0.94)  # OK here — this box isn't layered
        box.configure(bg="#161b22")
        x = self.root.winfo_x()
        y = self.root.winfo_y() + WINDOW_H + 8
        w = max(WINDOW_W, 320)
        box.geometry(f"{w}x40+{x}+{y}")

        entry = tk.Entry(
            box, font=("Segoe UI", 11), bg="#161b22", fg="#e8f6ff",
            insertbackground="#e8f6ff", relief="flat",
        )
        entry.pack(fill="both", expand=True, padx=10, pady=8)
        entry.focus_set()

        def submit(_e=None):
            text = entry.get().strip()
            self._hide_type_box()
            if text:
                self.typed_queue.put(text)

        def cancel(_e=None):
            self._hide_type_box()

        entry.bind("<Return>", submit)
        entry.bind("<Escape>", cancel)
        box.bind("<FocusOut>", lambda e: self._hide_type_box())
        self._type_box = box

    def _hide_type_box(self):
        if self._type_box is not None:
            try:
                self._type_box.destroy()
            except Exception:
                pass
            self._type_box = None

    # ---------- Rendering ----------

    def _current_amplitude(self):
        if self.state != "speaking":
            t = time.time()
            return 0.15 + 0.08 * math.sin(t * 1.3)
        if self.envelope:
            elapsed = time.time() - self.envelope_started_at
            idx = int(elapsed / max(self.envelope_duration, 0.01) * len(self.envelope))
            if 0 <= idx < len(self.envelope):
                return self.envelope[idx]
            return 0.2
        t = time.time()
        return 0.35 + 0.35 * abs(math.sin(t * 6.0)) * abs(math.sin(t * 2.3))

    def _color_for_state(self):
        return {
            "speaking": SPEAK_COLOR,
            "listening": LISTEN_COLOR,
            "thinking": THINK_COLOR,
        }.get(self.state, IDLE_COLOR)

    def _draw_wheel(self, draw, cx, cy, now, color, wheel, speed_mult, s):
        rx = (ORB_SIZE / 2 - 12) * wheel["radius"] * s
        ry = rx * wheel["squash"]
        angle_offset = now * wheel["speed"] * wheel["direction"] * speed_mult
        draw.ellipse(
            (cx - rx, cy - ry, cx + rx, cy + ry),
            outline=color, width=s,
        )
        n = wheel["ticks"]
        for i in range(n):
            angle = (i / n) * 2 * math.pi + angle_offset
            x = cx + rx * math.cos(angle)
            y = cy + ry * math.sin(angle)
            if wheel["style"] == "dot":
                r = 2.4 * s
                draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
            else:
                inset = 0.88
                x2 = cx + rx * inset * math.cos(angle)
                y2 = cy + ry * inset * math.sin(angle)
                draw.line((x, y, x2, y2), fill=color, width=s)

    def _render_orb_image(self):
        s = SUPERSAMPLE
        w = h = ORB_SIZE * s
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")

        cx = cy = w / 2
        click_r = (ORB_SIZE / 2 - 2) * s
        draw.ellipse(
            (cx - click_r, cy - click_r, cx + click_r, cy + click_r),
            fill=(0, 0, 0, 1),
        )

        amp = self._current_amplitude()
        color_hex = self._color_for_state()
        color = _hex_to_rgba(color_hex)
        now = time.time()
        speed_mult = 0.5 if self.state == "idle" else 1.8

        for wheel in WHEELS:
            self._draw_wheel(draw, cx, cy, now, color, wheel, speed_mult, s)

        core_r = (ORB_SIZE / 2 - 48) * (0.5 + 0.5 * amp) * s
        draw.ellipse(
            (cx - core_r, cy - core_r, cx + core_r, cy + core_r),
            fill=color,
        )
        inner_r = core_r * 0.5
        base_rgb = color[:3]
        light = tuple(min(255, int(c + (255 - c) * 0.6)) for c in base_rgb) + (255,)
        draw.ellipse(
            (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
            fill=light,
        )

        orb_img = img.resize((ORB_SIZE, ORB_SIZE), Image.LANCZOS)

        full = Image.new("RGBA", (WINDOW_W, WINDOW_H), (0, 0, 0, 0))
        full.paste(orb_img, (0, 0), orb_img)

        status = self.status_text or {
            "speaking": "Speaking",
            "listening": "Listening",
            "thinking": "Thinking",
        }.get(self.state, "Idle")

        draw_full = ImageDraw.Draw(full, "RGBA")
        bbox = self._status_font.getbbox(status)
        text_w = bbox[2] - bbox[0]
        draw_full.text(
            ((ORB_SIZE - text_w) / 2, ORB_SIZE + 4),
            status, fill=STATUS_COLOR, font=self._status_font,
        )
        return full

    def _tick(self):
        self._drain_events()

        # Caption fade — done in the layered image, never via Tk attributes.
        # Skip redundant pushes: for the first CAPTION_FADE_START seconds
        # the fade is a constant 1.0, and re-pushing the same image 30 times
        # a second was burning GDI bandwidth on a second layered window.
        if self._caption_visible:
            elapsed = time.time() - self._caption_shown_at
            if elapsed >= CAPTION_FADE_START + CAPTION_FADE_DURATION:
                self._hide_caption()
            else:
                if elapsed <= CAPTION_FADE_START:
                    fade = 1.0
                else:
                    fade = max(
                        0.0,
                        1.0 - (elapsed - CAPTION_FADE_START) / CAPTION_FADE_DURATION,
                    )
                if fade != self._caption_last_fade:
                    self._push_caption_with_fade(fade)
                    self._caption_last_fade = fade

        img = self._render_orb_image()
        self._orb_layered.push(img)

        self.root.after(FPS_MS, self._tick)

    def run(self):
        self.root.mainloop()