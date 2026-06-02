"""
CognitiveObserver — wraps sinoclaw's EventBus as a MIDCA BaseModule (PERCEIVE phase).

Observes sinoclaw events (messages, tool results, memory updates) and
writes structured world state into CognitiveMemory.

Usage:
    observer = EventBusObserver(event_bus, cognitive_memory)
    mgr.append_module(Phase.PERCEIVE, observer)
"""

import logging
from typing import Any, Dict, List, Optional

from cognitive.phase_manager import BaseModule, PhaseManager, Phase
from cognitive.memory import CognitiveMemory
from cognitive.world import CognitiveWorld, Atom

logger = logging.getLogger("cognitive.observer")


class CognitiveObserver(BaseModule):
    """
    PERCEIVE module: reads sinoclaw context and updates CognitiveWorld.

    Reads from:
        - conversation_history (user messages, assistant responses)
        - tool_results (tool call results)
        - memory updates

    Writes to:
        - CognitiveMemory.current_state (CognitiveWorld)
    """

    def __init__(
        self,
        event_bus=None,        # sinoclaw EventBus (optional)
        memory: CognitiveMemory = None,
        world: CognitiveWorld = None,
        conversation_getter: callable = None,  # () -> list[messages]
    ):
        """
        Args:
            event_bus: sinoclaw EventBus instance
            memory: CognitiveMemory to write to
            world: CognitiveWorld to update (defaults to memory.current_state)
            conversation_getter: () -> list of message dicts (from AIAgent)
        """
        super().__init__(name="CognitiveObserver")
        self.event_bus = event_bus
        self.memory = memory
        self.world = world
        self.conversation_getter = conversation_getter
        self._last_msg_count = 0

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        """
        PERCEIVE phase: observe world state from sinoclaw events and context.

        Injects observed facts into mem.current_state (CognitiveWorld).
        Subclass and override _extract_atoms() for domain-specific extraction.
        """
        world = self.world or mem.current_state
        if world is None:
            world = CognitiveWorld()
            mem.current_state = world

        # Extract atoms from conversation
        if self.conversation_getter:
            messages = self.conversation_getter()
            new_atoms = self._extract_atoms_from_messages(messages)
            for atom in new_atoms:
                world.add_atom(atom)

        # Extract atoms from event bus (if available)
        if self.event_bus:
            events = self._drain_events()
            for event in events:
                atoms = self._extract_atoms_from_event(event)
                for atom in atoms:
                    world.add_atom(atom)

        mem.current_state = world

        logger.debug(
            "[Cycle %d] PERCEIVE: world has %d atoms",
            cycle,
            len(world.atoms),
        )
        return None

    def _extract_atoms_from_messages(self, messages: List[Dict]) -> List[Atom]:
        """
        Override in subclass to extract CognitiveWorld atoms from conversation messages.

        Default: no atoms extracted (neutral observation).
        Example extraction for a blocks world:
            - ("on", "A", "table") from "A is on the table"
            - ("clear", "A") from "A is clear"
        """
        return []

    def _extract_atoms_from_event(self, event) -> List[Atom]:
        """Extract atoms from a sinoclaw EventBus event."""
        return []

    def _drain_events(self) -> List[Any]:
        """Drain pending events from event bus. Override as needed."""
        return []


class SimplePerceive(BaseModule):
    """
    Standalone PERCEIVE module that doesn't depend on sinoclaw internals.

    Accepts an arbitrary getter callable that returns observed facts.
    """

    def __init__(
        self,
        fact_getter: callable = None,  # () -> list[Atom]
        memory: CognitiveMemory = None,
        world: CognitiveWorld = None,
    ):
        super().__init__(name="SimplePerceive")
        self.fact_getter = fact_getter
        self.memory = memory
        self.world = world

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        world = self.world or mem.current_state or CognitiveWorld()

        if self.fact_getter:
            atoms = self.fact_getter()
            for atom in atoms:
                if isinstance(atom, Atom):
                    world.add_atom(atom)
                elif isinstance(atom, tuple):
                    world.add_atom(Atom.from_tuple(atom))

        mem.current_state = world
        return None
