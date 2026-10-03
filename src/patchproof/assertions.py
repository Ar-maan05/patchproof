"""Deterministic, AST-based assertion-strength analysis of Python test functions.

A test that flips from fail (base) to pass (head) only *proves* a fix if what it asserts is
specific enough to notice a wrong result. This module classifies a test function as

* ``STRONG``: it contains at least one check that pins a concrete behaviour (equality or
  inequality against an expected value, a specific ``pytest.raises``, a specific member or
  length, a predicate call, a mock call assertion, ...).
* ``WEAK``: every check it makes is a no-crash / type-only / truthiness-only / tautological
  check, or it makes no check at all.
* ``UNKNOWN``: the function could not be analysed (the caller must treat this as not weak).

The heuristic is deliberately conservative: it only calls a test WEAK for well-understood
weakness *patterns* (see ``classify_expr``), because a false WEAK_TEST on a good test is worse
than a missed weak one. Anything not recognised counts as a strong check.

Known limits: it is purely syntactic. It cannot know whether ``assert is_ok(x)`` is a precise
predicate or ``assert parse(x)`` is a truthiness check on a container (both are calls, both
are counted as strong). It does not follow fixtures, ``conftest.py`` helpers in other modules,
or data-driven expected values loaded from files.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field

STRONG = "STRONG"
WEAK = "WEAK"
UNKNOWN = "UNKNOWN"

_BROAD_EXC = {"Exception", "BaseException"}
_SWALLOWED = _BROAD_EXC | {"AssertionError"}
# Builtins whose truthiness says "something non-empty came back", not a specific behaviour.
_SHAPE_BUILTINS = {
    "len", "str", "list", "dict", "set", "tuple", "frozenset", "bool", "int", "float",
    "bytes", "repr", "sorted", "abs", "sum", "type", "id",
}  # fmt: skip
_TYPE_ONLY_CALLS = {"isinstance", "issubclass", "callable", "hasattr", "type"}
_TYPE_NAMES = {
    "str", "int", "float", "bool", "bytes", "list", "dict", "set", "tuple", "frozenset",
    "object", "type", "complex",
}  # fmt: skip
_NONNEG_CALLS = {"len", "abs"}
_MAX_HELPER_DEPTH = 3


@dataclass
class Check:
    strength: str  # STRONG | WEAK
    reason: str


@dataclass
class Strength:
    cls: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"class": self.cls, "reasons": self.reasons}


# --------------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------------
class _NotConst(Exception):
    pass


def _fold(node: ast.expr):
    """Evaluate a side-effect-free constant expression; raise _NotConst otherwise."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        v = _fold(node.operand)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise _NotConst
    if isinstance(node, ast.BinOp):
        a, b = _fold(node.left), _fold(node.right)
        ops = {
            ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
            ast.Mod: lambda: a % b, ast.FloorDiv: lambda: a // b, ast.Div: lambda: a / b,
        }  # fmt: skip
        if isinstance(node.op, ast.Pow):
            if not (isinstance(b, int) and 0 <= b <= 64):
                raise _NotConst
            return a**b
        fn = ops.get(type(node.op))
        if fn is None:
            raise _NotConst
        try:
            return fn()
        except ArithmeticError, TypeError:
            raise _NotConst from None
    if isinstance(node, ast.Tuple | ast.List):
        vals = [_fold(e) for e in node.elts]
        return tuple(vals) if isinstance(node, ast.Tuple) else vals
    if isinstance(node, ast.Set):
        return {_fold(e) for e in node.elts}
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            raise _NotConst
        return {_fold(k): _fold(v) for k, v in zip(node.keys, node.values, strict=True)}
    if isinstance(node, ast.BoolOp):
        vals = [_fold(v) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left = _fold(node.left)
        for op, comp in zip(node.ops, node.comparators, strict=True):
            right = _fold(comp)
            try:
                ok = _CMP[type(op)](left, right)
            except KeyError, TypeError:
                raise _NotConst from None
            if not ok:
                return False
            left = right
        return True
    raise _NotConst


_CMP = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}  # fmt: skip


