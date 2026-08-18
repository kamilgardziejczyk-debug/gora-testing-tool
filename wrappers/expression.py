r"""The `validation` expression language shared by every asserting tag.

A scenario states its condition as a Python expression, with the values the
tag makes available written in braces:

    validation: "'tracker.log' in {files}"
    validation: "{count} == 192"
    validation: 'matches({log}, "unix=[0-9]+")'

The braces are what make a scenario readable: `{count}` is plainly the thing
the tag measured, while `count` on its own could be anything. They also turn a
misspelling into an error at load time - every bare name is rejected, so
`{fils}` and `files` both fail while the scenario is being read, rather than
at the moment the DUT is finally in the right state to be checked.

Expressions are walked as an AST against a whitelist rather than handed to
`eval()`. This is not a security boundary - `!ExecuteCommand` already runs
arbitrary shell, so a scenario author needs no help from this module - it is
here so that a typo fails as a clear message naming what was disallowed
instead of as an AttributeError from somewhere inside the runner.

Substitution runs over `tokenize` output rather than a regex, so braces
*inside a string literal* are left alone. That matters more than it sounds:
`{2,4}` is an ordinary regular expression quantifier, and the regex-based
tags pass their patterns through here as string literals.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import re
import tokenize
import warnings
from typing import Any, Iterable, Mapping, Sequence


# Prefix a braced name is rewritten to before parsing. Bare names starting with
# an underscore are rejected outright (see `_check_name`), so a scenario cannot
# reach one of these by typing it, and a braced name can never collide with
# something the author wrote themselves.
MANGLE_PREFIX = "_v_"

# How much of a value is quoted when reporting what an expression actually saw.
# A device log or an MQTT payload runs to kilobytes, and pasting one whole
# would bury the assertion it was supposed to explain.
MAX_RENDERED_CHARS = 300

# Node types an expression may be built from. Everything outside this set is
# rejected by name, so a new Python syntax feature cannot quietly become
# available here just because the interpreter was upgraded.
ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.List,
    ast.Tuple,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.Attribute,
    ast.GeneratorExp,
    ast.ListComp,
    ast.comprehension,
)

# Method names an expression may call on a value. An allowlist rather than a
# denylist because `''.__class__.__mro__[1].__subclasses__()` is the shape of
# every escape from a Python expression sandbox, and enumerating the handful
# of string and list methods a scenario actually wants is far easier to be
# sure about than enumerating what must stay out of reach.
ALLOWED_METHODS = frozenset({
    "count",
    "endswith",
    "find",
    "lower",
    "lstrip",
    "replace",
    "rstrip",
    "split",
    "startswith",
    "strip",
    "upper",
})


def _matches(text: str, pattern: str) -> bool:
    """Whether `pattern` is found anywhere in `text`, as `re.search` would.

    The bridge for tags whose condition was a bare regular expression before
    expressions existed: `matches({log}, "unix=[0-9]+")` means exactly what
    `validation: 'unix=[0-9]+'` used to.
    """
    try:
        return re.search(pattern, text) is not None
    except re.error as error:
        raise ValueError(f"matches(): '{pattern}' is not a valid regular expression ({error})") from None


def _matching(pattern: str, items: Iterable[str]) -> list[str]:
    """The items matching a shell-style glob, e.g. `matching("session_*", {dirs})`.

    Exists because `in` is exact membership, and the `path` fields of the same
    tags take globs - so `'session_*' in {dirs}` reads as though it should
    pattern-match and quietly does not. This gives the glob an obvious spelling
    instead.

    Returns the matches rather than a bool so it composes: an empty list is
    falsey, which makes a bare `matching(...)` mean "at least one", while
    `len(matching(...)) == 3` counts them and `in` checks a specific one.
    """
    return fnmatch.filter(list(items), pattern)


# Callables an expression may use. Deliberately small: these are the ones that
# turn a list of names into an assertion, and nothing here touches the runner,
# the filesystem, or the scenario.
BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "matches": _matches,
    "matching": _matching,
    "max": max,
    "min": min,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
}


# Comparison node types mapped back to the symbol that produced them, for
# `Expression.as_simple_comparison`.
COMPARISON_SYMBOLS: dict[type[ast.AST], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}


class ExpressionError(ValueError):
    """A `validation` expression that cannot be compiled or evaluated."""


class ExpressionSyntaxError(ExpressionError):
    """A `validation` field that is not a Python expression at all.

    Kept distinct because it is the one failure that a pre-expression scenario
    produces - a bare regular expression is rarely valid Python - so it is
    where the migration hint belongs. A scenario that *is* an expression but
    names something wrong gets its own, more specific message instead.
    """


class Expression:
    """A compiled `validation` expression, bound to one tag's variables.

    Compiled when the scenario is read and evaluated when the command runs, so
    a scenario that cannot possibly pass is rejected before any hardware is
    touched - the same bargain the tags used to get from compiling their
    regular expressions at parse time.
    """

    def __init__(self, source: str, allowed: Sequence[str], context: str) -> None:
        """Compile `source`, permitting the braced names in `allowed`.

        `context` prefixes every error message, and should name the tag and
        field being compiled (e.g. `"DutLogExpect: 'validation'"`).
        """
        self.source = source
        self.context = context
        self.allowed = tuple(allowed)
        rewritten, self.used = _substitute(source, self.allowed, context)
        self._tree = _parse(rewritten, self.allowed, context)
        self._code = compile(self._tree, filename="<validation>", mode="eval")

    def evaluate(self, variables: Mapping[str, object]) -> bool:
        """Evaluate against `variables`, a mapping of unbraced name to value."""
        missing = [name for name in self.used if name not in variables]
        if missing:
            raise ExpressionError(
                f"{self.context}: no value was supplied for {_join_braced(missing)}. "
                f"This is a bug in the wrapper, not in the scenario."
            )

        namespace = {MANGLE_PREFIX + name: variables[name] for name in self.used}
        namespace["__builtins__"] = {}
        namespace.update(BUILTINS)
        try:
            return bool(eval(self._code, namespace))  # noqa: S307 - AST-checked above
        except ExpressionError:
            raise
        except Exception as error:  # noqa: BLE001 - reported as a scenario error
            raise ExpressionError(
                f"{self.context}: {self.source!r} could not be evaluated "
                f"({type(error).__name__}: {error})"
            ) from error

    def as_simple_comparison(self) -> tuple[str, str, object] | None:
        """`(variable, operator, literal)` if this is one variable against one constant.

        Returns None for anything more involved. A caller that can act on the
        simple shape - !MqttExpect knows a growing count can settle `{count}
        == 5` before its window is up - uses this to keep that optimization,
        and falls back to evaluating the whole expression otherwise.
        """
        node = self._tree.body
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return None
        left, operator, right = node.left, node.ops[0], node.comparators[0]
        symbol = COMPARISON_SYMBOLS.get(type(operator))
        if symbol is None or not isinstance(left, ast.Name) or not isinstance(right, ast.Constant):
            return None
        if not left.id.startswith(MANGLE_PREFIX):
            return None
        return left.id[len(MANGLE_PREFIX):], symbol, right.value

    def describe(self, variables: Mapping[str, object]) -> str:
        """Render the variables this expression used, for the test report.

        Only the ones actually referenced: a tag may offer half a dozen, and
        listing the unused ones alongside a failed assertion is noise that
        makes the relevant value harder to find.
        """
        return ", ".join(f"{name}={_render(variables.get(name))}" for name in self.used) or "(no variables)"


def compile_expression(
    source: str,
    allowed: Sequence[str],
    context: str,
    hint: str | None = None,
) -> Expression:
    """Compile one `validation` field, rejecting the pre-expression syntax.

    `hint` is shown when `source` looks like the old syntax - a value with no
    braces in it at all. Every meaningful assertion names at least one
    variable, so that is a reliable tell, and catching it here means an
    un-migrated scenario fails saying so rather than being read as an
    expression that happens to be nonsense.
    """
    try:
        return Expression(source, allowed, context)
    except ExpressionSyntaxError as error:
        if "{" in source:
            raise
        message = (
            f"{context}: {source!r} is not an expression, so it looks like the pre-expression "
            f"syntax. Validations are now Python expressions naming their variables in braces, "
            f"e.g. {_join_braced(allowed)}."
        )
        raise ExpressionError(f"{message} {hint}" if hint else message) from error


def _substitute(source: str, allowed: Sequence[str], context: str) -> tuple[str, tuple[str, ...]]:
    """Rewrite every `{name}` to a mangled identifier, reporting which were used.

    Runs over tokens so that a brace inside a string literal - a regex
    quantifier such as `[0-9]{4}` - is never touched.
    """
    tokens = _tokenize(source, context)
    pieces: list[str] = []
    used: list[str] = []
    index = 0

    while index < len(tokens):
        name = _braced_name_at(tokens, index)
        if name is None:
            pieces.append(tokens[index].string)
            index += 1
            continue

        _check_braced(name, allowed, context)
        if name not in used:
            used.append(name)
        pieces.append(MANGLE_PREFIX + name)
        index += 3

    _check_no_stray_braces(pieces, source, allowed, context)
    return " ".join(pieces), tuple(used)


def _tokenize(source: str, context: str) -> list[tokenize.TokenInfo]:
    """Tokenize `source`, keeping only the tokens that carry meaning."""
    skipped = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    try:
        produced = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as error:
        if "{" in source:
            raise ExpressionError(
                f"{context}: {source!r} has an unclosed brace. Write a variable as {{name}}."
            ) from None
        raise ExpressionSyntaxError(
            f"{context}: {source!r} is not a valid expression ({error})"
        ) from None
    return [token for token in produced if token.type not in skipped and token.string]


def _braced_name_at(tokens: Sequence[tokenize.TokenInfo], index: int) -> str | None:
    """The name in a `{ name }` triple starting at `index`, or None."""
    if index + 2 >= len(tokens):
        return None
    opening, name, closing = tokens[index], tokens[index + 1], tokens[index + 2]
    if opening.string != "{" or closing.string != "}" or name.type != tokenize.NAME:
        return None
    return name.string


def _check_braced(name: str, allowed: Sequence[str], context: str) -> None:
    """Reject a braced name this tag does not offer."""
    if name.startswith("_"):
        raise ExpressionError(f"{context}: variable names may not start with an underscore ('{{{name}}}')")
    if name not in allowed:
        raise ExpressionError(
            f"{context}: unknown variable '{{{name}}}'. This tag offers {_join_braced(allowed)}."
        )


def _check_no_stray_braces(pieces: Sequence[str], source: str, allowed: Sequence[str], context: str) -> None:
    """Reject a brace left over outside a string literal.

    An unmatched or malformed one (`{files`, `{ }`, `{2}`) would otherwise be
    parsed as a set literal and fail with something far less obvious.
    """
    if "{" in pieces or "}" in pieces:
        raise ExpressionError(
            f"{context}: {source!r} has a brace that is not a variable. Write a variable as "
            f"{{name}} with nothing between the braces but the name, one of {_join_braced(allowed)}."
        )


def _parse(rewritten: str, allowed: Sequence[str], context: str) -> ast.Expression:
    """Parse the rewritten source and check every node against the whitelist."""
    try:
        with warnings.catch_warnings():
            # An unrecognized escape ("\S") is only a warning today and an error
            # in a future Python. Promoting it here means a regex written as a
            # plain string fails now, with an explanation, rather than working
            # until the interpreter is upgraded under a rig nobody is watching.
            warnings.simplefilter("error", SyntaxWarning)
            tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as error:
        # With the filter above, CPython reports the bad escape as a SyntaxError
        # rather than the SyntaxWarning it would otherwise emit.
        if "escape sequence" in str(error.msg):
            raise ExpressionError(
                f"{context}: {error.msg} Write a regular expression as a raw string - "
                f'r"\\S+" rather than "\\S+" - so its backslashes reach the regex intact.'
            ) from None
        raise ExpressionSyntaxError(f"{context}: not a valid expression ({error.msg})") from None

    locals_ = _comprehension_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ExpressionError(
                f"{context}: {type(node).__name__} is not allowed in a validation expression."
            )
        if isinstance(node, ast.Attribute):
            _check_attribute(node, context)
        if isinstance(node, ast.Name):
            _check_name(node, allowed, locals_, context)
    return tree


def _comprehension_names(tree: ast.Expression) -> frozenset[str]:
    """Names bound by a comprehension, e.g. `f` in `any(f in {files} for f in ...)`.

    Collected up front because they are legitimate bare names - the one case
    where a name in an expression is neither a variable nor a builtin - and
    the walk that checks names has no view of which scope it is inside.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return frozenset(names)


