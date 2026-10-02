/*
 * NU40DK Launcher — 4-button app launcher
 *
 * The board makes no decisions. It only reports over serial that a button was pressed.
 * Which app to open is decided by config.json of launcher.py on the Mac side.
 * Putting the mapping on the board would mean reflashing it every time an app changes,
 * so the board is only responsible for "which button was pressed".
 *
 * Protocol (one per line, terminated by \n)
 *   READY        boot complete. The Mac treats seeing this as "connected"
 *   BTN1 ~ BTN4  once, at the moment a button is pressed
 *
 * Sent only at the press (falling edge). If it kept sending while held,
 * the Mac side would open the app dozens of times. Nothing is sent on release —
 * the launcher does not need to know when it was released.
 *
 * Upload:
 *   CLI="/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
 *   "$CLI" compile --fqbn nucode:nrf52:nu40dk ~/Documents/Arduino/nu40dk_launcher
 *   "$CLI" upload  --fqbn nucode:nrf52:nu40dk -p /dev/cu.usbmodem1101 ~/Documents/Arduino/nu40dk_launcher
 */

// This board's Serial is TinyUSB CDC; without this header you get a link error
#include <Adafruit_TinyUSB.h>
#include <math.h>

const uint8_t LEDS[4]    = { PIN_LED1, PIN_LED2, PIN_LED3, PIN_LED4 };
const uint8_t BUTTONS[4] = { PIN_BUTTON1, PIN_BUTTON2, PIN_BUTTON3, PIN_BUTTON4 };

// ---------------------------------------------------------------- tunables

// Debounce window (ms). A tact switch bounces several times for 1-5 ms as the contacts close.
// If too large, a fast double press gets swallowed into one.
const uint16_t DEBOUNCE_MS = 30;

// How long (ms) the LED keeps glowing after a press. It lets you see by eye that the press registered.
// It fills the gap until the app actually appears.
const uint16_t AFTERGLOW_MS = 400;

// Idle breathing brightness (0.0-1.0) and period (ms).
// If off, the board looks dead on the desk. If too bright, it is hard to tell from a pressed LED.
const float    IDLE_LEVEL  = 0.05f;
const uint16_t IDLE_PERIOD = 4200;

// ---------------------------------------------------------------- internal state

static uint16_t gammaLut[256];

struct Button {
  bool     stable;      // current state after debounce (true = pressed)
  bool     lastRaw;     // raw state read last time
  uint32_t changedAt;   // time lastRaw changed
  uint32_t glowUntil;   // keep the LED on until this time
};

static Button btn[4];

// Human eyes perceive brightness logarithmically. Using raw PWM values makes the dark end look bunched up.
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

// Idle brightness. Fold a sin wave to 0-1 and swell and fade very slowly.
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

    // Buttons are active-low. The pull-up must be enabled so the pin reads HIGH when not pressed
    pinMode(BUTTONS[i], INPUT_PULLUP);

    btn[i].stable    = false;
    btn[i].lastRaw   = false;
    btn[i].changedAt = 0;
    btn[i].glowUntil = 0;
  }

  // Wait up to 3 s for the serial monitor to attach. Proceed anyway if it doesn't —
  // the board must run on its own whether or not a program is running on the Mac
  uint32_t start = millis();
  while (!Serial && millis() - start < 3000) delay(10);

  Serial.println("READY");
}

void loop() {
  uint32_t now  = millis();
  float    idle = idleLevel(now);

  for (uint8_t i = 0; i < 4; i++) {
    bool raw = (digitalRead(BUTTONS[i]) == LOW);

    // While the raw state is jittering, only reset the clock and defer the decision
    if (raw != btn[i].lastRaw) {
      btn[i].lastRaw   = raw;
      btn[i].changedAt = now;
    } else if (raw != btn[i].stable && now - btn[i].changedAt >= DEBOUNCE_MS) {
      btn[i].stable = raw;

      // Report only at the moment of press. Stay quiet on release
      if (raw) {
        Serial.printf("BTN%d\n", i + 1);
        btn[i].glowUntil = now + AFTERGLOW_MS;
      }
    }

    // Keep it on while held; after release, keep it on until the afterglow ends
    bool lit = btn[i].stable || (int32_t) (btn[i].glowUntil - now) > 0;
    writeLed(i, lit ? 1.0f : idle);
  }

  delay(5);
}