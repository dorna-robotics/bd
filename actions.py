"""bd protocol — Start → [per-tube split chain] ×tube_count → Park.

Each tube t (rack slot, cap-holder slot and tip slot all share index t)
moves through a SPLIT chain of small BT actions, one recipe unit per
action, threaded by facts (the BT moves action→action as each eff is
asserted). If a step fails, the leaf fails THERE — the planner replans
from observed state and re-selects exactly the step that didn't happen
(pattern: the apc project / examples/scale).

  Tube gripper (tool = "gripper"):
    1. Pick           pick the tube from the falcon rack
    2. PlaceOnScale   release it on the scaletop
    3. Weigh          read the settled weight — NO motion, a pure device
                      read (declarative retry, see below)
    4. PickFromScale  re-grip the tube off the scaletop
    5. Inspect        present to the camera + detect
    6. Scan           present to the barcode reader + read the code
    7. Decap          place in the decapper + unscrew (cap → gripper)
    8. ParkCap        park the cap at index t on the cap holder
    9. RetrieveTube   pick the (open) tube back out of the decapper
   10. Return         return it to its rack slot

  Pipettor (tool = "pipettor", swap handled by the planner):
   11. PickTip        fresh tip t from the tip rack
   12. Dispense       immerse → dispense → retract (one physical unit)
   13. EjectTip       drop the used tip into the waste bin

Device reads + declarative retry (project-guide §8): ``Weigh`` / ``Scan``
assert their fact ONLY on a valid reading and ``return False`` otherwise
(``Inspect`` converts ``CameraUnavailableError`` the same way). No retry
loop anywhere — a False leaf replans from observed state, the planner
re-selects the same read, and a ``critical`` device that is down has
already paused the runtime until the operator fixes it and resumes.

``tube_count`` (launch kwarg, default 1) sets how many tubes to run.
Tubes stay uncapped in the rack; caps stay on the cap holder, indexed by
tube, so a future re-cap flow knows which cap belongs to which tube.
"""

from __future__ import annotations

from workspace.bt import Action, predicate
from workspace.components.inspection.vision_station import CameraUnavailableError


# ── Per-tube facts (the action chain) ─────────────────────────────────
started    = predicate("started")
picked     = predicate("picked")      # tube in the gripper (off the rack)
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
dispensed  = predicate("dispensed")   # volume dispensed into the tube
ejected    = predicate("ejected")     # used tip in the waste bin (= done)
parked     = predicate("parked")

# ── Single-occupancy resources (capacity-1, no args) ──────────────────
# Without these the planner batches steps across tubes that physically
# can't overlap (one gripper, one scaletop seat, one decapper, one
# pipettor nozzle). Each fact is consumed (-) when its slot fills and
# restored (+) when it empties. See project-guide §8.
hand_empty    = predicate("hand_empty")     # gripper holds nothing
scale_free    = predicate("scale_free")     # scaletop seat is empty
decapper_free = predicate("decapper_free")  # decapper holds no tube
nozzle_free   = predicate("nozzle_free")    # pipettor carries no tip


# Component names — slot lists are read from these at runtime.
FALCON_RACK = "rack_falcon_15ml_1"
CAP_HOLDER  = "capholder_falcon_15ml_1"
TIP_RACK    = "rack_tip"

# Pipetting parameters
IMMERSE_DEPTH = 20     # mm below the tube top
VOL_UL        = 400    # microliters dispensed per tube

# Motion parameters
GRAV_OFFSET   = 4      # mm — gravity press applied on every release
# Planner gravity constraint for planned travel hops while carrying:
# keep the payload upright (+z) within 5 deg of tilt. Passed as
# motion_plan_kwargs to the actions that fly with a tube or tip.
MOTION_PLAN_GRAVITY = {"gravity_vec": [0, 0, 1], "gravity_thr": 5}
# Present pose tweak for camera + barcode, xyzabc in the station's
# "place"-anchor frame: 50 mm lower in z.
PRESENT_OFFSET = [0, 0, -50, 0, 0, 0]

_STEPS = 13            # per-tube steps for progress

_CHAIN = (picked, on_scale, weighed, off_scale, inspected, scanned,
          decapped, cap_parked, retrieved, returned, tipped, dispensed,
          ejected)


def _slot(action, tube):
    return action.ctx.workspace.components[FALCON_RACK].slot["body"][tube]


def _cap_slot(action, tube):
    return action.ctx.workspace.components[CAP_HOLDER].slot["body"][tube]


def _tip(action, tube):
    return action.ctx.workspace.components[TIP_RACK].slot["body"][tube]


