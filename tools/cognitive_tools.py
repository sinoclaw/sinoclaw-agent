#!/usr/bin/env python3
"""
Cognitive Tools — MIDCA-inspired cognitive cycle as sinoclaw tools.

Phase order per cycle:
    SIMULATE → PERCEIVE → INTERPRET → EVALUATE → INTEND → PLAN → ACT

SIMULATE applies actions selected by the PREVIOUS cycle's Act phase.
Act phase selects the next action for NEXT cycle's Simulate to apply.
This one-cycle delay is the key MIDCA design: plan first, execute later.

Tools:
    cognitive_goal_add     — insert a goal into the GoalGraph
    cognitive_plan_generate — generate a Plan from current goals (no execution)
    cognitive_simulate(n) — run n complete cycles (plan + execute in same call)
    cognitive_plan_show    — show current goal graph, plans, and world state

Requirements: MIDCA cloned at /data/midca (or anywhere on sys.path)
"""

import logging
import sys
import os
import json
import copy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure cognitive package is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cognitive.phase_manager import PhaseManager, Phase, BaseModule
from cognitive.memory import CognitiveMemory
from cognitive.goals import GoalGraph, Goal
from cognitive.plans import Plan, Action
from cognitive.world import CognitiveWorld, Atom, Operator
from cognitive.trace import CogTrace
from cognitive.modules import (
    SimpleIntend,
    SimpleEval,
    SimplePlanner,
    DiscrepancyDetector,
)
from cognitive.executor import SimpleAct, MidcaActionSimulator

from tools.registry import registry

logger = logging.getLogger(__name__)


# ── Global cognitive state (one per agent session) ──────────────────────────

_cog_state: Dict[str, Any] = {}


def _get_cog_state() -> Dict[str, Any]:
    """Lazily initialise and return the global cognitive state dict."""
    global _cog_state
    if not _cog_state:
        _cog_state = _init_cognitive_state()
    return _cog_state


def reset_cognitive_state() -> None:
    """Clear the global cognitive state — use between independent tasks."""
    global _cog_state
    _cog_state = None


def _init_cognitive_state() -> Dict[str, Any]:
    """Build the cognitive stack: PhaseManager + CognitiveMemory + world + modules."""
    mem = CognitiveMemory()
    trace = CogTrace()
    gg = GoalGraph()
    mem.goal_graph = gg

    # CognitiveWorld with blocksworld operators
    world = _create_blocksworld()
    mem.current_state = world

    mgr = PhaseManager(memory=mem, goal_graph=gg, trace=trace)

    # SIMULATE — apply actions from previous cycle's Act phase
    mgr.append_module(Phase.SIMULATE, MidcaActionSimulator(world=world, memory=mem))

    # PERCEIVE — read world state (CognitiveObserver placeholder)
    mgr.append_module(Phase.PERCEIVE, _NoOpModule("CognitivePerceive"))

    # INTERPRET — detect discrepancies
    mgr.append_module(Phase.INTERPRET, DiscrepancyDetector(world=world))

    # EVALUATE — check goal achievement
    mgr.append_module(Phase.EVALUATE, SimpleEval(goal_graph=gg, world=world))

    # INTEND — select from GoalGraph
    mgr.append_module(Phase.INTEND, SimpleIntend(goal_graph=gg))

    # PLAN — MIDCA PyHop HTN planner
    mgr.append_module(Phase.PLAN, _MidcaPyHopPlanner(mem=mem, world=world))

    # ACT — select next action (Simulate will apply it next cycle)
    mgr.append_module(Phase.ACT, SimpleAct(memory=mem))

    return {
        "manager": mgr,
        "memory": mem,
        "world": world,
        "goal_graph": gg,
        "trace": trace,
        "cycles": 0,
    }


# ── Cognitive State Persistence (Phase 7) ───────────────────────────────────

_PERSIST_FILE = Path.home() / ".sinoclaw" / "cognitive_state.json"


def _get_persist_path() -> Path:
    p = Path.home() / ".sinoclaw"
    p.mkdir(parents=True, exist_ok=True)
    return p / "cognitive_state.json"


def _serialize_cog_state() -> Dict[str, Any]:
    """
    Serialize the live cognitive state to a JSON-serializable dict.

    Strategy: persist atoms (world), current_goals (memory), actions_history,
    cycles. PhaseManager/MIDCA objects are NOT serialized — they are
    reconstructed by _init_cognitive_state() and then atoms/goals are restored.
    """
    state = _get_cog_state()
    world = state["world"]
    mem = state["memory"]

    # Serialize world atoms
    atoms_list = [
        {"pred": a.predicate, "args": list(a.args) if a.args else []}
        for a in world.atoms
    ]

    # Serialize goals
    goals_list = [
        {"predicate": g.predicate, "args": list(g.args)}
        for g in mem.current_goals
    ]

    # Serialize actions history
    actions_list = [list(a) for a in mem.actions_history]

    return {
        "version": 1,
        "atoms": atoms_list,
        "goals": goals_list,
        "actions_history": actions_list,
        "cycles": state["cycles"],
        "plan_completed": (
            state["memory"].current_plan.completed
            if state["memory"].current_plan is not None else None
        ),
    }


def _sync_midca_world_from_cognitive(world, mem) -> None:
    """
    Overwrite _midca_world atoms to match the restored CognitiveWorld atoms.

    MIDCA's PyHop planner reads from world._midca_world, not CognitiveWorld.atoms.
    After _restore_cognitive_state updates CognitiveWorld.atoms, _midca_world
    still holds the original initial state. This function rebuilds _midca_world
    from the current CognitiveWorld state so planners and action simulators
    agree on the current world configuration.
    """
    if world._midca_world is None:
        return

    try:
        import sys as _sys
        _sys.path.insert(0, '/data/midca')
        from midca.worldsim import worldsim
        mw = world._midca_world

        # Remove all managed predicate atoms and rebuild from CognitiveWorld
        MANAGED = {"on", "on-table", "clear", "holding", "arm-empty", "block"}

        # Get existing atoms grouped by predicate for efficient removal
        to_remove = [a for a in mw.atoms if a.predicate.name in MANAGED]
        for a in to_remove:
            mw.remove_atom(a)

        # Build new atoms from CognitiveWorld state
        for cog_atom in world.atoms:
            pred = cog_atom.predicate
            if pred not in MANAGED:
                continue
            args = cog_atom.args
            # Resolve each arg name to a MIDCA Obj in the world
            midca_args = []
            for arg_name in args:
                name_str = str(arg_name)
                if name_str in mw.objects:
                    midca_args.append(mw.objects[name_str])
                else:
                    midca_args = None
                    break
            if midca_args is None:
                continue

            # Use existing Predicate objects from mw.predicates (not creating new)
            pred_obj = mw.predicates.get(pred)
            if pred_obj is None:
                continue
            midca_atom = worldsim.Atom(pred_obj, midca_args)
            mw.add_atom(midca_atom)
    except Exception as exc:
        logger.warning("Could not sync _midca_world from CognitiveWorld: %s", exc)


