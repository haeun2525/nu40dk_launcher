# NU40DK Launcher

버튼 4개짜리 미니 보드. 갑자기 코딩하고 싶을 때, 갑자기 인스타 보고 싶을 때
고민 없이 하나 누르면 그 앱이 뜬다.

| 버튼 | GPIO | 앱 |
|------|------|-----|
| 1 | 11 | 클로드 |
| 2 | 12 | 노션 |
| 3 | 24 | 인스타그램 (웹 — Mac 앱이 없다) |
| 4 | 25 | 팀즈 |

## 구성

저장소는 Arduino 스케치 폴더(`~/Documents/Arduino/nu40dk_launcher`)에 그대로 얹혀 있다.
Arduino IDE가 스케치를 여기서 찾기 때문에, 저장소를 다른 곳에 두면 사본이 둘로 갈라진다.

```
nu40dk_launcher.ino    보드 펌웨어 (버튼 → 시리얼)
host/
  launcher.py          Mac 프로그램 (시리얼 → 앱 실행)
  config.json          버튼 매핑. 앱 바꾸려면 여기만 고친다
  run.command          더블클릭 실행
```

역할을 이렇게 나눈 이유: 보드는 "몇 번이 눌렸나"까지만 알린다.
어떤 앱을 여는지는 Mac이 정한다. 매핑을 보드에 넣으면 앱 하나 바꿀 때마다
보드를 다시 구워야 한다.

**프로토콜** — 보드가 시리얼(115200)로 한 줄씩 보낸다.

```
READY          부팅 완료
BTN1 ~ BTN4    버튼이 눌린 순간 한 번만
```

## 실행

```sh
python3 ~/Documents/Arduino/nu40dk_launcher/host/launcher.py    # 또는 run.command 더블클릭
```

보드가 안 꽂혀 있어도 그냥 뜬다. 꽂으면 알아서 붙고, 뽑았다 꽂아도 알아서 다시 붙는다.
종료는 Ctrl-C.

## 펌웨어 올리기

2026-08-12 업로드·실물 검증 완료. 버튼 4개 모두 `BTN1`~`BTN4`가 나오는 것을 확인했다.
고친 뒤 다시 구울 때만 아래를 쓴다.

```sh
CLI="/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
"$CLI" compile --fqbn nucode:nrf52:nu40dk ~/Documents/Arduino/nu40dk_launcher
"$CLI" upload  --fqbn nucode:nrf52:nu40dk -p /dev/cu.usbmodem12301 ~/Documents/Arduino/nu40dk_launcher
```

포트 이름은 꽂을 때마다 바뀐다. `ls /dev/cu.usbmodem*`로 확인하고 넣는다.

Together 보드로 되돌리려면 `~/Documents/Arduino/nu40dk_together`를 같은 방식으로 다시 올리면 된다.

## 앱 바꾸기

`config.json`만 고치고 런처를 다시 실행한다.

```json
"2": { "name": "슬랙", "app": "Slack" }
"3": { "name": "유튜브", "url": "https://youtube.com" }
```

`app`은 `/Applications` 안의 앱 이름. 이름이 맞는지는 실행 없이 확인할 수 있다.

```sh
osascript -e 'id of app "Slack"'    # 번들 ID가 나오면 정상
```

## 함정

- **시리얼 포트는 한 번에 하나.** Arduino 시리얼 모니터를 열어두면 런처가 못 붙거나,
  붙어도 데이터를 나눠 먹어서 버튼이 씹힌다. 모니터는 닫고 쓴다.
- **같은 버튼 0.8초 재입력은 무시한다** (`launcher.py`의 `COOLDOWN_SEC`).
  손 떨려서 두 번 눌렸을 때 앱이 두 번 뜨는 걸 막는다. 연타를 살리려면 낮춘다.
- **pyserial을 안 쓴다.** 이 맥에 없어서 termios로 직접 연다. 표준 라이브러리만
  쓰므로 설치할 게 없다 — 촬영 직전에 pip이 막혀 곤란해지는 상황을 피하려는 것.
- **LED는 눌린 버튼만 밝게, 나머지는 아주 약하게 숨쉰다.** 앱이 뜨기까지의
  공백을 눈으로 메우는 용도다. 밝기는 펌웨어의 `IDLE_LEVEL`로 조절한다.