def _progress_pct(action) -> int:
    """Monotonic % over all per-tube steps. This action's eff hasn't
    applied yet, so count it as +1."""
    tubes = action._ctx_all_objects().get("tube", [])
    total = (len(tubes) or 1) * _STEPS
    ctx_state = getattr(action.ctx, "state", None) or {}
    facts = ctx_state.get("facts") or set()
    done = sum(1 for t in tubes for p in _CHAIN if (p.name, t) in facts)
    return int((done + 1) / total * 100)


def setup(**kwargs):
    tube_count = int(kwargs.get("tube_count", 1))
    tubes = list(range(tube_count))

    def item_done(state, tube):
        return (ejected.name, tube) in state

    def goal(state):
        return (
            (started.name,) in state
            and all(item_done(state, t) for t in tubes)
            and (parked.name,) in state
        )

    goal_facts = frozenset(
        [(ejected.name, t) for t in tubes]
        + [(started.name,), (parked.name,)]
    )

    return {
        "initial_facts": frozenset(),
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
        rcp["robot"].park(joint=self.START_JOINTS, has_motion_plan=True)
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


class PlaceOnScale(Action):
    """Release the held tube on the scaletop."""
    params   = ["tube"]
    duration = 8
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return picked(tube) & scale_free() & ~on_scale(tube)

    def eff(self, tube):
        return {"on_scale": (+on_scale(tube), +hand_empty(), -scale_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: place on scale")
        rt.step(_progress_pct(self), level="progress")
        rcp["scale_holder"].place("place", gravity_offset=GRAV_OFFSET, soft_approach=False)
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
        return scanned(tube) & decapper_free() & ~decapped(tube)

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
        rcp["capholder"].place(cap_slot, soft_approach=True, gravity_offset=GRAV_OFFSET)
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
        rcp["decapper"].pick()
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
        rcp["falcon_rack"].place(slot, gravity_offset=GRAV_OFFSET, soft_approach=False, motion_plan_kwargs=MOTION_PLAN_GRAVITY)
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
        # pick_tip verifies via the tip sensor — False also covers an
        # unreachable pump (declarative retry, no loop).
        if not rcp["tip_rack"].pick_tip(tip, motion_plan_kwargs=MOTION_PLAN_GRAVITY):
            rt.step(f"tube {tube + 1}: no tip detected — check the pipettor, then Resume")
            return False
        return "tipped"


class Dispense(Action):
    """Dispense into the (uncapped) tube.

    immerse → dispense → retract stay ONE action: the needle must not be
    left immersed across a replan boundary, so the three recipe calls
    form a single physical unit that always ends retracted."""
    params   = ["tube"]
    duration = 12
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return tipped(tube) & ~dispensed(tube)

    def eff(self, tube):
        return {"dispensed": (+dispensed(tube),)}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        slot = _slot(self, tube)
        rt.step(f"tube {tube + 1} [{slot}]: dispense {VOL_UL} µL")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_pipette"].immerse(anchor=slot, depth=IMMERSE_DEPTH)
        ok = rcp["falcon_pipette"].dispense(vol=VOL_UL)
        # ALWAYS retract — never leave the needle immersed, even when
        # the pump refused (unreachable device → False).
        rcp["falcon_pipette"].retract(anchor=slot)
        if not ok:
            rt.step(f"tube {tube + 1}: pipettor pump unavailable — reconnect it, then Resume")
            return False
        return "dispensed"


class EjectTip(Action):
    """Drop the used tip into the waste bin."""
    params   = ["tube"]
    duration = 6
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return dispensed(tube) & ~ejected(tube)

    def eff(self, tube):
        return {"ejected": (+ejected(tube), +nozzle_free())}

    def execute(self, tube):
        rt, rcp = self.ctx.runtime, self.ctx.recipes
        rt.step(f"tube {tube + 1}: tip to waste")
        rt.step(_progress_pct(self), level="progress")
        # eject_tip verifies the tip is GONE via the sensor — False
        # covers a stuck tip or an unreachable pump.
        if not rcp["waste_bin"].eject_tip():
            rt.step(f"tube {tube + 1}: tip still on — check the pipettor, then Resume")
            return False
        return "ejected"


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
            expr = expr & ejected(t)
        return expr

    def eff(self):
        return {"parked": (+parked(),)}

    def execute(self):
        rcp = self.ctx.recipes
        rcp["robot"].park(joint=self.PARK_JOINTS, has_motion_plan=True)
        return "parked"


class OperatorPark(Park):
    """Operator-initiated park — fires on the Park button, outside the plan."""
    trigger = "park"
