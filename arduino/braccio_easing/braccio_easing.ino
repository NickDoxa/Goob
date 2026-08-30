// Serial-protocol firmware v2 for the Braccio + Elegoo Uno R3.
// Replaces the stock Braccio library's fixed-step movement with ServoEasing,
// so all six joints depart and arrive together on a cubic ease-in/ease-out
// curve instead of grinding through 1-degree steps with hard stops.
// See documentation/OPTIMIZATION.md "Phase O2" for the spec.
//
// -------------------------------------------------------------------------
// LIBRARY DEPENDENCY
// -------------------------------------------------------------------------
// ServoEasing by Armin Joachimsmeyer, version 3.6.0 or newer.
// Arduino IDE: Tools > Manage Libraries... > search "ServoEasing" > Install.
// It pulls in the stock Servo library, which is already installed.
//
// ServoEasing 3.x uses the include-the-implementation pattern: the main
// sketch includes <ServoEasing.hpp> (not .h) so that the #defines below
// actually reach the library's compilation. Including .h here instead
// produces linker errors.
//
// The Braccio library is deliberately NOT included. We bypass it entirely
// and talk to the servos through ServoEasing, which means we also have to
// reproduce its soft-start by hand (see below).
//
// -------------------------------------------------------------------------
// SOFT-START RATIONALE  (the part that breaks hardware if it is wrong)
// -------------------------------------------------------------------------
// The Braccio shield (V4+) gates servo rail power through a FET driven by
// D12. Held LOW the rail is off; held HIGH it is fully on. At boot every
// servo is somewhere unknown (wherever the arm was left when power died),
// so the instant they are both powered and commanded they all slam toward
// the target at once. Six stalled servos on a 5V/4A supply is a brown-out,
// which resets the Uno mid-move: exactly the failure mode this guards.
//
// Braccio::begin() avoids that by software-PWMing D12 at roughly 15% duty
// for six seconds, current-limiting the rail while the servos creep into
// position, then latching it HIGH. The ordering matters as much as the
// waveform: D12 goes LOW *before* the servos are attached, so the very
// first pulse train arrives with the rail already limited.
//
// Replicated below, cycle for cycle, from Braccio.cpp 2.0 (_softStart /
// _softwarePWM with soft_start_level = SOFT_START_DEFAULT = 0):
//
//   pinMode(12, OUTPUT); digitalWrite(12, LOW);
//   attach all six servos and write their initial angles
//   for 0..2000ms:    80us HIGH / 450us LOW   (530us period)
//   for 2000..6000ms: 75us HIGH / 430us LOW   (505us period)
//   digitalWrite(12, HIGH)
//
// Note this is a fixed current limit, not a rising ramp -- the duty cycle
// is essentially constant across both phases. Also note the timings drift
// slightly because the Servo library's Timer1 ISR preempts
// delayMicroseconds(); the stock library has servos attached during its
// soft start too, so it drifts identically. Do not "fix" this.
//
// Total boot is ~6s of soft start on top of the bootloader, and READY is
// only printed once it finishes.
//
// -------------------------------------------------------------------------
// PROTOCOL v2 -- line-based ASCII, 115200 baud, '\n' terminated
// -------------------------------------------------------------------------
// Backward compatible with braccio_serial.ino (protocol v1).
//
//   READY                       once, after soft start + attach complete
//   PING                     -> OK
//   HOME                     -> OK   eased to 90 90 90 90 90 10 over 1000ms
//   MOVE <step_delay> <m1..m6>  LEGACY. step_delay clamped to 10..30, then
//                               mapped to a duration that approximates v1
//                               timing: v1 stepped 1 degree per step_delay
//                               ms, so duration = max_delta * step_delay,
//                               clamped to 200..3000ms.          -> OK
//   EASE <duration_ms> <m1..m6> NEW. duration clamped to 100..5000ms. -> OK
//
//   ERR parse                   malformed arguments
//   ERR unknown                 unrecognized command
//
// Angles are clamped per servo (base 0-180, shoulder 15-165, elbow 0-180,
// wrist_v 0-180, wrist_r 0-180, gripper 10-73). The reply is sent after
// the move physically completes -- blocking is intentional, it is what
// makes the host's send/wait-for-OK handshake mean "the arm got there".

