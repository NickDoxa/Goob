"""Inverse kinematics for aiming the gripper-mounted camera at a point.

Phase O3 of documentation/OPTIMIZATION.md. Gives Claude a `look_at` tool that
takes a Cartesian target instead of six joint angles.

The Braccio is 5-DOF (base, shoulder, elbow, wrist_v, wrist_r). This model
is only valid at wrist_r = 90: with the perpendicular camera mount, rolling
the wrist swings the optical axis out of the arm's plane, which this chain
does not model. solve_look_at always returns wrist_r=90, so we effectively
have 4 DOF for *aiming*. Placing the camera at an exact pose (position AND
orientation) is over-constrained. We do not need that: for looking, all that
matters is that the camera's optical ray passes through the target from a
comfortable standoff. That is a 3-constraint problem (azimuth + two in-plane
constraints), which the 4 aiming joints satisfy with one DOF to spare — the
spare DOF is used to choose a comfortable, collision-free posture.

Solver choice: analytic 2-link trig, not ikpy's optimizer.
-----------------------------------------------------------------
ikpy builds the chain model and provides forward kinematics (used by
`forward_camera` and by the smoke test), but the solve itself is closed
form, because the wrist can still be *placed* directly from a choice of
aiming parameters.

The lens does not look along the gripper axis (see "Camera mount" below) —
it looks sideways off it — so the wrist pivot W, the lens C and the target T
are NOT collinear. But they are still rigidly related: C is a fixed distance
CAMERA_OFFSET_MM from W along the gripper axis, and the ray leaves C at a
fixed angle to that axis. So W, C and T form a triangle with one fixed side
and one fixed angle. Fix the direction of W->T (call its in-plane elevation
chi) and the length |W -> T| (call it D), and every remaining unknown falls
out by trigonometry: the standoff, the camera's elevation psi, and hence
wrist_v. W = T - D * (cos chi, sin chi) is then a textbook two-link planar
IK with a closed-form solution.

There is nothing left for a numerical optimizer to do except fail to
converge. The closed form is deterministic, sub-millisecond, has no local
minima, cannot return a pose that "almost" aims at the target, and lets us
enumerate the whole 1-DOF family of valid postures and pick the most
comfortable one — which a least-squares solver with an orientation_mode
cannot do. This is the fallback the plan pre-authorized, chosen on merit
rather than after failure.

Units: millimetres internally (the link dimensions are in mm), centimetres
in the public API because Claude reasons about a room in cm.
"""
from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Optional

import numpy as np

from src.arm import LIMITS, POSES

logger = logging.getLogger(__name__)


class KinematicsError(ValueError):
    """The requested target cannot be aimed at. Message is user-facing."""


# ---------------------------------------------------------------------------
# Link dimensions
# ---------------------------------------------------------------------------
# NOMINAL published TinkerKit Braccio dimensions — VERIFY WITH CALIPERS on the
# physical arm before trusting look_at accuracy (OPTIMIZATION.md O3 step 1).
# Every one of these is a direct multiplier on the aiming error.
BASE_HEIGHT_MM = 71.5    # desk surface -> shoulder (M2) pitch axis
UPPER_ARM_MM = 125.0     # shoulder (M2) axis -> elbow (M3) axis
FOREARM_MM = 125.0       # elbow (M3) axis -> wrist_v (M4) axis
WRIST_TO_TIP_MM = 192.0  # wrist_v axis -> gripper fingertips (not used for
                         # aiming; kept as the reference for the published
                         # "wrist 60 + gripper 132" breakdown)
# Distance from the wrist_v pivot to the lens, measured ALONG the gripper
# axis. The Arducam is bolted to the top of the gripper roughly one wrist
# segment out, i.e. about where the wrist_r (M5) axis is. Measure this one
# first — it is the shortest link and therefore the one most easily
# mis-estimated in relative terms, and it sets the parallax between "where
# the arm points" and "where the camera looks".
CAMERA_OFFSET_MM = 60.0

_MM_PER_CM = 10.0


