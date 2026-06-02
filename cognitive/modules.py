"""
Default MIDCA modules adapted for sinoclaw cognitive extension.

Each module is a BaseModule that reads/writes CognitiveMemory.

Modules:
    SimpleIntend   — read goals from GoalGraph → current_goals
    SimpleEval     — check goal achievement + detect discrepancies → mark GoalGraph nodes
    SimplePlanner  — generate Plan from goals → current_plan
    DiscrepancyDetector — compare expected vs actual world state → discrepancy
    GoalGenerator  — inject goals into GoalGraph periodically
"""

import logging
from typing import Any, List, Optional

from cognitive.phase_manager import BaseModule, Phase, PhaseManager
from cognitive.memory import CognitiveMemory, CURRENT_GOALS, CURR_PLAN, DISCREPANCY
from cognitive.goals import GoalGraph, Goal
from cognitive.plans import Plan
from cognitive.world import CognitiveWorld

logger = logging.getLogger("cognitive.modules")


# ── INTEND ──────────────────────────────────────────────────────────────────


class SimpleIntend(BaseModule):
    """
    INTEND phase: read unrestricted goals from GoalGraph → current_goals.

    If a discrepancy was detected, mark current plan as incomplete so
    PLAN phase will re-plan.
    """

    def __init__(self, goal_graph: GoalGraph = None):
        super().__init__(name="SimpleIntend")
        self._goal_graph = goal_graph

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        gg = self._goal_graph or mem.goal_graph
        if gg is None:
            return None

        # Check for discrepancy — if found, invalidate current plan ONLY if
        # the plan has been completed or is irrelevant (don't interrupt in-progress plans)
        discrepancy = mem.get("__discrepancy")
        if discrepancy and mem.current_plan is not None:
            # Only invalidate if plan is completed OR goal was already achieved
            # An in-progress plan should be allowed to continue even with discrepancies
            current = mem.current_plan
            completed = getattr(current, 'completed', False)
            remaining_steps = current.actions[getattr(current, 'step', 0):] if hasattr(current, 'actions') else []
            if completed or not remaining_steps:
                logger.info(
                    "[Cycle %d] INTEND: discrepancy detected, plan %s completed=%s — invalidating, forcing re-plan",
                    cycle, current.name, completed,
                )
                mem.current_plan = None
            else:
                logger.info(
                    "[Cycle %d] INTEND: discrepancy detected but plan %s still has %d steps — preserving plan",
                    cycle, current.name, len(remaining_steps),
                )
            mem.set("__discrepancy", None)    # clear after handling

        goals = gg.get_unrestricted_goals()
        mem.set(CURRENT_GOALS, goals)

        if goals:
            logger.debug("[Cycle %d] INTEND: %d unrestricted goals", cycle, len(goals))
        return None


# ── EVALUATE ────────────────────────────────────────────────────────────────


