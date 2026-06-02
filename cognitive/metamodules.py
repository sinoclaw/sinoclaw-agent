"""
Meta-cognitive modules — monitor, detect, intend, control.

These run after each cognitive cycle (metaEnabled mode) to:
    - Monitor cognitive trace for anomalies
    - Detect expectation violations
    - Generate meta-goals
    - Control (modify) the cognitive architecture dynamically
"""

import logging
from typing import Any, List, Optional

from cognitive.phase_manager import BaseModule, Phase, PhaseManager
from cognitive.memory import CognitiveMemory, META_ANOMALIES, META_GOALS, META_CURR
from cognitive.goals import Goal, GoalNode, GoalGraph

logger = logging.getLogger("cognitive.meta")


# ── META MONITOR ─────────────────────────────────────────────────────────────


class MRSimpleMonitor(BaseModule):
    """
    Meta Monitor: records trace data into memory for the meta-level.

    Reads:   CogTrace
    Writes:  CognitiveMemory[META_GOALS]  (as goal objects)
    """

    def __init__(self, trace=None):
        super().__init__(name="MRSimpleMonitor")
        self._trace = trace

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        trace = self._trace or mgr.trace
        if trace is None:
            return None

        # Store recent trace segment
        recent = trace.get_n_prev_phase(n=2)
        if recent:
            mem.set("__trace segment", recent)
        return None


# ── META DETECT ─────────────────────────────────────────────────────────────


class MRSimpleDetect(BaseModule):
    """
    Meta Detect: inspect trace for expectation violations → META_ANOMALIES.

    Checks:
        - MentalExpectation: required phase ordering
        - PrimitiveExpectation: module produced expected data
        - RepetitionAnomaly: same data repeated N times
    """

    def __init__(
        self,
        expectations: List[callable] = None,
        repetition_threshold: int = 5,
    ):
        super().__init__(name="MRSimpleDetect")
        self._expectations = expectations or []
        self._repetition_threshold = repetition_threshold

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        anomalies = mem.get(META_ANOMALIES) or []

        # Check mental expectations (phase ordering)
        trace_seg = mem.get("__trace segment")
        if trace_seg:
            for expectation in self._expectations:
                violation = expectation(trace_seg)
                if violation:
                    anomalies.append(violation)

        # Check for repetition anomalies
        recent = mem.get("__trace segment") or []
        if len(recent) >= self._repetition_threshold:
            if self._detect_repetition(recent):
                anomalies.append({"type": "repetition", "data": recent[-1]})

        mem.set(META_ANOMALIES, anomalies)

        if anomalies:
            logger.info("[Cycle %d] META DETECT: %d anomalies", cycle, len(anomalies))

        return None

    def _detect_repetition(self, trace_segment: List) -> bool:
        if len(trace_segment) < self._repetition_threshold:
            return False
        last = trace_segment[-1]
        return all(e.data == last.data for e in trace_segment[-self._repetition_threshold:])


# ── META INTEND ────────────────────────────────────────────────────────────


class MRSimpleIntend(BaseModule):
    """
    Meta Intend: select meta-goals based on anomalies → META_CURR.
    """

    def __init__(self, goal_graph: GoalGraph = None):
        super().__init__(name="MRSimpleIntend")
        self._goal_graph = goal_graph

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        anomalies = mem.get(META_ANOMALIES) or []
        meta_goals = mem.get(META_GOALS) or []

        # Generate meta-goals from anomalies
        for anomaly in anomalies:
            mg = self._anomaly_to_goal(anomaly)
            if mg:
                meta_goals.append(mg)

        mem.set(META_GOALS, meta_goals)

        # Top meta-goal → META_CURR
        if meta_goals:
            mem.set(META_CURR, meta_goals[0])

        return None

    def _anomaly_to_goal(self, anomaly: dict) -> Optional[Goal]:
        """Convert an anomaly dict to a meta-goal."""
        atype = anomaly.get("type", "")
        if atype == "repetition":
            return Goal("meta", "resolve_repetition", id=f"meta_{atype}")
        if atype == "phase_violation":
            return Goal("meta", "fix_phase_order", id=f"meta_{atype}")
        return None


# ── META CONTROL ────────────────────────────────────────────────────────────


class MRSimpleControl(BaseModule):
    """
    Meta Control: execute meta-level actions (META_PAN) to modify the cognitive architecture.

    Supports actions:
        - REMOVE-MODULE(phase, module_name)
        - ADD-MODULE(phase, module)
        - REPLACE-MODULE(phase, old_module, new_module)
        - CHANGE-GOAL-CMP(cmp_function)
    """

    def __init__(self):
        super().__init__(name="MRSimpleControl")

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        meta_curr = mem.get(META_CURR)
        if meta_curr is None:
            return None

        # Execute meta-PAN (meta-level action)
        if hasattr(meta_curr, "predicate") and meta_curr.predicate == "meta":
            action = meta_curr.args[0] if meta_curr.args else None
            if action == "resolve_repetition":
                self._handle_repetition(meta_curr, mgr, mem)
            elif action == "fix_phase_order":
                self._handle_phase_violation(meta_curr, mgr, mem)

        return None

    def _handle_repetition(self, meta_goal, mgr, mem):
        """Handle repetition anomaly by resetting current plan."""
        logger.info("META CONTROL: handling repetition anomaly")
        mem.current_plan = None

    def _handle_phase_violation(self, meta_goal, mgr, mem):
        """Handle phase violation by re-enabling correct phase ordering."""
        logger.info("META CONTROL: handling phase violation")


# ── Meta Goal Evaluator ─────────────────────────────────────────────────────


class MRSimpleEval(BaseModule):
    """
    Meta Eval: check if meta-goals have been achieved.
    """

    def __init__(self):
        super().__init__(name="MRSimpleEval")

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        meta_curr = mem.get(META_CURR)
        if meta_curr is None:
            return None

        achieved = self._evaluate_meta_goal(meta_curr, mem)
        if achieved:
            mem.set(META_CURR, None)
            logger.info("[Cycle %d] META EVAL: achieved %s", cycle, meta_curr)

        return None

    def _evaluate_meta_goal(self, goal: Goal, mem: CognitiveMemory) -> bool:
        """Stub evaluator — override with domain logic."""
        anomalies = mem.get(META_ANOMALIES) or []
        if goal.args and goal.args[0] == "resolve_repetition":
            return len(anomalies) == 0
        return False