# ---------------------------------------------------------------------------
# Camera mount
# ---------------------------------------------------------------------------
# The Arducam sits on TOP of the gripper, so its optical axis is roughly
# PERPENDICULAR to the gripper axis, not along it. This single fact drives
# the whole solver, so here is the evidence and the sign derivation.
#
# Fixed point 1 — `home` (all servos 90). That is the Braccio calibration
# pose: the arm stands straight up, so the gripper axis has elevation +90.
# Empirically the camera at `home` sees the ROOM and the USER, i.e. it looks
# roughly HORIZONTALLY toward the user (+y in the public frame, which is the
# outward radial direction of the arm plane at base=90). Elevation 0.
#   camera_elevation = gripper_elevation - 90   ->   90 - 90 = 0.  CORRECT.
#   camera_elevation = gripper_elevation + 90   ->   180, i.e. horizontal but
#   pointing back OVER the base, away from the user.  WRONG.
# So the offset is SUBTRACTED, and the camera looks out of the side of the
# gripper that faces the user when the arm is upright.
#
# Fixed point 2 — `scan_left` / `scan_right` are `home` with base rotation
# only. Because the camera ray is horizontal at `home`, rotating the base
# sweeps that ray around the room, which is exactly what those presets do in
# practice. (Under an along-the-axis mount they would look at the ceiling and
# base rotation would merely spin the image — which is not what the arm does.)
#
# Fixed point 3 — `look_at_hands` (shoulder 75, elbow 100, wrist_v 110) finds
# the user's hands at chest height. Under this model its ray leaves the lens
# at (y=6.9cm, z=37.1cm) angled 25 degrees down and passes through
# (y=35cm, z=24cm) — within a centimetre of the "hands = (0, 35, 25)" anchor
# published in llm.py's look_at tool description.
#
# CALIBRATION: 90 assumes the lens is exactly square to the gripper axis. If
# the mount tilts the lens slightly forward or back, tune this by the tilt
# (e.g. 80 if the lens is pitched 10 degrees toward the fingertips). The
# physical symptom is a CONSTANT elevation bias: every look_at lands the
# subject the same amount too high or too low in frame, at every distance and
# every part of the workspace. See the calibration block further down for how
# that differs from a sign error or a link-length error.
CAMERA_PITCH_OFFSET_DEG = 90.0

# Standoff = distance from the camera lens to the target. 15-30 cm keeps a
# desk-sized subject filling a useful part of the frame while staying outside
# the Vitade's close-focus limit.
STANDOFF_MIN_MM = 150.0
STANDOFF_PREFERRED_MM = 200.0
# Hard floor: never park the lens closer than 5 cm to the commanded point.
_STANDOFF_FLOOR_MM = 50.0

# Distance from the shoulder axis to the wrist_v axis. Because the elbow
# servo is limited to 0..180 (chain -90..+90 deg) and the two arm segments
# are equal length, the wrist can only live in a fairly narrow annulus:
#   |W| = 2 * UPPER_ARM * cos(elbow_chain / 2)
_WRIST_R_MAX_MM = UPPER_ARM_MM + FOREARM_MM
_WRIST_R_MIN_MM = 2.0 * UPPER_ARM_MM * math.cos(math.radians(90.0) / 2.0)

# No part of the arm may dip below this height above the desk.
_MIN_CLEARANCE_MM = 15.0

# Sweep over chi, the in-plane elevation of the vector from the WRIST PIVOT to
# the target. (The old collinear model swept the camera elevation directly;
# with the perpendicular mount the wrist-to-target direction is the parameter
# that places the wrist, and the camera elevation is recovered from it.) chi
# runs a little past the camera's own useful range because chi leads the
# camera elevation by the offset angle alpha, which is up to ~50 degrees at
# the closest allowed standoff. Coarse enough to stay fast, fine enough that
# the comfort scoring has real choices. Accuracy does NOT depend on this step:
# every candidate aims exactly at the target by construction, the sweep only
# picks the posture.
_CHI_MIN_DEG = -120.0
_CHI_MAX_DEG = 200.0
_CHI_STEP_DEG = 2.0


# ---------------------------------------------------------------------------
# Coordinate frames
# ---------------------------------------------------------------------------
# PUBLIC frame (what Claude and the look_at tool speak), origin at the arm's
# base on the desk:
#     +x = the USER'S right      (= the arm's physical LEFT)
#     +y = from the arm toward the user
#     +z = up
# This matches the user-perspective direction language in llm.py's
# MOVE_ARM_TOOL and LOOK_AT_TOOL. It is deliberately user-centric and
# therefore LEFT-handed: with y pointing at the user and z up, a
# right-handed frame would put the user's right at -x. (The user faces the
# arm, so their right really is the arm's physical left; that mirror is a
# fact about the room, not about the servos.)
#
# INTERNAL frame (the ikpy chain), same origin:
#     +x = the arm's physical right (= the user's LEFT)
#     +y = from the arm toward the user
#     +z = up
# Right-handed, so numpy/ikpy rotation matrices behave. The only difference
# from the public frame is the sign of x, so conversion both ways is a single
# mirror and is its own inverse.


