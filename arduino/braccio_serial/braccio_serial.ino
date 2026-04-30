// Serial-protocol firmware for the Braccio + Elegoo Uno R3.
// Host (Uno Q running Python) drives the arm over USB serial at 115200 baud.
// See PLAN.md "Phase 1: Arduino sketch + serial protocol" for the spec.

#include <Braccio.h>
#include <Servo.h>

// Names are required by the Braccio library — it references them as externs.
Servo base;        // M1: 0-180
Servo shoulder;    // M2: 15-165
Servo elbow;       // M3: 0-180
Servo wrist_ver;   // M4: 0-180
Servo wrist_rot;   // M5: 0-180
Servo gripper;     // M6: 10 (open) - 73 (closed)

// Servo limits — clamp on the Arduino side so a buggy host can't push past
// hardware-safe ranges.
const int M1_MIN = 0,   M1_MAX = 180;
const int M2_MIN = 15,  M2_MAX = 165;
const int M3_MIN = 0,   M3_MAX = 180;
const int M4_MIN = 0,   M4_MAX = 180;
const int M5_MIN = 0,   M5_MAX = 180;
const int M6_MIN = 10,  M6_MAX = 73;
const int DELAY_MIN = 10, DELAY_MAX = 30;

static int clampi(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

void setup() {
  Serial.begin(115200);
  // Braccio.begin() runs the soft-start and parks the arm. Takes ~3-4s.
  Braccio.begin();
  Serial.println("READY");
}

// Blocks until a full line arrives. Strips trailing \r so CRLF hosts work too.
static String readLine() {
  String buf;
  while (true) {
    while (Serial.available() == 0) { /* spin */ }
    char c = Serial.read();
    if (c == '\n') {
      if (buf.length() > 0 && buf[buf.length() - 1] == '\r') {
        buf.remove(buf.length() - 1);
      }
      return buf;
    }
    buf += c;
  }
}

// Parse "<delay> <m1> <m2> <m3> <m4> <m5> <m6>", clamp, execute.
static void doMove(const String& args) {
  int parts[7];
  int n = 0;
  int start = 0;
  int len = args.length();
  for (int i = 0; i <= len && n < 7; i++) {
    if (i == len || args[i] == ' ') {
      if (i > start) {
        parts[n++] = args.substring(start, i).toInt();
      }
      start = i + 1;
    }
  }
  if (n != 7) {
    Serial.println("ERR parse");
    return;
  }
  int d  = clampi(parts[0], DELAY_MIN, DELAY_MAX);
  int m1 = clampi(parts[1], M1_MIN,    M1_MAX);
  int m2 = clampi(parts[2], M2_MIN,    M2_MAX);
  int m3 = clampi(parts[3], M3_MIN,    M3_MAX);
  int m4 = clampi(parts[4], M4_MIN,    M4_MAX);
  int m5 = clampi(parts[5], M5_MIN,    M5_MAX);
  int m6 = clampi(parts[6], M6_MIN,    M6_MAX);
  Braccio.ServoMovement(d, m1, m2, m3, m4, m5, m6);
  Serial.println("OK");
}

void loop() {
  String line = readLine();
  if (line.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd  = (sp == -1) ? line : line.substring(0, sp);
  String args = (sp == -1) ? ""   : line.substring(sp + 1);

  if (cmd == "MOVE") {
    doMove(args);
  } else if (cmd == "HOME") {
    // Mirror the home pose used by ArmController.home() on the host.
    Braccio.ServoMovement(20, 90, 90, 90, 90, 90, 30);
    Serial.println("OK");
  } else if (cmd == "PING") {
    Serial.println("OK");
  } else {
    Serial.println("ERR unknown");
  }
}