def _persist_cognitive_state() -> None:
    """Write current cognitive state to disk (Phase 7)."""
    try:
        data = _serialize_cog_state()
        path = _get_persist_path()
        path.write_text(json.dumps(data, indent=2))
        logger.debug("Cognitive state persisted to %s", path)
    except Exception as exc:
        logger.warning("Failed to persist cognitive state: %s", exc)


def _restore_cognitive_state() -> bool:
    """
    Restore cognitive state from disk into the live _cog_state.

    Returns True if restoration succeeded, False otherwise.
    """
    path = _get_persist_path()
    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Failed to load cognitive state from %s: %s", path, exc)
        return False

    try:
        from cognitive.world import Atom

        state = _get_cog_state()
        world = state["world"]
        mem = state["memory"]

        # Restore world atoms — replace existing atoms with persisted ones
        # Strategy: clear non-block atoms (free, arsonist etc), restore all others
        # We identify "structural" atoms by predicate that we manage
        WORLD_MANAGED_PREDICATES = {
            "on", "on-table", "clear", "holding", "arm-empty", "block",
        }
        to_remove = [
            a for a in world.atoms
            if a.predicate in WORLD_MANAGED_PREDICATES
        ]
        for a in to_remove:
            world.remove_atom(a)

        for item in data.get("atoms", []):
            pred = item["pred"]
            args = tuple(item["args"]) if item["args"] else ()
            if pred in WORLD_MANAGED_PREDICATES:
                world.add_atom(Atom(pred, *args))

        # Restore cycles
        state["cycles"] = data.get("cycles", 0)

        # Restore actions history
        mem.actions_history.clear()
        for a in data.get("actions_history", []):
            mem.actions_history.append(a)

        # Restore goals — use setter to persist through property
        # Build list of Goal objects from persisted data
        restored_goals = []
        gg = state["goal_graph"]
        gg.nodes.clear()  # clear existing nodes
        plan_completed = data.get("plan_completed")

        for g_data in data.get("goals", []):
            from cognitive.goals import Goal
            predicate = g_data["predicate"]
            args = g_data["args"]
            goal = Goal(predicate, *args)
            gg.insert(goal)
            # If the plan was marked completed, mark goal as achieved
            if plan_completed:
                for node in gg.nodes:
                    if node.goal == goal:
                        node.achieved = True
                        break
            restored_goals.append(goal)

        # Set via property setter (not append, which is a no-op on the property)
        mem.current_goals = restored_goals

        # Re-initialize PyHop planners so their MIDCA worlds are re-synced
        # from the restored CognitiveWorld atoms. Without this, the planner
        # would keep reading stale MIDCA world state (old on(B,A) instead of
        # the restored on(D,A)), causing actions to fail.
        mgr = state["manager"]
        for p in mgr.modules.get("PLAN", []):
            if hasattr(p, "_init_pyhop"):
                p._pyhop_initialized = False  # force re-init on next use

        # Sync _midca_world atoms from CognitiveWorld atoms.
        # MIDCA's PyHop planner reads from _midca_world, not CognitiveWorld.atoms.
        # Without this, _midca_world retains stale state from the original world,
        # causing plan generation and action execution to fail after restore.
        _sync_midca_world_from_cognitive(world, mem)

        logger.info(
            "Cognitive state restored: cycles=%d, goals=%d, atoms=%d",
            state["cycles"],
            len(mem.current_goals),
            len(data.get("atoms", [])),
        )
        return True

    except Exception as exc:
        logger.warning("Failed to restore cognitive state: %s", exc)
        return False


# ── MIDCA PyHop Planner wrapper ─────────────────────────────────────────────


class _MidcaPyHopPlanner(BaseModule):
    """
    Wraps MIDCA's PyHopPlanner as a cognitive BaseModule.

    Reads: mem.current_goals (list of Goal)
    Writes: mem.current_plan (Plan)

    Falls back to a simple reactive planner if MIDCA PyHop fails.
    """

    def __init__(self, mem: CognitiveMemory, world: CognitiveWorld):
        super().__init__(name="MidcaPyHopPlanner")
        self.mem = mem
        self._world = world
        self._pyhop_planner = None
        self._pyhop_working = False
        self._pyhop_initialized = False

    def _init_pyhop(self, mem):
        """Lazily initialise MIDCA PyHop planner on first run."""
        if self._pyhop_initialized:
            return
        self._pyhop_initialized = True
        try:
            import sys as _sys
            _sys.path.insert(0, '/data/midca')

            from midca.modules.plan.PyHopPlanner import PyHopPlanner as PyHopPlannerClass
            from midca.domains.blocksworld.plan import methods, operators
            from midca.domains.blocksworld import util

            self._pyhop_planner = PyHopPlannerClass(
                util.pyhop_state_from_world,
                util.pyhop_tasks_from_goals,
                methods.declare_methods,
                operators.declare_ops,
            )
            self._pyhop_working = self._pyhop_planner.working
            if self._pyhop_working:
                self._pyhop_planner.init(self._world._midca_world, mem)
                logger.info("MIDCA PyHop planner initialised OK")
            else:
                logger.warning("MIDCA PyHop planner failed to initialise — using fallback")
        except Exception as e:
            logger.warning("Could not load MIDCA PyHop: %s — using fallback planner", e)
            self._pyhop_working = False

    def run(self, mgr: PhaseManager, mem: CognitiveMemory, cycle: int) -> Optional[str]:
        """PhaseManager hook — runs via phases. Prefer _run_planner() directly."""
        plan = self._run_planner(mem, cycle)
        if plan:
            mem.current_plan = plan
        return None

    def _run_planner(self, mem: CognitiveMemory, cycle: int) -> Optional[Plan]:
        """Run planning directly (no PhaseManager). Sets _planning_mode internally."""
        # Lazy-init PyHop on first run
        if not self._pyhop_initialized:
            self._init_pyhop(mem)

        # Skip if plan already in progress
        if mem.current_plan is not None and not mem.current_plan.completed:
            return None

        goals = mem.current_goals
        if not goals:
            return None

        # Enable dry-run so PyHop's apply_action calls don't mutate world
        old_mode = self._world._planning_mode
        self._world._planning_mode = True

        try:
            # Try MIDCA PyHop first
            plan = None
            if self._pyhop_working:
                plan = self._plan_with_pyhop(mem, goals)
                if plan:
                    return plan  # early return — MUST restore mode below!

            # Fallback: simple reactive planner
            plan = _simple_blocksworld_planner(mem.current_state, goals)
            return plan
        finally:
            # ALWAYS restore _planning_mode, even on early return
            self._world._planning_mode = old_mode

    def _plan_with_pyhop(self, mem, goals) -> Optional[Plan]:
        """Use MIDCA PyHop to generate a plan."""
        cognitive_goals = list(mem.current_goals)  # save before try
        old_mode = self._world._planning_mode
        try:
            from midca.goals import Goal as MidcaGoal

            # Convert our Goal objects -> MIDCA Goal objects (they use different APIs)
            midca_goals = []
            for g in (goals if isinstance(goals, list) else [goals]):
                midca_goal = MidcaGoal(*g.args, predicate=g.predicate)
                midca_goals.append(midca_goal)

            # Set up MIDCA memory format
            mem.set(mem.CURRENT_GOALS, midca_goals)
            mem.set(mem.STATES, [self._world._midca_world])

            # Enable dry-run mode
            self._world._planning_mode = True

            # Run PyHop planner
            self._pyhop_planner.run(0, verbose=0)

            # Read back plan from goal graph
            gg = mem.goal_graph
            if gg and hasattr(gg, 'plans') and gg.plans:
                midca_plan = list(gg.plans)[-1]
                actions = []
                for a in midca_plan.actions:
                    name = str(a.op) if hasattr(a, 'op') else str(a)
                    args = tuple(str(x) for x in (a.args if hasattr(a, 'args') else []))
                    actions.append(Action(name, args))
                plan = Plan(actions=actions, goals=goals if isinstance(goals, list) else [goals])
                return plan
        except Exception as e:
            logger.warning("PyHop planning failed: %s", e)
        finally:
            # ALWAYS restore — even on early return or exception
            self._world._planning_mode = old_mode
            mem.set(mem.CURRENT_GOALS, cognitive_goals)
        return None