def _const(node: ast.expr):
    """(True, value) when ``node`` is a constant expression, else (False, None)."""
    try:
        return True, _fold(node)
    except _NotConst, RecursionError, ValueError:
        return False, None


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _same(a: ast.expr, b: ast.expr) -> bool:
    return ast.dump(a) == ast.dump(b)


def _call_name(node: ast.expr) -> str:
    f = node.func if isinstance(node, ast.Call) else node
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _dotted(node: ast.expr) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# --------------------------------------------------------------------------------------------
# expression classification
# --------------------------------------------------------------------------------------------
def _flip(op: ast.cmpop) -> ast.cmpop:
    pairs = [(ast.Eq, ast.NotEq), (ast.Is, ast.IsNot), (ast.In, ast.NotIn),
             (ast.Lt, ast.GtE), (ast.Gt, ast.LtE)]  # fmt: skip
    for a, b in pairs:
        if isinstance(op, a):
            return b()
        if isinstance(op, b):
            return a()
    return op


def _is_type_expr(node: ast.expr) -> bool:
    if isinstance(node, ast.Name) and node.id in _TYPE_NAMES:
        return True
    return isinstance(node, ast.Call) and _call_name(node) == "type"


def _falsy_const(node: ast.expr) -> bool:
    ok, v = _const(node)
    if ok:
        try:
            return not v
        except Exception:  # noqa: BLE001
            return False
    return False


def _nonneg_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in _NONNEG_CALLS


def _compare_pair(left: ast.expr, op: ast.cmpop, right: ast.expr, negated: bool) -> Check:
    if negated:
        op = _flip(op)
    if _same(left, right):
        return Check(WEAK, "compares a value to itself (tautology)")
    lc, _ = _const(left)
    rc, _ = _const(right)
    if lc and rc:
        return Check(WEAK, "compares two constants (tautology)")
    if isinstance(op, ast.Eq | ast.NotEq | ast.Is | ast.IsNot) and (
        _is_type_expr(left) or _is_type_expr(right)
    ):
        return Check(WEAK, "checks only the type of the result")
    if isinstance(op, ast.Eq | ast.NotEq):
        if isinstance(op, ast.NotEq):
            for side in (left, right):
                if _is_none(side):
                    return Check(WEAK, "`!= None` only checks that something was returned")
            if _falsy_const(right) or _falsy_const(left):
                return Check(WEAK, "inequality against an empty/zero value is a truthiness check")
        return Check(STRONG, "equality/inequality against an expected value")
    if isinstance(op, ast.Is | ast.IsNot):
        if isinstance(op, ast.IsNot) and (_is_none(right) or _is_none(left)):
            return Check(WEAK, "`is not None` only checks that something was returned")
        return Check(STRONG, "identity check against a specific object")
    if isinstance(op, ast.In | ast.NotIn):
        return Check(STRONG, "membership of a specific element")
    # ordering comparisons
    if isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE):
        # normalise to (expr OP const)
        expr, const_node, o = (left, right, op) if rc else (right, left, _mirror(op))
        if (lc or rc) and _nonneg_call(expr):
            _, c = _const(const_node)
            try:
                if (isinstance(o, ast.GtE) and c <= 0) or (isinstance(o, ast.Gt) and c < 0):
                    return Check(WEAK, "tautology: a length/absolute value is always >= 0")
                if (isinstance(o, ast.Gt) and c == 0) or (isinstance(o, ast.GtE) and c == 1):
                    return Check(WEAK, "only checks that the result is non-empty")
            except TypeError:
                pass
        return Check(STRONG, "bound check against a concrete value")
    return Check(STRONG, "comparison")


def _mirror(op: ast.cmpop) -> ast.cmpop:
    return {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE}[type(op)]()


