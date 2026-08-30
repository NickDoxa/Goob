"""Phase O3 desk check. Pure math — no arm, no camera, no API key.

    python -m tests.test_kinematics_smoke

Checks, in order:
1. Forward kinematics on the preset poses matches what the presets are NAMED.
   This is the sanity gate on the whole model: those six poses were authored
   by hand from months of watching the real arm, so if the model disagrees
   with them the model is wrong.
2. The servo convention fixed points, one assertion each.
3. solve_look_at -> forward_camera round trip: the camera ray actually
   passes within a couple of cm of every target we asked for.
4. The look_at anchors published in llm.py's tool description.
5. Unreachable targets raise KinematicsError with a readable reason.
6. Solve time stays well under the 100ms budget from OPTIMIZATION.md.

Exits non-zero if anything fails, so it can gate a commit.
"""
from __future__ import annotations

import math
import time

from src.arm import POSES
from src.kinematics import (
    KinematicsError,
    forward_camera,
    solve_look_at,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def ray_miss_cm(pose: dict, target: tuple[float, float, float]) -> float:
    """Perpendicular distance from the target to the camera's optical ray."""
    pos, direction = forward_camera(pose)
    v = [target[i] - pos[i] for i in range(3)]
    along = sum(v[i] * direction[i] for i in range(3))
    if along <= 0:
        # Target is behind the lens: the ray misses by the whole distance.
        return math.sqrt(sum(c * c for c in v))
    perp = [v[i] - along * direction[i] for i in range(3)]
    return math.sqrt(sum(c * c for c in perp))


def _pose(**overrides) -> dict:
    p = dict(POSES["home"])
    p.update(overrides)
    return p


def _desk_hit(pos, direction) -> str:
    """Where a downward ray meets the desk, as a readable annotation."""
    if direction[2] > -0.05:
        return ""
    t = pos[2] / -direction[2]
    return (f"  hits desk at ({pos[0] + t * direction[0]:5.1f},"
            f"{pos[1] + t * direction[1]:5.1f})cm")


def test_presets() -> None:
    """The model must agree with what the six hand-authored presets DO.

    These are the strongest evidence available without the arm in front of
    us: `home` sees the room and the user, the scan presets pan across it,
    `look_at_hands` finds hands at chest height. A model that says otherwise
    has a sign or a mount angle wrong.
    """
    print("\n== forward kinematics on the presets ==")
    for name, pose in POSES.items():
        pos, d = forward_camera(pose)
        print(
            f"  {name:14s} cam=({pos[0]:6.1f},{pos[1]:6.1f},{pos[2]:6.1f})cm "
            f"dir=({d[0]:5.2f},{d[1]:5.2f},{d[2]:5.2f}){_desk_hit(pos, d)}"
        )

    print()
    home_pos, home_dir = forward_camera(POSES["home"])
    check(
        "home camera is directly over the base",
        abs(home_pos[0]) < 0.5 and abs(home_pos[1]) < 0.5,
        f"x={home_pos[0]:.2f} y={home_pos[1]:.2f}",
    )
    check(
        "home camera height ~38cm",
        36.0 < home_pos[2] < 40.0,
        f"z={home_pos[2]:.1f}cm",
    )
    # The camera is bolted across the gripper, so with the arm straight up it
    # looks out horizontally at the room and the user rather than at the
    # ceiling. This is the fixed point CAMERA_PITCH_OFFSET_DEG is derived from.
    check(
        "home looks horizontally at the user (+y)",
        home_dir[1] > 0.99 and abs(home_dir[2]) < 0.05,
        f"dir=({home_dir[0]:.2f},{home_dir[1]:.2f},{home_dir[2]:.2f})",
    )

    hands_pos, hands_dir = forward_camera(POSES["look_at_hands"])
    check(
        "look_at_hands aims ~25 degrees below horizontal at the user",
        hands_dir[1] > 0.3 and -0.52 < hands_dir[2] < -0.32,
        f"dy={hands_dir[1]:.2f} dz={hands_dir[2]:.2f} "
        f"({math.degrees(math.asin(hands_dir[2])):.0f} deg)",
    )
    # Chest-height hands at (0, 35, 25) is the anchor llm.py publishes.
    t = (35.0 - hands_pos[1]) / hands_dir[1]
    z_at_hands = hands_pos[2] + t * hands_dir[2]
    check(
        "look_at_hands ray passes near (0, 35, 25)",
        20.0 < z_at_hands < 30.0,
        f"z={z_at_hands:.1f}cm at y=35cm",
    )

    down_pos, down_dir = forward_camera(POSES["look_down"])
    check(
        "look_down looks at least 50 degrees below horizontal",
        down_dir[2] < -0.766,
        f"dz={down_dir[2]:.2f} ({math.degrees(math.asin(down_dir[2])):.0f} deg)",
    )
    t = down_pos[2] / -down_dir[2]
    hit_y = down_pos[1] + t * down_dir[1]
    check(
        "look_down lands on the desk 15-25cm in front of the base",
        15.0 < hit_y < 25.0,
        f"desk hit y={hit_y:.1f}cm",
    )

    _, up_dir = forward_camera(POSES["look_up"])
    check(
        "look_up looks at least 45 degrees above horizontal",
        up_dir[2] > 0.707,
        f"dz={up_dir[2]:.2f} ({math.degrees(math.asin(up_dir[2])):.0f} deg)",
    )
    check(
        "look_up leans toward the user's side, not back over the base",
        up_dir[1] > 0.0,
        f"dy={up_dir[1]:.2f}",
    )

    # scan_* rotate the base only, so the lens does not move — the horizontal
    # ray sweeps instead. That only works because the camera looks sideways
    # off the gripper; along-the-axis it would just spin the image.
    sl_pos, sl_dir = forward_camera(POSES["scan_left"])
    check(
        "scan_left sweeps horizontally to the user's left (-x)",
        sl_dir[0] < -0.3 and abs(sl_dir[2]) < 0.05,
        f"dir=({sl_dir[0]:.2f},{sl_dir[1]:.2f},{sl_dir[2]:.2f})",
    )
    sr_pos, sr_dir = forward_camera(POSES["scan_right"])
    check(
        "scan_right sweeps horizontally to the user's right (+x)",
        sr_dir[0] > 0.3 and abs(sr_dir[2]) < 0.05,
        f"dir=({sr_dir[0]:.2f},{sr_dir[1]:.2f},{sr_dir[2]:.2f})",
    )
    check(
        "scan presets pan the view without moving the lens",
        abs(sl_pos[2] - sr_pos[2]) < 0.01 and abs(sl_pos[2] - home_pos[2]) < 0.01,
        f"z {home_pos[2]:.1f} / {sl_pos[2]:.1f} / {sr_pos[2]:.1f}cm",
    )


def test_conventions() -> None:
    """The servo fixed points, one assertion each.

    Every one of these mirrors a step of tests/test_direction_calibration.py,
    which was run on the real arm (2026-08-30) with the user watching from
    their normal seat. Those observations are ground truth. If one of these
    fails on hardware again, flip the sign in the matching *_servo_to_chain
    function in src/kinematics.py and nothing else.
    """
    print("\n== servo convention fixed points ==")

    # Lean the arm out horizontally so base rotation actually moves the lens.
    # elbow 0 swings the forearm down and out (ELBOW_SIGN = +1).
    out = dict(shoulder=90, elbow=0, wrist_v=90)

    fwd, _ = forward_camera(_pose(base=90, **out))
    check(
        "base 90 points forward, toward the user (+y)",
        fwd[1] > 10.0 and abs(fwd[0]) < 0.5,
        f"cam=({fwd[0]:.1f},{fwd[1]:.1f})",
    )
    # Measured on the hardware: raising the base servo pans toward the
    # user's LEFT. So 0 is the user's right and 180 is the user's left.
    right, _ = forward_camera(_pose(base=0, **out))
    check(
        "base 0 swings to the USER'S right (+x)",
        right[0] > 10.0 and abs(right[1]) < 0.5,
        f"cam x={right[0]:.1f}cm",
    )
    left, _ = forward_camera(_pose(base=180, **out))
    check(
        "base 180 swings to the USER'S left (-x)",
        left[0] < -10.0 and abs(left[1]) < 0.5,
        f"cam x={left[0]:.1f}cm",
    )

    up_pos, _ = forward_camera(_pose(base=90, shoulder=90))
    lean_pos, _ = forward_camera(_pose(base=90, shoulder=60))
    check(
        "shoulder below 90 leans toward the user",
        lean_pos[1] > up_pos[1] + 5.0,
        f"y {up_pos[1]:.1f} -> {lean_pos[1]:.1f}cm",
    )

    # Calibration steps C and D, driven from home exactly as on the hardware:
    # elbow 140 folded the forearm UP and BACK over the base, elbow 40 swung
    # it DOWN and TOWARD the user. The discriminating axis is y.
    fold_pos, _ = forward_camera(_pose(base=90, shoulder=90, elbow=140))
    check(
        "elbow 140 folds up and back over the base",
        fold_pos[1] < -10.0,
        f"cam y={fold_pos[1]:.1f}cm",
    )
    reach_pos, _ = forward_camera(_pose(base=90, shoulder=90, elbow=40))
    check(
        "elbow 40 swings down and out toward the user",
        reach_pos[1] > 10.0,
        f"cam y={reach_pos[1]:.1f}cm",
    )
    inline_pos, _ = forward_camera(_pose(base=90, shoulder=90, elbow=90))
    check(
        "elbow 90 is inline (arm stays straight up)",
        abs(inline_pos[1]) < 0.5 and inline_pos[2] > 35.0,
        f"cam=({inline_pos[1]:.2f},{inline_pos[2]:.1f})",
    )

    # Calibration steps E and F. The camera and gripper are mounted upside
    # down, so wrist_v runs OPPOSITE to the elbow: raising it drops the view.
    _, level_dir = forward_camera(_pose(base=90, wrist_v=90))
    _, wv140_dir = forward_camera(_pose(base=90, wrist_v=140))
    _, wv40_dir = forward_camera(_pose(base=90, wrist_v=40))
    check(
        "wrist_v 140 tilts the view down, toward the user",
        wv140_dir[2] < level_dir[2] - 0.5 and wv140_dir[1] > 0.3,
        f"dz {level_dir[2]:.2f} -> {wv140_dir[2]:.2f}, dy={wv140_dir[1]:.2f}",
    )
    check(
        "wrist_v 40 tilts the view up",
        wv40_dir[2] > level_dir[2] + 0.5,
        f"dz {level_dir[2]:.2f} -> {wv40_dir[2]:.2f}",
    )

    # The solver has to agree with the same convention: the public frame's
    # +x is the user's right, and the user's right is the LOW end of the
    # base servo. This is the check that fails if someone "fixes" a mirrored
    # left/right in two places at once.
    to_the_right = solve_look_at(30.0, 25.0, 10.0, POSES["home"])
    check(
        "look_at to the user's right (+x) gives base below 90",
        to_the_right["base"] < 90,
        f"base={to_the_right['base']}",
    )
    to_the_left = solve_look_at(-30.0, 25.0, 10.0, POSES["home"])
    check(
        "look_at to the user's left (-x) gives base above 90",
        to_the_left["base"] > 90,
        f"base={to_the_left['base']}",
    )


TARGETS = [
    ("user's hands, seated", (0.0, 35.0, 25.0)),
    ("desk in front of me", (0.0, 20.0, 5.0)),
    ("desk, user's right", (20.0, 20.0, 3.0)),
    ("desk, user's left", (-20.0, 20.0, 3.0)),
    ("user's face", (0.0, 45.0, 45.0)),
    ("far wall", (0.0, 150.0, 60.0)),
    ("something high up", (0.0, 25.0, 70.0)),
    ("hard left, low", (-40.0, 10.0, 8.0)),
    ("straight up overhead", (0.0, 0.0, 60.0)),
]


def test_round_trip() -> None:
    print("\n== solve_look_at -> forward_camera round trip ==")
    current = dict(POSES["home"])
    times_ms = []
    worst = 0.0
    for label, target in TARGETS:
        t0 = time.perf_counter()
        pose = solve_look_at(*target, current)
        dt = (time.perf_counter() - t0) * 1000.0
        times_ms.append(dt)

        miss = ray_miss_cm(pose, target)
        worst = max(worst, miss)
        cam, _ = forward_camera(pose)
        standoff = math.sqrt(sum((target[i] - cam[i]) ** 2 for i in range(3)))
        print(
            f"  {label:22s} -> b{pose['base']:3d} s{pose['shoulder']:3d} "
            f"e{pose['elbow']:3d} w{pose['wrist_v']:3d}  "
            f"miss={miss:5.2f}cm standoff={standoff:5.1f}cm  {dt:5.1f}ms"
        )
        check(f"aim at {label}", miss < 3.0, f"miss={miss:.2f}cm")
        check(f"standoff sane for {label}", standoff > 4.0, f"{standoff:.1f}cm")
        # Chain the poses the way a real conversation would, so the
        # warm-start / travel term gets exercised from realistic starts.
        current = pose

    print(
        f"\n  solve time: mean {sum(times_ms)/len(times_ms):.1f}ms, "
        f"max {max(times_ms):.1f}ms   worst ray miss {worst:.2f}cm"
    )
    check("solve time under 100ms", max(times_ms) < 100.0, f"max {max(times_ms):.1f}ms")


# Verbatim from LOOK_AT_TOOL's "Anchors to calibrate against" in src/llm.py.
# If these stop producing sane poses, the tool description is lying to Claude.
ANCHORS = [
    ("user's hands", (0.0, 35.0, 25.0)),
    ("desk in front", (0.0, 20.0, 5.0)),
    ("user's face", (0.0, 45.0, 45.0)),
    ("mug, user's right", (20.0, 25.0, 5.0)),
]


def test_anchors() -> None:
    print("\n== llm.py look_at anchors ==")
    for label, target in ANCHORS:
        pose = solve_look_at(*target, POSES["home"])
        pos, d = forward_camera(pose)
        print(
            f"  {label:18s} {target} -> b{pose['base']:3d} s{pose['shoulder']:3d} "
            f"e{pose['elbow']:3d} w{pose['wrist_v']:3d}  "
            f"cam=({pos[0]:5.1f},{pos[1]:5.1f},{pos[2]:5.1f})cm "
            f"dir=({d[0]:5.2f},{d[1]:5.2f},{d[2]:5.2f})"
        )
        check(f"{label} is on target", ray_miss_cm(pose, target) < 3.0,
              f"miss={ray_miss_cm(pose, target):.2f}cm")
        standoff = math.sqrt(sum((target[i] - pos[i]) ** 2 for i in range(3)))
        # No direction assertion here on purpose: for a desk anchor the
        # solver correctly picks a top-down view from above the point, so
        # "must look toward the user" would be wrong. Aim and framing
        # distance are what the anchor promises Claude.
        check(f"{label} framed from a usable distance",
              10.0 < standoff < 60.0, f"standoff={standoff:.1f}cm")

    hands = solve_look_at(0.0, 35.0, 25.0, POSES["home"])
    preset = POSES["look_at_hands"]
    delta = {k: hands[k] - preset[k] for k in ("base", "shoulder", "elbow", "wrist_v")}
    print(f"  hands solve vs look_at_hands preset: delta {delta}")
    check(
        "hands anchor solves near the look_at_hands preset",
        max(abs(v) for v in delta.values()) < 45,
        f"max joint delta {max(abs(v) for v in delta.values())} deg",
    )


def test_unreachable() -> None:
    print("\n== unreachable targets ==")
    # Points close to the base are NOT in this list any more. With the camera
    # perpendicular to the gripper the lens can stand off to one side and
    # look back over the base, so "inside my own shoulder" is now a solvable
    # top-down/reverse view rather than an error.
    #
    # Nor is "far out to one side AND high up" here any more. Aiming at a
    # distant point only asks for a DIRECTION, and the fully-extended arm can
    # point almost anywhere in the forward half-space, so distant targets
    # nearly always solve. (The one that used to be asserted here,
    # (-120, 10, 150), sat exactly on the fully-extended boundary and its
    # outcome turned on floating-point rounding — see the note in the report:
    # _feasible_d_intervals clamps d to where the arm is exactly straight,
    # and acos then lands on either side of 1.0 at random.)
    #
    # What is left is structural: below the desk, and outside the base's
    # forward half-circle.
    cases = [
        ("below the desk", (0.0, 30.0, -5.0)),
        ("behind the base", (0.0, -30.0, 20.0)),
        ("out to the side and behind the base", (-60.0, -10.0, 30.0)),
    ]
    for label, target in cases:
        try:
            pose = solve_look_at(*target, POSES["home"])
        except KinematicsError as exc:
            check(f"{label} raises", True, str(exc))
        else:
            check(f"{label} raises", False, f"returned {pose}")


def test_limits_respected() -> None:
    print("\n== joint limits on a sweep of targets ==")
    from src.arm import LIMITS

    bad = 0
    solved = 0
    for x in range(-40, 41, 10):
        for y in range(5, 61, 10):
            for z in range(0, 61, 15):
                try:
                    pose = solve_look_at(float(x), float(y), float(z), POSES["home"])
                except KinematicsError:
                    continue
                solved += 1
                for name, value in pose.items():
                    lo, hi = getattr(LIMITS, name)
                    if not lo <= value <= hi:
                        bad += 1
                if ray_miss_cm(pose, (float(x), float(y), float(z))) > 3.0:
                    bad += 1
    check(
        "grid sweep stays in limits and on target",
        bad == 0,
        f"{solved} solved, {bad} bad",
    )


def main() -> int:
    test_presets()
    test_conventions()
    test_round_trip()
    test_anchors()
    test_unreachable()
    test_limits_respected()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        return 1
    print("all kinematics checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