def _user_to_midca(name: str) -> str:
    """Map user-level block name (A→A_, B→B_, etc.) to MIDCA domain name."""
    if name in ('A', 'B', 'C', 'D'):
        return name + '_'
    return name


def _simple_blocksworld_planner(world: CognitiveWorld, goals) -> Optional[Plan]:
    """
    HTN-style recursive planner for blocksworld supporting on, on-table, and clear goals.

    Handles:
    - on(x, y) goals: recursively clear y, then pickup + stack x onto y
    - on-table(x) goals: unstack x from wherever it is, then putdown x
    - clear(x) goals: recursively move whatever is on x to the table
    """
    from cognitive.world import Atom

    # Separate goals by predicate
    on_goals = []
    ontable_goals = []
    clear_goals = []
    for g in goals:
        if not isinstance(g, Goal):
            continue
        if g.predicate == "on" and len(g.args) == 2:
            on_goals.append(g)
        elif g.predicate == "on-table" and len(g.args) == 1:
            ontable_goals.append(g)
        elif g.predicate == "clear" and len(g.args) == 1:
            clear_goals.append(g)

    # If no relevant goals, return None
    if not on_goals and not ontable_goals and not clear_goals:
        return None

    # Simulated world state for planning (separate from real world)
    sim_atoms = {a for a in world.atoms}

    def sim_atom_true(pred, *args):
        return any(a.predicate == pred and a.args == args for a in sim_atoms)

    def sim_where_is(block):
        """Return where block is (parent block, 'table', or None if holding)."""
        for atom in sim_atoms:
            if atom.predicate == "on" and atom.args[0] == block:
                return atom.args[1]
        if any(atom.predicate == "on-table" and atom.args[0] == block for atom in sim_atoms):
            return "table"
        return None

    def sim_remove(pred, *args):
        for a in list(sim_atoms):
            if a.predicate == pred and a.args == args:
                sim_atoms.discard(a)
                break

    def sim_add(pred, *args):
        sim_atoms.add(Atom(pred, *args))

    def apply_action(name, args_tuple):
        """Apply action effects to simulated state."""
        if name == "unstack" and len(args_tuple) == 2:
            b, c = args_tuple
            sim_remove("arm-empty")
            sim_remove("on", b, c)
            sim_remove("clear", b)
            sim_add("holding", b)
            sim_add("clear", c)
        elif name == "putdown" and len(args_tuple) == 1:
            b = args_tuple[0]
            sim_remove("holding", b)
            sim_add("on-table", b)
            sim_add("clear", b)
            sim_add("arm-empty")
        elif name == "pickup" and len(args_tuple) == 1:
            b = args_tuple[0]
            sim_remove("arm-empty")
            sim_remove("on-table", b)
            sim_remove("clear", b)
            sim_add("holding", b)
        elif name == "stack" and len(args_tuple) == 2:
            b, c = args_tuple
            sim_remove("holding", b)
            sim_remove("clear", c)
            sim_add("on", b, c)
            sim_add("clear", b)
            sim_add("arm-empty")

    def is_applicable(name, args_tuple):
        if name == "pickup" and len(args_tuple) == 1:
            b = args_tuple[0]
            return (sim_atom_true("arm-empty")
                    and sim_atom_true("clear", b)
                    and sim_atom_true("on-table", b))
        elif name == "putdown" and len(args_tuple) == 1:
            b = args_tuple[0]
            return sim_atom_true("holding", b)
        elif name == "unstack" and len(args_tuple) == 2:
            b, c = args_tuple
            return (sim_atom_true("arm-empty")
                    and sim_atom_true("clear", b)
                    and sim_atom_true("on", b, c))
        elif name == "stack" and len(args_tuple) == 2:
            b, c = args_tuple
            return sim_atom_true("holding", b) and sim_atom_true("clear", c)
        return False

    # ── Helper: achieve clear ────────────────────────────────────────────────
    def achieve_clear(block, depth=0):
        """Recursively clear a block by moving its occupant to the table."""
        if depth > 20:
            return None
        if sim_atom_true("clear", block):
            return []
        occupant = None
        for atom in sim_atoms:
            if atom.predicate == "on" and atom.args[1] == block:
                occupant = atom.args[0]
                break
        if occupant is None:
            return []
        if not sim_atom_true("clear", occupant):
            sub = achieve_clear(occupant, depth+1)
            if sub is None:
                return None
            rest = achieve_clear(block, depth+1)
            if rest is None:
                return None
            return sub + rest
        # Occupant is clear — unstack and putdown
        loc = sim_where_is(occupant)
        if loc is None or loc == "table":
            if is_applicable("pickup", (occupant,)):
                apply_action("pickup", (occupant,))
                result = [("pickup", (occupant,))]
            else:
                return None
        else:
            if is_applicable("unstack", (occupant, loc)):
                apply_action("unstack", (occupant, loc))
                result = [("unstack", (occupant, loc))]
            else:
                return None
        if is_applicable("putdown", (occupant,)):
            apply_action("putdown", (occupant,))
            result.append(("putdown", (occupant,)))
        else:
            return None
        rest = achieve_clear(block, depth+1)
        if rest is None:
            return None
        return result + rest

    # ── Helper: achieve putdown (on-table) ──────────────────────────────────
    def achieve_putdown(block, depth=0):
        """Achieve on-table(block)."""
        if depth > 20:
            return None
        if sim_atom_true("on-table", block):
            return []
        loc = sim_where_is(block)
        if loc is None:
            return None
        if loc == "table":
            return []  # already on table
        if not sim_atom_true("clear", block):
            sub = achieve_clear(block, depth+1)
            if sub is None:
                return None
            rest = achieve_putdown(block, depth+1)
            if rest is None:
                return None
            return sub + rest
        # Block is clear and on another block — unstack and putdown
        if is_applicable("unstack", (block, loc)):
            apply_action("unstack", (block, loc))
            result = [("unstack", (block, loc))]
        else:
            return None
        if is_applicable("putdown", (block,)):
            apply_action("putdown", (block,))
            result.append(("putdown", (block,)))
        else:
            return None
        return result

    # ── Helper: achieve on(x, y) ────────────────────────────────────────────
    def achieve_on(x, y, depth=0):
        """Achieve on(x, y)."""
        if depth > 20:
            return None
        if sim_atom_true("on", x, y):
            return []
        if not sim_atom_true("clear", y):
            occupant = None
            for atom in sim_atoms:
                if atom.predicate == "on" and atom.args[1] == y:
                    occupant = atom.args[0]; break
            if occupant:
                sub = achieve_clear(y, depth+1)
                if sub is None:
                    return None
                rest = achieve_on(x, y, depth+1)
                if rest is None:
                    return None
                return sub + rest
        x_loc = sim_where_is(x)
        if x_loc is None:
            return None
        if x_loc == "table":
            if is_applicable("pickup", (x,)):
                apply_action("pickup", (x,))
                result = [("pickup", (x,))]
            else:
                return None
        else:
            if is_applicable("unstack", (x, x_loc)):
                apply_action("unstack", (x, x_loc))
                result = [("unstack", (x, x_loc))]
            else:
                return None
        if is_applicable("stack", (x, y)):
            apply_action("stack", (x, y))
            result.append(("stack", (x, y)))
        else:
            return None
        return result

    # ── Main: execute on-table/clear goals first ─────────────────────────────
    all_actions = []

    for g in ontable_goals:
        seq = achieve_putdown(g.args[0])
        if seq is None:
            return None
        all_actions.extend(seq)

    for g in clear_goals:
        seq = achieve_clear(g.args[0])
        if seq is None:
            return None
        all_actions.extend(seq)

    # ── Main: execute on goals ──────────────────────────────────────────────
    for g in on_goals:
        x, y = g.args
        seq = achieve_on(x, y)
        if seq is None:
            return None
        all_actions.extend(seq)

    if not all_actions:
        return Plan(actions=[], name="blocksworld")
    action_objs = [Action(name, tuple(args), None) for name, args in all_actions]
    return Plan(actions=action_objs, name="blocksworld")


