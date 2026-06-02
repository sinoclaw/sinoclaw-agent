"""
PhaseManager — the cognitive cycle driver from MIDCA.

One cycle = run every enabled phase in order.
Multi-cycle = repeat until a phase returns Phase.STOP.

Phases (default order):
    PERCEIVE  → INTERPRET  → EVALUATE  → INTEND  → PLAN  → ACT

Supports:
    - enable / disable phases by name
    - append_phase (insert at end)
    - append_module (register a callable under a phase)
    - meta-phases run after each cognitive cycle
    - cycle callbacks (on_cycle_start / on_cycle_end)
"""

import time as time_module
from typing import Callable, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger("cognitive.phase_manager")


class Phase:
    SIMULATE = "SIMULATE"
    PERCEIVE  = "PERCEIVE"
    INTERPRET = "INTERPRET"
    EVALUATE  = "EVALUATE"
    INTEND    = "INTEND"
    PLAN      = "PLAN"
    ACT       = "ACT"
    STOP      = "__STOP__"  # returned by a phase to halt the cycle loop


class BaseModule:
    """
    MIDCA BaseModule spec.

    A module is a callable (or object with run()) that takes (PhaseManager, CognitiveMemory)
    and returns None (ok) or Phase.STOP (halt).
    """

    def __init__(self, name: str = None):
        self.name = name or self.__class__.__name__

    def run(self, mgr: "PhaseManager", mem: "CognitiveMemory", cycle: int) -> Optional[str]:
        """
        Execute the module logic.

        Args:
            mgr:  PhaseManager instance (for reading phase context)
            mem:  CognitiveMemory instance (read/write cognitive state)
            cycle: Current cycle number (1-based)

        Returns:
            None to continue, or Phase.STOP to halt the cycle loop.
        """
        raise NotImplementedError


class PhaseManager:
    """
    Drives the MIDCA cognitive cycle.

    cycle()  — run one full PERCEIVE→ACT loop
    run(n)   — run n cycles (or until a phase returns STOP)
    """

    # Default phase execution order
    DEFAULT_PHASES = [
        Phase.PERCEIVE,
        Phase.INTERPRET,
        Phase.INTEND,
        Phase.PLAN,
        Phase.ACT,       # Select next action from plan
        Phase.SIMULATE,  # Apply action to world
        Phase.EVALUATE,  # Check goal achievement AFTER action is applied
    ]

    def __init__(
        self,
        memory=None,
        enable_meta: bool = False,
        goal_graph=None,
        trace=None,
    ):
        self.memory = memory
        self.goal_graph = goal_graph
        self.trace = trace

        # Phase → list of BaseModule callables
        self.modules: Dict[str, List[BaseModule]] = {
            p: [] for p in self.DEFAULT_PHASES
        }
        # Per-phase enable flag
        self._enabled: Dict[str, bool] = {p: True for p in self.DEFAULT_PHASES}
        # Meta-phases (run after each cycle)
        self.meta_modules: List[BaseModule] = []
        # Callbacks
        self._on_cycle_start: List[Callable] = []
        self._on_cycle_end: List[Callable] = []
        self._on_phase_end: List[Callable] = []

        # Runtime state
        self.cycle_count = 0
        self.verbose = False
        self._stop_requested = False

    # ── module registration ──────────────────────────────────────────

    def append_module(self, phase: str, module: BaseModule):
        """Register a BaseModule under a phase."""
        if phase not in self.modules:
            self.modules[phase] = []
        self.modules[phase].append(module)
        logger.debug("Registered module %s under phase %s", module.name, phase)

    def enable(self, phase: str):
        self._enabled[phase] = True

    def disable(self, phase: str):
        self._enabled[phase] = False

    def append_phase(self, phase: str, after: str = None):
        """Append a new phase to the execution order (default: at end)."""
        if phase in self.modules:
            return
        self.modules[phase] = []
        self._enabled[phase] = True
        if after is None:
            self.DEFAULT_PHASES.append(phase)
        else:
            idx = self.DEFAULT_PHASES.index(after)
            self.DEFAULT_PHASES.insert(idx + 1, phase)

    def add_meta_module(self, module: BaseModule):
        self.meta_modules.append(module)

    def on_cycle_start(self, cb: Callable):
        self._on_cycle_start.append(cb)

    def on_cycle_end(self, cb: Callable):
        self._on_cycle_end.append(cb)

    def on_phase_end(self, cb: Callable):
        """Register a callback fired after each phase ends.

        Callback signature: cb(cycle: int, phase: str, memory: CognitiveMemory)
        """
        self._on_phase_end.append(cb)

    # ── execution ───────────────────────────────────────────────────

    def one_cycle(self) -> Optional[str]:
        """
        Run one cognitive cycle: PERCEIVE → ... → ACT.

        Returns Phase.STOP if a phase requested a halt, else None.
        """
        self.cycle_count += 1
        cycle = self.cycle_count

        if self.verbose:
            logger.info("=== Cycle %d START ===", cycle)

        for cb in self._on_cycle_start:
            cb(cycle)

        # Run trace segment start
        if self.trace:
            self.trace.begin_cycle(cycle)

        # Execute each phase in order
        for phase in self.DEFAULT_PHASES:
            if not self._enabled.get(phase, True):
                continue

            if self.verbose:
                logger.info("  Phase %s", phase)

            # Begin phase in trace
            if self.trace:
                self.trace.begin_phase(phase, cycle)

            # Run all modules registered for this phase
            for module in self.modules.get(phase, []):
                module_name = getattr(module, 'name', None) or str(module)
                try:
                    result = module.run(self, self.memory, cycle)
                    if result == Phase.STOP:
                        if self.verbose:
                            logger.info("  Phase %s requested STOP", phase)
                        return Phase.STOP
                except Exception as e:
                    logger.exception("Module %s raised in phase %s", module.name, phase)

            # End phase in trace
            if self.trace:
                self.trace.end_phase(phase)

            # Fire per-phase end callbacks
            for cb in self._on_phase_end:
                cb(cycle, phase, self.memory)

        # Run meta-phases (if enabled)
        for meta_module in self.meta_modules:
            try:
                result = meta_module.run(self, self.memory, cycle)
                if result == Phase.STOP:
                    return Phase.STOP
            except Exception as e:
                logger.exception("Meta-module %s raised", meta_module.name)

        for cb in self._on_cycle_end:
            cb(cycle)

        if self.trace:
            self.trace.end_cycle()

        if self.verbose:
            logger.info("=== Cycle %d END ===", cycle)

        return None

    def several_cycles(self, n: int) -> int:
        """
        Run up to n cycles.

        Returns the number of cycles actually executed.
        """
        for i in range(n):
            result = self.one_cycle()
            if result == Phase.STOP:
                return i + 1
        return n

    def run(self, max_cycles: int = 100) -> int:
        """
        Run cycles until STOP, max_cycles reached, or keyboard interrupt.

        Returns total cycles executed.
        """
        self.cycle_count = 0
        try:
            while self.cycle_count < max_cycles:
                result = self.one_cycle()
                if result == Phase.STOP:
                    break
        except KeyboardInterrupt:
            logger.info("PhaseManager interrupted after %d cycles", self.cycle_count)
        return self.cycle_count

    def stop(self):
        self._stop_requested = True

    def restart(self, memory=None, goal_graph=None, trace=None):
        """Reset state for a new cognitive session."""
        self.cycle_count = 0
        self._stop_requested = False
        if memory is not None:
            self.memory = memory
        if goal_graph is not None:
            self.goal_graph = goal_graph
        if trace is not None:
            self.trace = trace
