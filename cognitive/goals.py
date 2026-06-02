"""
Goal / GoalNode / GoalGraph — MIDCA's goal management system.

GoalGraph supports:
    - Insertion with partial-order constraints (goal_cmp function)
    - Finding unrestricted (ready-to-execute) goals
    - Matching goals to existing plans
    - Goal removal on completion
"""

from __future__ import annotations
from typing import Callable, List, Optional, Set, Tuple


class Goal:
    """A goal is a predicate with optional arguments."""

    def __init__(self, predicate_or_list, *args, **kwargs):
        # Support two forms:
        #   Goal("on", "a", "b")  — predicate + args
        #   Goal(["on", "a", "b"]) — list form
        if isinstance(predicate_or_list, list):
            pred = predicate_or_list[0]
            rest = tuple(predicate_or_list[1:])
        elif isinstance(predicate_or_list, tuple) and len(args) == 0:
            # tuple passed as single arg: Goal(("on", "a", "b"))
            pred = predicate_or_list[0]
            rest = predicate_or_list[1:]
        else:
            pred = predicate_or_list
            rest = args
        self.predicate = pred
        self.args = rest  # tuple for hashability
        self.kwargs = kwargs
        self.id = kwargs.pop("id", None)
        self.parent = kwargs.pop("parent", None)
        self.meta = kwargs.pop("meta", False)  # meta-level goal flag

    def __repr__(self):
        args = ", ".join(map(repr, self.args))
        return f"Goal({self.predicate}, {args})"

    def __eq__(self, other):
        return (
            isinstance(other, Goal)
            and self.predicate == other.predicate
            and self.args == other.args
        )

    def __hash__(self):
        return hash((self.predicate, self.args))

    def satisfied_by(self, atom) -> bool:
        """Check if a world atom satisfies this goal."""
        if isinstance(atom, tuple):
            pred, args = atom[0], atom[1:]
            return pred == self.predicate and args == self.args
        return False


class GoalNode:
    """
    A node in the GoalGraph DAG.

    Goals can have multiple children (sub-goals) and multiple parents.
    The graph maintains a partial order: parent goals must be achieved before children.
    """

    def __init__(self, goal: Goal):
        self.goal = goal
        self.children: Set[GoalNode] = set()
        self.parents: Set[GoalNode] = set()
        self.completed = False
        self.achieved = False

    def __repr__(self):
        status = "✓" if self.achieved else ("✗" if self.completed else "?")
        return f"GoalNode({self.goal}, {status})"

    def add_child(self, node: GoalNode):
        self.children.add(node)
        node.parents.add(self)

    def remove_child(self, node: GoalNode):
        self.children.discard(node)
        node.parents.discard(self)


