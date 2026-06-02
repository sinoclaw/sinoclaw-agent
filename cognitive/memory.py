"""
Cognitive Memory — wraps Sinoclaw's memory_provider as a MIDCA-style Memory.

MIDCA Memory slot naming convention: __PREFIX for internal slots.
We expose both the raw slot API (get/set) and a typed accessor API.

Thread-safe via threading.RLock.
"""

import threading
from typing import Any, Dict, Optional


# MIDCA-standard slot names (exact MIDCA Memory constants)
STATE         = "__current state"
STATES        = "__world states"
GOAL_GRAPH    = "__goals"
CURRENT_GOALS = "__current goals"
PLANS         = "__plans"
CURR_PLAN     = "__CurrPlan"
ACTIONS       = "__actions"
DISCREPANCY   = "__discrepancy"
META_ANOMALIES = "__meta anomalies"
META_GOALS    = "__meta goals"
META_CURR     = "__meta current goal"
TRACE_SEGMENT = "__trace segment"
PLANNING_COUNT = "__PlanningCount"
GOALS_ACHIEVED = "__GoalsAchieved"
ACTIONS_EXECUTED = "__ActionsExecuted"
MIDCA_CYCLES  = "__MIDCA Cycles"


class CognitiveMemory:
    """
    Thread-safe key-value store with MIDCA slot conventions.

    Inherits from MIDCA Memory to ensure attribute access like
    `mem.PLANNING_COUNT` (→ string constant) works for MIDCA modules.
    """

    # MIDCA Memory constants as class attributes (same as midca.mem.Memory)
    PLANNING_COUNT = "__PlanningCount"
    GOALS_ACHIEVED = "__GoalsAchieved"
    ACTIONS_EXECUTED = "__ActionsExecuted"
    MIDCA_CYCLES = "__MIDCA Cycles"
    STATES = "__world states"        # also a class attr for PyHop's self.mem.STATES
    STATE = "__current state"       # also a class attr for self.mem.STATE
    CURRENT_GOALS = "__current goals"  # PyHopPlanner line 55
    GOAL_GRAPH = "__goals"          # PyHopPlanner line 58

    def __init__(self, memory_provider=None):
        self._lock = threading.RLock()
        self._slots: Dict[str, Any] = {}
        self._provider = memory_provider  # optional external provider
        self.trace = False  # lazy: CogTrace() initialized on first access (same as MIDCA Memory)
        self._init_slots()

    def _init_slots(self):
        """Initialise MIDCA-standard slots to empty containers."""
        self._slots = {
            STATE: None,
            STATES: [],
            GOAL_GRAPH: None,
            CURRENT_GOALS: [],
            PLANS: set(),
            CURR_PLAN: None,
            ACTIONS: [],
            DISCREPANCY: None,
            META_ANOMALIES: [],
            META_GOALS: [],
            META_CURR: None,
            TRACE_SEGMENT: None,
            PLANNING_COUNT: 0,
            GOALS_ACHIEVED: 0,
            ACTIONS_EXECUTED: 0,
            MIDCA_CYCLES: 0,
        }

    # ── raw slot API ──────────────────────────────────────────────

    def get(self, slot: str, default=None) -> Any:
        with self._lock:
            return self._slots.get(slot, default)

    def set(self, slot: str, value: Any):
        with self._lock:
            self._slots[slot] = value

    def append(self, slot: str, item: Any):
        """Append item to a list-type slot (e.g. state history)."""
        with self._lock:
            if slot not in self._slots or not isinstance(self._slots[slot], list):
                self._slots[slot] = []
            self._slots[slot].append(item)

    def clear(self, slot: str):
        with self._lock:
            if slot in self._slots:
                v = self._slots[slot]
                if isinstance(v, list):
                    v.clear()
                elif isinstance(v, set):
                    v.clear()
                else:
                    self._slots[slot] = None

    # ── typed accessor API ──────────────────────────────────────────

    @property
    def current_state(self) -> Any:
        return self.get(STATE)

    @current_state.setter
    def current_state(self, value: Any):
        # Push to history before overwriting
        self.append(STATES, self.get(STATE))
        self.set(STATE, value)

    @property
    def goal_graph(self) -> Any:
        return self.get(GOAL_GRAPH)

    @goal_graph.setter
    def goal_graph(self, value: Any):
        self.set(GOAL_GRAPH, value)

    @property
    def current_goals(self) -> list:
        return self.get(CURRENT_GOALS) or []

    @current_goals.setter
    def current_goals(self, value: list):
        self.set(CURRENT_GOALS, value)

    def add_goal(self, goal):
        goals = self.current_goals
        goals.append(goal)
        self.set(CURRENT_GOALS, goals)

    def remove_goal(self, goal):
        goals = self.current_goals
        if goal in goals:
            goals.remove(goal)
            self.set(CURRENT_GOALS, goals)

    @property
    def current_plan(self) -> Any:
        return self.get(CURR_PLAN)

    @current_plan.setter
    def current_plan(self, value: Any):
        self.set(CURR_PLAN, value)

    @property
    def all_plans(self) -> set:
        return self.get(PLANS) or set()

    def add_plan(self, plan):
        plans = self.all_plans
        plans.add(plan)
        self.set(PLANS, plans)

    def remove_plan(self, plan):
        plans = self.all_plans
        if plan in plans:
            plans.remove(plan)
            self.set(PLANS, plans)

    @property
    def actions_history(self) -> list:
        return self.get(ACTIONS) or []

    def append_action(self, action):
        self.append(ACTIONS, action)

    @property
    def discrepancy(self) -> Any:
        return self.get(DISCREPANCY)

    @discrepancy.setter
    def discrepancy(self, value: Any):
        self.set(DISCREPANCY, value)

    # ── sync with external provider ──────────────────────────────────

    def sync_to_provider(self, provider):
        """Persist cognitive state to an external memory provider."""
        # Provider must implement: read(key) / write(key, value)
        if provider is None:
            return
        state = {
            STATE_CURRENT: self.get(STATE_CURRENT),
            GOALS_CURRENT: self.get(GOALS_CURRENT),
            PLANS_ALL: list(self.get(PLANS_ALL) or []),
            PLANS_CURR: self.get(PLANS_CURR),
            ACTIONS_HIST: self.get(ACTIONS_HIST),
            DISCREPANCY: self.get(DISCREPANCY),
        }
        for key, value in state.items():
            if value is not None:
                provider.write(key, value)

    def load_from_provider(self, provider):
        """Restore cognitive state from an external memory provider."""
        if provider is None:
            return
        for slot in [STATE_CURRENT, GOALS_CURRENT, PLANS_CURR, DISCREPANCY]:
            val = provider.read(slot)
            if val is not None:
                self.set(slot, val)

    # ── debug ───────────────────────────────────────────────────────

    def dump(self) -> Dict[str, Any]:
        """Return a snapshot of all slots (for debugging)."""
        with self._lock:
            return dict(self._slots)
