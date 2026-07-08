"""falcon protocol — Start → [Process(t) → Dispense(t)] ×tube_count → Park.

Per tube t (rack slot, cap-holder slot and tip slot all share index t):

  Process(t)  — tube gripper:
    1. pick the tube from the falcon rack
    2. weigh: place on the scaletop → scale.weight() → pick back up
    3. present to the camera + detect
    4. present to the barcode reader + detect
    5. decap in the decapper (cap ends up on the gripper)
    6. place the cap at the ASSOCIATED index t on the cap holder
    7. pick the tube from the decapper → return it to its rack slot

  Dispense(t) — pipettor (tool swap handled by the planner):
    8. pick tip t from the tip rack
    9. immerse into the tube → dispense → retract
   10. eject the used tip into the waste bin

``tube_count`` (launch kwarg, default 1) sets how many tubes to run.
Tubes stay uncapped in the rack; caps stay on the cap holder, indexed by
tube, so a future re-cap flow knows which cap belongs to which tube.
"""

from __future__ import annotations

from workspace.bt import Action, predicate


started   = predicate("started")
processed = predicate("processed")   # weighed + inspected + scanned + decapped
dispensed = predicate("dispensed")   # tip → dispense → waste done
parked    = predicate("parked")


# Component names — slot lists are read from these at runtime.
FALCON_RACK = "rack_falcon_15ml_1"
CAP_HOLDER  = "capholder_falcon_15ml_1"
TIP_RACK    = "rack_tip"

# Pipetting parameters
IMMERSE_DEPTH = 20     # mm below the tube top
VOL_UL        = 400    # microliters dispensed per tube


def setup(**kwargs):
    tube_count = int(kwargs.get("tube_count", 1))
    tubes = list(range(tube_count))

    def item_done(state, tube):
        return (dispensed.name, tube) in state

    def goal(state):
        return (
            (started.name,) in state
            and all(item_done(state, t) for t in tubes)
            and (parked.name,) in state
        )

    goal_facts = frozenset(
        [(dispensed.name, t) for t in tubes]
        + [(started.name,), (parked.name,)]
    )

    return {
        "initial_facts": frozenset(),
        "goal":          goal,
        "item_done":     item_done,
        "goal_facts":    goal_facts,
        "objects":       {"tube": tubes},
    }


def _progress_pct(action) -> int:
    """Monotonic progress: 2 steps per tube (processed, dispensed)."""
    tubes = action._ctx_all_objects().get("tube", [])
    total = (len(tubes) or 1) * 2
    ctx_state = getattr(action.ctx, "state", None) or {}
    facts = ctx_state.get("facts") or set()
    done = sum(1 for t in tubes for p in (processed, dispensed) if (p.name, t) in facts)
    return int((done + 1) / total * 100)


class Start(Action):
    params      = []
    duration    = 5
    resource    = "robot"
    HOME_JOINTS = [0, 45, -90, 0, -45, 0, 100]

    def pre(self):
        return ~started()

    def eff(self):
        return {"started": (+started(),)}

    def execute(self):
        rt  = self.ctx.runtime
        rcp = self.ctx.recipes
        rt.motor(1)
        rcp["robot"].park(joint=self.HOME_JOINTS, has_motion_plan=True)
        return "started"


class Process(Action):
    """Pick → weigh → camera → barcode → decap → cap to holder[t] → tube back."""

    params   = ["tube"]
    duration = 60
    resource = "robot"
    tool     = "gripper"

    def pre(self, tube):
        return started() & ~processed(tube)

    def eff(self, tube):
        return {"processed": (+processed(tube),)}

    def execute(self, tube):
        rt  = self.ctx.runtime
        rcp = self.ctx.recipes
        ws  = self.ctx.workspace

        slot     = ws.components[FALCON_RACK].slot["body"][tube]
        cap_slot = ws.components[CAP_HOLDER].slot["body"][tube]

        rt.step(f"tube {tube + 1} [{slot}]: pick")
        rt.step(_progress_pct(self), level="progress")
        rcp["falcon_rack"].pick(slot, soft_approach=True)

        # Weigh on the scaletop.
        rcp["scale_holder"].place("place", gravity_offset=4)
        grams = rcp["scale"].weight(sim_return=12.345)
        rt.step(f"tube {tube + 1}: weight {grams} g")
        rcp["scale_holder"].pick("place")

        # Camera.
        rt.step(f"tube {tube + 1}: camera")
        rcp["inspector"].present()
        rcp["inspector"].detect()

        # Barcode.
        rt.step(f"tube {tube + 1}: barcode")
        rcp["barcode_reader"].present()
        scan = rcp["barcode_reader"].detect()
        rt.step(f"tube {tube + 1}: scan {scan}")

        # Decap — cap ends up on the gripper; park it at index t.
        rt.step(f"tube {tube + 1}: decap → cap to holder [{cap_slot}]")
        rcp["decapper"].place(exit=False)
        rcp["decapper"].decap(approach=False)
        rcp["capholder"].place(cap_slot, soft_approach=True, gravity_offset=4)

        # Tube back to its rack slot (uncapped).
        rcp["decapper"].pick()
        rcp["falcon_rack"].place(slot, gravity_offset=4, soft_approach=True)

        return "processed"


class Dispense(Action):
    """Fresh tip → dispense into the (uncapped) tube → tip to waste."""

    params   = ["tube"]
    duration = 25
    resource = "robot"
    tool     = "pipettor"

    def pre(self, tube):
        return processed(tube) & ~dispensed(tube)

    def eff(self, tube):
        return {"dispensed": (+dispensed(tube),)}

    def execute(self, tube):
        rt  = self.ctx.runtime
        rcp = self.ctx.recipes
        ws  = self.ctx.workspace

        slot = ws.components[FALCON_RACK].slot["body"][tube]
        tip  = ws.components[TIP_RACK].slot["body"][tube]

        rt.step(f"tube {tube + 1} [{slot}]: tip {tip} → dispense {VOL_UL} µL")
        rt.step(_progress_pct(self), level="progress")

        rcp["tip_rack"].pick_tip(tip)
        rcp["falcon_pipette"].immerse(anchor=slot, depth=IMMERSE_DEPTH)
        rcp["falcon_pipette"].dispense(vol=VOL_UL)
        rcp["falcon_pipette"].retract(anchor=slot)
        rcp["waste_bin"].eject_tip()

        return "dispensed"


class Park(Action):
    """Final park — planned once every tube is processed + dispensed."""
    params      = []
    duration    = 5
    resource    = "robot"
    tool        = None
    PARK_JOINTS = [0, 185, -94, 0, 0, 0, 100]

    def pre(self):
        tubes = self._ctx_all_objects().get("tube", [])
        expr = ~parked() & started()
        for t in tubes:
            expr = expr & dispensed(t)
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
