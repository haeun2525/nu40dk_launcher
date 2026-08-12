#!/usr/bin/env python3
"""
NU40DK Launcher — 보드 버튼 4개로 Mac 앱 열기

보드가 시리얼로 "BTN1" ~ "BTN4"를 뱉으면 config.json의 매핑대로 앱을 연다.
보드가 없어도, 뽑았다 꽂아도, 프로그램은 계속 살아서 기다린다.

pyserial 없이 termios로 직접 포트를 연다. 이 맥에 pyserial이 없고,
데모 하루 전에 pip이 막혀 있는 상황을 만들고 싶지 않아서다.
USB CDC라 보드레이트는 사실 아무 값이나 무시되지만 관례대로 115200을 건다.

실행:  python3 ~/nu40-launcher/launcher.py
종료:  Ctrl-C
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

HERE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# 같은 버튼이 이 시간 안에 다시 들어오면 무시한다. 펌웨어가 디바운스를 하지만
# 손가락이 떨려 두 번 눌리는 것까지는 막지 못한다. 앱이 두 번 뜨는 것보다
# 한 번 씹히는 게 낫다.
COOLDOWN_SEC = 0.8

# 보드를 못 찾거나 끊겼을 때 다시 찾아보는 간격
RECONNECT_SEC = 1.0

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


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    buttons = {}
    for key, entry in cfg.get("buttons", {}).items():
        if key in ("1", "2", "3", "4"):
            buttons[key] = entry

    return cfg.get("port"), buttons


def find_port(configured):
    """설정에 포트가 박혀 있으면 그것만, 아니면 usbmodem을 훑는다."""
    if configured:
        return configured if os.path.exists(configured) else None

    # 보드가 여러 개 꽂혀 있으면 이름순으로 첫 번째. config.json의 port로 고정할 수 있다
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def open_port(path):
    """포트를 raw 모드로 연다. 실패하면 OSError가 그대로 올라간다."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)

        # 줄바꿈 변환, 에코, 시그널 해석을 전부 끈다. 들어온 바이트를 그대로 받는다
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
        # 꽂아둔 사이 쌓인 묵은 출력은 버린다. 안 그러면 켜자마자 앱이 우르르 뜬다
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        os.close(fd)
        raise

    return fd


def launch(entry):
    """config.json 한 항목을 실행한다. 이미 떠 있는 앱이면 앞으로 끌어온다."""
    name = entry.get("name", "?")

    if entry.get("app"):
        cmd = ["open", "-a", entry["app"]]
    elif entry.get("url"):
        cmd = ["open", entry["url"]]
    else:
        log(f"'{name}'에 app도 url도 없습니다 — config.json 확인", YELLOW)
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"{BOLD}{name}{RESET} 열림", GREEN)
    else:
        detail = (result.stderr or "").strip() or f"open 종료코드 {result.returncode}"
        log(f"'{name}' 못 엶 — {detail}", YELLOW)


def run():
    port_cfg, buttons = load_config()

    print()
    log("NU40DK Launcher 시작", CYAN)
    for key in sorted(buttons):
        target = buttons[key].get("app") or buttons[key].get("url") or "-"
        log(f"  버튼 {key} → {buttons[key].get('name', '?')} {DIM}({target}){RESET}")
    log("종료하려면 Ctrl-C", DIM)
    print()

    fd = None
    path = None
    buf = b""
    last_fire = {}
    warned_missing = False    # "보드 못 찾음"을 매초 찍지 않기 위한 빗장
    warned_wrong_fw = False   # 다른 펌웨어 경고도 한 번만

    try:
        while True:
            # --- 연결 ---
            if fd is None:
                path = find_port(port_cfg)
                if path is None:
                    if not warned_missing:
                        log("보드를 찾는 중… (USB 연결 확인)", YELLOW)
                        warned_missing = True
                    time.sleep(RECONNECT_SEC)
                    continue

                try:
                    fd = open_port(path)
                except OSError as e:
                    if not warned_missing:
                        log(f"{path} 못 엶 — {e.strerror}. "
                            f"Arduino 시리얼 모니터가 켜져 있으면 닫아주세요", YELLOW)
                        warned_missing = True
                    time.sleep(RECONNECT_SEC)
                    continue

                buf = b""
                warned_missing = False
                warned_wrong_fw = False
                log(f"보드 연결됨 {DIM}{path}{RESET}", GREEN)

            # --- 읽기 ---
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    # 보드를 뽑으면 조용해지기만 할 뿐 에러가 안 날 수 있다.
                    # 노드가 사라졌는지 직접 확인한다
                    if not os.path.exists(path):
                        raise OSError(f"{path} 사라짐")
                    continue

                chunk = os.read(fd, 4096)
                if not chunk:
                    raise OSError(f"{path} 연결 끊김")
            except OSError as e:
                log(f"보드 끊김 — 다시 찾는 중 {DIM}({e}){RESET}", YELLOW)
                os.close(fd)
                fd = None
                time.sleep(RECONNECT_SEC)
                continue

            buf += chunk

            # --- 줄 단위 처리 ---
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
                        log(f"버튼 {key}에 매핑이 없습니다", YELLOW)
                        continue

                    now = time.monotonic()
                    if now - last_fire.get(key, 0.0) < COOLDOWN_SEC:
                        continue
                    last_fire[key] = now

                    launch(entry)

                elif line == "READY":
                    log("보드 준비 완료", GREEN)

                elif not warned_wrong_fw:
                    # 런처 말고 다른 펌웨어가 올라가 있으면 여기로 떨어진다.
                    # 원인을 모른 채 버튼만 눌러보는 시간을 없애준다
                    log(f"버튼 신호가 아닌 출력이 옵니다: {DIM}{line[:60]}{RESET}", YELLOW)
                    log("nu40dk_launcher 펌웨어가 올라가 있는지 확인하세요", YELLOW)
                    warned_wrong_fw = True

            # 줄바꿈 없이 쓰레기만 계속 들어오는 펌웨어를 만나도 메모리가 안 새게 한다
            if len(buf) > 8192:
                buf = buf[-1024:]

    except KeyboardInterrupt:
        print()
        log("종료합니다", CYAN)
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(run())
