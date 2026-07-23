"""bd protocol — Start → [per-tube split chain] ×tube_count → Park.

Each tube t (rack slot, cap-holder slot and tip slot all share index t)
moves through a SPLIT chain of small BT actions, one recipe unit per
action, threaded by facts (the BT moves action→action as each eff is
asserted). If a step fails, the leaf fails THERE — the planner replans
from observed state and re-selects exactly the step that didn't happen
(pattern: the apc project / examples/scale).

Order (customer-specified): decap first, then pipette, then re-cap,
and only then weigh / inspect / scan — so every measurement is taken
on the FINISHED tube.

  Tube gripper (tool = "gripper") — open the tube:
    1. Pick           pick the tube from the falcon rack
    1b. PrintLabel    print + apply the barcode label on the printer —
                      ONLY when the ``print_label`` launch kwarg is on
                      (see setup(): with it off every tube starts already
                      ``printed``, so the planner skips straight to Decap)
    2. Decap          place in the decapper + unscrew (cap → gripper)
    3. ParkCap        park the cap at index t on the cap holder
    4. RetrieveTube   pick the (open) tube back out of the decapper
    5. Return         return it to its rack slot, open, ready to dose

  Pipettor (tool = "pipettor", swap handled by the planner):
    6. PickTip        fresh tip t from the tip rack
    7. Aspirate       draw the dose from the D5 reservoir (one physical
                      unit: immerse → aspirate → retract)
    8. Dispense       immerse → dispense → retract into tube t
    9. EjectTip       drop the used tip into the waste bin

  Tube gripper again — re-cap (the printer project is the reference for
  the cap(exit=False) + pick(approach=False) pair):
   10. PickForCap     pick the (open, dosed) tube off the rack
   11. PlaceInDecapper seat it in the decapper
   12. PickCap        pick its own cap back off the cap holder
   13. Cap            screw the cap on + lift the capped tube out —
                      the capped tube stays IN THE GRIPPER, which is
                      why the measurements below need no extra pick

  Measure the finished tube (still tool = "gripper"):
   14. PlaceOnScale   release it on the scaletop
   15. Weigh          read the settled weight — NO motion, a pure device
                      read (declarative retry, see below)
   16. PickFromScale  re-grip the tube off the scaletop
   17. Inspect        present to the camera + detect
   18. Scan           present to the barcode reader + read the code
   19. ReturnCapped   return the capped tube to its rack slot

Slot D5 of the falcon rack is the RESERVOIR: an open (uncapped) tube the
pipettor aspirates every dose from. It is never picked, weighed, decapped
or re-capped — ``tube_count`` is clamped so the processed tubes never
reach it (see ``MAX_TUBES``).

Device reads + declarative retry (project-guide §8): ``Weigh`` / ``Scan``
assert their fact ONLY on a valid reading and ``return False`` otherwise
(``Inspect`` converts ``CameraUnavailableError`` the same way). No retry
loop anywhere — a False leaf replans from observed state, the planner
re-selects the same read, and a ``critical`` device that is down has
already paused the runtime until the operator fixes it and resumes.

``tube_count`` (launch kwarg, default 1) sets how many tubes to run.
Each tube ends back in its own rack slot with its OWN cap screwed back
on: the cap holder is a temporary park indexed by tube, so step 17
picks up exactly the cap step 8 put down.
"""

from __future__ import annotations

from workspace.bt import Action, predicate
from workspace.components.inspection.vision_station import CameraUnavailableError


# ── Per-tube facts (the action chain) ─────────────────────────────────
started    = predicate("started")
picked     = predicate("picked")      # tube in the gripper (off the rack)
printed    = predicate("printed")     # barcode label printed + applied
on_scale   = predicate("on_scale")    # tube released on the scaletop
weighed    = predicate("weighed")     # a valid weight was read
off_scale  = predicate("off_scale")   # tube re-gripped off the scaletop
inspected  = predicate("inspected")   # camera present + detect ran
scanned    = predicate("scanned")     # barcode read
decapped   = predicate("decapped")    # tube in decapper, cap on gripper
cap_parked = predicate("cap_parked")  # cap parked at holder index t
retrieved  = predicate("retrieved")   # open tube back in the gripper
returned   = predicate("returned")    # tube back in its rack slot
tipped     = predicate("tipped")      # fresh tip on the pipettor
aspirated  = predicate("aspirated")   # dose drawn from the D5 reservoir
dispensed  = predicate("dispensed")   # volume dispensed into the tube
ejected    = predicate("ejected")     # used tip in the waste bin
# Re-cap chain — runs on the gripper once the tube has been dosed.
recap_held  = predicate("recap_held")   # dosed tube back in the gripper
in_decapper = predicate("in_decapper")  # dosed tube seated in the decapper
cap_held    = predicate("cap_held")     # its own cap back in the gripper
capped      = predicate("capped")       # cap screwed on, tube in the gripper
recapped    = predicate("recapped")     # capped tube back in its slot (= done)
parked     = predicate("parked")