def _truthiness(node: ast.expr, negated: bool) -> Check:
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in _TYPE_ONLY_CALLS:
            return Check(WEAK, f"`{name}(...)` checks only type/attribute presence")
        if name in _SHAPE_BUILTINS and isinstance(node.func, ast.Name):
            return Check(WEAK, f"truthiness of `{name}(...)` only checks non-emptiness")
        return Check(STRONG, "predicate call result is asserted")
    if isinstance(node, ast.Attribute) and node.attr in ("called", "ok"):
        return Check(STRONG, "specific flag is asserted")
    return Check(WEAK, "bare truthiness check on a value")


def classify_expr(node: ast.expr, negated: bool = False) -> Check:
    """Strength of a single boolean expression that a test asserts to be true."""
    ok, val = _const(node)
    if ok:
        truthy = bool(val) != negated
        return Check(WEAK, "constant assertion: " + ("always true" if truthy else "always false"))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return classify_expr(node.operand, not negated)
    if isinstance(node, ast.BoolOp):
        parts = [classify_expr(v, negated) for v in node.values]
        # `and` (or negated `or`): every part must hold, so the strongest part counts.
        # `or` (or negated `and`): the weakest part bounds the assertion, a weak branch
        # makes the whole assertion pass on any input that satisfies it.
        conj = isinstance(node.op, ast.And) != negated
        if conj:
            strong = [p for p in parts if p.strength == STRONG]
            return strong[0] if strong else parts[0]
        weak = [p for p in parts if p.strength == WEAK]
        if weak:
            return Check(WEAK, "disjunction with a weak alternative: " + weak[0].reason)
        return parts[0]
    if isinstance(node, ast.Compare):
        checks = []
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            checks.append(_compare_pair(left, op, right, negated))
            left = right
        strong = [c for c in checks if c.strength == STRONG]
        return strong[0] if strong else checks[0]
    if isinstance(node, ast.NamedExpr):
        return classify_expr(node.value, negated)
    if isinstance(node, ast.Await):
        return classify_expr(node.value, negated)
    return _truthiness(node, negated)


# --------------------------------------------------------------------------------------------
# statement-level collection
# --------------------------------------------------------------------------------------------
def _handler_types(h: ast.ExceptHandler) -> set[str]:
    if h.type is None:
        return {"BaseException"}
    elts = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
    return {_dotted(e).split(".")[-1] for e in elts}


def _is_fail_stmt(s: ast.stmt) -> bool:
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        return _dotted(s.value.func) in ("pytest.fail", "fail", "self.fail")
    if isinstance(s, ast.Raise):
        return bool(s.exc) and "AssertionError" in ast.dump(s.exc)
    if isinstance(s, ast.Assert):
        ok, v = _const(s.test)
        return ok and not v
    return False


def _is_swallow_stmt(s: ast.stmt) -> bool:
    if isinstance(s, ast.Pass | ast.Continue | ast.Break):
        return True
    if isinstance(s, ast.Return):
        return s.value is None or _const(s.value)[0]
    if isinstance(s, ast.Expr):
        v = s.value
        if isinstance(v, ast.Constant):
            return True
        if isinstance(v, ast.Call) and _dotted(v.func) in (
            "print",
            "logging.exception",
            "logger.exception",
            "log.exception",
        ):
            return True
    return False


_UNITTEST_CMP = {
    "assertEqual": ast.Eq, "assertEquals": ast.Eq, "assertDictEqual": ast.Eq,
    "assertListEqual": ast.Eq, "assertTupleEqual": ast.Eq, "assertSetEqual": ast.Eq,
    "assertSequenceEqual": ast.Eq, "assertMultiLineEqual": ast.Eq,
    "assertCountEqual": ast.Eq, "assertAlmostEqual": ast.Eq, "assertAlmostEquals": ast.Eq,
    "assertNotEqual": ast.NotEq, "assertNotEquals": ast.NotEq,
    "assertNotAlmostEqual": ast.NotEq, "assertIs": ast.Is, "assertIsNot": ast.IsNot,
    "assertIn": ast.In, "assertNotIn": ast.NotIn, "assertGreater": ast.Gt,
    "assertGreaterEqual": ast.GtE, "assertLess": ast.Lt, "assertLessEqual": ast.LtE,
}  # fmt: skip
_UNITTEST_RAISES = {"assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex"}
_STRONG_MOCK = {
    "assert_called_with", "assert_called_once_with", "assert_any_call", "assert_has_calls",
    "assert_awaited_with", "assert_awaited_once_with", "assert_called_once",
    "assert_not_called", "assert_called", "assert_awaited",
}  # fmt: skip
_HELPER_HINTS = ("assert", "check", "verify", "expect", "validate", "ensure")


