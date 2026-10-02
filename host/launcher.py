#!/usr/bin/env python3
"""
NU40DK Launcher — open Mac apps with the board's 4 buttons

When the board prints "BTN1" to "BTN4" over serial, this opens apps as mapped in config.json.
With no board, or if you unplug and replug it, the program stays alive and keeps waiting.

Opens the port directly with termios instead of pyserial. This Mac has no pyserial,
and I didn't want to end up with pip blocked the day before a demo.
It's USB CDC, so the baud rate is actually ignored, but 115200 is set by convention.

Run:   python3 ~/nu40-launcher/launcher.py
Quit:  Ctrl-C
"""

import glob
import json
import os
import re
import select
import subprocess
import sys
import termios
import time
import urllib.parse

HERE          = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(HERE, "config.json")
FAREWELL_PAGE = os.path.join(HERE, "farewell.html")

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# If the same button comes in again within this time, ignore it. The firmware debounces,
# but it can't stop a shaky finger from pressing twice. Missing one press is
# better than an app opening twice.
COOLDOWN_SEC = 0.8

# How often to look again when the board isn't found or got disconnected
RECONNECT_SEC = 1.0

# Gap between apps when one mode opens several
STAGGER_SEC = 0.35

# How long to wait for an app to quit. After that, give up and move to the next app
QUIT_TIMEOUT_SEC = 6.0

# How long any other one-line AppleScript may take to answer
OSA_TIMEOUT_SEC = 8.0

BTN_RE = re.compile(r"^BTN([1-4])$")

DIM    = "\033[2m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
RESET  = "\033[0m"


def log(msg, color=""):
    stamp = time.strftime("%H:%M:%S")
    print(f"{DIM}{stamp}{RESET}  {color}{msg}{RESET}", flush=True)


def load_config(path=None):
    path = path or CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    buttons = {}
    for key, entry in cfg.get("buttons", {}).items():
        if key in ("1", "2", "3", "4"):
            buttons[key] = entry

    return cfg.get("port"), buttons


def find_port(configured):
    """If the config pins a port, use only that. Otherwise scan usbmodem ports."""
    if configured:
        return configured if os.path.exists(configured) else None

    # With several boards plugged in, take the first by name. Pin one with "port" in config.json
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def open_port(path):
    """Open the port in raw mode. On failure the OSError propagates as-is."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)

        # Turn off newline translation, echo and signal handling. Take bytes exactly as they come
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        ispeed = ospeed = termios.B115200
        cc = list(cc)
        cc[termios.VMIN]  = 0
        cc[termios.VTIME] = 0

        termios.tcsetattr(
            fd, termios.TCSANOW,
            [iflag, oflag, cflag, lflag, ispeed, ospeed, cc],
        )
        # Drop stale output that piled up while plugged in. Otherwise a pile of apps opens at startup
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        os.close(fd)
        raise

    return fd


def targets_of(entry):
    """Flatten an entry into a list of things to open. Also accepts the old single app/url form."""
    items = entry.get("open")
    if items is not None:
        return items
    if entry.get("app"):
        return [{"app": entry["app"]}]
    if entry.get("url"):
        return [{"url": entry["url"]}]
    return []


def osa(script, timeout=OSA_TIMEOUT_SEC):
    """Run a snippet of AppleScript."""
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=timeout)


def applescript_str(text):
    """Wrap text so it can go inside an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def app_is_running(name):
    try:
        result = osa(f'application "{name}" is running')
    except subprocess.TimeoutExpired:
        # No answer: treat it as not running and open it normally. Stalling here
        # would freeze the whole launcher on one button
        return False
    return (result.stdout or "").strip() == "true"


def open_new_window(item):
    """Open a 'new window' in an app that's already running.

    This is for filming. open -a only brings a running app to the front,
    so on screen it looks like nothing happened.
    """
    app = item["app"]

    # If it isn't running, just open it. A new window appears anyway
    if not app_is_running(app):
        return False

    if app == "Terminal":
        # Terminal can make a new window without ⌘N. Windows already running are left alone.
        # Give it a command and it runs in that window — nice on camera
        script = ('tell application "Terminal"\n'
                  '  activate\n'
                  f'  do script {applescript_str(item.get("command", ""))}\n'
                  'end tell')
    else:
        # Everything else: bring the app to the front, then send ⌘N.
        # The delay waits until the app is actually in front
        script = (f'tell application "{app}" to activate\n'
                  'delay 0.4\n'
                  'tell application "System Events" to keystroke "n" using command down')

    try:
        result = osa(script)
    except subprocess.TimeoutExpired:
        log(f"  ↳ '{app}' new window timed out", YELLOW)
        return True

    if result.returncode == 0:
        log(f"  ↳ {app} new window", GREEN)
    else:
        detail = (result.stderr or "").strip()[:100]
        log(f"  ↳ '{app}' new window failed — {detail}", YELLOW)
        if "1002" in detail or "assistive" in detail.lower():
            # ⌘N presses a key on your behalf, so it needs Accessibility permission.
            # Grant it to the app that runs the launcher (usually Terminal)
            log("     Turn on Terminal in System Settings → Privacy & Security → "
                "Accessibility", YELLOW)
            log("     For a new window without that permission, use \"file\" "
                "instead of \"new\" in config.json", YELLOW)
    return True