# ── Single-occupancy resources (capacity-1, no args) ──────────────────
# One gripper, one scaletop seat, one decapper, one pipettor nozzle —
# each fact is consumed (-) when its slot fills and restored (+) when
# it empties. See project-guide §8.
#
# capacity=True: these facts are shared MUTUAL-EXCLUSION signals, not
# causal ones — the same gripper toggles hand_empty across every
# tube, not just one. Without the flag, the scheduler ties precedence
# to whichever tube's action the plan's own linearization happened to
# set the fact last, which welds every tube's chain into one serial
# sequence the moment a tube revisits a tool twice (this project's
# re-cap chain sends the gripper back for a second visit after the
# pipettor phase) — batching collapses into full one-tube-at-a-time
# execution. capacity=True keeps the fact's mutual exclusion (no two
# tubes ever share the slot) while letting the scheduler interleave
# tubes to cluster by tool again. See dsl.py's module docstring.
hand_empty    = predicate("hand_empty", capacity=True)     # gripper holds nothing
scale_free    = predicate("scale_free", capacity=True)     # scaletop seat is empty
decapper_free = predicate("decapper_free", capacity=True)  # decapper holds no tube
nozzle_free   = predicate("nozzle_free", capacity=True)    # pipettor carries no tip


# Component names — slot lists are read from these at runtime.
FALCON_RACK = "rack_falcon_15ml_1"
CAP_HOLDER  = "capholder_falcon_15ml_1"
TIP_RACK    = "rack_tip"

# Label printing (launch kwarg ``print_label``). The label carries the
# tube's own rack slot, so the barcode Scan step later in the chain reads
# back exactly what was printed here.
LABEL_PREFIX = "BD-"
LABEL_CODE   = "code128"   # what the Zebra DS457 reads back
PRINTER_GRAV_OFFSET = 4    # mm — press onto the applicator pad

# Pipetting parameters
IMMERSE_DEPTH = 20     # mm below the tube top
# soft_approach=False on the two immerse calls. With it True, touch()
# stops DEAD at the gap point and runs the last leg as its own slow
# lmove ("the deliberate slow insertion IS the feature" — recipe.py's
# stop rule). That halt sat 1.7 mm over the rim at the default gap=2, so
# nobody saw it; raising gap to get real clearance just lifted the same
# halt into open air and the arm visibly froze in midair before creeping
# down. False drops the gap point entirely: one continuous descent from
# a_pad (padding=50 above contact) straight in, S-curve decelerating to
# zero at the target. ``gap`` is not read at all in this mode.
IMMERSE_SOFT_APPROACH = False
VOL_UL        = 400    # microliters aspirated from D5 → dispensed per tube
# Reservoir: the rack slot holding the OPEN (uncapped) source tube every
# dose is drawn from. The rack's slot list is row-major (A1..A5, B1..B5,
# C1..C5, D1..D5), so D5 is the last of the 20 — reserving it leaves
# MAX_TUBES slots for the processed tubes and keeps the two disjoint.
SOURCE_SLOT   = "D5"
MAX_TUBES     = 19

