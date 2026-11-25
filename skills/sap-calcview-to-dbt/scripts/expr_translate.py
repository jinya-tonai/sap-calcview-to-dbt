#!/usr/bin/env python3
"""HANA calculation-engine expression -> Snowflake expression translator.

Implements section 7 of references/hanacv2sql-reproduction-spec.md.
Deterministic, token-level rewriting only:
- string literals and quoted identifiers are never touched,
- $$VAR$$ placeholders become dbt var() calls (spec section 6.1 default),
- known functions are mapped per the spec 7.3 dictionary,
- unknown functions are kept verbatim and reported as TODOs.
"""
from __future__ import annotations

import re

VAR_RE = re.compile(r"\$\$([A-Za-z0-9_]+)\$\$")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# spec 7.3: functions that pass through unchanged
PASSTHROUGH = {
    "concat", "replace", "upper", "lower", "trim", "ltrim", "rtrim",
    "round", "abs", "sign", "mod", "coalesce", "nullif", "cast",
    "count", "sum", "min", "max", "avg",
}
# simple 1:1 renames (same argument order)
RENAME = {
    "string": "to_varchar",
    "float": "to_double",
    "int": "to_number",
    "date": "to_date",
    "leftstr": "left",
    "rightstr": "right",
    "midstr": "substr",
    "strlen": "length",
    "if": "iff",
}
# Infix operators that can look like function calls when a parenthesised
# operand follows (`("A"='1') and ("B"='2')`, `"X" in ('A','B')`).
# They pass through verbatim and are NOT unconverted functions.
OPERATORS = {"and", "or", "not", "in"}

# component(d, n): part numbers per spec 7.3 note (order is unusual;
# flagged TODO whenever used so a human confirms against the real view)
COMPONENT_PARTS = {1: "year", 2: "day", 3: "month", 4: "hour", 5: "minute", 6: "second"}

NUMERIC_TYPES = {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT",
                 "DECIMAL", "DOUBLE", "REAL", "FLOAT"}


def _glob_to_like(pattern_sql: str) -> str:
    """Convert a column-engine wildcard pattern to a LIKE pattern.

    Only string literals are converted ('*' -> '%', '?' -> '_', with
    LIKE's own wildcards and the escape character escaped first).
    Non-literal patterns pass through unchanged.
    """
    p = pattern_sql.strip()
    if len(p) >= 2 and p.startswith("'") and p.endswith("'"):
        body = p[1:-1].replace("''", "'")
        body = (body.replace("\\", "\\\\").replace("%", "\\%")
                    .replace("_", "\\_")
                    .replace("*", "%").replace("?", "_"))
        return "'" + body.replace("'", "''") + "'"
    return pattern_sql