def _mirror_x(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert between the public (user-centric, left-handed) frame and the
    internal (right-handed) chain frame. Self-inverse."""
    return (-x, y, z)


# ---------------------------------------------------------------------------
# Servo angle <-> chain angle conversions
# ---------------------------------------------------------------------------
# THIS IS THE PART THAT BREAKS THINGS. Every function below states the two
# fixed points it was derived from.
#
# In the internal frame, after the base rotation the arm works in a vertical
# plane. Within that plane we speak of "elevation": the angle of a segment
# measured from the outward horizontal (radially away from the base, toward
# whatever direction the base is pointing) rotating up toward +z. So
# elevation 0 = horizontal and pointing away from the base, +90 = straight
# up, -90 = straight down, 180 = horizontal and pointing back over the base.


def base_servo_to_chain(deg: float) -> float:
    """base servo -> rotation about +z, measured CCW from the internal +x.

    Derivation, from hardware (2026-08-30 left/right calibration on the real
    arm): servo 90 = forward, toward the user = internal +y = pi/2. Raising
    the servo swings the view toward the USER'S LEFT, and the user's left is
    the arm's physical right = internal +x. So servo 180 = internal +x =
    0 rad, and servo 0 = the user's right = internal -x = pi. Unit slope,
    negative sign, 180 offset:

        chain_deg = 180 - servo_deg

    Only the SIGN of the base yaw lives here. The public<->internal x mirror
    lives in _mirror_x and is a separate fact (the public frame is stated
    from the user's point of view, the chain frame from the arm's). Fixing a
    mirrored left/right in both places at once is the classic way to get a
    bot that confidently looks the wrong way.
    """
    return math.radians(180.0 - deg)


def chain_to_base_servo(rad: float) -> float:
    return 180.0 - math.degrees(rad)


def shoulder_servo_to_chain(deg: float) -> float:
    """shoulder servo -> upper-arm elevation.

    Derivation: servo 90 = upright, i.e. elevation +90. Lower values lean
    toward the user, i.e. toward elevation 0 (horizontal, pointing away from
    the base along the direction the base faces). Two points, unit slope, no
    offset: elevation_deg == servo_deg.
    """
    return math.radians(deg)


def chain_to_shoulder_servo(rad: float) -> float:
    return math.degrees(rad)


# Signs of the elbow and wrist_v servos' contributions to elevation.
#
# BOTH come straight off the hardware ground-truth run of
# tests/test_direction_calibration.py (2026-08-30), which drove raw servo
# values with no model in the loop while the user watched from their normal
# seat facing the arm. These observations outrank every derivation:
#
#   * elbow 140 -> the forearm folds UP and BACK over the base.
#     elbow 40  -> it swings DOWN and TOWARD the user.
#     So raising the servo RAISES the forearm relative to the upper arm:
#     elbow_rel = +(servo - 90). From `home` (all 90, upper arm straight up
#     at elevation +90), elbow 140 gives forearm elevation 140 — past
#     vertical, back over the base — which is exactly what was observed.
#     Hence +1.
#
#   * wrist_v 140 -> the gripper/camera tips TOWARD the user and DOWN.
#     wrist_v 40  -> it tips AWAY and UP.
#     So raising the servo LOWERS the gripper axis:
#     wrist_rel = -(servo - 90). From `home`, wrist_v 140 puts the gripper
#     axis at elevation 90 - 50 = 40, i.e. the camera (gripper elevation
#     minus 90) at -50: fifty degrees below horizontal, pointing at the
#     user. Hence -1. The camera+gripper assembly is mounted upside down,
#     which is the physical reason this runs opposite to the elbow.
#
# An earlier revision of this file had both of these inverted, derived from
# the Braccio library's stock demo poses rather than from the arm. The arm
# wins. If a future calibration disagrees, flip the relevant constant here
# and nothing else — every other use is expressed through the four
# conversion functions below.
ELBOW_SIGN = 1.0
WRIST_V_SIGN = -1.0


def elbow_servo_to_chain(deg: float) -> float:
    """elbow servo -> forearm elevation RELATIVE to the upper arm.

    Derivation: servo 90 = forearm inline with the upper arm = relative 0.
    Servo 140 = folded up and back over the base = relative +50 (see
    ELBOW_SIGN above). Servo 40 = swung down and out toward the user = -50.
    """
    return math.radians(ELBOW_SIGN * (deg - 90.0))


def chain_to_elbow_servo(rad: float) -> float:
    return 90.0 + math.degrees(rad) / ELBOW_SIGN


def wrist_v_servo_to_chain(deg: float) -> float:
    """wrist_v servo -> gripper axis elevation RELATIVE to the forearm.

    Derivation: servo 90 = gripper inline with the forearm = relative 0.
    Servo 140 tips the gripper — and therefore the camera bolted upside down
    to it — TOWARD the user and DOWN, i.e. decreases elevation, so
    relative_deg == -(servo_deg - 90). Servo 40 tips it away and up.
    """
    return math.radians(WRIST_V_SIGN * (deg - 90.0))


def chain_to_wrist_v_servo(rad: float) -> float:
    return 90.0 + math.degrees(rad) / WRIST_V_SIGN


# CALIBRATION NOTE — read before chasing a look_at that aims wrong.
#
# The conversions above encode the Braccio servo conventions this model was
# derived from. tests/test_kinematics_smoke.py asserts each of their fixed
# points AND validates all six arm.py presets against the model, so the arm
# has several independent ways to contradict it. Symptom -> cause:
#
#   * Every look_at is off by the SAME elevation, everywhere, at every
#     distance, and `home` does not look level -> CAMERA_PITCH_OFFSET_DEG.
#     Nudge it by the observed bias (a subject that lands high in frame means
#     the camera points lower than modelled, so DECREASE the constant).
#   * Aiming error grows with how far the arm is from `home`, and the arm
#     folds the wrong way (reaching back when it should reach out) -> a sign
#     error, i.e. ELBOW_SIGN / WRIST_V_SIGN, or the shoulder convention.
#     Signs fail loudly and geometrically; they never look "a bit off".
#     This HAPPENED (2026-08-30): both pitch signs were inverted, derived
#     from the Braccio library's stock demo poses. The hardware run of
#     tests/test_direction_calibration.py settled them; see the block above
#     ELBOW_SIGN for the observations.
#   * Aiming is directionally right but consistently overshoots or
#     undershoots, worse for distant targets and better for near ones ->
#     link lengths. Measure BASE_HEIGHT/UPPER_ARM/FOREARM/CAMERA_OFFSET with
#     calipers; CAMERA_OFFSET_MM is the likeliest culprit because it is the
#     shortest and the only one not published by TinkerKit.
#   * Left/right mirrored -> _mirror_x or base_servo_to_chain, and fix it in
#     exactly ONE of them. This HAPPENED (2026-08-30): "look left" panned to
#     the user's right. The fix went into base_servo_to_chain, because the
#     wrong claim was about the servo ("servo 0 = the arm's physical right"),
#     not about either frame — both frame definitions above are unchanged,
#     the chain frame stays right-handed, and _mirror_x stays a pure mirror.
#     The base servo simply counts the other way around +z than assumed.
#
# Servo-angle summary, for the record:
#
#   joint     servo range   chain angle (rad)          chain 0 means
#   -------   -----------   ------------------------   ---------------------
#   base      0..180        radians(180 - servo)       arm's physical right
#                                                      (= the user's LEFT,
#                                                       i.e. base servo 180)
#   shoulder  15..165       radians(servo)             upper arm horizontal
#   elbow     0..180        radians(servo - 90)        forearm inline
#   wrist_v   0..180        radians(90 - servo)        gripper inline
#   wrist_r   0..180        (not in the chain)         camera roll only
#   gripper   10..73        (not in the chain)         no effect on aim


_CHAIN_JOINTS = ("base", "shoulder", "elbow", "wrist_v")
_SERVO_TO_CHAIN = {
    "base": base_servo_to_chain,
    "shoulder": shoulder_servo_to_chain,
    "elbow": elbow_servo_to_chain,
    "wrist_v": wrist_v_servo_to_chain,
}


# ---------------------------------------------------------------------------
# The camera offset, resolved into the ray's own frame
# ---------------------------------------------------------------------------
# Work in the arm's vertical plane, coordinates (r, z): r outward along the
# direction the base faces, z up. Let
#     theta3 = gripper axis elevation
#     psi    = camera optical elevation = theta3 - CAMERA_PITCH_OFFSET_DEG
#     u      = (cos psi, sin psi)          the optical ray direction
#     u_perp = (-sin psi, cos psi)         u rotated +90 (CCW)
#     g      = (cos theta3, sin theta3)    the gripper axis
# Because theta3 = psi + P, the gripper axis is just u rotated by P:
#     g = cos(P) * u + sin(P) * u_perp
# The lens sits at C = W + CAMERA_OFFSET * g, and the target is s (the
# standoff) further along the ray: T = C + s * u. Substituting,
#     T - W = (s + K cos P) * u  +  (K sin P) * u_perp        [K = CAMERA_OFFSET]
# so the wrist-to-target vector has
#     length  D     = hypot(s + K cos P, K sin P)
#     bearing chi   = psi + alpha,   alpha = atan2(K sin P, s + K cos P)
# At P = 90 this is the clean right triangle: one leg s along the ray, the
# other leg K straight up the gripper axis, and alpha is the parallax between
# "where the wrist is aimed" and "where the camera is looking".
#
# The solver sweeps (chi, D) and inverts the pair above to recover s and psi,
# which is what makes the wrist placement closed form.
_P_RAD = math.radians(CAMERA_PITCH_OFFSET_DEG)
_CAM_ALONG_MM = CAMERA_OFFSET_MM * math.cos(_P_RAD)  # K cos P (~0 at P=90)
_CAM_PERP_MM = CAMERA_OFFSET_MM * math.sin(_P_RAD)   # K sin P (~K at P=90)


def _standoff_alpha(d: float) -> Optional[tuple[float, float]]:
    """Invert D -> (standoff, alpha). None if D is too short to be a triangle.

    From D^2 = (s + K cos P)^2 + (K sin P)^2, taking the s > -K cos P root
    (the other root puts the target BEHIND the lens).
    """
    par_sq = d * d - _CAM_PERP_MM * _CAM_PERP_MM
    if par_sq <= 0.0:
        return None
    par = math.sqrt(par_sq)
    return par - _CAM_ALONG_MM, math.atan2(_CAM_PERP_MM, par)


def _d_for_standoff(s: float) -> float:
    return math.hypot(s + _CAM_ALONG_MM, _CAM_PERP_MM)


_D_FLOOR_MM = _d_for_standoff(_STANDOFF_FLOOR_MM)
_D_PREFERRED_MM = _d_for_standoff(STANDOFF_PREFERRED_MM)


@lru_cache(maxsize=1)
def braccio_chain() -> Chain:
    """The Braccio as an ikpy Chain, ending at the camera lens.

    Built programmatically (no URDF file). Lengths are in metres, which is
    what ikpy's numerical defaults assume; the mm constants above are the
    single source of truth and are converted here.

    Link order: origin, base yaw, shoulder pitch, elbow pitch, wrist_v pitch,
    fixed camera offset. At the all-zeros chain configuration the arm is
    fully extended horizontally along the internal +x axis, so the
    end-effector frame's local +x is always the GRIPPER axis (not the optical
    axis — see forward_camera for the pitch offset that separates them).
    """
    # Deferred import: ikpy drags in scipy + sympy (~800ms and a heavy
    # aarch64 wheel dependency). solve_look_at is pure trig and never needs
    # it — only forward_camera (used by the smoke test) builds the chain,
    # so the bot's runtime path stays numpy-only.
    from ikpy.chain import Chain
    from ikpy.link import OriginLink, URDFLink

    m = 1.0 / 1000.0
    pitch_axis = [0.0, -1.0, 0.0]  # +theta about -y raises local +x toward +z

    def bounds(name: str) -> tuple[float, float]:
        lo, hi = getattr(LIMITS, name)
        a = _SERVO_TO_CHAIN[name](lo)
        b = _SERVO_TO_CHAIN[name](hi)
        # A negative sign constant inverts the servo->chain ordering (wrist_v
        # does exactly that); ikpy wants (min, max).
        return (min(a, b), max(a, b))

    links = [
        OriginLink(),
        URDFLink(
            name="base",
            origin_translation=np.array([0.0, 0.0, 0.0]),
            origin_orientation=np.array([0.0, 0.0, 0.0]),
            rotation=np.array([0.0, 0.0, 1.0]),
            bounds=bounds("base"),
        ),
        URDFLink(
            name="shoulder",
            origin_translation=np.array([0.0, 0.0, BASE_HEIGHT_MM * m]),
            origin_orientation=np.array([0.0, 0.0, 0.0]),
            rotation=np.array(pitch_axis),
            bounds=bounds("shoulder"),
        ),
        URDFLink(
            name="elbow",
            origin_translation=np.array([UPPER_ARM_MM * m, 0.0, 0.0]),
            origin_orientation=np.array([0.0, 0.0, 0.0]),
            rotation=np.array(pitch_axis),
            bounds=bounds("elbow"),
        ),
        URDFLink(
            name="wrist_v",
            origin_translation=np.array([FOREARM_MM * m, 0.0, 0.0]),
            origin_orientation=np.array([0.0, 0.0, 0.0]),
            rotation=np.array(pitch_axis),
            bounds=bounds("wrist_v"),
        ),
        URDFLink(
            name="camera",
            origin_translation=np.array([CAMERA_OFFSET_MM * m, 0.0, 0.0]),
            origin_orientation=np.array([0.0, 0.0, 0.0]),
            joint_type="fixed",
        ),
    ]
    # ikpy requires the first and last links to be inactive.
    mask = [False, True, True, True, True, False]
    return Chain(links, active_links_mask=mask, name="braccio_camera")


def pose_to_chain_angles(pose: dict) -> list[float]:
    """Six chain values (origin + 4 joints + fixed camera) for a servo pose."""
    return (
        [0.0]
        + [_SERVO_TO_CHAIN[j](float(pose[j])) for j in _CHAIN_JOINTS]
        + [0.0]
    )


# The optical axis expressed in the end-effector's own frame. That frame's
# local +x is the gripper axis (elevation theta3) and its local +z is the
# gripper axis rotated +90 in elevation, so a direction at elevation
# theta3 + e is cos(e) * x_local + sin(e) * z_local. The camera sits at
# e = -CAMERA_PITCH_OFFSET_DEG.
_CAM_DIR_LOCAL = np.array([math.cos(_P_RAD), 0.0, -math.sin(_P_RAD)])


def forward_camera(pose: dict) -> tuple[tuple[float, float, float],
                                        tuple[float, float, float]]:
    """Where the camera is and where it looks, for a full servo pose.

    Returns ((x_cm, y_cm, z_cm), (dx, dy, dz)) in the PUBLIC frame, with the
    direction a unit vector along the optical axis. The lens position is on
    the gripper axis, CAMERA_OFFSET_MM past the wrist pivot; only the
    direction carries the perpendicular mount offset. gripper is ignored.
    wrist_r is also ignored, which means the result is only correct for
    poses with wrist_r = 90: under the perpendicular mount, rolling the
    wrist swings the optical axis out of the arm's plane, which this chain
    does not model.
    """
    frame = braccio_chain().forward_kinematics(pose_to_chain_angles(pose))
    pos_m = frame[:3, 3]
    dir_int = frame[:3, :3] @ _CAM_DIR_LOCAL
    pos_cm = _mirror_x(*(pos_m * 100.0))
    direction = _mirror_x(*dir_int)
    return pos_cm, direction


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------

def _two_link_ik(wr: float, wz: float) -> list[tuple[float, float]]:
    """Planar 2-link IK for the wrist point, in the arm's vertical plane.

    (wr, wz) is the wrist_v axis relative to the shoulder axis: wr along the
    outward horizontal, wz up. Returns [(theta1, elbow_rel), ...] in radians
    for the elbow-back and elbow-forward branches, theta1 being the upper
    arm's elevation and elbow_rel the forearm's elevation relative to it.
    """
    r2 = wr * wr + wz * wz
    cos_rel = (r2 - UPPER_ARM_MM ** 2 - FOREARM_MM ** 2) / (2 * UPPER_ARM_MM * FOREARM_MM)
    if cos_rel < -1.0 or cos_rel > 1.0:
        return []
    rel = math.acos(cos_rel)
    out = []
    for signed_rel in (rel, -rel):
        phi = math.atan2(wz, wr)
        theta1 = phi - math.atan2(
            FOREARM_MM * math.sin(signed_rel),
            UPPER_ARM_MM + FOREARM_MM * math.cos(signed_rel),
        )
        out.append((theta1, signed_rel))
    return out


def _d_window(b: float, c: float, radius: float) -> Optional[float]:
    """Half-width of the D interval where |T - D*u| == radius, or None.

    b = T . u and c = |T|^2, so |T - D u|^2 = D^2 - 2 b D + c.
    """
    disc = b * b - c + radius * radius
    if disc < 0.0:
        return None
    return math.sqrt(disc)


def _feasible_d_intervals(b: float, c: float) -> list[tuple[float, float]]:
    """D ranges that put the wrist inside its reachable annulus."""
    s_max = _d_window(b, c, _WRIST_R_MAX_MM)
    if s_max is None:
        return []
    lo, hi = b - s_max, b + s_max
    s_min = _d_window(b, c, _WRIST_R_MIN_MM)
    if s_min is None:
        spans = [(lo, hi)]
    else:
        spans = [(lo, b - s_min), (b + s_min, hi)]
    out = []
    for a, z in spans:
        a = max(a, _D_FLOOR_MM)
        if z > a:
            out.append((a, z))
    return out


def _limit_cost(name: str, value: float) -> Optional[float]:
    lo, hi = getattr(LIMITS, name)
    if value < lo - 0.5 or value > hi + 0.5:
        return None
    span = hi - lo
    mid = (lo + hi) / 2.0
    centering = ((value - mid) / (span / 2.0)) ** 2
    margin = min(value - lo, hi - value)
    crowding = max(0.0, 1.0 - margin / 12.0) ** 2
    return 0.35 * centering + 1.4 * crowding


def _evaluate(chi: float, d: float, tr: float, tz: float,
              base_deg: float, current: dict) -> Optional[tuple[float, dict]]:
    """Score one (wrist bearing, wrist-to-target distance) candidate.

    chi is the elevation of the vector from the wrist pivot to the target and
    d its length, so the wrist pivot is pinned at W = T - d * (cos chi,
    sin chi). Inverting the offset triangle gives the standoff and the camera
    elevation psi; the gripper must then sit at psi + CAMERA_PITCH_OFFSET,
    which fixes wrist_v. Returns (cost, servo angles) or None if invalid.
    """
    resolved = _standoff_alpha(d)
    if resolved is None:
        return None
    standoff, alpha = resolved
    psi = chi - alpha
    theta3 = psi + _P_RAD

    ur, uz = math.cos(chi), math.sin(chi)
    wr = tr - d * ur
    wz = tz - d * uz

    best: Optional[tuple[float, dict]] = None
    for theta1, elbow_rel in _two_link_ik(wr, wz):
        theta2 = theta1 + elbow_rel
        angles = {
            "base": base_deg,
            "shoulder": math.degrees(theta1),
            "elbow": chain_to_elbow_servo(elbow_rel),
            "wrist_v": chain_to_wrist_v_servo(theta3 - theta2),
        }
        cost = 0.0
        rejected = False
        for name, value in angles.items():
            c = _limit_cost(name, value)
            if c is None:
                rejected = True
                break
            cost += c
        if rejected:
            continue

        # Desk clearance for the elbow, the wrist and the lens itself. The
        # lens rides on the gripper axis, so its height follows theta3.
        elbow_z = BASE_HEIGHT_MM + UPPER_ARM_MM * math.sin(theta1)
        wrist_z = BASE_HEIGHT_MM + wz
        cam_z = wrist_z + CAMERA_OFFSET_MM * math.sin(theta3)
        if min(elbow_z, wrist_z, cam_z) < _MIN_CLEARANCE_MM:
            continue

        cost += abs(standoff - STANDOFF_PREFERRED_MM) / 150.0
        cost += 2.0 * max(0.0, STANDOFF_MIN_MM - standoff) / STANDOFF_MIN_MM
        travel = sum(
            abs(angles[j] - float(current[j])) for j in _CHAIN_JOINTS
        ) / (4 * 180.0)
        cost += 0.8 * travel

        if best is None or cost < best[0]:
            best = (cost, angles)
    return best


def _aim_wrist_v(tr: float, tz: float, wr: float, wz: float,
                 theta2: float) -> float:
    """wrist_v (degrees) that puts the optical ray through the target.

    With the wrist pivot already fixed at (wr, wz), the wrist-to-target
    bearing chi and distance d are known, so psi = chi - alpha(d) and the
    gripper must sit at theta3 = psi + P.
    """
    d = math.hypot(tr - wr, tz - wz)
    chi = math.atan2(tz - wz, tr - wr)
    resolved = _standoff_alpha(d)
    # Degenerate only if the target is inside the lens' own offset circle,
    # which the standoff floor already rules out for solver output; fall back
    # to the extreme (alpha = 90) rather than raising from a rounding fixup.
    alpha = resolved[1] if resolved is not None else math.pi / 2.0
    return chain_to_wrist_v_servo(chi - alpha + _P_RAD - theta2)


def solve_look_at(x_cm: float, y_cm: float, z_cm: float,
                  current_pose: dict) -> dict:
    """Six servo angles that put the camera's optical ray through a target.

    Coordinates are in the public frame documented at the top of this module:
    origin at the arm's base on the desk, +x to the USER'S right, +y from the
    arm toward the user, +z up, centimetres.

    wrist_r is forced to 90 (the camera-upright baseline) and gripper is
    carried over from current_pose. Raises KinematicsError with a
    human-readable reason if no aiming pose exists.
    """
    current = dict(POSES["home"])
    current.update({k: v for k, v in (current_pose or {}).items() if k in current})

    if z_cm < 0.0:
        raise KinematicsError(
            f"target is below the desk (z={z_cm:.0f}cm); the desk surface is z=0"
        )

    xi, yi, zi = _mirror_x(x_cm * _MM_PER_CM, y_cm * _MM_PER_CM, z_cm * _MM_PER_CM)
    ground = math.hypot(xi, yi)

    if ground < 5.0:
        # Straight above the base: azimuth is undefined, keep where we are.
        base_deg = float(current["base"])
    else:
        base_deg = chain_to_base_servo(math.atan2(yi, xi))
        lo, hi = LIMITS.base
        if base_deg < lo - 5.0 or base_deg > hi + 5.0:
            raise KinematicsError(
                "that is behind my base — I can only swing through the half of "
                "the room in front of me"
            )
        base_deg = max(float(lo), min(float(hi), base_deg))

    # Project the target into the arm's vertical plane. With base_deg exactly
    # on the target's azimuth this is just the horizontal distance; the cosine
    # keeps it honest when the base had to be clamped.
    base_rad = base_servo_to_chain(base_deg)
    tr = xi * math.cos(base_rad) + yi * math.sin(base_rad)
    tz = zi - BASE_HEIGHT_MM

    c = tr * tr + tz * tz
    best: Optional[tuple[float, dict]] = None
    geometrically_possible = False

    steps = int(round((_CHI_MAX_DEG - _CHI_MIN_DEG) / _CHI_STEP_DEG))
    for i in range(steps + 1):
        chi = math.radians(_CHI_MIN_DEG + i * _CHI_STEP_DEG)
        b = tr * math.cos(chi) + tz * math.sin(chi)
        for d_lo, d_hi in _feasible_d_intervals(b, c):
            geometrically_possible = True
            d = min(max(_D_PREFERRED_MM, d_lo), d_hi)
            found = _evaluate(chi, d, tr, tz, base_deg, current)
            if found is not None and (best is None or found[0] < best[0]):
                best = found

    if best is None:
        dist_cm = math.sqrt(c + 0.0) / _MM_PER_CM
        reach_cm = (_WRIST_R_MAX_MM + CAMERA_OFFSET_MM) / _MM_PER_CM
        if not geometrically_possible:
            raise KinematicsError(
                f"that point is {dist_cm:.0f}cm from my shoulder — too close in "
                f"to fold around and look at; my arm only folds down to about "
                f"{_WRIST_R_MIN_MM / _MM_PER_CM:.0f}cm of wrist reach"
            )
        raise KinematicsError(
            f"I can't find a pose that aims at that without hitting a joint "
            f"limit (target is {dist_cm:.0f}cm from my shoulder; my camera "
            f"reaches about {reach_cm:.0f}cm)"
        )

    angles = best[1]
    pose = {
        "base": int(round(angles["base"])),
        "shoulder": int(round(angles["shoulder"])),
        "elbow": int(round(angles["elbow"])),
        "wrist_v": int(round(angles["wrist_v"])),
        "wrist_r": 90,
        "gripper": int(current["gripper"]),
    }

    # Re-aim after rounding. Rounding base/shoulder/elbow to whole degrees
    # moves the wrist pivot by a millimetre or two; wrist_v can absorb that
    # exactly, because once the pivot is known there is a unique gripper
    # elevation whose perpendicular optical ray passes through the target.
    base_rad = base_servo_to_chain(pose["base"])
    tr = xi * math.cos(base_rad) + yi * math.sin(base_rad)
    theta1 = shoulder_servo_to_chain(pose["shoulder"])
    theta2 = theta1 + elbow_servo_to_chain(pose["elbow"])
    wr = UPPER_ARM_MM * math.cos(theta1) + FOREARM_MM * math.cos(theta2)
    wz = UPPER_ARM_MM * math.sin(theta1) + FOREARM_MM * math.sin(theta2)
    pose["wrist_v"] = int(round(_aim_wrist_v(tr, tz, wr, wz, theta2)))

    for name in ("base", "shoulder", "elbow", "wrist_v", "wrist_r", "gripper"):
        lo, hi = getattr(LIMITS, name)
        pose[name] = max(lo, min(hi, pose[name]))

    logger.debug(
        "look_at (%.0f, %.0f, %.0f)cm -> %s", x_cm, y_cm, z_cm, pose
    )
    return pose
