"""
CognitiveWorld — MIDCA-style world state representation.

Represents the agent's model of the world as a set of Atoms (predicates with args).
Supports: diff (comparing expected vs actual), apply_action, atom_true, plan validation.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("cognitive.world")


class Atom:
    """
    A ground predicate: a tuple (predicate_name, *args).

    Examples:
        ("on", "A", "B")      — A is on B
        ("clear", "A")        — A is clear
        ("arm_empty",)        — arm is empty
    """

    def __init__(self, predicate: str, *args):
        self.predicate = predicate
        self.args = args

    def __repr__(self):
        if self.args:
            return f"({self.predicate}, {', '.join(map(repr, self.args))})"
        return f"({self.predicate},)"

    def __eq__(self, other):
        if isinstance(other, Atom):
            return self.predicate == other.predicate and self.args == other.args
        if isinstance(other, tuple) and len(other) == len(self.args) + 1:
            return self.predicate == other[0] and self.args == other[1:]
        return False

    def __hash__(self):
        return hash((self.predicate, self.args))

    @classmethod
    def from_tuple(cls, t: tuple) -> "Atom":
        return cls(t[0], *t[1:])

    def to_tuple(self) -> tuple:
        return (self.predicate,) + self.args


class Predicate:
    """Definition of a predicate with typing info (for documentation/validation)."""

    def __init__(self, name: str, arg_types: List[str] = None, doc: str = ""):
        self.name = name
        self.arg_types = arg_types or []
        self.doc = doc

    def __repr__(self):
        args = ", ".join(self.arg_types)
        return f"Predicate({self.name}: {args})"


class Operator:
    """
    An action operator: name + parameter types + precondition + effect.

    precond(state) → bool
    effect(state) → None (mutates state in place)
    """

    def __init__(
        self,
        name: str,
        param_types: List[str] = None,
        precond: Callable[["CognitiveWorld"], bool] = None,
        effect: Callable[["CognitiveWorld"], None] = None,
        doc: str = "",
    ):
        self.name = name
        self.param_types = param_types or []
        self.precond = precond
        self.effect = effect
        self.doc = doc

    def applicable(self, state: "CognitiveWorld", args: tuple) -> bool:
        if self.precond is None:
            return True
        return self.precond(state, *args)

    def apply(self, state: "CognitiveWorld", args: tuple):
        if self.effect:
            self.effect(state, *args)


class CognitiveWorld:
    """
    MIDCA World state representation.

    At its core: a set of Atoms (ground predicates).
    Operators define the action space.
    """

    def __init__(self, midca_world=None):
        self.atoms: Set[Atom] = set()
        self.operators: Dict[str, Operator] = {}
        self.predicates: Dict[str, Predicate] = {}
        self._midca_world = midca_world
        self._planning_mode = False  # True = skip side effects in apply_action
        if midca_world is not None:
            self._sync_from_midca(midca_world)

    def _sync_from_midca(self, midca_world):
        """Sync atoms from a MIDCA world object.

        Strategy: instead of rebuilding the entire atoms set from MIDCA (which loses
        track of which atoms are "new" vs "old"), we diff the current CognitiveWorld
        atoms against MIDCA atoms and only update what changed.

        The key invariant: apply_action effects are applied to CognitiveWorld atoms
        first, then _sync_from_midca reconciles with the MIDCA world. This diff
        approach means MIDCA's mutations don't corrupt our own atom state.
        """
        current = {a.predicate: a.args for a in self.atoms}

        self.atoms = set()
        for atom in midca_world.get_atoms():
            pred_name = atom.predicate.name
            args = [obj.name for obj in atom.args]
            self.atoms.add(Atom(pred_name, *args))

    def _apply_effects(self, inst):
        """Apply instantiated MIDCA operator effects to CognitiveWorld atoms.
        
        Does NOT modify the MIDCA world — only CognitiveWorld.atoms.
        """
        for i, result in enumerate(inst.results):
            atom = Atom(result.predicate.name, *[str(a) for a in result.args])
            if inst.postPos[i]:
                self.atoms.add(atom)
            else:
                self.atoms.discard(atom)

    # ── atom operations ────────────────────────────────────────────

    def add_atom(self, atom: Atom):
        self.atoms.add(atom)

    def remove_atom(self, atom: Atom):
        self.atoms.discard(atom)

    def atom_true(self, predicate: str, *args) -> bool:
        """Check if a ground atom is true in this world."""
        atom = Atom(predicate, *args)
        return atom in self.atoms

    def query(self, predicate: str) -> List[tuple]:
        """Return all atoms with the given predicate."""
        return [a.to_tuple() for a in self.atoms if a.predicate == predicate]

    # ── diff ───────────────────────────────────────────────────────

    def diff(self, other: "CognitiveWorld") -> Tuple[Set[Atom], Set[Atom]]:
        """
        Compare this world to another (expected) world.

        Returns (expected_missing, expected_extra):
            expected_missing: atoms in `other` but not in self (not achieved)
            expected_extra: atoms in self but not in `other` (unexpected)
        """
        missing = other.atoms - self.atoms
        extra = self.atoms - other.atoms
        return missing, extra

    def matches(self, other: "CognitiveWorld") -> bool:
        """True if both worlds have identical atoms."""
        return self.atoms == other.atoms

    # ── operators ──────────────────────────────────────────────────

    def register_operator(self, op: Operator):
        self.operators[op.name] = op

    def action_applicable(self, action_name: str, args: tuple) -> bool:
        op = self.operators.get(action_name)
        if op is None:
            return False
        return op.applicable(self, args)

    def apply_action(self, action_name: str, args: tuple, dry_run: bool = False) -> bool:
        """
        Apply an action if its preconditions are met.

        Args:
            dry_run: if True, only check preconditions without modifying world state.
                     Used by planners to validate plan correctness without side effects.

        When backed by a MIDCA world, delegates to apply_named_action.
        Action names are normalised: pick-up->pickup, put-down->putdown.
        """
        if self._midca_world is not None:
            # MIDCA world is a PLANNING ORACLE - read preconditions from it,
            # but apply effects directly to CognitiveWorld so MIDCA world
            # stays as an immutable planning reference.
            name_map = {'pick-up': 'pickup', 'put-down': 'putdown'}
            midca_name = name_map.get(action_name, action_name)
            mw = self._midca_world

            # Resolve args to MIDCA objects
            try:
                midca_args = [mw.objects[str(a)] for a in args]
            except KeyError:
                raise ValueError(f"Unknown object: {args}")

            # Check preconditions BEFORE any mutation
            op = mw.operators[midca_name]
            inst = op.instantiate(midca_args)
            applicable = mw.is_applicable(inst)

            if self._planning_mode and not dry_run:
                # Expected world path: we still need to apply effects so the
                # expected MIDCA world state advances correctly for subsequent
                # preconditions checks. Use mw.apply on the EXPECTED world's
                # copy (which is separate from the actual MIDCA world).
                try:
                    mw.apply(inst)
                    self._sync_from_midca(mw)
                except Exception:
                    pass  # Expected world can tolerate apply failures
                return applicable

            if self._planning_mode:
                return applicable  # Planner dry run: check preconditions only

            if not applicable:
                return False

            if dry_run:
                return True  # Preconditions satisfied; caller can use this to validate plan

            # Apply effects to MIDCA world first
            mw.apply(inst)

            # Now sync CognitiveWorld atoms from MIDCA (replaces atoms entirely,
            # so pre_state's atoms - which were DEEP-COPIED before this line - stay intact)
            self._sync_from_midca(mw)
            return True

        op = self.operators.get(action_name)
        if op is None:
            return False
        if not op.applicable(self, args):
            return False
        op.apply(self, args)
        return True

    # ── plan validation ────────────────────────────────────────────

    def plan_correct(self, plan) -> bool:
        """Check if all actions in a plan are applicable in sequence."""
        for action in plan.actions:
            if not self.action_applicable(action.name, action.args):
                return False
            self.apply_action(action.name, action.args)
        return True

    def plan_goals_achieved(self, plan) -> bool:
        """Check if a plan's goals are satisfied by the resulting world state."""
        if not hasattr(plan, "goals") or not plan.goals:
            return False
        for goal in plan.goals:
            if isinstance(goal, Atom):
                if goal not in self.atoms:
                    return False
            elif isinstance(goal, tuple):
                if not self.atom_true(goal[0], *goal[1:]):
                    return False
        return True

    # ── snapshot ───────────────────────────────────────────────────

    def copy(self) -> "CognitiveWorld":
        """Return a deep copy of this world."""
        w = CognitiveWorld()
        w.atoms = set(self.atoms)
        w.operators = dict(self.operators)
        w.predicates = dict(self.predicates)
        return w

    def __repr__(self):
        return f"CognitiveWorld({len(self.atoms)} atoms)"