# Motion parameters
GRAV_OFFSET   = 4      # mm — gravity press applied on every release
SCALE_GRAV_OFFSET = GRAV_OFFSET + 5   # mm — extra press to seat the tube on the scaletop
# TCP over-drive on the two rigid-jaw picks. The decapper pick is a jaw
# grip on a screwed-on cap, so Decapper.pick sets compliant=False and the
# over-drive folds into the attach offset — the tube really does sit
# lower on the tool (the printer project used the same -1).
DECAPPER_TCP_Z_OFFSET  = -1   # mm — bite lower lifting a tube out of the decapper
CAPHOLDER_TCP_Z_OFFSET = 1    # mm — bite higher on a cap parked in the holder
# Planner gravity constraint for planned travel hops while carrying:
# keep the payload within 45 deg of upright. The platform falls back
# to an unconstrained (still collision-checked) plan if unsatisfiable.
MOTION_PLAN_GRAVITY = {"gravity_vec": [0, 0, 1], "gravity_thr": 15, "planner": "aitstar", "time_limit_sec": 10}
# Present pose tweak for camera + barcode, xyzabc in the station's
# "place"-anchor frame: 50 mm lower in z.
PRESENT_OFFSET = [0, 0, -50, 0, 0, 0]

# Execution order — _progress_pct counts each tube's FURTHEST entry,
# so this tuple must stay in the order the actions actually run.
# ``printed`` is only in the chain when the print_label kwarg is on;
# setup() picks the matching tuple so the bar is honest either way.
_CHAIN_PRINT = (picked, printed, decapped, cap_parked, retrieved, returned,
                tipped, aspirated, dispensed, ejected,
                recap_held, in_decapper, cap_held, capped,
                on_scale, weighed, off_scale, inspected, scanned, recapped)
_CHAIN_PLAIN = tuple(p for p in _CHAIN_PRINT if p is not printed)

_CHAIN  = _CHAIN_PLAIN       # rebound by setup()
_STEPS  = len(_CHAIN)        # per-tube steps for progress


def _slot(action, tube):
    return action.ctx.workspace.components[FALCON_RACK].slot["body"][tube]


def _cap_slot(action, tube):
    return action.ctx.workspace.components[CAP_HOLDER].slot["body"][tube]


def _tip(action, tube):
    return action.ctx.workspace.components[TIP_RACK].slot["body"][tube]


def _progress_pct(action) -> int:
    """Monotonic % over all per-tube steps. This action's eff hasn't
    applied yet, so count it as +1.

    Counts how FAR each tube has got in the chain (its highest set
    predicate), not how many chain facts it currently holds. The two
    differ because ``cap_parked`` is CONSUMED by PickCap when the cap
    leaves the holder: a plain fact count nets zero for that step
    (cap_held added, cap_parked dropped), so the bar stalls for one
    step and tops out at 95% instead of 100%.
    """
    tubes = action._ctx_all_objects().get("tube", [])
    total = (len(tubes) or 1) * _STEPS
    ctx_state = getattr(action.ctx, "state", None) or {}
    facts = ctx_state.get("facts") or set()
    done = 0
    for t in tubes:
        reached = 0
        for i, p in enumerate(_CHAIN):
            if (p.name, t) in facts:
                reached = i + 1
        done += reached
    return int((done + 1) / total * 100)


def setup(**kwargs):
    global _CHAIN, _STEPS

    # Clamp: slot D5 is the reservoir, so it must never be handed out as
    # a processed tube (see SOURCE_SLOT).
    tube_count = max(0, min(int(kwargs.get("tube_count", 1)), MAX_TUBES))
    tubes = list(range(tube_count))

    # Label printing is a launch-time choice, so it is expressed in the
    # INITIAL STATE rather than in preconditions: Decap always requires
    # ``printed``, and with the checkbox off every tube starts already
    # ``printed``. The planner then never selects PrintLabel and Decap
    # fires straight after Pick — one static precondition graph, one
    # switch, no conditional actions.
    print_label = bool(kwargs.get("print_label", False))
    _CHAIN = _CHAIN_PRINT if print_label else _CHAIN_PLAIN
    _STEPS = len(_CHAIN)
    initial_facts = frozenset(
        () if print_label else [(printed.name, t) for t in tubes]
    )

    def item_done(state, tube):
        return (recapped.name, tube) in state

    def goal(state):
        return (
            (started.name,) in state
            and all(item_done(state, t) for t in tubes)
            and (parked.name,) in state
        )

    goal_facts = frozenset(
        [(recapped.name, t) for t in tubes]
        + [(started.name,), (parked.name,)]
    )

    return {
        "initial_facts": initial_facts,
        "goal":          goal,
        "item_done":     item_done,
        "goal_facts":    goal_facts,
        "objects":       {"tube": tubes},
    }