class Translator:
    def __init__(self, variables: dict[str, dict] | None = None):
        """variables: {VAR_ID: {defaultValue, dataType}} from the IR."""
        self.variables = variables or {}
        self.todos: list[str] = []

    # ------------------------------------------------------------- public
    def translate(self, formula: str) -> str:
        out, _ = self._scan(formula, 0, stop_at_comma_or_paren=False)
        return out.strip()

    # ------------------------------------------------------------ scanner
    def _scan(self, s: str, i: int, stop_at_comma_or_paren: bool):
        """Translate s starting at i. Returns (text, next_index)."""
        buf: list[str] = []
        depth = 0
        while i < len(s):
            ch = s[i]
            if ch == "'":  # string literal: copy verbatim ('' is an escape)
                j = i + 1
                while j < len(s):
                    if s[j] == "'":
                        if j + 1 < len(s) and s[j + 1] == "'":
                            j += 2
                            continue
                        j += 1
                        break
                    j += 1
                buf.append(s[i:j])
                i = j
                continue
            if ch == '"':  # quoted identifier: copy verbatim
                j = s.find('"', i + 1)
                j = len(s) if j == -1 else j + 1
                buf.append(s[i:j])
                i = j
                continue
            if s.startswith("$$", i):
                m = VAR_RE.match(s, i)
                if m:
                    buf.append(self._var_sql(m.group(1)))
                    i = m.end()
                    continue
            if ch == "(":
                depth += 1
                buf.append(ch)
                i += 1
                continue
            if ch == ")":
                if stop_at_comma_or_paren and depth == 0:
                    return "".join(buf), i
                depth -= 1
                buf.append(ch)
                i += 1
                continue
            if ch == "," and stop_at_comma_or_paren and depth == 0:
                return "".join(buf), i
            m = IDENT_RE.match(s, i)
            if m:
                name = m.group(0)
                j = m.end()
                k = j
                while k < len(s) and s[k] in " \t":
                    k += 1
                if k < len(s) and s[k] == "(":  # function call
                    args, endpos = self._parse_args(s, k)
                    buf.append(self._apply(name.lower(), args))
                    i = endpos
                    continue
                buf.append(name)
                i = j
                continue
            buf.append(ch)
            i += 1
        return "".join(buf), i

    def _parse_args(self, s: str, open_paren: int):
        """Parse a parenthesised argument list; args are translated."""
        args: list[str] = []
        i = open_paren + 1
        probe = s[i:].lstrip()
        if probe.startswith(")"):
            return args, i + (len(s[i:]) - len(probe)) + 1
        while True:
            text, i = self._scan(s, i, stop_at_comma_or_paren=True)
            args.append(text.strip())
            if i < len(s) and s[i] == ",":
                i += 1
                continue
            if i < len(s) and s[i] == ")":
                return args, i + 1
            return args, i  # unbalanced; bail out with what we have

    # ------------------------------------------------------------ mapping
    def _var_sql(self, var_id: str) -> str:
        name = var_id.lower()
        info = self.variables.get(var_id) or {}
        default = info.get("defaultValue")
        if default is None or default == "":
            return "{{ var('" + name + "') }}"
        if (info.get("dataType") or "").upper() in NUMERIC_TYPES:
            return "{{ var('" + name + "', " + str(default) + ") }}"
        return "{{ var('" + name + "', '" + str(default) + "') }}"

    def _apply(self, fn: str, args: list[str]) -> str:
        a = args
        if fn == "in" and len(a) >= 2:
            # function-style in(x, v1, v2) -> operator form x in (v1, v2)
            return f"{a[0]} in ({', '.join(a[1:])})"
        if fn == "not" and len(a) == 1:
            return f"not ({a[0]})"
        if fn in OPERATORS:
            return f"{fn}({', '.join(a)})"
        if fn == "match" and len(a) == 2:
            # Column-engine match(value, pattern): wildcard pattern match
            # (SAP HANA Developer Guide example: match("ABC",'*abc*')).
            # '*' -> '%', '?' -> '_', compared case-insensitively per the
            # official example; flagged for verification like weekday().
            self.todos.append("match() converted to ilike (wildcards * -> %, "
                              "? -> _); verify case sensitivity against "
                              "source data (spec 7.3)")
            return f"{a[0]} ilike {_glob_to_like(a[1])}"
        if fn == "case" and len(a) >= 3:
            # HANA calc-engine case(expr, value, result, ..., default?)
            # -> Snowflake CASE expr WHEN value THEN result ... ELSE d END
            head, rest = a[0], a[1:]
            whens = []
            while len(rest) >= 2:
                whens.append(f"when {rest[0]} then {rest[1]}")
                rest = rest[2:]
            else_sql = f" else {rest[0]}" if rest else ""
            return f"case {head} {' '.join(whens)}{else_sql} end"
        if fn in PASSTHROUGH:
            return f"{fn}({', '.join(a)})"
        if fn in RENAME:
            return f"{RENAME[fn]}({', '.join(a)})"
        if fn == "isnull" and len(a) == 1:
            return f"({a[0]} is null)"
        if fn == "now" and not a:
            return "current_timestamp()"
        if fn == "daysbetween" and len(a) == 2:
            return f"datediff(day, {a[0]}, {a[1]})"
        if fn == "secondsbetween" and len(a) == 2:
            return f"datediff(second, {a[0]}, {a[1]})"
        if fn == "adddays" and len(a) == 2:  # argument order reverses (7.3)
            return f"dateadd(day, {a[1]}, {a[0]})"
        if fn == "addseconds" and len(a) == 2:
            return f"dateadd(second, {a[1]}, {a[0]})"
        if fn == "instr" and len(a) == 2:
            return f"position({a[1]} in {a[0]})"
        if fn == "rounddown" and len(a) in (1, 2):
            self.todos.append("rounddown() mapped to trunc(); "
                              "verify negative-number behaviour (spec 7.3)")
            return f"trunc({', '.join(a)})"
        if fn == "weekday" and len(a) == 1:
            self.todos.append("weekday() origin corrected (HANA Mon=0 vs "
                              "Snowflake Sun=0); verify with sample data (spec 7.3)")
            return f"mod(dayofweek({a[0]}) + 6, 7)"
        if fn == "component" and len(a) == 2:
            self.todos.append("component() part-number mapping needs "
                              "per-case confirmation (spec 7.3)")
            try:
                part = COMPONENT_PARTS[int(a[1])]
                return f"extract({part} from {a[0]})"
            except (ValueError, KeyError):
                return f"component({', '.join(a)})"
        self.todos.append(f"unconverted function {fn}()")
        return f"{fn}({', '.join(a)})"


def translate_expression(formula: str,
                         variables: dict[str, dict] | None = None):
    """Convenience wrapper: returns (snowflake_sql, todo_messages)."""
    t = Translator(variables)
    return t.translate(formula), t.todos