def _raises_check(call: ast.Call) -> Check:
    exc = call.args[0] if call.args else None
    for kw in call.keywords:
        if kw.arg in ("match", "msg") and kw.value is not None:
            return Check(STRONG, "pytest.raises with a match on the message")
        if kw.arg == "expected_exception":
            exc = kw.value
    if len(call.args) >= 2 and _dotted(call.func).endswith(
        ("assertRaisesRegex", "assertWarnsRegex")
    ):
        return Check(STRONG, "assertRaisesRegex pins the message")
    if exc is None:
        return Check(WEAK, "raises without naming an exception")
    names = {_dotted(e).split(".")[-1] for e in (exc.elts if isinstance(exc, ast.Tuple) else [exc])}
    if names & _BROAD_EXC:
        return Check(WEAK, "raises generic Exception without a match: any failure would do")
    return Check(STRONG, "raises a specific exception type")


class _Collector:
    def __init__(self, module: ast.Module | None):
        self.checks: list[Check] = []
        self.helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        if module is not None:
            for n in module.body:
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith(
                    "test"
                ):
                    self.helpers[n.name] = n
        self.seen_helpers: set[str] = set()

    def stmts(self, body: list[ast.stmt], depth: int = 0) -> None:
        for s in body:
            self.stmt(s, depth)

    def stmt(self, s: ast.stmt, depth: int) -> None:
        if isinstance(s, ast.Assert):
            self.checks.append(classify_expr(s.test))
        elif isinstance(s, ast.With | ast.AsyncWith):
            for item in s.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _call_name(ce) in (
                    "raises",
                    "warns",
                    "deprecated_call",
                ):
                    self.checks.append(
                        _raises_check(ce)
                        if _call_name(ce) != "deprecated_call"
                        else Check(WEAK, "only checks that a warning was emitted")
                    )
                elif isinstance(ce, ast.Call) and _call_name(ce) in _UNITTEST_RAISES:
                    self.checks.append(_raises_check(ce))
                elif isinstance(ce, ast.Call) and _call_name(ce) in ("assertLogs", "assertNoLogs"):
                    self.checks.append(Check(WEAK, "only checks that something was logged"))
            self.stmts(s.body, depth)
        elif isinstance(s, ast.Try | ast.TryStar):
            self.try_stmt(s, depth)
        elif isinstance(s, ast.For | ast.AsyncFor | ast.While | ast.If):
            self.stmts(s.body, depth)
            self.stmts(s.orelse, depth)
        elif isinstance(s, ast.Match):
            for c in s.cases:
                self.stmts(c.body, depth)
        elif isinstance(s, ast.Expr):
            self.expr_stmt(s.value, depth)
        # nested defs/classes are not executed by the test itself

    def try_stmt(self, s: ast.Try | ast.TryStar, depth: int) -> None:
        swallows = False
        no_crash = False
        for h in s.handlers:
            if not (_handler_types(h) & _SWALLOWED):
                continue
            if h.body and all(_is_swallow_stmt(b) for b in h.body):
                swallows = True
            elif any(_is_fail_stmt(b) for b in h.body):
                no_crash = True
        inner = _Collector(None)
        inner.helpers, inner.seen_helpers = self.helpers, self.seen_helpers
        inner.stmts(s.body, depth)
        if swallows:
            # An AssertionError raised in the body is caught and ignored: nothing is checked.
            self.checks.append(Check(WEAK, "try/except swallows every failure (nothing can fail)"))
        else:
            self.checks.extend(inner.checks)
            if no_crash:
                self.checks.append(Check(WEAK, "only checks that the call does not raise"))
        self.stmts(s.orelse, depth)
        self.stmts(s.finalbody, depth)
        for h in s.handlers:
            if not (_handler_types(h) & _SWALLOWED):
                self.stmts(h.body, depth)  # asserts in a specific-exception handler count

    def expr_stmt(self, v: ast.expr, depth: int) -> None:
        if isinstance(v, ast.Await):
            v = v.value
        if not isinstance(v, ast.Call):
            return
        name = _call_name(v)
        dotted = _dotted(v.func)
        is_pytest_raises = name in ("raises", "warns") and dotted.startswith(
            ("pytest", "raises", "warns")
        )
        if is_pytest_raises or name in _UNITTEST_RAISES:
            self.checks.append(_raises_check(v))
        elif name in _UNITTEST_CMP and v.args:
            self.unittest_cmp(v, name)
        elif name in ("assertTrue", "assertFalse") and v.args:
            self.checks.append(classify_expr(v.args[0], name == "assertFalse"))
        elif name in ("assertIsNone", "assertIsNotNone") and v.args:
            if name == "assertIsNone":
                self.checks.append(Check(STRONG, "value is asserted to be None"))
            else:
                self.checks.append(
                    Check(WEAK, "`is not None` only checks that something was returned")
                )
        elif name in ("assertIsInstance", "assertNotIsInstance", "assertRegex"):
            if name == "assertRegex":
                self.checks.append(Check(STRONG, "regex match on the result"))
            else:
                self.checks.append(Check(WEAK, "checks only the type of the result"))
        elif name in _STRONG_MOCK:
            self.checks.append(Check(STRONG, "mock call assertion"))
        elif name in self.helpers and depth < _MAX_HELPER_DEPTH and name not in self.seen_helpers:
            self.seen_helpers.add(name)
            self.stmts(self.helpers[name].body, depth + 1)
            self.seen_helpers.discard(name)
        elif name.startswith("assert") or any(h in name.lower() for h in _HELPER_HINTS):
            self.checks.append(
                Check(STRONG, f"unrecognised check helper `{name}` (assumed strong)")
            )

    def unittest_cmp(self, call: ast.Call, name: str) -> None:
        if len(call.args) < 2:
            self.checks.append(Check(STRONG, f"{name} (assumed strong)"))
            return
        a, b = call.args[0], call.args[1]
        if name in ("assertIn", "assertNotIn"):  # assertIn(member, container)
            op = _UNITTEST_CMP[name]()
            self.checks.append(_compare_pair(a, op, b, False))
        else:
            self.checks.append(_compare_pair(a, _UNITTEST_CMP[name](), b, False))


def classify_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef, module: ast.Module | None = None
) -> Strength:
    """Classify one test function's assertions."""
    c = _Collector(module)
    c.stmts(copy.deepcopy(func.body))
    if not c.checks:
        return Strength(WEAK, ["no assertions: the test only calls code and checks nothing"])
    strong = [k for k in c.checks if k.strength == STRONG]
    if strong:
        return Strength(STRONG, _dedupe(k.reason for k in strong))
    return Strength(WEAK, _dedupe(k.reason for k in c.checks))


def classify_source(source: str, test_name: str, class_name: str | None = None) -> Strength:
    """Classify ``test_name`` (optionally inside ``class_name``) found in Python ``source``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Strength(UNKNOWN, ["test file does not parse"])
    scope: list[ast.stmt] = tree.body
    if class_name:
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
                   None)  # fmt: skip
        if cls is None:
            return Strength(UNKNOWN, [f"class {class_name} not found"])
        scope = cls.body
    for n in scope:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == test_name:
            return classify_function(n, tree)
    return Strength(UNKNOWN, [f"test function {test_name} not found"])


def _dedupe(items) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out