# ── Lifecycle ─────────────────────────────────────────────────────────

class Start(Action):
    params      = []
    duration    = 5
    resource    = "robot"
    START_JOINTS = [0, 45, -90, 0, -45, 0, 100]

    def pre(self):
        return ~started()

    def eff(self):
        # Seed the single-occupancy resources: hand, scaletop, decapper
        # and pipettor nozzle all start free.
        return {"started": (+started(), +hand_empty(), +scale_free(),
                            +decapper_free(), +nozzle_free())}

    def execute(self):
        rt  = self.ctx.runtime
        rcp = self.ctx.recipes
        ws  = self.ctx.workspace
        core = ws.components["core"]
        rt.motor(1)
        # Home the rail before any move that assumes a homed axis:
        # set_axis_with_stop configures the axis + PID and homes against
        # the hard stop — already-homed axes (and sim) short-circuit to
        # True, so calling it every Start is cheap. A homing failure is
        # FATAL: return the reserved "killed" outcome — the runtime is
        # killed on the spot, nothing else runs, no motion ever happens
        # on the unhomed rail. The operator must Reset / re-Launch.
        if core.has_rail:
            rt.step("homing rail")
            if not rcp["robot"].set_axis_with_stop(core.rail_cfg):
                rt.step("homing failed")
                return "killed"
        rcp["robot"].park(joint=self.START_JOINTS)
        return "started"


# ── Tube gripper chain ────────────────────────────────────────────────