def _create_blocksworld() -> CognitiveWorld:
    """
    Blocks world backed by the real MIDCA world state.

    Uses MIDCA's defstate so PyHop and the cognitive world agree on
    the initial configuration (D_ on B_ on A_, C_ on table).
    """
    midca_world = None
    try:
        import sys as _sys
        _sys.path.insert(0, '/data/midca')
        from midca.worldsim import domainread, stateread
        midca_world = domainread.load_domain(
            '/data/midca/midca/domains/blocksworld/arsonist.sim')
        stateread.apply_state_file(
            midca_world,
            '/data/midca/midca/domains/blocksworld/states/defstate.sim')
    except Exception as e:
        logger.warning("Could not load MIDCA world: %s", e)

    world = CognitiveWorld(midca_world=midca_world)

    # Register local operators for non-MIDCA path (expected world uses this)
    from cognitive.world import Operator, Atom

    world.register_operator(Operator(
        "pickup", ["x"],
        lambda s, x: s.atom_true("clear", x) and s.atom_true("on-table", x) and s.atom_true("arm-empty"),
        lambda s, x: (
            s.remove_atom(Atom("on-table", x)),
            s.remove_atom(Atom("clear", x)),
            s.remove_atom(Atom("arm-empty")),
            s.add_atom(Atom("holding", x)),
        )
    ))

    world.register_operator(Operator(
        "putdown", ["x"],
        lambda s, x: s.atom_true("holding", x),
        lambda s, x: (
            s.remove_atom(Atom("holding", x)),
            s.add_atom(Atom("on-table", x)),
            s.add_atom(Atom("clear", x)),
            s.add_atom(Atom("arm-empty")),
        )
    ))

    world.register_operator(Operator(
        "stack", ["x", "y"],
        lambda s, x, y: s.atom_true("holding", x) and s.atom_true("clear", y),
        lambda s, x, y: (
            s.remove_atom(Atom("holding", x)),
            s.remove_atom(Atom("clear", y)),
            s.add_atom(Atom("on", x, y)),
            s.add_atom(Atom("clear", x)),
            s.add_atom(Atom("arm-empty")),
        )
    ))

    world.register_operator(Operator(
        "unstack", ["x", "y"],
        lambda s, x, y: s.atom_true("clear", x) and s.atom_true("on", x, y),
        lambda s, x, y: (
            s.remove_atom(Atom("on", x, y)),
            s.remove_atom(Atom("clear", y)),
            s.add_atom(Atom("clear", x)),
            s.remove_atom(Atom("arm-empty")),
            s.add_atom(Atom("holding", x)),
        )
    ))

    return world


class _NoOpModule(BaseModule):
    """No-op module for phases we don't need yet."""
    def __init__(self, name: str):
        super().__init__(name=name)
    def run(self, mgr, mem, cycle):
        return None


# ── Tool schemas ─────────────────────────────────────────────────────────────


