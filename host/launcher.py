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
import urllib.parse

HERE          = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(HERE, "config.json")
FAREWELL_PAGE = os.path.join(HERE, "farewell.html")

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 같은 버튼이 이 시간 안에 다시 들어오면 무시한다. 펌웨어가 디바운스를 하지만
# 손가락이 떨려 두 번 눌리는 것까지는 막지 못한다. 앱이 두 번 뜨는 것보다
# 한 번 씹히는 게 낫다.
COOLDOWN_SEC = 0.8

# 보드를 못 찾거나 끊겼을 때 다시 찾아보는 간격
RECONNECT_SEC = 1.0

# 한 모드에서 앱을 여러 개 열 때 사이 간격
STAGGER_SEC = 0.35

# 앱 종료를 기다려주는 한계. 넘어가면 포기하고 다음 앱으로 간다
QUIT_TIMEOUT_SEC = 6.0

# 그 밖의 AppleScript 한 줄이 응답을 기다리는 한계
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


def targets_of(entry):
    """항목을 '열 것' 목록으로 편다. 예전의 단일 app/url 형식도 그대로 받는다."""
    items = entry.get("open")
    if items is not None:
        return items
    if entry.get("app"):
        return [{"app": entry["app"]}]
    if entry.get("url"):
        return [{"url": entry["url"]}]
    return []


def osa(script, timeout=OSA_TIMEOUT_SEC):
    """AppleScript 한 토막 실행."""
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=timeout)


