#include <Braccio.h>
#include <Servo.h>

// The Braccio library expects these exact names — it attaches them
// to the shield's PWM pins (M1=11, M2=10, M3=9, M4=6, M5=5, M6=3).
// Pin 12 is reserved by the shield's soft-start circuit.
Servo base;        // M1: 0–180
Servo shoulder;    // M2: 15–165
Servo elbow;       // M3: 0–180
Servo wrist_ver;   // M4: 0–180
Servo wrist_rot;   // M5: 0–180
Servo gripper;     // M6: 10 (open) – 73 (closed)

void setup() {
  // Runs the soft-start, attaches the servos, and parks the arm
  // in its safety position.
  Braccio.begin();
}

void loop() {
  // ServoMovement(stepDelayMs, M1, M2, M3, M4, M5, M6)
  // stepDelayMs: 10–30 ms per 1-degree step (smaller = faster).

  // Pose 1: upright, gripper open
  Braccio.ServoMovement(20, 90, 90, 90, 90, 90, 10);
  delay(800);

  // Pose 2: reach forward
  Braccio.ServoMovement(20, 90, 45, 180, 180, 90, 10);
  delay(800);

  // Pose 3: close gripper
  Braccio.ServoMovement(20, 90, 45, 180, 180, 90, 73);
  delay(800);

  // Pose 4: lift back up
  Braccio.ServoMovement(20, 90, 90, 90, 90, 90, 73);
  delay(800);

  // Pose 5: rotate base, then return
  Braccio.ServoMovement(20,   0, 90, 90, 90, 90, 73);
  delay(800);
  Braccio.ServoMovement(20, 180, 90, 90, 90, 90, 73);
  delay(800);

  // Pose 6: back to safety, gripper open
  Braccio.ServoMovement(20, 90, 45, 180, 180, 90, 10);
  delay(1500);
}
