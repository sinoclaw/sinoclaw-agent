"""
CogTrace — cognitive trajectory recorder.

Records every cognition event: cycle start/end, phase start/end, module execution, data produced.
Used for debugging, meta-cognition, and audit.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict


@dataclass
class CogEvent:
    """A single cognitive event."""
    cycle: int
    phase: str
    module: str
    event_type: str  # "start" | "data" | "end"
    data: Any = None
    timestamp: float = field(default_factory=time.time)


class CogTrace:
    """
    Records the full cognitive execution trace.

    Trace structure: {(cycle, phase, module): [CogEvent, ...]}
    """

    def __init__(self):
        self.trace: Dict[Tuple, List[CogEvent]] = defaultdict(list)
        self._current_cycle: int = 0
        self._current_phase: str = ""
        self._cycle_start: float = 0
        self._phase_start: float = 0

    def begin_cycle(self, cycle: int):
        self._current_cycle = cycle
        self._cycle_start = time.time()

    def end_cycle(self):
        self._current_cycle = 0

    def begin_phase(self, phase: str, cycle: int):
        self._current_phase = phase
        self._phase_start = time.time()

    def end_phase(self, phase: str = None):
        self._current_phase = ""

    def add_event(
        self,
        module: str,
        event_type: str,
        data: Any = None,
        cycle: int = None,
        phase: str = None,
    ):
        key = (
            cycle or self._current_cycle,
            phase or self._current_phase,
            module,
        )
        self.trace[key].append(
            CogEvent(
                cycle=self._current_cycle,
                phase=self._current_phase,
                module=module,
                event_type=event_type,
                data=data,
            )
        )

    def get_trace(self, cycle: int = None, phase: str = None) -> List[CogEvent]:
        """Get events for a specific cycle and/or phase."""
        if cycle and phase:
            return self.trace.get((cycle, phase), [])
        elif cycle:
            return [e for (c, p, m), events in self.trace.items() if c == cycle for e in events]
        elif phase:
            return [e for (c, p, m), events in self.trace.items() if p == phase for e in events]
        else:
            return [e for events in self.trace.values() for e in events]

    def get_n_prev_phase(self, n: int = 1) -> List[CogEvent]:
        """Get events from the previous n phases."""
        all_keys = sorted(self.trace.keys())
        if not all_keys:
            return []
        recent = all_keys[-n:]
        return [e for k in recent for e in self.trace[k]]

    def cycle_duration(self, cycle: int) -> float:
        """Return duration of a cycle in seconds (estimated from first/last event)."""
        events = self.get_trace(cycle=cycle)
        if not events:
            return 0.0
        return events[-1].timestamp - events[0].timestamp

    def clear(self):
        self.trace.clear()
        self._current_cycle = 0
        self._current_phase = ""

    def summary(self) -> Dict[str, Any]:
        """Return a human-readable summary of the trace."""
        cycles = sorted(set(e.cycle for events in self.trace.values() for e in events))
        phases = sorted(set(e.phase for events in self.trace.values() for e in events))
        modules = sorted(set(e.module for events in self.trace.values() for e in events))
        return {
            "total_cycles": len(cycles),
            "phases": phases,
            "modules": modules,
            "total_events": sum(len(v) for v in self.trace.values()),
        }
