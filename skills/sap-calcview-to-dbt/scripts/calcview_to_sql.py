#!/usr/bin/env python3
"""Calculation View IR -> Snowflake SQL (CTE per node).

Implements sections 5-7 of references/hanacv2sql-reproduction-spec.md:
each node becomes one CTE in dependency order; the final SELECT follows
the logicalModel (section 5.6 — base measures are projected, never
re-aggregated). Unconvertible elements are emitted as TODO comments,
never silently dropped.

Usage:
  python3 calcview_to_sql.py <xml-or-dir> --out output_sql [--mapping mapping.csv]

Mapping CSV (spec section 4.1, minimal): datasource_id plus either
sf_table or sf_database/sf_schema/sf_table. Without a mapping, data
sources are referenced by a placeholder name derived from resourceUri
and flagged as TODO.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calcview_parser import parse, slugify  # noqa: E402
from expr_translate import Translator  # noqa: E402

JOIN_TYPES = {
    "inner": "inner join",
    "leftouter": "left outer join",
    "rightouter": "right outer join",
    "fullouter": "full outer join",
    "text": "left join",
    "referential": "left join",
}
AGG_FUNCS = {"sum", "min", "max", "count", "avg"}


def snake(name: str) -> str:
    base = name.rsplit("::", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9]+", "_", base)
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", base)
    base = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", base)
    return base.strip("_").lower() or "x"


def strip_qualifier(source: str | None) -> str:
    return (source or "").rsplit(".", 1)[-1]


_PLAIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def qident(name: str) -> str:
    """Double-quote an identifier when it is not a plain Snowflake name.

    BW InfoObject names such as 0BASE_UOM_KEY (leading digit),
    $BIC$EKF0128 or /BIC/ECCH0028 are invalid unquoted in Snowflake.
    Plain names pass through unchanged.
    """
    if _PLAIN_IDENT.match(name) or (name.startswith('"') and name.endswith('"')):
        return name
    return '"' + name.replace('"', '""') + '"'


class Generator:
    def __init__(self, ir: dict, mapping: dict[str, str]):
        self.ir = ir
        self.mapping = mapping
        self.todos: list[str] = []
        self.ds_ids = {d["id"] for d in ir["dataSources"]}
        self.nodes = {n["id"]: n for n in ir["nodes"]}
        self._unmapped_reported: set[str] = set()
        variables = {v["id"]: {"defaultValue": v.get("defaultValue"),
                               "dataType": v.get("dataType")}
                     for v in ir["variables"]}
        self.translator = Translator(variables)

    # ------------------------------------------------------------ helpers
    def todo(self, where: str, msg: str) -> None:
        self.todos.append(f"{where}: {msg}")

    def ref(self, input_id: str) -> str:
        """FROM-clause target for an input: CTE name or (mapped) table."""
        if input_id in self.nodes:
            return qident(snake(input_id))
        ds = next((d for d in self.ir["dataSources"] if d["id"] == input_id), None)
        if ds is None:
            self.todo(input_id, "unresolved input reference")
            return qident(snake(input_id))
        if input_id in self.mapping:
            return self.mapping[input_id]
        if input_id not in self._unmapped_reported:
            self._unmapped_reported.add(input_id)
            self.todo(input_id, "data source not in mapping table; "
                                "placeholder table name used")
        return qident(snake(ds.get("resourceUri") or input_id))

    def expr(self, formula: str | None, where: str) -> str:
        if not formula:
            return "null"
        before = len(self.translator.todos)
        sql = self.translator.translate(formula)
        for t in self.translator.todos[before:]:
            self.todo(where, t)
        return sql

    @staticmethod
    def find_mapping(node: dict, input_id: str, target: str):
        for inp in node["inputs"]:
            if inp["node"] == input_id:
                for m in inp["mappings"]:
                    if m["target"] == target:
                        return m
        return None

    def select_items(self, node: dict, aliases: dict[str, str],
                     skip: set[str] | None = None) -> list[str]:
        """viewAttributes (+ calculated) as SELECT items for one node."""
        skip = skip or set()
        calc = {c["id"]: c for c in node["calculatedAttributes"]}
        items: list[str] = []
        seen: set[str] = set()
        for va in node["viewAttributes"]:
            col = va["id"]
            if col in skip:
                continue
            seen.add(col)
            if col in calc:
                items.append(f"{self.expr(calc[col]['formula'], node['id'] + '.' + col)} as {qident(col)}")
                continue
            hit = None
            for inp in node["inputs"]:
                m = self.find_mapping(node, inp["node"], col)
                if m:
                    hit = (inp["node"], m)
                    break
            if hit is None:
                self.todo(node["id"], f"output column {col} has no input mapping")
                items.append(f"null as {qident(col)}")
            elif hit[1]["mappingType"] == "ConstantMapping":
                items.append(f"{self._literal(hit[1].get('constantValue'))} as {qident(col)}")
            else:
                items.append(f"{aliases[hit[0]]}.{qident(strip_qualifier(hit[1]['source']))} as {qident(col)}")
        for c in node["calculatedAttributes"]:
            if c["id"] not in seen and c["id"] not in skip:
                items.append(f"{self.expr(c['formula'], node['id'] + '.' + c['id'])} as {qident(c['id'])}")
        return items

    @staticmethod
    def _literal(value: str | None) -> str:
        if value is None:
            return "null"
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            return value
        return "'" + value.replace("'", "''") + "'"

    # -------------------------------------------------------------- nodes
    def node_sql(self, node: dict) -> str:
        handler = {
            "ProjectionView": self._projection,
            "JoinView": self._join,
            "AggregationView": self._aggregation,
            "UnionView": self._union,
            "RankView": self._rank,
        }.get(node["type"])
        if handler is None:
            self.todo(node["id"], f"node type {node['type']} not supported by the converter")
            return "select null as unsupported_node /* TODO */"
        return handler(node)

    def _single_from(self, node: dict):
        inp = node["inputs"][0]
        alias = qident(snake(inp["node"]))
        return inp, alias, f"{self.ref(inp['node'])} as {alias}"

    def _projection(self, node: dict) -> str:
        if len(node["inputs"]) != 1:
            self.todo(node["id"], "projection with != 1 input")
        inp, alias, from_clause = self._single_from(node)
        items = self.select_items(node, {inp["node"]: alias})
        sql = "select\n    " + ",\n    ".join(items) + f"\nfrom {from_clause}"
        if node["filter"]:
            sql += "\nwhere " + self.expr(node["filter"]["expression"],
                                          node["id"] + ".filter")
        return sql

    def _join(self, node: dict) -> str:
        if len(node["inputs"]) != 2:
            self.todo(node["id"], "join with != 2 inputs")
            return self._projection(node)
        left, right = node["inputs"][0], node["inputs"][1]
        la, ra = snake(left["node"]), snake(right["node"])
        if la == ra:
            la, ra = la + "_l", ra + "_r"
        la, ra = qident(la), qident(ra)
        aliases = {left["node"]: la, right["node"]: ra}
        items = self.select_items(node, aliases)
        join = node["join"] or {}
        jt_raw = (join.get("joinType") or "inner").replace(" ", "")
        jt = JOIN_TYPES.get(jt_raw.lower())
        if jt is None:
            self.todo(node["id"], f"unknown joinType {jt_raw}; inner join used")
            jt = "inner join"
        if jt_raw.lower() in ("text", "referential"):
            self.todo(node["id"], f"{jt_raw} join approximated as left join; "
                                  "language/referential semantics are manual (spec 5.2)")
        conds = []
        for ja in join.get("joinAttributes") or []:
            lm = self.find_mapping(node, left["node"], ja)
            rm = self.find_mapping(node, right["node"], ja)
            lcol = strip_qualifier(lm["source"]) if lm else ja
            rcol = strip_qualifier(rm["source"]) if rm else ja
            conds.append(f"{la}.{qident(lcol)} = {ra}.{qident(rcol)}")
        if not conds:
            self.todo(node["id"], "no joinAttributes found")
            conds = ["true /* TODO: join condition */"]
        sql = ("select\n    " + ",\n    ".join(items)
               + f"\nfrom {self.ref(left['node'])} as {la}"
               + f"\n{jt} {self.ref(right['node'])} as {ra}"
               + "\n  on " + "\n and ".join(conds))
        if node["filter"]:
            self.todo(node["id"], "filter on join node placed as WHERE; "
                                  "verify ON vs WHERE intent (spec 5.2)")
            sql += "\nwhere " + self.expr(node["filter"]["expression"],
                                          node["id"] + ".filter")
        return sql

    def _aggregation(self, node: dict) -> str:
        inp, alias, from_clause = self._single_from(node)
        measures = {}
        if node["aggregation"]:
            measures = {m["column"]: m["aggregationType"]
                        for m in node["aggregation"]["measures"]}
        calc_ids = {c["id"] for c in node["calculatedAttributes"]}
        group_cols, agg_items = [], []
        for va in node["viewAttributes"]:
            col = va["id"]
            if col in calc_ids:
                continue
            m = self.find_mapping(node, inp["node"], col)
            src = strip_qualifier(m["source"]) if m else col
            if col in measures:
                fn = (measures[col] or "sum").lower()
                if fn not in AGG_FUNCS:
                    self.todo(node["id"], f"aggregationType {fn} not supported; emitted as-is")
                agg_items.append(f"{fn}({alias}.{qident(src)}) as {qident(col)}")
            else:
                group_cols.append(col)
                agg_items.append(f"{alias}.{qident(src)} as {qident(col)}")
        inner = ("select\n    " + ",\n    ".join(agg_items)
                 + f"\nfrom {from_clause}")
        if group_cols:
            inner += "\ngroup by " + ", ".join(qident(c) for c in group_cols)
        if node["filter"]:
            inner += "\nwhere " + self.expr(node["filter"]["expression"],
                                            node["id"] + ".filter")
        if not calc_ids:
            return inner
        outer_items = [qident(c) for c in group_cols] + [qident(m) for m in measures]
        for c in node["calculatedAttributes"]:
            outer_items.append(
                f"{self.expr(c['formula'], node['id'] + '.' + c['id'])} as {qident(c['id'])}")
        return ("select\n    " + ",\n    ".join(outer_items)
                + f"\nfrom (\n{inner}\n) as {snake(node['id'])}_inner")

    def _union(self, node: dict) -> str:
        targets = [va["id"] for va in node["viewAttributes"]]
        branches = []
        for inp in node["inputs"]:
            items = []
            for col in targets:
                m = self.find_mapping(node, inp["node"], col)
                if m is None:
                    self.todo(node["id"], f"column {col} missing in union input "
                                          f"{inp['node']}; cast(null) used, type unverified")
                    items.append(f"cast(null as varchar) as {qident(col)}")
                elif m["mappingType"] == "ConstantMapping":
                    items.append(f"{self._literal(m.get('constantValue'))} as {qident(col)}")
                else:
                    items.append(f"{qident(strip_qualifier(m['source']))} as {qident(col)}")
            branches.append("select\n    " + ",\n    ".join(items)
                            + f"\nfrom {self.ref(inp['node'])}")
        sql = "\nunion all\n".join(branches)
        if node["calculatedAttributes"]:
            outer = [qident(t) for t in targets]
            for c in node["calculatedAttributes"]:
                outer.append(f"{self.expr(c['formula'], node['id'] + '.' + c['id'])} as {qident(c['id'])}")
            sql = ("select\n    " + ",\n    ".join(outer)
                   + f"\nfrom (\n{sql}\n) as union_inner")
        return sql

    def _rank(self, node: dict) -> str:
        inp, alias, from_clause = self._single_from(node)
        items = self.select_items(node, {inp["node"]: alias})
        rank = node["rank"] or {}
        part = (rank.get("rankPartition") or {}).get("attribute")
        order = rank.get("rankOrder") or {}
        ocol, odir = order.get("attribute"), order.get("direction", "descending")
        threshold = (node["extras"].get("@rankThreshold")
                     or (rank.get("rankThreshold") if isinstance(rank.get("rankThreshold"), str) else None))
        if not ocol:
            self.todo(node["id"], "rank order attribute not found; row_number not applied")
            return ("select\n    " + ",\n    ".join(items) + f"\nfrom {from_clause}")
        over = "row_number() over ("
        if part:
            over += f"partition by {qident(part)} "
        over += f"order by {qident(ocol)} {'desc' if odir == 'descending' else 'asc'})"
        cols = [qident(va["id"]) for va in node["viewAttributes"]]
        inner = ("select\n    " + ",\n    ".join(items)
                 + f",\n    {over} as __rn\nfrom {from_clause}")
        sql = ("select\n    " + ",\n    ".join(cols)
               + f"\nfrom (\n{inner}\n) as ranked")
        if threshold:
            sql += f"\nwhere __rn <= {threshold}"
        else:
            self.todo(node["id"], "rank threshold not found; no __rn filter applied")
        return sql

    # -------------------------------------------------------------- final
    def topo_order(self) -> list[dict]:
        order, visited = [], set()

        def visit(nid: str):
            if nid in visited or nid not in self.nodes:
                return
            visited.add(nid)
            for inp in self.nodes[nid]["inputs"]:
                visit(inp["node"])
            order.append(self.nodes[nid])

        for nid in self.nodes:
            visit(nid)
        return order

    def final_select(self) -> str:
        lm = self.ir["logicalModel"]
        if not lm:
            self.todo("logicalModel", "missing; final select is select * from top node")
            top = self.topo_order()[-1]["id"]
            return f"select *\nfrom {snake(top)}"
        top_ref = self.ref(lm["topNode"])
        items: list[str] = []

        def by_order(entries):
            def key(e):
                try:
                    return int(e.get("order") or 0)
                except ValueError:
                    return 0
            return sorted(entries, key=key)

        for a in by_order(lm["attributes"]):
            col = (a.get("keyMapping") or {}).get("columnName") or a["id"]
            items.append(f"{qident(col)} as {qident(a['id'])}" if col != a["id"] else qident(col))
        for c in by_order(lm["calculatedAttributes"]):
            items.append(f"{self.expr(c['formula'], 'logicalModel.' + c['id'])} as {qident(c['id'])}")
        for m in by_order(lm["baseMeasures"]):
            # spec 5.6: aggregationType is a BI-consumption default,
            # NOT an instruction to re-aggregate here.
            col = (m.get("measureMapping") or {}).get("columnName") or m["id"]
            items.append(f"{qident(col)} as {qident(m['id'])}" if col != m["id"] else qident(col))
        for c in by_order(lm["calculatedMeasures"]):
            items.append(f"{self.expr(c['formula'], 'logicalModel.' + c['id'])} as {qident(c['id'])}")
        for r in lm["restrictedMeasures"]:
            self.todo("logicalModel", f"restricted measure {r['id']} not generated (spec 9)")
        return "select\n    " + ",\n    ".join(items) + f"\nfrom {top_ref}"

    def generate(self) -> tuple[str, list[str]]:
        ctes = []
        for node in self.topo_order():
            body = self.node_sql(node)
            indented = "\n".join("    " + line if line else line
                                 for line in body.splitlines())
            ctes.append(f"-- node: {node['id']} ({node['type']})\n"
                        f"{qident(snake(node['id']))} as (\n{indented}\n)")
        final = self.final_select()
        sc = self.ir["scenario"]
        header = [
            f"-- {slugify(sc['id'] or 'view')}.sql",
            f"-- generated by sap-calcview-to-dbt from {self.ir['sourceFile']}",
            f"-- view: {sc['id']} (scenario type: {sc['type']})",
            "-- do not edit by hand; fix the converter or the mapping table instead",
        ]
        if self.todos:
            header.append(f"-- TODO items: {len(self.todos)}")
            header += [f"-- TODO {t}" for t in self.todos]
        if ctes:
            sql = ("\n".join(header) + "\n\nwith\n"
                   + ",\n\n".join(ctes) + "\n\n" + final + "\n")
        else:
            # node-less view (logicalModel sits directly on a data source)
            sql = "\n".join(header) + "\n\n" + final + "\n"
        return sql, self.todos


def load_mapping(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    out = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            parts = [row.get("sf_database", ""), row.get("sf_schema", ""),
                     row.get("sf_table", "")]
            qualified = ".".join(p for p in parts if p)
            if row.get("datasource_id") and qualified:
                out[row["datasource_id"]] = qualified
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("input", help="calculation view XML file, or a directory "
                                  "of them (searched recursively)")
    ap.add_argument("--out", default="output_sql",
                    help="output root; one .sql file is written per view")
    ap.add_argument("--mapping",
                    help="mapping CSV with columns datasource_id and "
                         "sf_database, sf_schema, sf_table; without it, "
                         "placeholder table names are used and flagged TODO")
    args = ap.parse_args()
    src = Path(args.input)
    files: list[Path] = []
    if src.is_dir():
        for pat in ("*.hdbcalculationview", "*.calculationview", "*.xml"):
            files.extend(sorted(src.rglob(pat)))
    else:
        files = [src]
    if not files:
        print("no input files found", file=sys.stderr)
        return 1
    mapping = load_mapping(Path(args.mapping) if args.mapping else None)
    for f in files:
        ir = parse(f)
        gen = Generator(ir, mapping)
        sql, todos = gen.generate()
        slug = slugify(ir["scenario"]["id"] or f.stem)
        out = Path(args.out) / slug
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{slug}.sql").write_text(sql, encoding="utf-8")
        print(json.dumps({"view": ir["scenario"]["id"], "slug": slug,
                          "nodes": len(ir["nodes"]), "todoItems": len(todos)},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