class SimpleEval(BaseModule):
    """
    EVALUATE phase: check which goals have been achieved, detect discrepancies.

    Reads:   mem.current_state (CognitiveWorld), mem.ACTIONS, mem.STATES,
             mem.goal_graph
    Writes:  mem.goal_graph (marks achieved/failed nodes),
             mem.__discrepancy (set when expected ≠ actual world)
    """

    def __init__(self, goal_graph: GoalGraph = None, world: CognitiveWorld = None):
        super().__init__(name="SimpleEval")
        self._goal_graph = goal_graph
        self._world = world

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        gg = self._goal_graph or mem.goal_graph
        world = self._world or mem.current_state
        if world is None:
            return None

        # ── 1. Discrepancy detection ───────────────────────────────────
        self._detect_discrepancy(mem, world, cycle)

        # ── 2. Goal achievement check ──────────────────────────────────
        achieved_this_cycle = []
        failed_this_cycle = []

        if gg:
            for node in list(gg.nodes):
                if node.completed or node.achieved:
                    continue
                goal = node.goal
                satisfied = self._goal_satisfied(goal, world)
                if satisfied:
                    gg.mark_achieved(goal)
                    achieved_this_cycle.append(goal)
                elif self._goal_failed(goal, world):
                    gg.mark_failed(goal)
                    failed_this_cycle.append(goal)

        if achieved_this_cycle or failed_this_cycle:
            logger.info(
                "[Cycle %d] Eval: achieved=%s failed=%s",
                cycle,
                [str(g) for g in achieved_this_cycle],
                [str(g) for g in failed_this_cycle],
            )

    def _detect_discrepancy(
        self,
        mem: CognitiveMemory,
        world: CognitiveWorld,
        cycle: int,
    ):
        """
        Compare expected world (action applied to previous state) with actual.

        Stores (expected_missing, actual_extra) in mem.__discrepancy when
        a discrepancy is found, None otherwise.
        """
        all_actions = mem.get("__actions", [])
        if not all_actions:
            mem.set("__discrepancy", None)
            return

        # Get actions for the given cycle
        cycle_entries = [a for a in all_actions if a and a[0] == cycle]
        if not cycle_entries:
            mem.set("__discrepancy", None)
            return

        # Get previous world state (before this cycle's action was applied)
        states = mem.get("__states", [])
        if len(states) < 2:
            mem.set("__discrepancy", None)
            return

        prev_state: CognitiveWorld = states[-2]  # state BEFORE this cycle
        last_entry = cycle_entries[-1]
        if len(last_entry) < 3:
            mem.set("__discrepancy", None)
            return

        action_name, action_args = last_entry[1], last_entry[2]
        if isinstance(action_args, str):
            action_args = (action_args,)

        # Apply action to a copy of the previous state → expected world
        expected = CognitiveWorld()
        expected.atoms = prev_state.atoms.copy()
        expected.operators = prev_state.operators
        ok = expected.apply_action(action_name, action_args)

        if not ok:
            # Action was not applicable — discrepancy (agent tried something that failed)
            logger.info(
                "[Cycle %d] Discrepancy: action %s%s was not applicable",
                cycle, action_name, action_args,
            )
            mem.set("__discrepancy", (f"{action_name}{action_args} not applicable",))
            return

        # Compare expected atoms vs actual atoms
        expected_missing = world.atoms - expected.atoms
        actual_extra = expected.atoms - world.atoms

        if expected_missing or actual_extra:
            logger.info(
                "[Cycle %d] Discrepancy: expected - actual = %s | actual - expected = %s",
                cycle, expected_missing, actual_extra,
            )
            mem.set("__discrepancy", (expected_missing, actual_extra))
        else:
            mem.set("__discrepancy", None)

    def _goal_satisfied(self, goal, world: CognitiveWorld) -> bool:
        pred = goal.predicate
        args = list(goal.args)
        if pred == "on":
            return world.atom_true("on", args[0], args[1])
        elif pred == "on-table":
            return world.atom_true("on-table", args[0])
        elif pred == "clear":
            return world.atom_true("clear", args[0])
        elif pred == "holding":
            return world.atom_true("holding", args[0])
        elif pred == "arm-empty":
            return world.atom_true("arm-empty")
        return False

    def _goal_failed(self, goal, world: CognitiveWorld) -> bool:
        """Goals rarely 'fail' in blocksworld — hard to prove permanent failure."""
        return False


# ── PLAN ────────────────────────────────────────────────────────────────────