def open_item(item):
    """One app or one URL. If the app is already running, bring it to the front."""
    if item.get("app") and item.get("new") and open_new_window(item):
        return

    if item.get("app") and item.get("file"):
        # With a file, the app opens a new window on that file. Unlike ⌘N
        # it needs no Accessibility permission, and the content shows on screen
        path = os.path.expanduser(item["file"])
        cmd, label = ["open", "-a", item["app"], path], f'{item["app"]} ← {os.path.basename(path)}'
    elif item.get("app"):
        cmd, label = ["open", "-a", item["app"]], item["app"]
    elif item.get("url"):
        cmd, label = ["open", item["url"]], item["url"]
    else:
        log(f"  ↳ Nothing to open: {item}", YELLOW)
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"  ↳ Opened {label}", GREEN)
    else:
        detail = (result.stderr or "").strip() or f"open exit code {result.returncode}"
        log(f"  ↳ Couldn't open '{label}' — {detail}", YELLOW)


def close_app(name):
    """Quit an app normally. Check that it really quit and report honestly."""
    # Why check "is running" first: sending quit alone launches an app that wasn't running.
    # An app starting up when you press Clock out is the worst look.
    #
    # Also, a successful quit doesn't mean the app is gone. If a save dialog
    # appears, it stays alive. Only report after checking it really disappeared, so the log can be trusted
    script = (f'if application "{name}" is running then\n'
              f'  quit application "{name}"\n'
              '  repeat 12 times\n'
              f'    if not (application "{name}" is running) then return "quit"\n'
              '    delay 0.15\n'
              '  end repeat\n'
              '  return "still"\n'
              'else\n'
              '  return "absent"\n'
              'end if')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True,
                                timeout=QUIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        log(f"  ↳ '{name}' not responding — skipping", YELLOW)
        return

    verdict = (result.stdout or "").strip()
    if verdict == "quit":
        log(f"  ↳ Closed {name}", DIM)
    elif verdict == "absent":
        log(f"  ↳ {name} wasn't running", DIM)
    elif verdict == "still":
        # With an unsaved window, the app stays alive while it asks
        log(f"  ↳ '{name}' didn't quit — probably asking to save", YELLOW)
    else:
        detail = (result.stderr or "").strip()[:80] or f"exit code {result.returncode}"
        log(f"  ↳ Couldn't close '{name}' — {detail}", YELLOW)


