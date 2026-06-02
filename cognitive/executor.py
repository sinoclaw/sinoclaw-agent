"""
CognitiveExecutor — wraps AIAgent.run_conversation as a MIDCA BaseModule (ACT phase).

This is the key bridge: SINOCLAW's tool-calling loop becomes MIDCA's ACT phase.
A Plan's actions are serialized as a task prompt sent to AIAgent.

Usage:
    executor = AIAgentExecutor(agent=ai_agent, memory=cog_memory)
    mgr.append_module(Phase.ACT, executor)

The executor reads current_plan from memory, extracts the next action,
and calls AIAgent.run_conversation(task=action.description) to execute it.
"""

import copy
import logging
from typing import Any, Dict, List, Optional

from cognitive.phase_manager import BaseModule, PhaseManager, Phase
from cognitive.memory import CognitiveMemory
from cognitive.plans import Plan, Action
from cognitive.world import CognitiveWorld, Atom

logger = logging.getLogger("cognitive.executor")


class AIAgentExecutor(BaseModule):
    """
    BaseModule that executes the current plan's next action via AIAgent.

    Action execution flow:
        1. Read current_plan from memory
        2. Get next action from plan.get_next_step()
        3. Serialize action → user message
        4. Call AIAgent.run_conversation(task=...)
        5. Store result in CognitiveMemory[ACTIONS_HIST]
        6. Advance plan pointer
    """

    def __init__(
        self,
        agent,  # AIAgent instance
        memory: CognitiveMemory = None,
        max_turns: int = 30,
    ):
        super().__init__(name="AIAgentExecutor")
        self.agent = agent
        self.memory = memory
        self.max_turns = max_turns

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        """
        Execute the next action in the current plan.

        Returns Phase.STOP if plan execution should halt the cycle.
        """
        plan: Plan = mem.current_plan

        if plan is None:
            logger.debug("No current plan — ACT phase idle")
            return None

        action: Action = plan.get_next_step()

        if action is None:
            logger.info("Plan %s finished", plan.name)
            plan.completed = True
            mem.current_plan = None
            return None

        logger.info(
            "[Cycle %d] ACT: executing %s%s",
            cycle,
            action.name,
            action.args,
        )

        # Serialize action to a task description
        task_desc = self._action_to_task(action)

        try:
            # Call sinoclaw's AIAgent
            result = self.agent.run_conversation(
                user_message=task_desc,
                conversation_history=[],
            )
            response = result.get("final_response", "")
            tool_calls = result.get("tool_calls", [])

            # Record execution in memory
            execution_record = {
                "cycle": cycle,
                "action": action,
                "task": task_desc,
                "response": response,
                "tool_calls": tool_calls,
                "success": True,
            }
            mem.append_action(execution_record)

            logger.debug(
                "ACT result: %s | tool_calls: %d",
                str(response)[:100],
                len(tool_calls),
            )

        except Exception as e:
            logger.exception("AIAgentExecutor failed on %s", action)
            mem.append_action([{"cycle": cycle, "action": action, "error": str(e), "success": False}])

        # Advance plan pointer
        plan.advance()

        return None

    def _action_to_task(self, action: Action) -> str:
        """Convert an Action to a task prompt for AIAgent."""
        args_str = ", ".join(map(repr, action.args))
        meta = action.meta or {}

        base = f"Execute the action: {action.name}({args_str})"
        if "description" in meta:
            base = meta["description"]
        elif "goal" in meta:
            base = f"Goal: {meta['goal']}\nAction: {action.name}({args_str})"

        return base


class SimpleAct(BaseModule):
    """
    ACT phase module: select next action from current plan → writes to mem.ACTIONS.

    Does NOT apply the action to the world — that's Simulate phase's job.
    This separation is what allows MIDCA to "plan first, execute later".
    """

    def __init__(self, memory: CognitiveMemory = None):
        super().__init__(name="SimpleAct")
        self.memory = memory

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:
        plan = mem.current_plan
        if plan is None:
            return None

        action = plan.get_next_step()
        if action is None:
            plan.completed = True
            mem.current_plan = None
            return None

        logger.info("[Cycle %d] Act: selected %s%s", cycle, action.name, action.args)

        # Write to mem.ACTIONS — Simulate phase will pick this up.
        mem.append_action([cycle, action.name, action.args, "selected"])
        return None


class MidcaActionSimulator(BaseModule):
    """
    SIMULATE phase: apply actions selected by Act phase to the world.

    MIDCA's key design: plan-first, execute-later.
    - Act phase: selects action, writes to mem.ACTIONS
    - Simulate phase: applies action to world state
    """

    def __init__(self, world: CognitiveWorld, memory: CognitiveMemory = None):
        super().__init__(name="MidcaActionSimulator")
        self.world = world
        self.memory = memory

    def run(
        self,
        mgr: PhaseManager,
        mem: CognitiveMemory,
        cycle: int,
    ) -> Optional[str]:

        # Read actions written by Act phase this cycle
        # Format: [[cycle, name, args, status], ...] — last entry is current cycle
        all_actions = mem.get("__actions", [])
        current_actions = [a for a in all_actions if a and a[0] == cycle]

        if not current_actions:
            return None

        for entry in current_actions:
            if len(entry) < 3:
                continue
            name = entry[1]
            args = entry[2] if len(entry) > 2 else ()
            if isinstance(args, str):
                args = (args,)

            ok = self.world.apply_action(name, args)
            status = "✓" if ok else "✗"
            logger.info("[Cycle %d] Simulate: %s %s(%s)", cycle, status, name, args)

            if ok:
                # Advance plan ONLY after successful apply
                plan = mem.current_plan
                if plan and not plan.completed:
                    plan.advance()

            # Skip actions already applied this cycle (guard against double-execution
            # if cognitive_simulate is called multiple times for the same cycle)
            if len(entry) >= 4 and entry[3] == "applied":
                continue

            # Update the entry to mark as applied
            if len(entry) == 4:
                entry[3] = "applied" if ok else "failed"

        # Update mem.current_state so EVALUATE phase can check goal achievement
        # Using the raw setter to avoid pushing to STATES history (already done by world update)
        mem.set("__current state", self.world)
        return None
