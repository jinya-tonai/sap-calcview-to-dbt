#!/usr/bin/env python3
"""MVP-1: SAP HANA Graphical Calculation View XML -> IR + inventory.

Implements sections 2-3 of hanacv2sql-reproduction-spec.md:
parse calculation.xml (.calculationview / .hdbcalculationview) into an
intermediate representation (ir.json) and inventory files
(nodes / datasources / lineage / expressions / variables CSVs,
unsupported.md, summary.md). No SQL generation yet (that is MVP-2).

Design rules (from the spec):
- Judge elements by LOCAL NAME only (namespace URIs vary by generation).
- Never drop anything silently: unknown elements/attributes are captured
  in "extras", unresolved references land in unsupported.md.
Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

KNOWN_NODE_TYPES = {
    "ProjectionView", "JoinView", "AggregationView",
    "UnionView", "RankView", "SqlScriptView",
}

# Function dictionary keys from spec section 7.3 (+ common SQL functions).
KNOWN_FUNCTIONS = {
    "if", "isnull", "string", "int", "float", "date", "longdate", "time",
    "daysbetween", "secondsbetween", "adddays", "addseconds", "addmonths",
    "now", "leftstr", "rightstr", "midstr", "strlen", "length", "instr",
    "concat", "replace", "upper", "lower", "trim", "ltrim", "rtrim",
    "round", "rounddown", "abs", "sign", "mod", "component", "weekday",
    "case", "cast", "coalesce", "nullif", "count", "sum", "min", "max",
    "avg", "substring", "substr", "to_date", "datediff", "row_number",
    "rank", "dense_rank", "match",
}

VAR_RE = re.compile(r"\$\$([A-Za-z0-9_]+)\$\$")
FUNC_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Infix operators that FUNC_RE can mistake for function calls when a
# parenthesised operand follows (e.g. `("A"='1') and ("B"='2')` or
# `"X" in ('A','B')`). They are operators, not functions.
OPERATORS = {"and", "or", "not", "in"}


def ref_id(name: str | None) -> str | None:
    """Normalise a reference to a node or data source.

    XS classic exports (schemaVersion 2.x) prefix references with '#'
    (input node="#Join_1", mapping source="#Join_1.COL", keyMapping
    columnObjectName="#Join_1") while the definitions themselves carry
    no prefix. Strip it so references match definitions.
    """
    if name and name.startswith("#"):
        return name[1:]
    return name


def local(tag: str) -> str:
    """Element tag -> local name (strip {ns} and prefix:)."""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def xsi_type(elem: ET.Element) -> str | None:
    for k, v in elem.attrib.items():
        if local(k) == "type":
            return v.rsplit(":", 1)[-1]
    return None


def children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if local(c.tag) == name]


def first_child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if local(c.tag) == name:
            return c
    return None


def deep_text(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def find_formula(elem: ET.Element) -> str | None:
    """formula / expression text under an element, if any."""
    for name in ("formula", "expression"):
        f = first_child(elem, name)
        if f is not None and deep_text(f):
            return deep_text(f)
    return None


def slugify(text: str) -> str:
    base = text.rsplit("::", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_") or "view"


# ---------------------------------------------------------------- parse ---

def parse_scenario(root: ET.Element) -> dict:
    return {
        "id": root.attrib.get("id"),
        "type": root.attrib.get("calculationScenarioType"),
        "dataCategory": root.attrib.get("dataCategory"),
        "outputViewType": root.attrib.get("outputViewType"),
        "defaultClient": root.attrib.get("defaultClient"),
        "enforceSqlExecution": root.attrib.get("enforceSqlExecution"),
        "schemaVersion": root.attrib.get("schemaVersion"),
        "description": (
            (d.attrib.get("defaultDescription") if (d := first_child(root, "descriptions")) is not None else None)
        ),
    }


def parse_datasources(root: ET.Element) -> list[dict]:
    out = []
    container = first_child(root, "dataSources")
    if container is None:
        return out
    for ds in children(container, "DataSource"):
        uri = first_child(ds, "resourceUri")
        out.append({
            "id": ds.attrib.get("id"),
            "resourceUri": deep_text(uri) if uri is not None else None,
            "type": ds.attrib.get("type"),
            "mappedTable": None,  # resolved later via mapping sheet (spec S4)
        })
    return out


def parse_variables(root: ET.Element) -> list[dict]:
    out = []
    container = first_child(root, "localVariables")
    if container is None:
        return out
    for var in children(container, "variable"):
        props: dict = {}
        default_value = None
        for desc in var.iter():
            for k, v in desc.attrib.items():
                props.setdefault(local(k), v)
            if local(desc.tag) == "defaultValue" and deep_text(desc):
                default_value = deep_text(desc)
        out.append({
            "id": var.attrib.get("id"),
            "dataType": props.get("datatype") or props.get("dataType"),
            "parameter": props.get("parameter"),
            "mandatory": props.get("mandatory"),
            "defaultValue": default_value,
            "rawProperties": props,
            "usedIn": [],  # filled after expression scan
        })
    return out


def parse_node(node_elem: ET.Element) -> dict:
    node: dict = {
        "id": node_elem.attrib.get("id"),
        "type": xsi_type(node_elem),
        "inputs": [],
        "viewAttributes": [],
        "calculatedAttributes": [],
        "filter": None,
        "join": None,
        "aggregation": None,
        "union": None,
        "rank": None,
        "extras": {},
    }
    # element-level attributes worth keeping verbatim
    for k, v in node_elem.attrib.items():
        if local(k) not in ("id", "type"):
            node["extras"][f"@{local(k)}"] = v

    va_container = first_child(node_elem, "viewAttributes")
    if va_container is not None:
        for va in children(va_container, "viewAttribute"):
            entry = {"id": va.attrib.get("id")}
            for k, v in va.attrib.items():
                if local(k) != "id":
                    entry[local(k)] = v
            node["viewAttributes"].append(entry)
            if "aggregationType" in entry:
                node["aggregation"] = node["aggregation"] or {"measures": []}
                node["aggregation"]["measures"].append(
                    {"column": entry["id"], "aggregationType": entry["aggregationType"]})

    ca_container = first_child(node_elem, "calculatedViewAttributes")
    if ca_container is not None:
        for ca in ca_container:
            node["calculatedAttributes"].append({
                "id": ca.attrib.get("id"),
                "formula": find_formula(ca),
                "dataType": ca.attrib.get("dataType"),
            })

    for inp in children(node_elem, "input"):
        mappings = []
        for m in children(inp, "mapping"):
            mappings.append({
                "target": m.attrib.get("target"),
                "source": ref_id(m.attrib.get("source")),
                "mappingType": xsi_type(m) or "AttributeMapping",
                "constantValue": m.attrib.get("constantValue"),
            })
        node["inputs"].append({"node": ref_id(inp.attrib.get("node")),
                               "mappings": mappings})

    filt = first_child(node_elem, "filter")
    if filt is not None:
        expr = find_formula(filt) or deep_text(filt)
        if expr:
            node["filter"] = {"expression": expr}

    join_attrs = [ja.attrib.get("name") for ja in children(node_elem, "joinAttribute")]
    if node["type"] == "JoinView" or join_attrs:
        node["join"] = {
            "joinType": node_elem.attrib.get("joinType"),
            "joinAttributes": join_attrs,
        }

    if node["type"] == "UnionView":
        node["union"] = {"inputCount": len(node["inputs"])}

    # Rank details + any other unknown child elements -> extras (never drop)
    known_children = {"viewAttributes", "calculatedViewAttributes", "input",
                      "filter", "joinAttribute", "descriptions"}
    for child in node_elem:
        name = local(child.tag)
        if name in known_children:
            continue
        payload = dict(child.attrib)
        text = deep_text(child)
        if text:
            payload["text"] = text
        node["extras"][name] = payload
        if name.lower().startswith("rank"):
            node["rank"] = node["rank"] or {}
            node["rank"][name] = payload
    if node["type"] == "RankView" and node["rank"] is None:
        node["rank"] = {"note": "no rank detail elements found; see extras"}
    return node


def parse_logical_model(root: ET.Element) -> dict | None:
    lm = first_child(root, "logicalModel")
    if lm is None:
        return None
    model: dict = {
        "topNode": lm.attrib.get("id"),
        "attributes": [], "calculatedAttributes": [],
        "baseMeasures": [], "calculatedMeasures": [], "restrictedMeasures": [],
    }
    attrs = first_child(lm, "attributes")
    if attrs is not None:
        for a in children(attrs, "attribute"):
            km = first_child(a, "keyMapping")
            model["attributes"].append({
                "id": a.attrib.get("id"),
                "key": a.attrib.get("key") == "true",
                "order": a.attrib.get("order"),
                "keyMapping": ({"columnObjectName": ref_id(km.attrib.get("columnObjectName")),
                                "columnName": km.attrib.get("columnName")} if km is not None else None),
            })
    for container_name, key in (("calculatedAttributes", "calculatedAttributes"),
                                ("baseMeasures", "baseMeasures"),
                                ("calculatedMeasures", "calculatedMeasures"),
                                ("restrictedMeasures", "restrictedMeasures")):
        container = first_child(lm, container_name)
        if container is None:
            continue
        for item in container:
            entry = {"id": item.attrib.get("id"), "order": item.attrib.get("order")}
            if key in ("baseMeasures", "restrictedMeasures"):
                entry["aggregationType"] = item.attrib.get("aggregationType")
                entry["measureType"] = item.attrib.get("measureType")
                mm = first_child(item, "measureMapping")
                if mm is not None:
                    entry["measureMapping"] = {
                        "columnObjectName": ref_id(mm.attrib.get("columnObjectName")),
                        "columnName": mm.attrib.get("columnName")}
            else:
                entry["formula"] = find_formula(item)
            model[key].append(entry)
    return model


def parse(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    if local(root.tag) != "scenario":
        raise ValueError(f"{path}: root is not Calculation:scenario")
    nodes = []
    cv_container = first_child(root, "calculationViews")
    if cv_container is not None:
        for n in children(cv_container, "calculationView"):
            nodes.append(parse_node(n))
    return {
        "sourceFile": path.name,
        "scenario": parse_scenario(root),
        "dataSources": parse_datasources(root),
        "variables": parse_variables(root),
        "nodes": nodes,
        "logicalModel": parse_logical_model(root),
    }


# -------------------------------------------------------------- lineage ---

def trace_lineage(ir: dict, node_id: str, column: str,
                  seen: frozenset = frozenset()) -> list[dict]:
    """Trace one output column back to its data source. Returns chain steps."""
    node_id = ref_id(node_id) or node_id
    ds_ids = {d["id"] for d in ir["dataSources"]}
    if node_id in ds_ids:
        return [{"node": node_id, "column": column, "kind": "source"}]
    if (node_id, column) in seen:
        return [{"node": node_id, "column": column, "kind": "cycle"}]
    seen = seen | {(node_id, column)}
    node = next((n for n in ir["nodes"] if n["id"] == node_id), None)
    if node is None:
        return [{"node": node_id, "column": column, "kind": "unresolved_reference"}]
    for ca in node["calculatedAttributes"]:
        if ca["id"] == column:
            return [{"node": node_id, "column": column, "kind": "calculated",
                     "formula": ca.get("formula")}]
    for inp in node["inputs"]:
        for m in inp["mappings"]:
            if m["target"] == column:
                if m["mappingType"] == "ConstantMapping":
                    return [{"node": node_id, "column": column, "kind": "constant",
                             "constantValue": m.get("constantValue")}]
                src_col = (m["source"] or "").rsplit(".", 1)[-1]
                rest = trace_lineage(ir, inp["node"], src_col, seen)
                return [{"node": node_id, "column": column, "kind": "direct"}] + rest
    return [{"node": node_id, "column": column, "kind": "unresolved"}]


# ------------------------------------------------------------- analysis ---

def collect_expressions(ir: dict) -> list[dict]:
    rows = []
    def add(location, kind, item_id, formula):
        if not formula:
            return
        funcs = sorted({f.lower() for f in FUNC_RE.findall(formula)} - OPERATORS)
        unknown = [f for f in funcs if f not in KNOWN_FUNCTIONS]
        rows.append({
            "location": location, "kind": kind, "id": item_id,
            "formula": formula, "functions": funcs,
            "unknownFunctions": unknown,
            "variables": VAR_RE.findall(formula),
            "status": "TODO_unknown_function" if unknown else "auto_candidate",
        })
    for node in ir["nodes"]:
        for ca in node["calculatedAttributes"]:
            add(node["id"], "calculated_column", ca["id"], ca.get("formula"))
        if node["filter"]:
            add(node["id"], "filter", "-", node["filter"]["expression"])
    lm = ir["logicalModel"] or {}
    for ca in lm.get("calculatedAttributes", []):
        add("logicalModel", "calculated_attribute", ca["id"], ca.get("formula"))
    for cm in lm.get("calculatedMeasures", []):
        add("logicalModel", "calculated_measure", cm["id"], cm.get("formula"))
    return rows


def detect_unsupported(ir: dict, root_text_names: set[str],
                       expressions: list[dict]) -> list[dict]:
    items = []
    sc = ir["scenario"]
    if sc["type"] == "SCRIPT_BASED":
        items.append({"item": "SCRIPT_BASED scenario", "where": sc["id"] or "-",
                      "reason": "SQLScript body; node decomposition not applicable (spec 5.7)"})
    for node in ir["nodes"]:
        if node["type"] == "SqlScriptView":
            items.append({"item": "SqlScriptView node", "where": node["id"],
                          "reason": "Column-level lineage/conversion out of scope (spec 5.7)"})
        elif node["type"] not in KNOWN_NODE_TYPES:
            items.append({"item": f"Unknown node type: {node['type']}", "where": node["id"],
                          "reason": "Not in the supported node set; captured in IR extras"})
        if node["join"] and (node["join"].get("joinType") or "") in ("text", "temporal"):
            items.append({"item": f"{node['join']['joinType']} join", "where": node["id"],
                          "reason": "Language/validity semantics are case-specific (spec 9)"})
    lm = ir["logicalModel"] or {}
    for rm in lm.get("restrictedMeasures", []):
        items.append({"item": "Restricted measure", "where": f"logicalModel.{rm['id']}",
                      "reason": "Filtered measure; manual implementation (spec 9)"})
    if any("hierarch" in n.lower() for n in root_text_names):
        items.append({"item": "Hierarchy elements", "where": "scenario",
                      "reason": "Needs recursive/hierarchy design on Snowflake side (spec 9)"})
    if any("currenc" in n.lower() for n in root_text_names):
        items.append({"item": "Currency conversion elements", "where": "scenario",
                      "reason": "Needs rate-table model (spec 9)"})
    for e in expressions:
        for f in e["unknownFunctions"]:
            items.append({"item": f"Unknown function: {f}()", "where": f"{e['location']}.{e['id']}",
                          "reason": "Not in the spec 7.3 function dictionary yet"})
    return items


def all_element_names(path: Path) -> set[str]:
    tree = ET.parse(path)
    return {local(e.tag) for e in tree.getroot().iter()}


# --------------------------------------------------------------- output ---

def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def process(path: Path, out_root: Path) -> dict:
    ir = parse(path)
    expressions = collect_expressions(ir)
    unsupported = detect_unsupported(ir, all_element_names(path), expressions)

    # variable usage back-fill
    for var in ir["variables"]:
        var["usedIn"] = [f"{e['location']}.{e['id']}" for e in expressions
                         if var["id"] in e["variables"]]

    slug = slugify(ir["scenario"]["id"] or path.stem)
    out = out_root / slug
    inv = out / "inventory"
    inv.mkdir(parents=True, exist_ok=True)
    (out / "ir.json").write_text(json.dumps(ir, indent=2, ensure_ascii=False),
                                 encoding="utf-8")

    write_csv(inv / "nodes.csv",
              ["node_id", "type", "inputs", "output_columns", "has_filter",
               "calculated_columns", "join_type", "join_attributes"],
              [{"node_id": n["id"], "type": n["type"],
                "inputs": "|".join(i["node"] or "" for i in n["inputs"]),
                "output_columns": len(n["viewAttributes"]),
                "has_filter": bool(n["filter"]),
                "calculated_columns": len(n["calculatedAttributes"]),
                "join_type": (n["join"] or {}).get("joinType") or "",
                "join_attributes": "|".join((n["join"] or {}).get("joinAttributes") or [])}
               for n in ir["nodes"]])

    write_csv(inv / "datasources.csv",
              ["datasource_id", "resource_uri", "type", "mapped_table"],
              [{"datasource_id": d["id"], "resource_uri": d["resourceUri"],
                "type": d["type"] or "", "mapped_table": d["mappedTable"] or ""}
               for d in ir["dataSources"]])

    lineage_rows = []
    lm = ir["logicalModel"] or {}
    targets = ([(a["id"], "attribute", (a.get("keyMapping") or {})) for a in lm.get("attributes", [])]
               + [(m["id"], "measure", (m.get("measureMapping") or {})) for m in lm.get("baseMeasures", [])]
               + [(c["id"], "calculated_attribute", None) for c in lm.get("calculatedAttributes", [])])
    for out_col, kind, mapping in targets:
        if mapping is None:
            lineage_rows.append({"output_column": out_col, "kind": kind,
                                 "chain": "logicalModel (calculated)",
                                 "origin_datasource": "", "origin_column": "",
                                 "terminal_kind": "calculated"})
            continue
        chain = trace_lineage(ir, mapping.get("columnObjectName"),
                              mapping.get("columnName"))
        terminal = chain[-1] if chain else {}
        lineage_rows.append({
            "output_column": out_col, "kind": kind,
            "chain": " -> ".join(f"{s['node']}.{s['column']}" for s in chain),
            "origin_datasource": terminal.get("node", "") if terminal.get("kind") == "source" else "",
            "origin_column": terminal.get("column", "") if terminal.get("kind") == "source" else "",
            "terminal_kind": terminal.get("kind", ""),
        })
    write_csv(inv / "lineage.csv",
              ["output_column", "kind", "chain", "origin_datasource",
               "origin_column", "terminal_kind"], lineage_rows)

    write_csv(inv / "expressions.csv",
              ["location", "kind", "id", "formula", "functions",
               "unknown_functions", "variables", "status"],
              [{"location": e["location"], "kind": e["kind"], "id": e["id"],
                "formula": e["formula"], "functions": "|".join(e["functions"]),
                "unknown_functions": "|".join(e["unknownFunctions"]),
                "variables": "|".join(e["variables"]), "status": e["status"]}
               for e in expressions])

    write_csv(inv / "variables.csv",
              ["variable_id", "data_type", "parameter", "mandatory",
               "default_value", "used_in"],
              [{"variable_id": v["id"], "data_type": v["dataType"] or "",
                "parameter": v["parameter"] or "", "mandatory": v["mandatory"] or "",
                "default_value": v["defaultValue"] or "",
                "used_in": "|".join(v["usedIn"])} for v in ir["variables"]])

    with (inv / "unsupported.md").open("w", encoding="utf-8") as fh:
        fh.write(f"# Unsupported / manual items — {ir['scenario']['id']}\n\n")
        if not unsupported:
            fh.write("None detected by the MVP-1 checks.\n")
        for u in unsupported:
            fh.write(f"- **{u['item']}** ({u['where']}): {u['reason']}\n")

    resolved = sum(1 for r in lineage_rows if r["terminal_kind"] == "source")
    auto = sum(1 for e in expressions if e["status"] == "auto_candidate")
    summary = {
        "view": ir["scenario"]["id"], "slug": slug,
        "scenarioType": ir["scenario"]["type"],
        "nodes": len(ir["nodes"]),
        "nodeTypes": sorted({n["type"] for n in ir["nodes"]}),
        "dataSources": len(ir["dataSources"]),
        "outputColumns": len(lineage_rows),
        "lineageResolvedToSource": f"{resolved}/{len(lineage_rows)}",
        "expressions": len(expressions),
        "expressionsAutoCandidate": f"{auto}/{len(expressions)}",
        "unsupportedItems": len(unsupported),
    }
    with (out / "summary.md").open("w", encoding="utf-8") as fh:
        fh.write(f"# Summary — {summary['view']}\n\n")
        for k, v in summary.items():
            fh.write(f"- {k}: {v}\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="calculation view XML file or directory of them")
    ap.add_argument("--out", default="output", help="output root directory")
    args = ap.parse_args()
    src = Path(args.input)
    files: list[Path] = []
    if src.is_dir():
        for pat in ("*.hdbcalculationview", "*.calculationview", "*.xml"):
            files.extend(sorted(src.glob(pat)))
    else:
        files = [src]
    if not files:
        print("no input files found", file=sys.stderr)
        return 1
    for f in files:
        s = process(f, Path(args.out))
        print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