COGNITIVE_GOAL_ADD_SCHEMA = {
    "name": "cognitive_goal_add",
    "description": """Add a goal to the cognitive GoalGraph.

Goals are predicates with arguments:
  on(A, B)      — block A should be on block B
  on(A, table)  — block A should be on the table
  clear(B)      — block B should be clear (no blocks on top)
  holding(A)    — the arm should be holding block A

Goals are inserted into the GoalGraph. The next cognitive_simulate call
will generate a plan to achieve them.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "predicate": {
                "type": "string",
                "description": "Goal predicate name (e.g. 'on', 'clear', 'holding')",
            },
            "args": {
                "type": "array",
                "description": "Goal arguments as a list of strings",
                "items": {"type": "string"},
            },
        },
        "required": ["predicate", "args"],
    },
}

COGNITIVE_PLAN_GENERATE_SCHEMA = {
    "name": "cognitive_plan_generate",
    "description": """Generate a Plan from the current goals WITHOUT executing it.

Reads current goals from GoalGraph, runs the HTN planner (MIDCA PyHop if available,
fallback to reactive planner), and returns the full action sequence for review.

Use this to inspect what the planner intends to do before committing
to execution with cognitive_simulate.""",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

COGNITIVE_SIMULATE_SCHEMA = {
    "name": "cognitive_simulate",
    "description": """Run n complete MIDCA cognitive cycles.

Phase order: PERCEIVE → INTERPRET → INTEND → PLAN → ACT → SIMULATE → EVALUATE

SIMULATE applies the action selected by the PREVIOUS call's ACT phase.
INTEND selects from GoalGraph.
PLAN generates a plan for current goals.
ACT selects the next action to be applied by the NEXT call's SIMULATE phase.
EVALUATE checks goal achievement.

One-cycle delay between planning and execution:
- cognitive_simulate(1): plans, selects action (nothing applied yet)
- cognitive_simulate(2): applies action from C1, selects next
- cognitive_simulate(3): applies action from C2, etc.

Returns structured result with cycles run, goals achieved, actions taken,
current world state, and plan status.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "cycles": {
                "type": "integer",
                "description": "Number of cognitive cycles to run (default: 1, max: 10)",
                "default": 1,
            },
        },
    },
}