def applescript_str(text):
    """AppleScript 문자열 리터럴 안에 넣을 수 있게 감싼다."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def app_is_running(name):
    try:
        result = osa(f'application "{name}" is running')
    except subprocess.TimeoutExpired:
        # 답이 없으면 안 떠 있다고 보고 평범하게 연다. 여기서 멈추면
        # 버튼 하나에 런처 전체가 굳는다
        return False
    return (result.stdout or "").strip() == "true"


def open_new_window(item):
    """이미 떠 있는 앱에 '새 창'을 띄운다.

    촬영용이다. open -a는 이미 떠 있는 앱을 앞으로 끌어올 뿐이라
    화면에서는 아무 일도 안 일어난 것처럼 보인다.
    """
    app = item["app"]

    # 안 떠 있으면 그냥 열면 된다. 어차피 창이 새로 뜬다
    if not app_is_running(app):
        return False

    if app == "Terminal":
        # 터미널은 ⌘N 없이도 새 창을 만들 수 있다. 돌아가던 창은 안 건드린다.
        # command를 주면 그 창에서 바로 실행된다 — 카메라 앞에서 좋다
        script = ('tell application "Terminal"\n'
                  '  activate\n'
                  f'  do script {applescript_str(item.get("command", ""))}\n'
                  'end tell')
    else:
        # 나머지는 앱을 앞으로 불러낸 뒤 ⌘N을 보낸다.
        # delay는 앱이 실제로 맨 앞에 올 때까지 기다리는 시간이다
        script = (f'tell application "{app}" to activate\n'
                  'delay 0.4\n'
                  'tell application "System Events" to keystroke "n" using command down')

    try:
        result = osa(script)
    except subprocess.TimeoutExpired:
        log(f"  ↳ '{app}' 새 창 시간 초과", YELLOW)
        return True

    if result.returncode == 0:
        log(f"  ↳ {app} 새 창", GREEN)
    else:
        detail = (result.stderr or "").strip()[:100]
        log(f"  ↳ '{app}' 새 창 실패 — {detail}", YELLOW)
        if "1002" in detail or "assistive" in detail.lower():
            # ⌘N은 사람 대신 키를 눌러주는 것이라 손쉬운 사용 권한이 필요하다.
            # 권한은 런처를 실행한 앱(보통 터미널)에 준다
            log("     시스템 설정 → 개인정보 보호와 보안 → 손쉬운 사용에서 "
                "터미널을 켜주세요", YELLOW)
            log("     권한 없이 새 창을 원하면 config.json에서 new 대신 "
                "file로 파일을 지정하세요", YELLOW)
    return True


def open_item(item):
    """앱 하나 또는 URL 하나. 이미 떠 있는 앱이면 앞으로 끌어온다."""
    if item.get("app") and item.get("new") and open_new_window(item):
        return

    if item.get("app") and item.get("file"):
        # 파일을 지정하면 그 앱이 그 파일로 새 창을 연다. ⌘N과 달리
        # 손쉬운 사용 권한이 필요 없고, 화면에 내용까지 보인다
        path = os.path.expanduser(item["file"])
        cmd, label = ["open", "-a", item["app"], path], f'{item["app"]} ← {os.path.basename(path)}'
    elif item.get("app"):
        cmd, label = ["open", "-a", item["app"]], item["app"]
    elif item.get("url"):
        cmd, label = ["open", item["url"]], item["url"]
    else:
        log(f"  ↳ 열 대상이 비었습니다: {item}", YELLOW)
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"  ↳ {label} 열림", GREEN)
    else:
        detail = (result.stderr or "").strip() or f"open 종료코드 {result.returncode}"
        log(f"  ↳ '{label}' 못 엶 — {detail}", YELLOW)


def close_app(name):
    """앱을 정상 종료시킨다. 정말 죽었는지 확인하고 사실대로 알린다."""
    # is running을 먼저 보는 이유: quit만 보내면 안 떠 있던 앱이 오히려 실행된다.
    # 퇴근 눌렀는데 앱이 켜지는 건 제일 보기 싫은 그림이다.
    #
    # 그리고 quit이 성공했다고 앱이 죽은 건 아니다. 저장 확인 창이 뜨면
    # 그대로 살아 있다. 실제로 사라졌는지 보고 나서 말해야 로그를 믿을 수 있다
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
        log(f"  ↳ '{name}' 응답 없음 — 넘어갑니다", YELLOW)
        return

    verdict = (result.stdout or "").strip()
    if verdict == "quit":
        log(f"  ↳ {name} 닫음", DIM)
    elif verdict == "absent":
        log(f"  ↳ {name} 이미 꺼져 있었음", DIM)
    elif verdict == "still":
        # 저장 안 한 창이 있으면 앱이 물어보느라 안 죽는다
        log(f"  ↳ '{name}' 안 닫힘 — 저장할지 묻고 있는 것 같습니다", YELLOW)
    else:
        detail = (result.stderr or "").strip()[:80] or f"종료코드 {result.returncode}"
        log(f"  ↳ '{name}' 못 닫음 — {detail}", YELLOW)


def show_farewell(message, sub=None):
    """컨페티 화면을 크롬 앱 모드 창으로 띄운다."""
    # --app은 탭도 주소창도 없는 창을 연다. 페이지가 끝나면 스스로 닫히므로
    # 퇴근했는데 창이 남는 일이 없다.
    # tkinter로 먼저 만들었다가 버렸다 — 이 맥의 Tk는 캔버스를 못 그리고
    # 흰 화면만 남겼다 (root.update()에서 멈춤)
    if not os.path.exists(FAREWELL_PAGE):
        log("farewell.html이 없습니다 — 컨페티 건너뜀", YELLOW)
        return
    if not os.path.exists(CHROME_BIN):
        log("크롬을 못 찾아 컨페티를 건너뜁니다", YELLOW)
        return

    url = ("file://" + urllib.parse.quote(FAREWELL_PAGE)
           + "?msg=" + urllib.parse.quote(message))
    if sub is not None:
        url += "&sub=" + urllib.parse.quote(sub)
    try:
        subprocess.Popen([CHROME_BIN, f"--app={url}", "--start-fullscreen"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("  ↳ 컨페티", GREEN)
    except OSError as e:
        log(f"  ↳ 컨페티 못 띄움 — {e}", YELLOW)


def fire(entry):
    """버튼 하나가 뜻하는 일을 전부 수행한다."""
    name = entry.get("name", "?")
    log(f"{BOLD}{name}{RESET}", CYAN)

    # 닫기가 먼저다. 퇴근에서 컨페티가 마지막에 혼자 남아야 그림이 산다
    for app in entry.get("close", []):
        close_app(app)

    items = targets_of(entry)
    for i, item in enumerate(items):
        # 한꺼번에 열면 창들이 서로 앞으로 나오려고 싸우고, 화면에도 안 예쁘다
        if i:
            time.sleep(STAGGER_SEC)
        open_item(item)

    message = entry.get("farewell")
    if message:
        show_farewell(message, entry.get("farewell_sub"))

    if not items and not entry.get("close") and not message:
        log(f"'{name}'에 할 일이 없습니다 — config.json 확인", YELLOW)


def run(config_path=None):
    config_path = config_path or CONFIG_PATH
    port_cfg, buttons = load_config(config_path)

    print()
    # 어느 설정으로 떴는지 반드시 보여준다. 설정을 여러 개 두면
    # "왜 안 바뀌지"의 절반은 다른 파일을 보고 있어서 생긴다
    log(f"NU40DK Launcher 시작 {DIM}({os.path.basename(config_path)}){RESET}", CYAN)
    for key in sorted(buttons):
        entry = buttons[key]
        parts = [item.get("app") or item.get("url") or "?" for item in targets_of(entry)]
        if entry.get("close"):
            parts.append(f"{len(entry['close'])}개 닫기")
        if entry.get("farewell"):
            parts.append("컨페티")
        log(f"  버튼 {key} → {entry.get('name', '?')} "
            f"{DIM}({', '.join(parts) or '-'}){RESET}")
    log("종료하려면 Ctrl-C", DIM)

    # 컨페티는 별도 프로세스라 실패해도 조용히 묻힌다. 버튼을 누른 뒤에
    # 알게 되면 늦으므로 시작할 때 확인해둔다
    if any(b.get("farewell") for b in buttons.values()):
        for path, what in ((FAREWELL_PAGE, "farewell.html"), (CHROME_BIN, "크롬")):
            if not os.path.exists(path):
                log(f"{what}이(가) 없어 컨페티가 안 뜹니다 {DIM}{path}{RESET}", YELLOW)
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

                    fire(entry)

                    # 쿨다운은 '작업이 끝난 시점'부터 다시 센다. 앱을 여는 데
                    # 1초 넘게 걸리는 모드에서는 도장을 시작할 때 찍는 것만으로
                    # 부족했다 — 그 사이 들어온 신호가 쿨다운을 지나 통과했다
                    last_fire[key] = time.monotonic()

                    # 여는 동안 쌓인 입력은 버린다. 조급해서 또 눌렀거나
                    # 접점이 튄 것이지, 다음 모드를 요청한 게 아니다
                    termios.tcflush(fd, termios.TCIFLUSH)
                    buf = b""

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
    # 설정 파일을 인자로 받는다. 없으면 config.json.
    # 파일 이름만 주면 이 폴더에서 찾는다 — 매번 전체 경로를 치지 않게
    chosen = sys.argv[1] if len(sys.argv) > 1 else None
    if chosen and not os.path.isabs(chosen):
        chosen = os.path.join(HERE, chosen)
    if chosen and not os.path.exists(chosen):
        log(f"설정 파일이 없습니다: {chosen}", YELLOW)
        sys.exit(1)
    sys.exit(run(chosen))