#define MAX_EASING_SERVOS 6
#define ENABLE_EASE_CUBIC
#define DISABLE_MICROS_AS_DEGREE_PARAMETER
#define DISABLE_PAUSE_RESUME

// Naming ENABLE_EASE_CUBIC suppresses ServoEasing's default "enable
// everything" block, saving flash we would otherwise spend on sine, bounce,
// elastic and friends. If the library ever stops compiling with only cubic
// enabled, deleting that one #define restores the defaults (cubic included)
// at no cost but program size.
#include <ServoEasing.hpp>

// Braccio shield pin map. The stock library's variable names are famously
// crossed: it does wrist_rot.attach(6) / wrist_ver.attach(5), then in
// ServoMovement() compares its 4th argument against step_wrist_rot and its
// 5th against step_wrist_ver. The two swaps cancel, so the documented
// argument order (M4 = wrist vertical, M5 = wrist rotation) does reach
// pins 6 and 5 respectively. These constants encode the resolved mapping,
// which keeps v1 and v2 pointing the same joints at the same numbers.
const uint8_t PIN_BASE       = 11;
const uint8_t PIN_SHOULDER   = 10;
const uint8_t PIN_ELBOW      = 9;
const uint8_t PIN_WRIST_V    = 6;
const uint8_t PIN_WRIST_R    = 5;
const uint8_t PIN_GRIPPER    = 3;
const uint8_t PIN_SOFT_START = 12;

const uint8_t NUM_SERVOS = 6;

ServoEasing servoBase;
ServoEasing servoShoulder;
ServoEasing servoElbow;
ServoEasing servoWristV;
ServoEasing servoWristR;
ServoEasing servoGripper;

ServoEasing *const SERVOS[NUM_SERVOS] = {
  &servoBase, &servoShoulder, &servoElbow, &servoWristV, &servoWristR, &servoGripper
};

const uint8_t PINS[NUM_SERVOS] = {
  PIN_BASE, PIN_SHOULDER, PIN_ELBOW, PIN_WRIST_V, PIN_WRIST_R, PIN_GRIPPER
};

const int LIMIT_MIN[NUM_SERVOS] = {0,   15,  0,   0,   0,   10};
const int LIMIT_MAX[NUM_SERVOS] = {180, 165, 180, 180, 180, 73};

const int HOME_POSE[NUM_SERVOS] = {90, 90, 90, 90, 90, 10};
const unsigned int HOME_DURATION_MS = 1000;

const int STEP_DELAY_MIN = 10,  STEP_DELAY_MAX = 30;
const long MOVE_DUR_MIN  = 200, MOVE_DUR_MAX   = 3000;
const int  EASE_DUR_MIN  = 100, EASE_DUR_MAX   = 5000;

// Last commanded pose. Only used to size legacy MOVE durations; ServoEasing
// keeps its own authoritative current position for the easing start point.
// The two stay in step because every position change goes through easeTo().
int g_current[NUM_SERVOS];

const uint8_t LINE_MAX = 48;

static int clampi(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

static void softwarePWM(unsigned int highUs, unsigned int lowUs) {
  digitalWrite(PIN_SOFT_START, HIGH);
  delayMicroseconds(highUs);
  digitalWrite(PIN_SOFT_START, LOW);
  delayMicroseconds(lowUs);
}

static void softStart() {
  unsigned long t0 = millis();
  while (millis() - t0 < 2000) softwarePWM(80, 450);
  while (millis() - t0 < 6000) softwarePWM(75, 430);
  digitalWrite(PIN_SOFT_START, HIGH);
}

void setup() {
  Serial.begin(115200);

  // Rail off before the servos ever see a pulse.
  pinMode(PIN_SOFT_START, OUTPUT);
  digitalWrite(PIN_SOFT_START, LOW);

  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    // attach(pin, degrees) writes the angle immediately and seeds the
    // easing start position, so the first EASE has a known origin.
    SERVOS[i]->attach(PINS[i], HOME_POSE[i]);
    SERVOS[i]->setEasingType(EASE_CUBIC_IN_OUT);
    g_current[i] = HOME_POSE[i];
  }

  softStart();
  Serial.println(F("READY"));
}