def show_farewell(message, sub=None):
    """Show the confetti screen in a Chrome app-mode window."""
    # --app opens a window with no tabs and no address bar. The page closes itself
    # when done, so no window is left behind after Clock out.
    # It was first built with tkinter and dropped — this Mac's Tk couldn't draw the canvas
    # and left only a white screen (stuck in root.update())
    if not os.path.exists(FAREWELL_PAGE):
        log("farewell.html not found — skipping confetti", YELLOW)
        return
    if not os.path.exists(CHROME_BIN):
        log("Chrome not found — skipping confetti", YELLOW)
        return

    url = ("file://" + urllib.parse.quote(FAREWELL_PAGE)
           + "?msg=" + urllib.parse.quote(message))
    if sub is not None:
        url += "&sub=" + urllib.parse.quote(sub)
    try:
        subprocess.Popen([CHROME_BIN, f"--app={url}", "--start-fullscreen"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("  ↳ Confetti", GREEN)
    except OSError as e:
        log(f"  ↳ Couldn't show confetti — {e}", YELLOW)


def fire(entry):
    """Do everything one button stands for."""
    name = entry.get("name", "?")
    log(f"{BOLD}{name}{RESET}", CYAN)

    # Closing comes first. For Clock out, the confetti has to be left alone at the end
    for app in entry.get("close", []):
        close_app(app)

    items = targets_of(entry)
    for i, item in enumerate(items):
        # Opened all at once, the windows fight to come to the front, and it looks messy
        if i:
            time.sleep(STAGGER_SEC)
        open_item(item)

    message = entry.get("farewell")
    if message:
        show_farewell(message, entry.get("farewell_sub"))

    if not items and not entry.get("close") and not message:
        log(f"Nothing to do for '{name}' — check config.json", YELLOW)


def run(config_path=None):
    config_path = config_path or CONFIG_PATH
    port_cfg, buttons = load_config(config_path)

    print()
    # Always show which config it started with. With several config files,
    # half of "why didn't it change" is looking at the wrong file
    log(f"NU40DK Launcher started {DIM}({os.path.basename(config_path)}){RESET}", CYAN)
    for key in sorted(buttons):
        entry = buttons[key]
        parts = [item.get("app") or item.get("url") or "?" for item in targets_of(entry)]
        if entry.get("close"):
            parts.append(f"close {len(entry['close'])}")
        if entry.get("farewell"):
            parts.append("confetti")
        log(f"  Button {key} → {entry.get('name', '?')} "
            f"{DIM}({', '.join(parts) or '-'}){RESET}")
    log("Press Ctrl-C to quit", DIM)

    # Confetti runs as a separate process, so failures are silent. Finding out
    # after pressing the button is too late, so check at startup
    if any(b.get("farewell") for b in buttons.values()):
        for path, what in ((FAREWELL_PAGE, "farewell.html"), (CHROME_BIN, "Chrome")):
            if not os.path.exists(path):
                log(f"{what} not found — confetti won't show {DIM}{path}{RESET}", YELLOW)
    print()

    fd = None
    path = None
    buf = b""
    last_fire = {}
    warned_missing = False    # latch so "board not found" isn't printed every second
    warned_wrong_fw = False   # warn about other firmware only once too

    try:
        while True:
            # --- connect ---
            if fd is None:
                path = find_port(port_cfg)
                if path is None:
                    if not warned_missing:
                        log("Looking for the board… (check the USB cable)", YELLOW)
                        warned_missing = True
                    time.sleep(RECONNECT_SEC)
                    continue

                try:
                    fd = open_port(path)
                except OSError as e:
                    if not warned_missing:
                        log(f"Couldn't open {path} — {e.strerror}. "
                            f"Close the Arduino Serial Monitor if it's open", YELLOW)
                        warned_missing = True
                    time.sleep(RECONNECT_SEC)
                    continue

                buf = b""
                warned_missing = False
                warned_wrong_fw = False
                log(f"Board connected {DIM}{path}{RESET}", GREEN)

            # --- read ---
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    # Unplugging the board may just go quiet without an error.
                    # Check directly whether the device node is gone
                    if not os.path.exists(path):
                        raise OSError(f"{path} disappeared")
                    continue

                chunk = os.read(fd, 4096)
                if not chunk:
                    raise OSError(f"{path} disconnected")
            except OSError as e:
                log(f"Board disconnected — searching again {DIM}({e}){RESET}", YELLOW)
                os.close(fd)
                fd = None
                time.sleep(RECONNECT_SEC)
                continue

            buf += chunk

            # --- handle line by line ---
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue

                match = BTN_RE.match(line)
                if match:
                    key = match.group(1)
                    entry = buttons.get(key)
                    if entry is None:
                        log(f"No mapping for button {key}", YELLOW)
                        continue

                    now = time.monotonic()
                    if now - last_fire.get(key, 0.0) < COOLDOWN_SEC:
                        continue
                    last_fire[key] = now

                    fire(entry)

                    # Restart the cooldown from when the work finished. In modes that take
                    # over a second to open apps, stamping only at the start wasn't
                    # enough — signals that arrived meanwhile slipped past the cooldown
                    last_fire[key] = time.monotonic()

                    # Drop input that piled up while opening. It's an impatient extra press
                    # or contact bounce, not a request for the next mode
                    termios.tcflush(fd, termios.TCIFLUSH)
                    buf = b""

                elif line == "READY":
                    log("Board ready", GREEN)

                elif not warned_wrong_fw:
                    # Lands here when firmware other than the launcher is on the board.
                    # Saves time spent pressing buttons without knowing why
                    log(f"Got output that isn't a button signal: {DIM}{line[:60]}{RESET}", YELLOW)
                    log("Check that the nu40dk_launcher firmware is uploaded", YELLOW)
                    warned_wrong_fw = True

            # Don't leak memory even if firmware keeps sending junk with no newline
            if len(buf) > 8192:
                buf = buf[-1024:]

    except KeyboardInterrupt:
        print()
        log("Quitting", CYAN)
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    # Takes the config file as an argument. Without one, config.json.
    # A bare file name is looked up in this folder — no typing the full path every time
    chosen = sys.argv[1] if len(sys.argv) > 1 else None
    if chosen and not os.path.isabs(chosen):
        chosen = os.path.join(HERE, chosen)
    if chosen and not os.path.exists(chosen):
        log(f"Config file not found: {chosen}", YELLOW)
        sys.exit(1)
    sys.exit(run(chosen))