class Pick(Action):
    """Pick the tube from its falcon-rack slot."""
    params   = ["tube"]
    duration = 10
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return started() & hand_empty() & ~picked(tube)

    def eff(self, tube):
        return {"picked": (+picked(tube), -hand_empty())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1} [{slot}]: pick")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_rack"].pick(slot, soft_approach=True, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "picked"


class PrintLabel(Action):
    """Print + apply a barcode label on the held tube.

    Only planned when the ``print_label`` launch kwarg is on — with it
    off, setup() seeds ``printed`` for every tube so this action is never
    selectable (see setup()).

    place(exit=False) + print + pick(approach=False) stay ONE action, the
    same rule as Decap/Cap: the tube must never be left sitting on the
    applicator pad across a replan boundary. The action always ends with
    the tube back in the gripper, which is also what makes the failure
    path safe — on a bad print we return False with the physical state
    exactly as the precondition found it, so the planner re-selects this
    step and prints again (project-guide §8, declarative retry)."""
    params   = ["tube"]
    duration = 20
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return picked(tube) & ~printed(tube)

    def eff(self, tube):
        return {"printed": (+printed(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        data = f"{LABEL_PREFIX}{slot}"
        rt.step(f"tube {tube + 1} [{slot}]: print label {data}")
        rt.step(_progress_pct(self), level="progress")
        rcp["printer"].place(exit=False, gravity_offset=PRINTER_GRAV_OFFSET)
        ok = rcp["printer"].print_label(data, code_type=LABEL_CODE)
        # Retrieve the tube FIRST, then judge the print — never leave it
        # on the pad, whatever the printer reported.
        rcp["printer"].pick(approach=False)
        if not ok:
            rt.step(f"tube {tube + 1}: print failed — check the printer, then Resume")
            return False
        return "printed"


class PlaceOnScale(Action):
    """Release the held tube on the scaletop."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        # After Cap: the capped tube is already in the gripper, so the
        # weigh/inspect/scan block runs on the FINISHED tube with no
        # extra pick.
        return capped(tube) & scale_free() & ~on_scale(tube)

    def eff(self, tube):
        return {"on_scale": (+on_scale(tube), +hand_empty(), -scale_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: place on scale")
        rt.step(_progress_pct(self), level="progress")
        rcp["scale_holder"].place("place", gravity_offset=SCALE_GRAV_OFFSET, soft_approach=True)
        return "on_scale"


class Weigh(Action):
    """Read the settled weight — NO motion, a pure device read.

    Split from the place/pick motions ON PURPOSE so a failed reading is
    retried without redoing any arm move: assert ``weighed`` only on a
    real number, ``return False`` otherwise and let the planner re-select
    this action (examples/scale is the reference for this pattern)."""
    params   = ["tube"]
    duration = 3
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return on_scale(tube) & ~weighed(tube)

    def eff(self, tube):
        return {"weighed": (+weighed(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(_progress_pct(self), level="progress")
        grams = rcp["scale"].weight(sim_return=12.345)
        if grams is None:
            rt.step(f"tube {tube + 1}: scale unavailable — reconnect it, then Resume")
            return False
        rt.step(f"tube {tube + 1}: weight {grams} g")
        return "weighed"


class PickFromScale(Action):
    """Re-grip the tube and lift it off the scaletop."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return weighed(tube) & hand_empty() & ~off_scale(tube)

    def eff(self, tube):
        return {"off_scale": (+off_scale(tube), -hand_empty(), +scale_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: pick off scale")
        rt.step(_progress_pct(self), level="progress")
        rcp["scale_holder"].pick("place", soft_approach=True)
        return "off_scale"


class Inspect(Action):
    """Present the held tube to the camera and detect.

    present() has no state side effect (the tube stays in the gripper),
    so motion + read live in one re-runnable action. A capture failure
    (vision server / camera down) raises ``CameraUnavailableError`` —
    converted to ``return False`` so the planner re-selects the step."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return off_scale(tube) & ~inspected(tube)

    def eff(self, tube):
        return {"inspected": (+inspected(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: camera")
        rt.step(_progress_pct(self), level="progress")
        rcp["inspector"].present(approach=False, offset=PRESENT_OFFSET)
        # Spin the tube on its own axis (j5) so the camera sees all
        # four faces. Four 90-degree steps land ~90 apart (the wrap
        # keeps j5 inside its limit, so the last lands at +10 rather
        # than back at 0 — coverage is what matters here, not the
        # absolute angle).
        for _ in range(4):
            rcp["inspector"].rotate(rotation=90)
        try:
            rcp["inspector"].detect()
        except CameraUnavailableError:
            rt.step(f"tube {tube + 1}: camera unavailable — reconnect it, then Resume")
            return False
        return "inspected"


class Scan(Action):
    """Present the held tube to the barcode reader and read the code.

    Same shape as Inspect: motion + read in one re-runnable action;
    detect() returns None when the reader is disconnected → False."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return inspected(tube) & ~scanned(tube)

    def eff(self, tube):
        return {"scanned": (+scanned(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: barcode")
        rt.step(_progress_pct(self), level="progress")
        rcp["barcode_reader"].present(approach=False, offset=PRESENT_OFFSET)
        # A label sits on ONE face of the tube — spin j5 through four
        # 90-degree steps so every face passes the reader.
        for _ in range(4):
            rcp["barcode_reader"].rotate(rotation=90)
        scan = rcp["barcode_reader"].detect()
        if scan is None:
            rt.step(f"tube {tube + 1}: barcode reader unavailable — reconnect it, then Resume")
            return False
        rt.step(f"tube {tube + 1}: scan {scan}")
        return "scanned"


class Decap(Action):
    """Place the tube in the decapper and unscrew — cap ends on the gripper.

    place(exit=False) + decap(approach=False) stay ONE action: the pair
    is a single continuous physical unit (place doesn't exit, decap
    doesn't approach), so no other motion may be planned between them."""
    params   = ["tube"]
    duration = 15
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        # Decap is the FIRST thing after the pick — except for the label,
        # which is either printed by PrintLabel or seeded by setup() when
        # the print_label kwarg is off. ``picked`` has to stay in its own
        # right: when setup() seeds ``printed``, that fact carries no
        # implication that the tube was ever picked up, and the planner
        # will happily decap a tube still sitting in the rack.
        return picked(tube) & printed(tube) & decapper_free() & ~decapped(tube)

    def eff(self, tube):
        # Tube into the decapper, cap onto the gripper: the hand stays
        # occupied through the swap, the decapper fills.
        return {"decapped": (+decapped(tube), -decapper_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: decap")
        rt.step(_progress_pct(self), level="progress")
        rcp["decapper"].place(exit=False)
        rcp["decapper"].decap(approach=False)
        return "decapped"


class ParkCap(Action):
    """Park the held cap at the tube's index on the cap holder."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return decapped(tube) & ~cap_parked(tube)

    def eff(self, tube):
        return {"cap_parked": (+cap_parked(tube), +hand_empty())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        cap_slot = _cap_slot(self, tube)
        rt.step(f"tube {tube + 1}: cap to holder [{cap_slot}]")
        rt.step(_progress_pct(self), level="progress")
        rcp["capholder"].place(cap_slot, soft_approach=True, gravity_offset=GRAV_OFFSET, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "cap_parked"


class RetrieveTube(Action):
    """Pick the (open) tube back out of the decapper."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return cap_parked(tube) & hand_empty() & ~retrieved(tube)

    def eff(self, tube):
        return {"retrieved": (+retrieved(tube), -hand_empty(), +decapper_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: pick from decapper")
        rt.step(_progress_pct(self), level="progress")
        rcp["decapper"].pick(tool_tcp_z_offset=DECAPPER_TCP_Z_OFFSET)
        return "retrieved"


class Return(Action):
    """Return the open tube to its rack slot."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return retrieved(tube) & ~returned(tube)

    def eff(self, tube):
        return {"returned": (+returned(tube), +hand_empty())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1}: return to rack [{slot}]")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_rack"].place(slot, gravity_offset=GRAV_OFFSET, soft_approach=True, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "returned"


# ── Pipettor chain ────────────────────────────────────────────────────

class PickTip(Action):
    """Pull a fresh tip onto the pipettor."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return returned(tube) & nozzle_free() & ~tipped(tube)

    def eff(self, tube):
        return {"tipped": (+tipped(tube), -nozzle_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        tip = _tip(self, tube)
        rt.step(f"tube {tube + 1}: tip [{tip}]")
        rt.step(_progress_pct(self), level="progress")
        # TEMPORARY (blind mode): outcome deliberately ignored while the
        # pump comms are being sorted out — do the motion, assume success.
        rcp["tip_rack"].pick_tip(tip)
        return "tipped"


class Aspirate(Action):
    """Draw the dose from the D5 reservoir.

    immerse → aspirate → retract stay ONE action, same rule as Dispense:
    the needle must never be left immersed across a replan boundary. The
    tip ends loaded and clear of the reservoir."""
    params   = ["tube"]
    duration = 12
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return tipped(tube) & ~aspirated(tube)

    def eff(self, tube):
        return {"aspirated": (+aspirated(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: aspirate {VOL_UL} µL from [{SOURCE_SLOT}]")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_pipette"].immerse(anchor=SOURCE_SLOT, depth=IMMERSE_DEPTH,
                                        soft_approach=IMMERSE_SOFT_APPROACH)
        # TEMPORARY (blind mode): pump outcome deliberately ignored while
        # the comms are being sorted out — aspirate, retract, assume success.
        rcp["falcon_pipette"].aspirate(vol=VOL_UL)
        rcp["falcon_pipette"].retract(anchor=SOURCE_SLOT)
        return "aspirated"


class Dispense(Action):
    """Dispense the aspirated dose into the (uncapped) tube.

    immerse → dispense → retract stay ONE action: the needle must not be
    left immersed across a replan boundary, so the three recipe calls
    form a single physical unit that always ends retracted."""
    params   = ["tube"]
    duration = 12
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return aspirated(tube) & ~dispensed(tube)

    def eff(self, tube):
        return {"dispensed": (+dispensed(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1} [{slot}]: dispense {VOL_UL} µL")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_pipette"].immerse(anchor=slot, depth=IMMERSE_DEPTH,
                                        soft_approach=IMMERSE_SOFT_APPROACH)
        # TEMPORARY (blind mode): pump outcome deliberately ignored while
        # the comms are being sorted out — dispense, retract, assume success.
        rcp["falcon_pipette"].dispense(vol=VOL_UL)
        rcp["falcon_pipette"].retract(anchor=slot)
        return "dispensed"


class EjectTip(Action):
    """Drop the used tip into the waste bin."""
    params       = ["tube"]
    duration     = 6
    resource     = "robot"
    tool         = "pipettor"
    SHAKE_TRAVEL = 10   # lateral ± shake (mm) to dislodge the tip on exit

    def pre(self, tube):
        return dispensed(tube) & ~ejected(tube)

    def eff(self, tube):
        return {"ejected": (+ejected(tube), +nozzle_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: tip to waste")
        rt.step(_progress_pct(self), level="progress")
        # TEMPORARY (blind mode): outcome deliberately ignored while the
        # pump comms are being sorted out — eject, assume success.
        rcp["waste_bin"].eject_tip(shake_travel=self.SHAKE_TRAVEL)
        return "ejected"


# ── Re-cap chain (tube gripper again) ─────────────────────────────────
# Mirror of the decap chain: the tube goes back into the decapper, its
# own cap comes off the holder, and the decapper screws it on. The
# printer project is the reference for the cap/pick pairing.

class PickForCap(Action):
    """Pick the dosed (open) tube back off its rack slot."""
    params   = ["tube"]
    duration = 10
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return ejected(tube) & hand_empty() & ~recap_held(tube)

    def eff(self, tube):
        return {"recap_held": (+recap_held(tube), -hand_empty())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1} [{slot}]: pick to re-cap")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_rack"].pick(slot, soft_approach=True, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "recap_held"


class PlaceInDecapper(Action):
    """Seat the dosed tube in the decapper, ready for its cap."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return recap_held(tube) & decapper_free() & ~in_decapper(tube)

    def eff(self, tube):
        return {"in_decapper": (+in_decapper(tube), +hand_empty(),
                                -decapper_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: into the decapper")
        rt.step(_progress_pct(self), level="progress")
        rcp["decapper"].place()
        return "in_decapper"


class PickCap(Action):
    """Pick the tube's own cap back off the cap holder."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return in_decapper(tube) & cap_parked(tube) & hand_empty() & ~cap_held(tube)

    def eff(self, tube):
        # The cap leaves the holder — drop cap_parked so the slot reads
        # empty again.
        return {"cap_held": (+cap_held(tube), -hand_empty(),
                             -cap_parked(tube))}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        cap_slot = _cap_slot(self, tube)
        rt.step(f"tube {tube + 1}: cap from holder [{cap_slot}]")
        rt.step(_progress_pct(self), level="progress")
        rcp["capholder"].pick(cap_slot, soft_approach=True,
                              tool_tcp_z_offset=CAPHOLDER_TCP_Z_OFFSET,
                              motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "cap_held"


class Cap(Action):
    """Screw the cap on and lift the capped tube out.

    cap(exit=False) + pick(approach=False) stay ONE action, the mirror of
    Decap: the pair is a single continuous physical unit, so no other
    motion may be planned between them. The gripper hands the cap over to
    the tube and comes back holding the capped tube."""
    params   = ["tube"]
    duration = 15
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return cap_held(tube) & ~capped(tube)

    def eff(self, tube):
        # Cap → tube, then the capped tube → gripper: the hand is busy
        # on both sides of the swap, so only the decapper frees up.
        return {"capped": (+capped(tube), +decapper_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: cap")
        rt.step(_progress_pct(self), level="progress")
        rcp["decapper"].cap(exit=False)
        rcp["decapper"].pick(approach=False, tool_tcp_z_offset=DECAPPER_TCP_Z_OFFSET)
        return "capped"


class ReturnCapped(Action):
    """Return the capped tube to its rack slot — the tube is done."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        # Last step: the capped tube has been weighed, inspected and
        # scanned, and is still in the gripper.
        return scanned(tube) & ~recapped(tube)

    def eff(self, tube):
        return {"recapped": (+recapped(tube), +hand_empty())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1}: capped, back to rack [{slot}]")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_rack"].place(slot, gravity_offset=GRAV_OFFSET, soft_approach=True, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
        return "recapped"


class Park(Action):
    """Final park — planned once every tube is done."""
    params      = []
    duration    = 5
    resource    = "robot"
    tool        = None
    PARK_JOINTS = [0, 90, 0, 0, 0, 0, 100]

    def pre(self):
        tubes = self._ctx_all_objects().get("tube", [])
        expr = ~parked() & started()
        for t in tubes:
            expr = expr & recapped(t)
        return expr

    def eff(self):
        return {"parked": (+parked(),)}

    def execute(self):
        rcp = self.ctx.recipes
        rcp["robot"].park(joint=self.PARK_JOINTS)
        return "parked"


class OperatorPark(Park):
    """Operator-initiated park — fires on the Park button, outside the plan."""
    trigger = "park"