// Blocks until a full line arrives. Strips a trailing '\r' so CRLF hosts
// work, and trailing spaces so "MOVE 10 90 90 90 90 90 10 " still parses.
// Characters past LINE_MAX are dropped rather than buffered, but the line
// still terminates on '\n' -- an overlong line fails parsing instead of
// desyncing the stream.
static void readLine(char *buf) {
  uint8_t n = 0;
  while (true) {
    while (Serial.available() == 0) { /* spin */ }
    char c = Serial.read();
    if (c == '\n') {
      while (n > 0 && (buf[n - 1] == '\r' || buf[n - 1] == ' ')) n--;
      buf[n] = '\0';
      return;
    }
    if (n < LINE_MAX - 1) buf[n++] = c;
  }
}

// Strict space-separated integer parse. Returns true only when exactly
// `count` integers were present with no trailing junk.
static bool parseInts(char *s, int *out, uint8_t count) {
  uint8_t n = 0;
  char *p = s;
  while (*p != '\0') {
    if (*p == ' ') { p++; continue; }
    if (n >= count) return false;
    char *end;
    long v = strtol(p, &end, 10);
    if (end == p) return false;
    if (*end != '\0' && *end != ' ') return false;
    out[n++] = (int) v;
    p = end;
  }
  return n == count;
}

static void easeTo(const int *targets, unsigned int durationMs) {
  bool anyMoves = false;
  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    if (targets[i] != g_current[i]) anyMoves = true;
    SERVOS[i]->setEaseToD(targets[i], durationMs);
  }
  // Skipping the synchronized start when nothing has to move keeps a
  // no-op command from spending a refresh interval inside the wait loop.
  if (anyMoves) {
    // Starts every servo in the same interrupt tick and blocks until the
    // last one stops. All six were given the same duration, so cubic
    // ease-in/ease-out means they also arrive together.
    synchronizeAllServosStartAndWaitForAllServosToStop();
  }
  for (uint8_t i = 0; i < NUM_SERVOS; i++) g_current[i] = targets[i];
}

// v1 stepped every joint 1 degree per step_delay ms in a shared loop, so
// the move lasted as long as the furthest-travelling joint took.
static unsigned int legacyDuration(const int *targets, int stepDelay) {
  int maxDelta = 0;
  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    int d = targets[i] - g_current[i];
    if (d < 0) d = -d;
    if (d > maxDelta) maxDelta = d;
  }
  long ms = (long) maxDelta * (long) stepDelay;
  if (ms < MOVE_DUR_MIN) ms = MOVE_DUR_MIN;
  if (ms > MOVE_DUR_MAX) ms = MOVE_DUR_MAX;
  return (unsigned int) ms;
}

static void doMove(char *args, bool legacy) {
  int v[7];
  if (!parseInts(args, v, 7)) {
    Serial.println(F("ERR parse"));
    return;
  }
  int targets[NUM_SERVOS];
  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    targets[i] = clampi(v[i + 1], LIMIT_MIN[i], LIMIT_MAX[i]);
  }
  unsigned int duration = legacy
    ? legacyDuration(targets, clampi(v[0], STEP_DELAY_MIN, STEP_DELAY_MAX))
    : (unsigned int) clampi(v[0], EASE_DUR_MIN, EASE_DUR_MAX);

  easeTo(targets, duration);
  Serial.println(F("OK"));
}

void loop() {
  char line[LINE_MAX];
  readLine(line);
  if (line[0] == '\0') return;

  if (strcmp(line, "PING") == 0) {
    Serial.println(F("OK"));
    return;
  }
  if (strcmp(line, "HOME") == 0) {
    easeTo(HOME_POSE, HOME_DURATION_MS);
    Serial.println(F("OK"));
    return;
  }

  char *sp = strchr(line, ' ');
  if (sp != NULL) {
    *sp = '\0';
    char *args = sp + 1;
    if (strcmp(line, "MOVE") == 0) {
      doMove(args, true);
      return;
    }
    if (strcmp(line, "EASE") == 0) {
      doMove(args, false);
      return;
    }
  }
  Serial.println(F("ERR unknown"));
}
