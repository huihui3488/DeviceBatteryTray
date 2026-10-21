"""Windows tray and floating widget for two paired devices."""

from __future__ import annotations

import asyncio
import csv
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import time
import winreg
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from logging.handlers import RotatingFileHandler

import pystray
import win32api
from PIL import Image, ImageDraw, ImageFont
from winrt.windows.devices.bluetooth.advertisement import BluetoothLEAdvertisementWatcher

from battery_core import (AppleAdvertisements, DeviceState, parse_apple_proximity,
                          read_beats, read_mouse, windows_beats_battery)


APP_NAME = "DeviceBatteryTray"
CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "config.json"
LOG_PATH = CONFIG_PATH.with_name("diagnostic.log")
HISTORY_DIR = CONFIG_PATH.parent
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOG = logging.getLogger("battery_tray")
OVERLAY_WIDTH = 260
OVERLAY_HEIGHT = 36
TRANSPARENT = "#010203"


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=256_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)


def claim_single_instance() -> int | None:
    name = "Local\\DeviceBatteryTray_F8665A4852DB_C2559A4531ED"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle or kernel32.GetLastError() == 183:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


def enable_dpi_awareness() -> None:
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def position_visible(x: int, y: int, width: int, height: int) -> bool:
    for _handle, _dc, (left, top, right, bottom) in win32api.EnumDisplayMonitors():
        visible_width = min(x + width, right) - max(x, left)
        visible_height = min(y + height, bottom) - max(y, top)
        if visible_width >= min(40, width) and visible_height >= min(24, height):
            return True
    return False


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0] == startup_command()
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


@lru_cache(maxsize=32)
def battery_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(r"C:\Windows\Fonts\ARIALNB.TTF", size)
    except OSError:
        return ImageFont.load_default()


def icon_image(kind: str, state: DeviceState) -> Image.Image:
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    accent = ("#80e8ee" if kind == "beats" else "#ffd17b") if state.connected else "#a8b0b8"
    # The body occupies roughly 90% of the 64px tray canvas so the system's
    # small notification-area rendition remains legible.
    draw.rounded_rectangle((1, 1, 60, 62), radius=8, fill="#172027", outline=accent, width=4)
    draw.rectangle((60, 24, 63, 40), fill=accent)
    draw.ellipse((2, 3, 16, 17), fill="#45e486" if state.connected else "#858e97",
                 outline="#172027", width=2)
    if state.connected and state.level is not None:
        bar_end = 6 + round(50 * state.level / 100)
        draw.rectangle((6, 54, bar_end, 59), fill="#ff7373" if state.level <= 20 else accent)

    number = format_level(state.level) if state.connected else "--"
    for size in range(40, 17, -1):
        font = battery_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), number, font=font)
        if right - left <= 49 and bottom - top <= 34:
            break
    x = round(31 - (right - left) / 2 - left)
    y = round(32 - (bottom - top) / 2 - top)
    draw.text((x, y), number, font=font, fill="#ffffff" if state.connected else "#ced3d7",
              stroke_width=1, stroke_fill="#172027")
    return image


def append_history(path: Path, states: dict[str, DeviceState], when: datetime) -> None:
    fields = ("time", "device", "connected", "level", "left", "right", "case", "charging", "source")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for kind in ("beats", "mouse"):
            state = states[kind]
            writer.writerow({"time": when.isoformat(timespec="seconds"), "device": kind,
                             "connected": int(state.connected), "level": state.level,
                             "left": state.left, "right": state.right, "case": state.case,
                             "charging": int(state.charging), "source": state.source})


