"""
Plan / Action — MIDCA's action sequencing system.

A Plan wraps a sequence of Actions with a step pointer.
Goals that the plan is expected to achieve are attached.
"""

from typing import List, Optional, Any


class Action:
    """
    A single action: operator name + args + precond + effect.

    MIDCA actions are plain objects with:
        name: str
        args: tuple
        precond: callable(state) → bool
        effect: callable(state)
    """

    def __init__(
        self,
        name: str,
        args: tuple = (),
        precond: callable = None,
        effect: callable = None,
        **kwargs,
    ):
        self.name = name
        self.args = args
        self.precond = precond  # (state) → bool
        self.effect = effect    # (state) → None
        self.meta = kwargs       # extra metadata

    def __repr__(self):
        args = ", ".join(map(repr, self.args))
        return f"Action({self.name}, {args})"

    def __eq__(self, other):
        return isinstance(other, Action) and self.name == other.name and self.args == other.args

    def __hash__(self):
        return hash((self.name, self.args))


class Plan:
    """
    A plan is a sequence of actions with a step pointer.

    The plan tracks which goals it is expected to achieve.
    """

    def __init__(
        self,
        actions: List[Action] = None,
        goals: List[Any] = None,
        name: str = None,
    ):
        self.actions: List[Action] = actions or []
        self.goals: List[Any] = goals or []  # goals this plan satisfies
        self.step: int = 0
        self.name = name or f"Plan-{id(self)}"
        self.completed = False

    def get_next_step(self) -> Optional[Action]:
        """Return current action and advance step."""
        if self.step >= len(self.actions):
            self.completed = True
            return None
        action = self.actions[self.step]
        return action

    def advance(self):
        """Move step pointer to next action."""
        self.step += 1
        if self.step >= len(self.actions):
            self.completed = True

    @property
    def finished(self) -> bool:
        return self.step >= len(self.actions)

    def get_remaining_steps(self) -> List[Action]:
        return self.actions[self.step:]

    def __repr__(self):
        return f"Plan({self.name}, step={self.step}/{len(self.actions)})"

    def __str__(self):
        lines = [f"Plan: {self.name} (step {self.step})"]
        for i, action in enumerate(self.actions):
            marker = "→ " if i == self.step else "  "
            lines.append(f"  {marker}[{i}] {action}")
        return "\n".join(lines)