class GoalGraph:
    """
    Directed acyclic graph of goals with partial ordering.

    Uses a comparator function to maintain order when inserting goals.
    """

    def __init__(self, goal_cmp: Callable = None):
        """
        Args:
            goal_cmp: comparison function (g1, g2) → -1/0/1.
                      Used for partial ordering when inserting.
                      If None, insertion order is used (FIFO).
        """
        self.nodes: Set[GoalNode] = set()
        self.roots: Set[GoalNode] = set()  # goals with no parents
        self.goal_cmp = goal_cmp or self._default_cmp
        self._goal_to_node: dict[Goal, GoalNode] = {}
        self.plans: Set = set()  # MIDCA GoalGraph tracks plans for getMatchingPlan

    @staticmethod
    def _default_cmp(g1, g2):
        """Default: no ordering constraint."""
        return 0

    def insert(self, goal: Goal) -> GoalNode:
        """
        Insert a goal into the graph.
        Returns the GoalNode.
        """
        if goal in self._goal_to_node:
            return self._goal_to_node[goal]

        node = GoalNode(goal)
        self._goal_to_node[goal] = node
        self.nodes.add(node)
        self.roots.add(node)

        # Maintain partial order by checking against existing roots
        to_remove = set()
        for root in self.roots:
            cmp_result = self.goal_cmp(goal, root.goal)
            if cmp_result < 0:
                # new goal should come before this root → make it a child
                root.add_child(node)
                self.roots.discard(root)
                to_remove.add(root)
        for r in to_remove:
            self.roots.discard(r)

        return node

    def add_subgoal(self, parent_goal: Goal, child_goal: Goal) -> Tuple[GoalNode, GoalNode]:
        """
        Add a subgoal relationship: child must be achieved before parent is considered.
        """
        p_node = self.insert(parent_goal)
        c_node = self.insert(child_goal)
        c_node.add_child(p_node)
        self.roots.discard(p_node)
        return p_node, c_node

    def mark_achieved(self, goal: Goal):
        """Mark a goal as achieved (completed + satisfied)."""
        if goal not in self._goal_to_node:
            return
        node = self._goal_to_node[goal]
        node.achieved = True
        node.completed = True
        # Propagate up: if all children are completed, parent becomes root
        for parent in list(node.parents):
            if all(c.completed for c in parent.children):
                self.roots.add(parent)
        # Remove from roots if it somehow ended up there
        self.roots.discard(node)

    def mark_failed(self, goal: Goal):
        """Mark a goal as failed (cannot be achieved)."""
        if goal not in self._goal_to_node:
            return
        node = self._goal_to_node[goal]
        node.completed = True
        node.achieved = False
        self.roots.discard(node)

    def get_unrestricted_goals(self) -> List[Goal]:
        """
        Return goals that are ready to be executed:
        - No uncompleted parent goals
        - Not yet achieved
        """
        ready = []
        for node in self.roots:
            if not node.achieved and not node.completed:
                ready.append(node.goal)
        return ready

    def all_goals(self) -> List[Goal]:
        return [node.goal for node in self.nodes]

    def get_node(self, goal: Goal) -> Optional[GoalNode]:
        return self._goal_to_node.get(goal)

    def remove_goals_for_plan(self, plan):
        """Remove goals that a plan is expected to achieve."""
        if not hasattr(plan, "goals"):
            return
        for goal in plan.goals:
            if goal in self._goal_to_node:
                node = self._goal_to_node[goal]
                node.completed = True
                self.roots.discard(node)

    def clear(self):
        self.nodes.clear()
        self.roots.clear()
        self._goal_to_node.clear()
        self.plans.clear()

    def addPlan(self, plan):
        """Add a plan to the goal graph (for matching)."""
        self.plans.add(plan)

    def getMatchingPlan(self, goals):
        """
        Return the plan (from self.plans) that exactly covers all given goals,
        preferring plans with fewer extraneous goals. Returns None if no match.
        """
        best = None
        for plan in list(self.plans):
            if not hasattr(plan, 'goals'):
                continue
            # Check all goals are covered
            missing = False
            for goal in goals:
                found = False
                for plan_goal in plan.goals:
                    if self._consistentGoal(goal, plan_goal):
                        found = True
                        break
                if not found:
                    missing = True
                    break
            if not missing:
                if best is None or len(best.goals) > len(plan.goals):
                    best = plan
        return best

    @staticmethod
    def _consistentGoal(goal: Goal, plan_goal) -> bool:
        """Check if a user goal matches a plan goal (same predicate and consistent args)."""
        if isinstance(plan_goal, Goal):
            pg_pred, pg_args = plan_goal.predicate, plan_goal.args
        elif isinstance(plan_goal, tuple):
            pg_pred, pg_args = plan_goal[0], plan_goal[1:]
        else:
            return False
        if goal.predicate != pg_pred:
            return False
        # Check args are consistent (plan goals may use variables like '_')
        for g_arg, pg_arg in zip(goal.args, pg_args):
            if pg_arg == '_' or pg_arg == goal.args[0]:  # variable placeholder
                continue
            if g_arg != pg_arg:
                return False
        return True