def new_history_path(directory: Path, when: datetime | None = None) -> Path:
    """Create a fresh, header-only CSV for this application session."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"battery_history_{stamp}.csv"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"battery_history_{stamp}_{suffix}.csv"
        suffix += 1
    fields = ("time", "device", "connected", "level", "left", "right", "case", "charging", "source")
    with candidate.open("x", newline="", encoding="utf-8-sig") as stream:
        csv.DictWriter(stream, fieldnames=fields).writeheader()
    return candidate


def tooltip(name: str, state: DeviceState) -> str:
    if not state.connected:
        return f"{name}: 未连接"
    level = "--" if state.level is None else f"{state.level}%"
    text = f"{name}: {level} ({state.source})"
    if state.left is not None or state.right is not None or state.case is not None:
        chunks = [f"左 {format_level(state.left)}", f"右 {format_level(state.right)}"]
        if state.case is not None:
            chunks.append(f"盒 {format_level(state.case)}")
        text += " | " + " / ".join(chunks)
    return text[:127]


def format_level(value: int | None) -> str:
    return "--" if value is None else f"{value}%"


class BatteryApp:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("设备电量")
        self.config = load_config()
        self.history_path = new_history_path(HISTORY_DIR)
        self.commands: queue.Queue[str] = queue.Queue()
        self.states = {"beats": DeviceState(), "mouse": DeviceState()}
        self.stop_event = threading.Event()
        self.refresh_event: asyncio.Event | None = None
        self.ble_loop: asyncio.AbstractEventLoop | None = None
        self.visible = False
        self._drag_start: tuple[int, int] | None = None
        self._make_window()
        self.icons = self._make_icons()

    def _make_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-toolwindow", True)
        win.attributes("-transparentcolor", TRANSPARENT)
        win.configure(bg=TRANSPARENT)
        win.resizable(False, False)
        win.bind("<Escape>", lambda _e: self.hide_window())
        win.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.window = win
        self.canvas = tk.Canvas(win, width=OVERLAY_WIDTH, height=OVERLAY_HEIGHT,
                                bg=TRANSPARENT, highlightthickness=0, bd=0, cursor="fleur")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", self._show_window_menu)
        self.window_menu = tk.Menu(win, tearoff=False)
        self.window_menu.add_command(label="隐藏悬浮窗", command=self.hide_window)

        width, height = OVERLAY_WIDTH, OVERLAY_HEIGHT
        default_x = max(0, win.winfo_screenwidth() - width - 28)
        x = self.config.get("x", default_x)
        y = self.config.get("y", 80)
        if not isinstance(x, int) or not isinstance(y, int) or not position_visible(x, y, width, height):
            x, y = default_x, 80
        win.geometry(f"{width}x{height}+{x}+{y}")
        self._draw_overlay()

    def _make_icons(self) -> dict[str, pystray.Icon]:
        icons = {}
        for kind, name in (("beats", "Beats Fit Pro"), ("mouse", "迈从 A7 V3 Ultra+")):
            menu = pystray.Menu(
                pystray.MenuItem("显示/隐藏小窗口", lambda _icon, _item: self.commands.put("toggle"), default=True),
                pystray.MenuItem("立即刷新", lambda _icon, _item: self.commands.put("refresh")),
                pystray.MenuItem("开机启动", lambda _icon, _item: self.commands.put("startup"),
                                 checked=lambda _item: startup_enabled()),
                pystray.MenuItem("打开电量记录", lambda _icon, _item: self.commands.put("history")),
                pystray.MenuItem("打开诊断日志", lambda _icon, _item: self.commands.put("log")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda _icon, _item: self.commands.put("quit")),
            )
            icons[kind] = pystray.Icon(f"{APP_NAME}_{kind}", icon_image(kind, DeviceState()),
                                       tooltip(name, DeviceState()), menu)
        return icons

    def _drag_begin(self, event: tk.Event) -> None:
        self._drag_start = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_start:
            self.window.geometry(f"+{event.x_root - self._drag_start[0]}+{event.y_root - self._drag_start[1]}")

    def _drag_end(self, _event: tk.Event) -> None:
        self._drag_start = None
        self._save_config()

    def _show_window_menu(self, event: tk.Event) -> None:
        try:
            self.window_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.window_menu.grab_release()

    def _save_config(self) -> None:
        self.config.update(x=self.window.winfo_x(), y=self.window.winfo_y(), visible=self.visible)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        temp.replace(CONFIG_PATH)

    def show_window(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.visible = True
        self._save_config()

    def hide_window(self) -> None:
        self.window.withdraw()
        self.visible = False
        self._save_config()

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command == "toggle":
                self.hide_window() if self.visible else self.show_window()
            elif command == "refresh":
                if self.ble_loop and self.refresh_event:
                    self.ble_loop.call_soon_threadsafe(self.refresh_event.set)
            elif command == "startup":
                try:
                    set_startup(not startup_enabled())
                except OSError:
                    pass
                for icon in self.icons.values():
                    icon.update_menu()
            elif command == "log":
                try:
                    LOG_PATH.touch(exist_ok=True)
                    os.startfile(LOG_PATH)
                except OSError:
                    LOG.exception("Could not open diagnostic log")
            elif command == "history":
                try:
                    os.startfile(HISTORY_DIR)
                except OSError:
                    LOG.exception("Could not open battery history folder")
            elif command == "quit":
                self.stop_event.set()
                if self.ble_loop and self.refresh_event:
                    self.ble_loop.call_soon_threadsafe(self.refresh_event.set)
                for icon in self.icons.values():
                    icon.stop()
                self.root.destroy()
                return
            elif isinstance(command, tuple) and command[0] == "state":
                _, kind, state = command
                if state != self.states[kind]:
                    self.states[kind] = state
                    self._render(kind)
        self.root.after(200, self._drain_commands)

    def _render(self, kind: str) -> None:
        state = self.states[kind]
        name = "Beats Fit Pro" if kind == "beats" else "迈从 A7 V3 Ultra+"
        icon = self.icons[kind]
        icon.icon = icon_image(kind, state)
        icon.title = tooltip(name, state)
        self._draw_overlay()

    def _draw_overlay(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        for kind, icon_x in (("beats", 2), ("mouse", 174)):
            state = self.states[kind]
            color = ("#82e0e8" if kind == "beats" else "#e9c374") if state.connected else "#a0a8ae"
            if kind == "beats":
                for stroke, width in (("#172027", 5), (color, 2.5)):
                    canvas.create_arc(icon_x, 5, icon_x + 21, 25, start=0, extent=180,
                                      style="arc", outline=stroke, width=width)
                canvas.create_line(icon_x, 16, icon_x, 26, fill=color, width=4, capstyle="round")
                canvas.create_line(icon_x + 21, 16, icon_x + 21, 26,
                                   fill=color, width=4, capstyle="round")
            else:
                canvas.create_oval(icon_x, 4, icon_x + 20, 31, outline="#172027", width=5)
                canvas.create_oval(icon_x, 4, icon_x + 20, 31, outline=color, width=2.5)
                canvas.create_line(icon_x + 10, 6, icon_x + 10, 14, fill=color, width=2)

        beats = self.states["beats"]
        if beats.connected and (beats.left is not None or beats.right is not None):
            readings = (("L", beats.left, 29), ("R", beats.right, 94))
            for label, value, x in readings:
                self._overlay_text(x, 17, label, value)
        else:
            self._overlay_text(29, 17, "", beats.level if beats.connected else None)
        mouse = self.states["mouse"]
        self._overlay_text(199, 17, "", mouse.level if mouse.connected else None)

    def _overlay_text(self, x: int, y: int, label: str, value: int | None) -> None:
        number = f"{label}{format_level(value)}"
        foreground = "#ff7778" if value is not None and value <= 20 else (
            "#f7f9fa" if value is not None else "#b3bac0"
        )
        for offset_x, offset_y, color in ((1, 1, "#172027"), (0, 0, foreground)):
            self.canvas.create_text(x + offset_x, y + offset_y, text=number, anchor="w",
                                    font=("Arial", 13, "bold"), fill=color)

    async def _monitor(self) -> None:
        ads = AppleAdvertisements()
        pending_ads: queue.SimpleQueue[tuple[str, int, bytes]] = queue.SimpleQueue()

        def on_advertisement(_watcher, event) -> None:
            for item in event.advertisement.get_manufacturer_data_by_company_id(0x004C):
                data = bytes(item.data)
                if parse_apple_proximity(data) is not None:
                    pending_ads.put((f"{event.bluetooth_address:012X}",
                                     event.raw_signal_strength_in_dbm, data))

        watcher = None
        token = None
        try:
            watcher = BluetoothLEAdvertisementWatcher()
            token = watcher.add_received(on_advertisement)
            watcher.start()
        except Exception:
            LOG.exception("Bluetooth advertisement scan failed")
            if watcher and token is not None:
                watcher.remove_received(token)
            watcher = None
        self.ble_loop = asyncio.get_running_loop()
        self.refresh_event = asyncio.Event()
        last_logged_minute = None
        try:
            while not self.stop_event.is_set():
                while not pending_ads.empty():
                    address, rssi, data = pending_ads.get_nowait()
                    ads.record(address, rssi, data)
                snapshot = {}
                for kind, read in (("mouse", read_mouse),
                                   ("beats", lambda: read_beats(ads, windows_beats_battery))):
                    try:
                        state = await read()
                    except Exception:
                        LOG.exception("%s status read failed", kind)
                        state = DeviceState()
                    if kind == "beats" and not state.connected:
                        ads.clear()
                    snapshot[kind] = state
                    self.commands.put(("state", kind, state))
                minute = int(time.time() // 60)
                if minute != last_logged_minute:
                    try:
                        append_history(self.history_path, snapshot, datetime.now().astimezone())
                        last_logged_minute = minute
                    except OSError:
                        LOG.exception("Could not append battery history")
                try:
                    until_minute = 60 - time.time() % 60
                    await asyncio.wait_for(self.refresh_event.wait(), timeout=min(10, until_minute))
                    self.refresh_event.clear()
                except asyncio.TimeoutError:
                    pass
        finally:
            if watcher:
                try:
                    watcher.stop()
                finally:
                    watcher.remove_received(token)

    def run(self) -> None:
        for icon in self.icons.values():
            icon.run_detached()
        threading.Thread(target=lambda: asyncio.run(self._monitor()), daemon=True).start()
        if self.config.get("visible", False):
            self.show_window()
        self.root.after(200, self._drain_commands)
        self.root.mainloop()


if __name__ == "__main__":
    mutex = claim_single_instance()
    if mutex is not None:
        try:
            configure_logging()
            BatteryApp().run()
        finally:
            ctypes.windll.kernel32.CloseHandle(mutex)
