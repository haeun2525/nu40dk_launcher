/*
 * NU40DK Launcher — 버튼 4개짜리 앱 런처
 *
 * 보드는 판단하지 않는다. 버튼이 눌렸다는 사실만 시리얼로 알린다.
 * 어떤 앱을 열지는 Mac 쪽 launcher.py의 config.json이 정한다.
 * 매핑을 보드에 넣으면 앱을 바꿀 때마다 보드를 다시 구워야 하므로,
 * "누가 눌렸나"까지만 보드의 책임으로 둔다.
 *
 * 프로토콜 (한 줄에 하나, \n 종결)
 *   READY        부팅 완료. Mac이 이걸 보면 연결됐다고 판단한다
 *   BTN1 ~ BTN4  버튼이 눌린 순간 한 번만
 *
 * 눌린 순간(falling edge)에만 보낸다. 누르고 있는 동안 계속 보내면
 * Mac 쪽에서 앱이 수십 번 열린다. 뗄 때는 아무것도 보내지 않는다 —
 * 런처는 뗀 시점을 알 필요가 없다.
 *
 * 업로드:
 *   CLI="/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
 *   "$CLI" compile --fqbn nucode:nrf52:nu40dk ~/Documents/Arduino/nu40dk_launcher
 *   "$CLI" upload  --fqbn nucode:nrf52:nu40dk -p /dev/cu.usbmodem1101 ~/Documents/Arduino/nu40dk_launcher
 */

// 이 보드의 Serial은 TinyUSB CDC라 이 헤더가 없으면 링크 에러가 난다
#include <Adafruit_TinyUSB.h>
#include <math.h>

const uint8_t LEDS[4]    = { PIN_LED1, PIN_LED2, PIN_LED3, PIN_LED4 };
const uint8_t BUTTONS[4] = { PIN_BUTTON1, PIN_BUTTON2, PIN_BUTTON3, PIN_BUTTON4 };

// ---------------------------------------------------------------- 조절값

// 채터링 무시 구간(ms). 택트 스위치는 접점이 붙는 순간 1~5ms 동안 여러 번 튄다.
// 너무 키우면 빠르게 두 번 누르는 게 한 번으로 먹힌다.
const uint16_t DEBOUNCE_MS = 30;

// 누른 뒤 LED가 남아서 빛나는 시간(ms). 눌렀다는 걸 눈으로 확인시켜주는 용도다.
// 앱이 실제로 뜨기까지의 공백을 이게 메운다.
const uint16_t AFTERGLOW_MS = 400;

// 대기 중 숨쉬기 밝기(0.0~1.0)와 주기(ms).
// 꺼두면 책상 위에서 죽은 보드로 보인다. 너무 밝으면 눌린 LED와 구분이 안 된다.
const float    IDLE_LEVEL  = 0.05f;
const uint16_t IDLE_PERIOD = 4200;

// ---------------------------------------------------------------- 내부 상태

static uint16_t gammaLut[256];

struct Button {
  bool     stable;      // 디바운스를 통과한 현재 상태 (true = 눌림)
  bool     lastRaw;     // 직전에 읽은 원시 상태
  uint32_t changedAt;   // lastRaw가 바뀐 시각
  uint32_t glowUntil;   // 이 시각까지 LED를 켜둔다
};

static Button btn[4];

// 사람 눈은 밝기를 로그로 느낀다. PWM 값을 그대로 쓰면 어두운 쪽이 뭉쳐 보인다.
static void buildGamma() {
  for (uint16_t i = 0; i < 256; i++) {
    gammaLut[i] = (uint16_t) lroundf(powf(i / 255.0f, 2.6f) * 4095.0f);
  }
}

static void writeLed(uint8_t idx, float level) {
  if (level < 0.0f) level = 0.0f;
  if (level > 1.0f) level = 1.0f;
  analogWrite(LEDS[idx], gammaLut[(uint8_t) lroundf(level * 255.0f)]);
}

// 대기 중 밝기. sin을 0~1로 접어 아주 느리게 부풀렸다 꺼뜨린다.
static float idleLevel(uint32_t now) {
  float phase = (float) (now % IDLE_PERIOD) / (float) IDLE_PERIOD;
  return IDLE_LEVEL * (0.5f - 0.5f * cosf(phase * 2.0f * PI));
}

void setup() {
  Serial.begin(115200);

  buildGamma();
  analogWriteResolution(12);

  for (uint8_t i = 0; i < 4; i++) {
    pinMode(LEDS[i], OUTPUT);
    writeLed(i, 0.0f);

    // 버튼은 active-low. 풀업을 켜야 안 눌렸을 때 HIGH로 떠 있는다
    pinMode(BUTTONS[i], INPUT_PULLUP);

    btn[i].stable    = false;
    btn[i].lastRaw   = false;
    btn[i].changedAt = 0;
    btn[i].glowUntil = 0;
  }

  // 시리얼 모니터가 붙을 때까지 최대 3초 기다린다. 안 붙어도 그냥 진행한다 —
  // 보드는 Mac에 프로그램이 떠 있든 말든 혼자 돌아야 한다
  uint32_t start = millis();
  while (!Serial && millis() - start < 3000) delay(10);

  Serial.println("READY");
}

void loop() {
  uint32_t now  = millis();
  float    idle = idleLevel(now);

  for (uint8_t i = 0; i < 4; i++) {
    bool raw = (digitalRead(BUTTONS[i]) == LOW);

    // 원시 상태가 흔들리는 동안은 시계만 리셋하고 판단을 미룬다
    if (raw != btn[i].lastRaw) {
      btn[i].lastRaw   = raw;
      btn[i].changedAt = now;
    } else if (raw != btn[i].stable && now - btn[i].changedAt >= DEBOUNCE_MS) {
      btn[i].stable = raw;

      // 눌린 순간에만 알린다. 뗄 때는 조용히 넘어간다
      if (raw) {
        Serial.printf("BTN%d\n", i + 1);
        btn[i].glowUntil = now + AFTERGLOW_MS;
      }
    }

    // 누르고 있는 동안은 계속 켜두고, 뗀 뒤에는 잔광이 끝날 때까지 켜둔다
    bool lit = btn[i].stable || (int32_t) (btn[i].glowUntil - now) > 0;
    writeLed(i, lit ? 1.0f : idle);
  }

  delay(5);
}