COGNITIVE_PLAN_SHOW_SCHEMA = {
    "name": "cognitive_plan_show",
    "description": """Display the current cognitive state:
- GoalGraph: all goals (achieved, pending, failed)
- Current plan: actions queued for execution
- World state: current atoms in the world model
- Actions history: what has been executed so far
- Trace summary: cycles run, phases executed""",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


# ── Tool handlers ─────────────────────────────────────────────────────────────


def cognitive_goal_add(predicate: str, args: List[str]) -> Dict[str, Any]:
    """Add a goal to the GoalGraph.

    Block names (A-D) are automatically mapped to MIDCA domain names (A_-D_).
    """
    state = _get_cog_state()
    gg = state["goal_graph"]
    mem = state["memory"]

    midca_args = [_user_to_midca(a) for a in args]
    goal = Goal(predicate, *midca_args)
    gg.insert(goal)

    # Sync to mem.current_goals
    current = mem.current_goals
    if goal not in current:
        current = current + [goal]
        mem.current_goals = current

    return {
        "goal": str(goal),
        "goal_graph_size": len(gg.all_goals()),
        "unrestricted_goals": [str(g) for g in gg.get_unrestricted_goals()],
        "message": f"Goal {goal} inserted — call cognitive_simulate to plan and execute",
    }


def cognitive_plan_generate() -> Dict[str, Any]:
    """Generate a plan from current goals without executing it."""
    state = _get_cog_state()
    mem = state["memory"]
    world = state["world"]
    gg = state["goal_graph"]

    goals = mem.current_goals
    if not goals:
        return {"plan": None, "message": "No current goals — nothing to plan"}

    # Filter out already-satisfied goals (Phase 7: preserve achieved goals).
    # Achieved goals remain in the goal graph but are excluded from planning
    # so the planner won't generate actions that undo them.
    active_goals = [
        g for g in goals
        if not world.atom_true(g.predicate, *g.args)
    ]
    # PhaseManager.run() would trigger all phases including SIMULATE which would
    # apply actions to the world. We only want planning, not execution.
    planner_mod = None
    for mod in state["manager"].modules.get(Phase.PLAN, []):
        if hasattr(mod, '_pyhop_planner') or hasattr(mod, '_simple_planner'):
            planner_mod = mod
            break

    plan = None
    if planner_mod:
        # Swap current_goals to only active goals so planner won't try to
        # re-achieve already-satisfied goals (Phase 7 multi-goal preservation)
        saved_goals = mem.current_goals
        mem.current_goals = active_goals
        try:
            plan = planner_mod._run_planner(mem, state["cycles"])
        finally:
            mem.current_goals = saved_goals
        # Only accept non-empty plans from MIDCA planner
        if plan and len(plan.actions) > 0:
            mem.current_plan = plan
            plan = None  # signal fallback succeeded

    if plan is None or len(getattr(plan, 'actions', [])) == 0:
        # Fallback: runs when planner failed or returned empty plan
        plan = _simple_blocksworld_planner(world, active_goals)
        if plan:
            mem.current_plan = plan

    if plan is None:
        return {"plan": None, "goals": [str(g) for g in goals],
                "message": "Planner returned no plan"}

    return {
        "plan": {
            "name": plan.name,
            "goals": [str(g) for g in (plan.goals if isinstance(plan.goals, list) else [plan.goals])],
            "steps": [{"index": i, "name": a.name, "args": a.args} for i, a in enumerate(plan.actions)],
            "total_steps": len(plan.actions),
        },
        "goals": [str(g) for g in goals],
        "message": f"Plan '{plan.name}' generated — call cognitive_simulate to execute",
    }


# ── Trace / Callback API ──────────────────────────────────────────────────────

def cognitive_trace_register(
    trace_phase_end: bool = False,
    trace_cycle_end: bool = False,
) -> Dict[str, Any]:
    """
    Register callbacks to observe the cognitive cycle as it runs.

    NOTE: Callbacks are registered on the CURRENT state. If cognitive_plan_task
    is called with reset=True (default), the state is cleared and callbacks are
    lost. To trace a task, use cognitive_plan_task(goals, trace=True) instead.

    Args:
        trace_phase_end: If True, print a message after each phase ends.
        trace_cycle_end: If True, print a message after each cycle ends.

    Returns registration confirmation.
    """
    state = _get_cog_state()
    mgr = state["manager"]

    registered = []

    if trace_phase_end:
        def phase_logger(cycle, phase, memory):
            cp = memory.current_plan
            step = getattr(cp, 'step', 0) if cp else 0
            remaining = len(cp.actions) - step if cp and hasattr(cp, 'actions') else 0
            print(f"  [C{cycle}] {phase} done | plan step={step}/{remaining if cp else 0}")
        mgr.on_phase_end(phase_logger)
        registered.append({"type": "on_phase_end"})

    if trace_cycle_end:
        def cycle_logger(cycle):
            print(f"=== cycle {cycle} done ===")
        mgr.on_cycle_end(cycle_logger)
        registered.append({"type": "on_cycle_end"})

    return {
        "registered": registered,
        "message": f"Registered {len(registered)} trace callback(s)",
    }


def cognitive_trace_list() -> Dict[str, Any]:
    """List all currently registered trace callbacks."""
    state = _get_cog_state()
    mgr = state["manager"]
    return {
        "phase_end_count": len(mgr._on_phase_end),
        "cycle_end_count": len(mgr._on_cycle_end),
        "phases": list(mgr.DEFAULT_PHASES),
    }


def cognitive_simulate(cycles: int = 1) -> Dict[str, Any]:
    """
    Run n complete cognitive cycles.

    Phase order: PERCEIVE → INTERPRET → INTEND → PLAN → ACT → SIMULATE → EVALUATE

    ACT selects, SIMULATE applies, EVALUATE checks goal achievement.

    Callback support: call cognitive_trace_register(fn) to receive
    (cycle_num, phase_name, world_snapshot, actions_this_cycle) after
    each cycle completes.
    """
    state = _get_cog_state()
    mgr = state["manager"]
    mem = state["memory"]
    world = state["world"]

    cycles = max(1, min(cycles, 10))
    cycles_before = mgr.cycle_count

    # Capture goals before
    gg = state["goal_graph"]
    goals_before_ids = {id(n.goal) for n in gg.nodes} if gg else set()

    # Run cycles
    executed = mgr.several_cycles(cycles)
    cycles_after = mgr.cycle_count
    state["cycles"] = cycles_after

    # Current plan
    goals_after_ids = {id(n.goal) for n in gg.nodes} if gg else set()
    achieved_ids = goals_before_ids - goals_after_ids
    goals_achieved = []
    if gg:
        for node in gg.nodes:
            if id(node.goal) in achieved_ids:
                goals_achieved.append(str(node.goal))

    # Actions history
    actions_taken = []
    for entry in mem.actions_history:
        if isinstance(entry, list) and len(entry) >= 4:
            actions_taken.append({
                "cycle": entry[0],
                "action": entry[1],
                "args": entry[2],
                "status": entry[3],
            })

    # Current plan
    current_plan = mem.current_plan
    plan_status = None
    if current_plan:
        remaining = current_plan.get_remaining_steps() if hasattr(current_plan, 'get_remaining_steps') else []
        plan_status = {
            "name": current_plan.name,
            "completed": getattr(current_plan, 'completed', False),
            "remaining_steps": [{"name": a.name, "args": a.args} for a in remaining],
            "total_steps": len(current_plan.actions),
            "current_step": getattr(current_plan, 'step', 0),
        }

    # Discrepancy
    discrepancy = mem.get("__discrepancy")
    disc_report = None
    if discrepancy is not None:
        if isinstance(discrepancy, str):
            disc_report = {"type": "action_failed", "message": discrepancy}
        elif len(discrepancy) == 1:
            disc_report = {"type": "action_failed", "message": discrepancy[0]}
        else:
            missing, extra = discrepancy
            disc_report = {
                "type": "state_mismatch",
                "missing": [str(a) for a in missing],
                "extra": [str(a) for a in extra],
            }

    return {
        "cycles_run": cycles_after - cycles_before,
        "total_cycles": cycles_after,
        "goals_achieved": goals_achieved,
        "actions_taken": actions_taken[-cycles:],  # last n actions
        "current_plan": plan_status,
        "discrepancy": disc_report,
        "world_state": sorted([str(a) for a in world.atoms]),
        "message": f"Ran {executed} cycles — {len(goals_achieved)} goals achieved",
    }


COGNITIVE_PLAN_TASK_SCHEMA = {
    "name": "cognitive_plan_task",
    "description": """Plan and execute a task using the MIDCA cognitive cycle.

Give the agent a structured way to use MIDCA for complex multi-step planning.
You specify goals as predicate+args pairs, MIDCA generates the plan and executes it.

Blocksworld goals:
  on(A, B)      — block A should be on block B
  on(A, table)  — block A should be on the table
  clear(B)      — block B should have nothing on top
  holding(A)    — arm should be holding block A

Example goals:
  [{"predicate": "on", "args": ["A", "B"]}]  → move A onto B

This is a convenience wrapper that calls cognitive_goal_add,
cognitive_plan_generate, and cognitive_simulate in sequence.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "description": "List of goals, each a {predicate, args} pair",
                "items": {
                    "type": "object",
                    "properties": {
                        "predicate": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["predicate", "args"],
                },
            },
            "max_cycles": {
                "type": "integer",
                "description": "Maximum cognitive cycles to run (default: 20)",
                "default": 20,
            },
            "trace": {
                "type": "boolean",
                "description": "If true, print per-phase and per-cycle trace during execution",
                "default": False,
            },
        },
        "required": ["goals"],
    },
}


def cognitive_plan_task(
    goals: List[Dict[str, Any]],
    max_cycles: int = 20,
    trace: bool = False,
    reset: bool = True,
) -> Dict[str, Any]:
    """
    High-level task planning interface: goals → plan → execute.

    Wrapper that calls cognitive_goal_add + cognitive_plan_generate +
    cognitive_simulate(max_cycles) and returns a summary.

    Args:
        reset: If True (default), clear all prior state and start fresh.
               If False, attempt to restore state from disk first, then
               continue adding goals and planning on top.
    """
    if reset:
        reset_cognitive_state()
    else:
        restored = _restore_cognitive_state()
        if not restored:
            # Nothing to restore; fall back to fresh state
            reset_cognitive_state()
        # NOTE: when reset=False, we do NOT call reset_cognitive_state()
        # because that would create a new PhaseManager/Module instances
        # whose world references point to the NEW world, not the restored one.
        # The old PhaseManager still holds the original world reference.
        # _restore_cognitive_state() updates atoms in-place in that original world.

    trace_events = []
    if trace:
        def phase_logger(cycle, phase, memory):
            cp = memory.current_plan
            step = getattr(cp, 'step', 0) if cp else 0
            total = len(cp.actions) if cp and hasattr(cp, 'actions') else 0
            world = getattr(memory, 'current_state', None)
            holding = [str(a.args[0]) for a in world.atoms if a.predicate == "holding"] if world else []
            msg = f"  [C{cycle}] {phase} | plan {step}/{total} | holding={holding}"
            print(msg)
            trace_events.append({"cycle": cycle, "phase": phase, "step": step, "total": total, "holding": holding})
        def cycle_logger(cycle):
            msg = f"=== cycle {cycle} done ==="
            print(msg)
            trace_events.append({"cycle": cycle, "event": "cycle_end"})
        state = _get_cog_state()
        state["manager"].on_phase_end(phase_logger)
        state["manager"].on_cycle_end(cycle_logger)

    # 1. Add all goals
    for g in goals:
        cognitive_goal_add(g["predicate"], g["args"])

    # 2. Generate plan
    state = _get_cog_state()
    world = state["world"]
    mem = state["memory"]
    plan_result = cognitive_plan_generate()
    if not plan_result.get("plan"):
        # Check if goals are already satisfied in the world.
        all_satisfied = all(
            world.atom_true(g.predicate, *g.args)
            for g in mem.current_goals
        )
        if all_satisfied:
            result = {
                "success": True,
                "goals_achieved": True,
                "plan_completed": True,
                "cycles_used": 0,
                "actions_taken": [],
                "world_state": {
                    "holding": [str(a.args[0]) for a in world.atoms if a.predicate == "holding"],
                    "on": [(str(a.args[0]), str(a.args[1])) for a in world.atoms if a.predicate == "on"],
                },
                "message": "Goals already satisfied (0 actions needed)",
            }
            _persist_cognitive_state()
            return result
        result = {
            "success": False,
            "message": plan_result.get("message", "No plan could be generated"),
            "goals_added": len(goals),
        }
        _persist_cognitive_state()
        return result
    # 3. Execute up to max_cycles, with failure detection and re-planning
    mem = state["memory"]

    # Check if plan has 0 steps (trivially satisfied or already done)
    first_plan_info = plan_result["plan"]
    if first_plan_info["total_steps"] == 0:
        result = {
            "success": True,
            "goals_achieved": True,
            "plan_completed": True,
            "cycles_used": 0,
            "actions_taken": [],
            "total_steps": 0,
            "replan_count": 0,
            "failed_steps": [],
            "world_state": {
                "holding": [str(a.args[0]) for a in world.atoms if a.predicate == "holding"],
                "on": [(str(a.args[0]), str(a.args[1])) for a in world.atoms if a.predicate == "on"],
            },
            "trace_events": trace_events if trace else [],
            "message": "Goals already satisfied (0 actions needed)",
        }
        _persist_cognitive_state()
        return result

    actions_taken = []
    cycles_used = 0
    replan_count = 0
    failed_steps = []  # (cycle, action, reason)

    for i in range(1, max_cycles + 1):
        # Snapshot world state before this cycle
        world_before = frozenset(str(a) for a in world.atoms)

        r = cognitive_simulate(1)
        cycles_used += 1

        current_plan = mem.current_plan
        applied_action = r.get("actions_taken", [])[-1] if r.get("actions_taken") else None

        # Check: did the action actually change the world?
        world_after = frozenset(str(a) for a in world.atoms)
        world_changed = (world_before != world_after)
        plan_not_done = not (current_plan and current_plan.completed)

        if applied_action and plan_not_done and not world_changed:
            # Action failed — world didn't change but goal not achieved
            failed_steps.append((cycles_used, applied_action, "world unchanged"))
            # Clear current plan and re-plan from current world state
            mem.current_plan = None
            plan_result = cognitive_plan_generate()
            if not plan_result["plan"]:
                # Re-planning also produced nothing — give up
                break
            replan_count += 1
            current_plan = mem.current_plan
            if trace:
                print(f"  [C{cycles_used}] ⚠️ action {applied_action} failed, replanned (replan #{replan_count})")

        if applied_action:
            actions_taken.append(applied_action)
        if current_plan and current_plan.completed:
            break

    # 4. Build summary
    current_plan = mem.current_plan
    # Goal achieved if: goal atom exists in world state
    goal_achieved = False
    if goals:
        g = goals[0]
        for a in world.atoms:
            if a.predicate == g["predicate"]:
                args_match = all(str(a.args[i]) == f"{g['args'][i]}_" for i in range(len(g["args"])))
                if args_match:
                    goal_achieved = True
                    break

    plan_info = plan_result["plan"]

    result = {
        "success": goal_achieved or (current_plan and current_plan.completed),
        "goals_achieved": goal_achieved,
        "plan_completed": current_plan.completed if current_plan else False,
        "cycles_used": cycles_used,
        "actions_taken": actions_taken,
        "total_steps": plan_info["total_steps"],
        "replan_count": replan_count,
        "failed_steps": [{"cycle": f, "action": a, "reason": r} for f, a, r in failed_steps],
        "world_state": {
            "holding": [str(a.args[0]) for a in world.atoms if a.predicate == "holding"],
            "on": [(str(a.args[0]), str(a.args[1])) for a in world.atoms if a.predicate == "on"],
        },
        "trace_events": trace_events if trace else [],
        "message": f"Plan {plan_info['total_steps']} steps, {len(actions_taken)} executed in {cycles_used} cycles, goal={'achieved' if goal_achieved else 'not achieved'}",
    }
    _persist_cognitive_state()
    return result


def cognitive_plan_show() -> Dict[str, Any]:
    """Show the current cognitive state snapshot."""
    state = _get_cog_state()
    mgr = state["manager"]
    mem = state["memory"]
    world = state["world"]
    gg = state["goal_graph"]

    # Goal graph status
    goal_nodes = []
    if gg:
        for node in gg.nodes:
            status = "achieved" if node.achieved else ("failed" if node.completed else "pending")
            goal_nodes.append({"goal": str(node.goal), "status": status})

    # Current plan
    current_plan = mem.current_plan
    plan_info = None
    if current_plan:
        plan_info = {
            "name": current_plan.name,
            "completed": getattr(current_plan, 'completed', False),
            "steps": [
                {"index": i, "name": a.name, "args": a.args,
                 "marker": "→" if i == getattr(current_plan, 'step', 0) else " "}
                for i, a in enumerate(current_plan.actions)
            ],
        }

    # Actions history
    actions = []
    for entry in mem.actions_history:
        if isinstance(entry, list) and len(entry) >= 4:
            actions.append({"cycle": entry[0], "action": entry[1], "args": entry[2], "status": entry[3]})
        elif isinstance(entry, dict):
            actions.append(entry)

    # Discrepancy
    discrepancy = mem.get("__discrepancy")
    disc_info = None
    if discrepancy is not None:
        if isinstance(discrepancy, str):
            disc_info = {"type": "action_failed", "message": discrepancy}
        elif len(discrepancy) == 1:
            disc_info = {"type": "action_failed", "message": discrepancy[0]}
        else:
            missing, extra = discrepancy
            disc_info = {
                "type": "state_mismatch",
                "missing": [str(a) for a in missing],
                "extra": [str(a) for a in extra],
            }

    return {
        "total_cycles": mgr.cycle_count,
        "goals": goal_nodes,
        "unrestricted_goals": [str(g) for g in (gg.get_unrestricted_goals() if gg else [])],
        "current_plan": plan_info,
        "world_state": sorted([str(a) for a in world.atoms]),
        "discrepancy": disc_info,
        "actions_history": actions,
    }


def cognitive_plan_nl(task: str, max_cycles: int = 20) -> Dict[str, Any]:
    """
    High-level natural language task planning interface.

    Translates a natural language task description into goals using an LLM,
    then plans and executes via cognitive_plan_task.

    Args:
        task: Natural language task description, e.g. "把 D 放到 A 上" or
              "move block D onto block A"
        max_cycles: Maximum cognitive cycles to run (default 20)

    Returns:
        Same structure as cognitive_plan_task plus a 'goal_translation' field
        showing the LLM's parsed goals.
    """
    try:
        from agent.auxiliary_client import call_llm
    except ImportError:
        return {
            "success": False,
            "message": "LLM client not available (agent.auxiliary_client not importable)",
            "goal_translation": None,
        }

    prompt = f"""You are a blocksworld task translator. The world has 4 blocks: A, B, C, D.
Initial state:
- D is on B, B is on A, C is on the table
- All blocks are clear except B (has D on it) and A (has B on it)
- The arm is empty

Translate the user's natural language instruction into a JSON array of goal predicates.

Rules:
- "put X on Y" or "move X onto Y" or "把 X 放到 Y 上" → on(X, Y)
- "put X on table" or "put X down" or "把 X 放下" → on-table(X)
- "clear X" → clear(X)
- Only output the JSON array, nothing else

Examples:
User: "把 D 放到 A 上"
Output: [{{"predicate": "on", "args": ["D", "A"]}}]

User: "move D to the table"
Output: [{{"predicate": "on-table", "args": ["D"]}}]

User: "{task}"
Output:"""

    try:
        response = call_llm(
            task="agent",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
    except Exception as e:
        return {
            "success": False,
            "message": f"LLM translation failed: {e}",
            "goal_translation": None,
        }

    # Parse LLM response
    import json, re
    content_clean = re.sub(r"^```(?:json)?\s*", "", content).strip()
    content_clean = re.sub(r"\s*```$", "", content_clean).strip()

    try:
        goals = json.loads(content_clean)
    except json.JSONDecodeError:
        return {
            "success": False,
            "message": f"LLM output is not valid JSON: {content_clean[:200]}",
            "goal_translation": {"raw": content, "parsed": None, "error": "JSON parse failed"},
        }

    if not isinstance(goals, list):
        return {
            "success": False,
            "message": f"LLM output must be a JSON array, got: {type(goals).__name__}",
            "goal_translation": {"raw": content, "parsed": goals},
        }

    # Validate goal structure
    valid_preds = {"on", "on-table", "clear"}
    for g in goals:
        if not isinstance(g, dict) or "predicate" not in g or "args" not in g:
            return {
                "success": False,
                "message": f"Invalid goal structure: {g}",
                "goal_translation": {"raw": content, "parsed": goals},
            }
        if g["predicate"] not in valid_preds:
            return {
                "success": False,
                "message": f"Unknown predicate: {g['predicate']}",
                "goal_translation": {"raw": content, "parsed": goals},
            }
        if not isinstance(g["args"], list):
            return {
                "success": False,
                "message": f"args must be a list: {g}",
                "goal_translation": {"raw": content, "parsed": goals},
            }

    # Delegate to cognitive_plan_task
    try:
        result = cognitive_plan_task(goals, max_cycles=max_cycles)
    except Exception as e:
        return {
            "success": False,
            "message": f"cognitive_plan_task failed: {e}",
            "goal_translation": {"raw": content, "parsed": goals},
        }

    result["goal_translation"] = {"raw": content, "parsed": goals}
    return result


# ── Registration ─────────────────────────────────────────────────────────────


def check_cognitive_requirements() -> tuple:
    """Check if MIDCA is available on sys.path."""
    import sys
    if '/data/midca' in sys.path:
        return True, ""
    # Try to add it
    if os.path.isdir('/data/midca'):
        sys.path.insert(0, '/data/midca')
        return True, ""
    return False, "MIDCA not found at /data/midca — clone from https://github.com/COLAB2/midca"


registry.register(
    name="cognitive_goal_add",
    toolset="cognitive",
    schema=COGNITIVE_GOAL_ADD_SCHEMA,
    handler=cognitive_goal_add,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_plan_generate",
    toolset="cognitive",
    schema=COGNITIVE_PLAN_GENERATE_SCHEMA,
    handler=cognitive_plan_generate,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_simulate",
    toolset="cognitive",
    schema=COGNITIVE_SIMULATE_SCHEMA,
    handler=cognitive_simulate,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_plan_task",
    toolset="cognitive",
    schema=COGNITIVE_PLAN_TASK_SCHEMA,
    handler=cognitive_plan_task,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_plan_nl",
    toolset="cognitive",
    schema={
        "name": "cognitive_plan_nl",
        "description": "Plan and execute a blocksworld task from natural language. Translates the task into goals via LLM, then runs the cognitive cycle.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Natural language task description, e.g. '把 D 放到 A 上' or 'move D onto A'",
                },
                "max_cycles": {
                    "type": "integer",
                    "description": "Maximum cognitive cycles to run (default: 20)",
                    "default": 20,
                },
            },
            "required": ["task"],
        },
    },
    handler=cognitive_plan_nl,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_plan_show",
    toolset="cognitive",
    schema=COGNITIVE_PLAN_SHOW_SCHEMA,
    handler=cognitive_plan_show,
    check_fn=check_cognitive_requirements,
)

COGNITIVE_TRACE_REGISTER_SCHEMA = {
    "name": "cognitive_trace_register",
    "description": "Register callbacks to observe the cognitive cycle as it runs. Callbacks are invoked after each phase ends and/or after each cycle ends. Useful for debugging, logging, and real-time monitoring of the planning process.",
    "input_schema": {
        "type": "object",
        "properties": {
            "trace_phase_end": {
                "type": "boolean",
                "description": "If true, log every phase transition (PERCEIVE→INTERPRET→INTEND→PLAN→ACT→SIMULATE→EVALUATE)",
                "default": False,
            },
            "trace_cycle_end": {
                "type": "boolean",
                "description": "If true, log every cycle boundary",
                "default": False,
            },
        },
    },
}

COGNITIVE_TRACE_LIST_SCHEMA = {
    "name": "cognitive_trace_list",
    "description": "List all currently registered trace callbacks and available phases.",
    "input_schema": {"type": "object", "properties": {}},
}

registry.register(
    name="cognitive_trace_register",
    toolset="cognitive",
    schema=COGNITIVE_TRACE_REGISTER_SCHEMA,
    handler=cognitive_trace_register,
    check_fn=check_cognitive_requirements,
)

registry.register(
    name="cognitive_trace_list",
    toolset="cognitive",
    schema=COGNITIVE_TRACE_LIST_SCHEMA,
    handler=cognitive_trace_list,
    check_fn=check_cognitive_requirements,
)
