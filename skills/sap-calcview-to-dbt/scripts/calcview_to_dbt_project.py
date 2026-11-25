#!/usr/bin/env python3
"""MVP-3: Calculation View IRs -> dbt project (spec section 10).

Generates a dbt project skeleton from one or more calculation view XMLs:

  models/
    _sources.yml                 landed tables (from the mapping table)
    staging/stg_<table>.sql      one passthrough model per landed table
    intermediate/int_<cv>__<node>.sql
                                 nodes split out of large views
                                 (project profile layer_split rules)
    marts/<cv>.sql               final model per view (logicalModel)
    marts/_marts.yml             descriptions + key tests

Layer names, prefixes, the landing database/schema and the split rules
come from project_profile.yaml (see references/project-profile-template
.yaml). Without a profile, the template's dummy defaults are used and
the run reports that. Table/view wiring comes from the mapping CSV
(same file as MVP-2; the optional `kind` column distinguishes landed
tables from in-corpus view references).

Usage:
  python3 calcview_to_dbt_project.py <xml-or-dir> --out dbt_project \
      [--mapping mapping.csv] [--profile project_profile.yaml]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calcview_parser import parse, slugify, process as write_inventory  # noqa: E402
from calcview_to_sql import Generator, snake, qident  # noqa: E402

DEFAULT_PROFILE = {
    "outputs": {"parser_out": "output", "sql_out": "output_sql",
                "dbt_out": "dbt_project", "inventory_out": "inventory"},
    "landing": {"database": "RAW", "raw_schema": "SAP", "table_naming": "same"},
    "view_references": {"connect_to": "models"},
    "dbt_layers": {
        "staging": {"dir": "staging", "prefix": "stg_"},
        "intermediate": {"dir": "intermediate", "prefix": "int_"},
        "marts": {"dir": "marts", "prefix": ""},
        "sources_yml": "models/_sources.yml",
    },
    "layer_split": {"int_when": ["node_count > 10",
                                 "shared_by_multiple_views"],
                    "mart_is": "final_node"},
    "conventions": {"client_filter": "", "currency_uom": ""},
}


# ------------------------------------------------------------ profile I/O
def _scalar(text: str):
    text = text.strip()
    if text.startswith("{"):
        text = text[:text.index("}") + 1]
    elif text.startswith(("\"", "'")):
        end = text.find(text[0], 1)
        if end > 0:
            text = text[:end + 1]
    elif " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    if text.startswith("{") and text.endswith("}"):
        out = {}
        inner = text[1:-1].strip()
        if inner:
            for part in inner.split(","):
                k, _, v = part.partition(":")
                out[k.strip()] = _scalar(v)
        return out
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def load_profile(path: Path | None) -> dict:
    """Minimal reader for the documented profile shape (no PyYAML need)."""
    prof = json.loads(json.dumps(DEFAULT_PROFILE))
    if path is None:
        return prof
    stack: list[tuple[int, dict]] = [(-1, prof)]
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, rest = raw.strip().partition(":")
        key = key.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        rest = rest.strip()
        if rest.startswith("["):
            while not rest.endswith("]") and i < len(lines):
                rest += " " + lines[i].strip()
                i += 1
            parent[key] = _scalar(rest)
        elif rest:
            parent[key] = _scalar(rest)
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return prof


def int_threshold(profile: dict) -> int | None:
    for rule in profile.get("layer_split", {}).get("int_when", []):
        m = re.search(r"node_count\s*>\s*(\d+)", str(rule))
        if m:
            return int(m.group(1))
    return None


# ------------------------------------------------------------- dbt wiring
class DbtGenerator(Generator):
    """Generator whose FROM targets are dbt ref() calls.

    external: node ids rendered as separate intermediate models.
    wiring: datasource id -> ("stg", model_name) | ("model", model_name)
            | ("placeholder", qualified_name)
    """

    def __init__(self, ir, wiring, external, prefixes):
        super().__init__(ir, {})
        self.wiring = wiring
        self.external = external
        self.prefixes = prefixes

    def ref(self, input_id: str) -> str:
        if input_id in self.nodes:
            if input_id in self.external:
                name = (self.prefixes["int"]
                        + snake(self.ir["scenario"]["id"])
                        + "__" + snake(input_id))
                return "{{ ref('" + name + "') }}"
            return qident(snake(input_id))
        kind = self.wiring.get(input_id)
        if kind is None:
            self.todo(input_id, "data source not in mapping table; "
                                "placeholder table name used")
            ds = next((d for d in self.ir["dataSources"]
                       if d["id"] == input_id), None)
            return qident(snake((ds or {}).get("resourceUri") or input_id))
        sort, target = kind
        if sort in ("stg", "model"):
            return "{{ ref('" + target + "') }}"
        return target  # pre-quoted placeholder

    def header(self, model_name: str, extra: str = "") -> list[str]:
        sc = self.ir["scenario"]
        lines = [
            f"-- {model_name}.sql",
            f"-- generated by sap-calcview-to-dbt MVP-3 from {self.ir['sourceFile']}",
            f"-- view: {sc['id']} (scenario type: {sc['type']})",
            "-- do not edit by hand; fix the converter, the mapping "
            "table or the project profile instead",
        ]
        if extra:
            lines.append(extra)
        return lines

    def mart_sql(self, model_name: str) -> tuple[str, list[str]]:
        self.todos = []
        final = self.final_select()
        head = self.header(model_name)
        if self.todos:
            head.append(f"-- TODO items: {len(self.todos)}")
            head += [f"-- TODO {t}" for t in self.todos]
        if self.external or not self.nodes:
            return "\n".join(head) + "\n\n" + final + "\n", self.todos
        ctes = []
        for node in self.topo_order():
            body = self.node_sql(node)
            indented = "\n".join("    " + line if line else line
                                 for line in body.splitlines())
            ctes.append(f"-- node: {node['id']} ({node['type']})\n"
                        f"{qident(snake(node['id']))} as (\n{indented}\n)")
        head = self.header(model_name)
        if self.todos:
            head.append(f"-- TODO items: {len(self.todos)}")
            head += [f"-- TODO {t}" for t in self.todos]
        return ("\n".join(head) + "\n\nwith\n" + ",\n\n".join(ctes)
                + "\n\n" + final + "\n"), self.todos

    def int_sql(self, node: dict, model_name: str) -> tuple[str, list[str]]:
        self.todos = []
        body = self.node_sql(node)
        head = self.header(model_name,
                           f"-- intermediate model for node {node['id']} "
                           f"({node['type']})")
        if self.todos:
            head.append(f"-- TODO items: {len(self.todos)}")
            head += [f"-- TODO {t}" for t in self.todos]
        return "\n".join(head) + "\n\n" + body + "\n", self.todos


# ---------------------------------------------------------------- helpers
def load_mapping_rows(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    rows = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("datasource_id"):
                rows[row["datasource_id"]] = row
    return rows


def unquote(name: str) -> str:
    name = (name or "").strip()
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name


def yaml_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def build_project(files: list[Path], out: Path, mapping_rows: dict,
                  profile: dict, profile_given: bool) -> dict:
    layers = profile["dbt_layers"]
    prefixes = {"stg": layers["staging"]["prefix"],
                "int": layers["intermediate"]["prefix"],
                "mart": layers["marts"]["prefix"]}
    landing = profile["landing"]
    threshold = int_threshold(profile)
    connect_models = (profile.get("view_references", {})
                      .get("connect_to", "models") == "models")

    irs = [parse(f) for f in files]
    view_ids = {ir["scenario"]["id"] for ir in irs}
    mart_name = {vid: prefixes["mart"] + snake(vid) for vid in view_ids}

    # staging models: one per distinct landed table
    stg_for_table: dict[str, str] = {}
    for row in mapping_rows.values():
        table = unquote(row.get("sf_table", ""))
        if not table or row.get("kind") == "view_in_corpus":
            continue
        name = prefixes["stg"] + snake(table)
        if name in stg_for_table and stg_for_table[name] != table:
            name = name + "_2"
        stg_for_table[name] = table

    # per-datasource wiring
    def wiring_for(ir) -> dict[str, tuple[str, str]]:
        wiring = {}
        for d in ir["dataSources"]:
            row = mapping_rows.get(d["id"])
            base = d["id"].split("$$$$", 1)[1].rstrip("$") \
                if "$$$$" in d["id"] else None
            if base and base not in mapping_rows and row is None:
                row = mapping_rows.get(base)
            if row is None:
                continue
            kind = row.get("kind", "")
            table = unquote(row.get("sf_table", ""))
            if kind == "view_in_corpus" and connect_models \
                    and d["id"] in mart_name:
                wiring[d["id"]] = ("model", mart_name[d["id"]])
            elif kind == "generated_from_base" and table:
                if table in view_ids and connect_models:
                    wiring[d["id"]] = ("model", mart_name[table])
                else:
                    wiring[d["id"]] = ("stg", prefixes["stg"] + snake(table))
            elif table:
                wiring[d["id"]] = ("stg", prefixes["stg"] + snake(table))
        return wiring

    models_dir = out / "models"
    stg_dir = models_dir / layers["staging"]["dir"]
    int_dir = models_dir / layers["intermediate"]["dir"]
    mart_dir = models_dir / layers["marts"]["dir"]
    for d in (stg_dir, int_dir, mart_dir):
        d.mkdir(parents=True, exist_ok=True)

    report = {"views": [], "stagingModels": len(stg_for_table),
              "profileUsed": profile_given}

    # sources.yml
    src_rel = Path(layers["sources_yml"])
    src_path = out / src_rel
    src_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version: 2", "", "sources:",
             "  - name: sap_raw",
             f"    database: {landing.get('database', '')}",
             f"    schema: {landing.get('raw_schema', '')}",
             "    tables:"]
    for name in sorted(stg_for_table):
        table = stg_for_table[name]
        lines.append(f"      - name: {yaml_quote(table)}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            quoted = '"' + table.replace('"', '""') + '"'
            lines.append(f"        identifier: {yaml_quote(quoted)}")
    src_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # staging models
    for name, table in sorted(stg_for_table.items()):
        body = (f"-- {name}.sql\n"
                "-- generated by sap-calcview-to-dbt MVP-3 (staging: "
                "passthrough;\n-- renames / type fixes / client filter "
                "belong here, spec 10.1)\n\n"
                "select *\nfrom {{ source('sap_raw', "
                + yaml_quote(table) + ") }}\n")
        (stg_dir / f"{name}.sql").write_text(body, encoding="utf-8")

    # marts + intermediates
    marts_yml = ["version: 2", "", "models:"]
    total_int = 0
    issues_by_view: dict[str, list[tuple[str, str]]] = {}
    for ir in irs:
        sc = ir["scenario"]
        vid = sc["id"]
        name = mart_name[vid]
        split = threshold is not None and len(ir["nodes"]) > threshold \
            and sc["type"] != "SCRIPT_BASED"
        external = {n["id"] for n in ir["nodes"]} if split else set()
        gen = DbtGenerator(ir, wiring_for(ir), external, prefixes)
        sql, todos = gen.mart_sql(name)
        (mart_dir / f"{name}.sql").write_text(sql, encoding="utf-8")
        int_models = []
        if split:
            for node in ir["nodes"]:
                iname = (prefixes["int"] + snake(vid) + "__"
                         + snake(node["id"]))
                isql, itodos = gen.int_sql(node, iname)
                (int_dir / f"{iname}.sql").write_text(isql, encoding="utf-8")
                int_models.append(iname)
                todos = todos + [f"{iname}: {t}" for t in itodos]
            total_int += len(int_models)
        report["views"].append({"view": vid, "model": name,
                                "nodes": len(ir["nodes"]),
                                "splitToInt": len(int_models),
                                "todoItems": len(todos)})
        issues_by_view[vid] = [(categorize(t), t) for t in todos]
        # marts yml entry with key tests
        lm = ir["logicalModel"] or {}
        keys = [a["id"] for a in lm.get("attributes", []) if a.get("key")]
        desc = (sc.get("description") or vid).replace("\n", " ")
        marts_yml.append(f"  - name: {name}")
        marts_yml.append(f"    description: {yaml_quote(desc)}")
        if len(keys) == 1:
            marts_yml += ["    columns:",
                          f"      - name: {yaml_quote(keys[0])}",
                          "        tests:", "          - unique",
                          "          - not_null"]
        elif len(keys) > 1:
            marts_yml.append(
                f"    # composite key {keys}: add a "
                "unique_combination_of_columns test manually")
    (mart_dir / "_marts.yml").write_text("\n".join(marts_yml) + "\n",
                                         encoding="utf-8")

    # dbt_project.yml
    proj = ["name: 'sap_calcview_repro'", "version: '1.0.0'",
            "config-version: 2", "",
            "profile: 'sap_calcview_repro'", "",
            "model-paths: ['models']", "",
            "models:",
            "  sap_calcview_repro:",
            f"    {layers['staging']['dir']}:",
            "      +materialized: view",
            f"    {layers['intermediate']['dir']}:",
            "      +materialized: view",
            f"    {layers['marts']['dir']}:",
            "      +materialized: table", ""]
    (out / "dbt_project.yml").write_text("\n".join(proj), encoding="utf-8")
    report["intermediateModels"] = total_int
    report["totalTodos"] = sum(v["todoItems"] for v in report["views"])

    # The inventory is a shared team artifact: embed it in the project
    # so conversion decisions (and their corrections) are visible to
    # everyone working on the project, not just whoever ran the parser.
    inv_rel = profile.get("outputs", {}).get("inventory_out", "inventory")
    for f in files:
        write_inventory(f, out / inv_rel)
    report["inventoryDir"] = inv_rel
    report["inventoryViews"] = len(files)

    # ISSUES.md: one readable, checkbox-style backlog for the team.
    # Merges the generator TODOs with each view's unsupported.md items.
    for ir in irs:
        vid = ir["scenario"]["id"]
        upath = (out / inv_rel / slugify(vid) / "inventory"
                 / "unsupported.md")
        if not upath.exists():
            continue
        labels = []
        existing = issues_by_view.setdefault(vid, [])
        for line in upath.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- \*\*(.+?)\*\*", line)
            if not m:
                continue
            label = m.group(1)
            low = label.lower()
            # skip items the generator TODOs already cover per expression
            if low.startswith("unknown function"):
                continue
            if ("script_based" in low or "sqlscriptview" in low) and \
                    any(c == "SCRIPT_BASED" for c, _ in existing):
                continue
            labels.append(label)
            existing.append((label, line[2:].strip()))
        if any("Restricted measure" in lb for lb in labels):
            # the same item also appears as a generator TODO; keep the
            # unsupported.md version (it carries the fuller description)
            issues_by_view[vid] = [
                (c, t) for c, t in issues_by_view[vid]
                if not (c == "制限メジャー"
                        and "restricted measure" in t.lower())]
    total_issues = sum(len(v) for v in issues_by_view.values())
    (out / "ISSUES.md").write_text(
        render_issues(issues_by_view, len(irs)), encoding="utf-8")
    report["issuesFile"] = "ISSUES.md"
    report["issuesTotal"] = total_issues
    return report


def categorize(todo: str) -> str:
    t = todo.lower()
    if "not in mapping table" in t:
        return "マッピング未対応"
    if "unconverted function" in t:
        return "未知の関数"
    if "sqlscriptview" in t or "script_based" in t:
        return "SCRIPT_BASED"
    if "restricted measure" in t:
        return "制限メジャー"
    if "approximated as left join" in t:
        return "結合セマンティクス"
    if "verify" in t or "needs confirmation" in t or "ilike" in t:
        return "要検証の変換"
    if "not supported" in t:
        return "未対応の要素"
    return "その他"


def render_issues(issues_by_view: dict, total_views: int) -> str:
    from collections import Counter
    counts = Counter(c for items in issues_by_view.values() for c, _ in items)
    total = sum(counts.values())
    with_issues = sum(1 for v in issues_by_view.values() if v)
    lines = [
        "# 課題管理",
        "",
        "生成器が自動生成した課題一覧。手で編集せず、原因側"
        "（mapping.csv / プロファイル / 変換辞書 / 変換器）を直して",
        "再生成すること。対応が終わった課題は再生成で消える。",
        "",
        "## サマリー",
        "",
        f"- 対象ビュー: {total_views}",
        f"- 課題のあるビュー: {with_issues}",
        f"- 課題総数: {total}",
        "",
        "| 区分 | 件数 |",
        "|---|---|",
    ]
    for cat, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {cat} | {n} |")
    lines += ["", "## ビュー別", ""]
    for vid in sorted(issues_by_view):
        items = issues_by_view[vid]
        if not items:
            continue
        lines.append(f"### {vid}")
        lines.append("")
        for cat, text in items:
            lines.append(f"- [ ] [{cat}] {text}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="calculation view XML file or directory")
    ap.add_argument("--out", default="dbt_project",
                    help="output project directory")
    ap.add_argument("--mapping", help="mapping CSV (spec section 4.1)")
    ap.add_argument("--profile", help="project_profile.yaml")
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
    profile = load_profile(Path(args.profile) if args.profile else None)
    report = build_project(files, Path(args.out),
                           load_mapping_rows(Path(args.mapping)
                                             if args.mapping else None),
                           profile, bool(args.profile))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