def _check_attribute(node: ast.Attribute, context: str) -> None:
    """Reject an attribute outside the method allowlist."""
    if node.attr not in ALLOWED_METHODS:
        allowed = ", ".join(sorted(ALLOWED_METHODS))
        raise ExpressionError(
            f"{context}: '.{node.attr}' is not allowed in a validation expression "
            f"(allowed: {allowed})."
        )


def _check_name(
    node: ast.Name, allowed: Sequence[str], locals_: frozenset[str], context: str
) -> None:
    """Reject a bare name: a variable must be braced, a call must be a builtin."""
    name = node.id
    if name.startswith(MANGLE_PREFIX) or name in BUILTINS or name in locals_:
        return
    if name in allowed:
        raise ExpressionError(
            f"{context}: write '{{{name}}}' rather than a bare '{name}' - a validation "
            f"names its variables in braces."
        )
    if isinstance(node.ctx, ast.Store):
        raise ExpressionError(f"{context}: '{name}' cannot be assigned to in a validation expression.")
    raise ExpressionError(
        f"{context}: unknown name '{name}'. This tag offers {_join_braced(allowed)}, "
        f"and the functions {', '.join(sorted(BUILTINS))}."
    )


def _join_braced(names: Iterable[str]) -> str:
    """Render names as `{a}, {b}` for an error message."""
    braced = [f"{{{name}}}" for name in names]
    return ", ".join(braced) if braced else "(none)"


def _render(value: object) -> str:
    """One variable's value, short enough to sit inside a report cell."""
    text = repr(value)
    if len(text) <= MAX_RENDERED_CHARS:
        return text
    return f"{text[:MAX_RENDERED_CHARS]}... (+{len(text) - MAX_RENDERED_CHARS} chars)"