class SimplePlanner(BaseModule):
    """
    PLAN phase: generate a Plan from current goals.

    Reads:   mem.current_goals, mem.current_state
    Writes:  mem[PLANS_CURR] = Plan
    """

    def __init__(
        self,
        planner_fn: callable = None,
        # planner_fn(world, goals) → Plan
    ):
        super().__init__(name="SimplePlanner")
        self._planner_fn = planner_fn

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        # Skip if we already have a plan in progress
        if mem.current_plan is not None and not mem.current_plan.completed:
            return None

        goals = mem.current_goals
        if not goals:
            return None

        world = mem.current_state

        if self._planner_fn is None:
            # No planner — cannot plan
            logger.debug("[Cycle %d] PLAN: no planner_fn configured", cycle)
            return None

        try:
            plan = self._planner_fn(world, goals)
            if plan:
                mem.current_plan = plan
                logger.info(
                    "[Cycle %d] PLAN: generated %s with %d steps",
                    cycle,
                    plan.name,
                    len(plan.actions),
                )
        except Exception as e:
            logger.exception("Planner failed at cycle %d", cycle)

        return None


# ── INTERPRET ───────────────────────────────────────────────────────────────


class DiscrepancyDetector(BaseModule):
    """
    INTERPRET phase: compare expected vs actual world state → discrepancy.

    Reads:   mem.current_state, mem.current_plan (expected world after plan)
    Writes:  mem[DISCREPANCY] = (expected_missing, unexpected_atoms)
    """

    def __init__(self, world: CognitiveWorld = None):
        super().__init__(name="DiscrepancyDetector")
        self._world = world

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        world = self._world or mem.current_state
        if world is None:
            return None

        # Clear discrepancy at start of cycle — INTERPRET will compute fresh one
        # after Simulate has applied the action. This prevents stale discrepancies
        # from the PREVIOUS cycle from leaking into the current cycle's report.
        if hasattr(mem, 'discrepancy'):
            mem.discrepancy = None

        # Compare with expected state from current plan
        plan = mem.current_plan
        expected_world = self._compute_expected_world(plan, world)

        if expected_world is None:
            mem.discrepancy = None
            return None

        missing, extra = world.diff(expected_world)
        if missing or extra:
            mem.discrepancy = (missing, extra)
            logger.info(
                "[Cycle %d] DISCREPANCY: missing=%s extra=%s",
                cycle,
                [str(a) for a in missing],
                [str(a) for a in extra],
            )
        else:
            mem.discrepancy = None

        return None

    def _compute_expected_world(self, plan, world: CognitiveWorld) -> Optional[CognitiveWorld]:
        """Simulate plan execution to get expected world state.

        Only applies remaining actions (from plan.step onwards), because the
        current action has already been applied to the actual world by SIMULATE.

        Uses NON-MIDCA operators (mode=True -> _midca_world=None) to avoid
        polluting the actual MIDCA world shared with the real CognitiveWorld.
        """
        if plan is None:
            return None

        # DEEP COPY: expected world gets its own CognitiveWorld state.
        # _midca_world will be set to None below to isolate MIDCA mutations.
        import copy
        expected = copy.deepcopy(world)

        # Isolate: expected world uses NON-MIDCA operators only.
        # This prevents mw.apply() in the mode=True branch from mutating
        # the actual world's shared MIDCA atoms reference.
        expected._midca_world = None
        expected._planning_mode = True  # still set to skip planner-only code

        # Only remaining actions: plan.step points to next unexecuted action
        for action in plan.actions[plan.step:]:
            expected.apply_action(action.name, action.args)
        return expected


class StateDiscrepancyDetector(BaseModule):
    """
    INTERPRET phase: detect discrepancies between expected atoms and actual atoms.

    A simpler version that compares actual world against a reference expected state.
    """

    def __init__(self, expected_world: CognitiveWorld = None):
        super().__init__(name="StateDiscrepancyDetector")
        self._expected_world = expected_world

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        world = mem.current_state
        expected = self._expected_world

        if world is None or expected is None:
            return None

        missing, extra = world.diff(expected)
        if missing or extra:
            mem.discrepancy = (missing, extra)
            logger.info(
                "[Cycle %d] StateDiscrepancy: missing=%d extra=%d",
                cycle,
                len(missing),
                len(extra),
            )
        return None
