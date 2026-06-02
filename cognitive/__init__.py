"""
Cognitive extension for Sinoclaw — MIDCA-inspired dual-cycle architecture.

Architecture:
    PhaseManager drives a cognitive cycle:
        PERCEIVE → INTERPRET → EVALUATE → INTEND → PLAN → ACT → (loop)

    Sinoclaw's AIAgent.run_conversation() is wrapped as the ACT module.
    Sinoclaw's memory_provider is wrapped as the CognitiveMemory layer.

Design principles (from MIDCA):
    - GoalGraph: explicit goal hierarchy with partial ordering
    - Plan: action sequence with step pointer
    - Memory: 50+ named slots, thread-safe, with state history
    - Trace: full cognitive trajectory for debugging/audit
"""

from cognitive.phase_manager import PhaseManager, Phase, BaseModule
from cognitive.memory import CognitiveMemory
from cognitive.goals import GoalGraph, Goal, GoalNode
from cognitive.plans import Plan, Action
from cognitive.world import CognitiveWorld
from cognitive.trace import CogTrace

__all__ = [
    "PhaseManager",
    "Phase",
    "BaseModule",
    "CognitiveMemory",
    "GoalGraph",
    "Goal",
    "GoalNode",
    "Plan",
    "Action",
    "CognitiveWorld",
    "CogTrace",
]
