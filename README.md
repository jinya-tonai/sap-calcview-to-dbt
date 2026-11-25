# sap-calcview-to-dbt

**English** | [日本語](README.ja.md)

A Claude Code skill that parses **SAP HANA Graphical Calculation View** XML
(`calculation.xml` / `.hdbcalculationview`, as exported from HANA Studio)
into an intermediate representation and a complete inventory — the first
step of reproducing SAP BW/HANA calculation-view logic as **dbt models on
Snowflake**.

## Why

- Converter SaaS tools exist, but uploading a client's `calculation.xml` to
  an external service is often a compliance non-starter. This skill runs
  **entirely locally** — nothing leaves your machine.
- Manual spec-writing from the HANA Studio GUI is the usual bottleneck.
  The parser generates the inventory (nodes, lineage, expressions,
  variables, unsupported items) mechanically, so humans only review.
- Anything the parser cannot handle is **never silently dropped**: it is
  reported in `unsupported.md` and preserved in the IR.

Design: [skills/sap-calcview-to-dbt/references/hanacv2sql-reproduction-spec.md](skills/sap-calcview-to-dbt/references/hanacv2sql-reproduction-spec.md)
(a reproduction spec of the HANACV2SQL conversion pipeline, adapted to a
TROCCO → Snowflake → dbt stack).

## Install

As a Claude Code plugin — this repository is its own marketplace:

```
/plugin marketplace add jinya-tonai/sap-calcview-to-dbt
/plugin install sap-calcview-to-dbt@sap-calcview-to-dbt
```

Or as a standalone skill — clone the repository and copy the skill
folder into your Claude Code skills directory:

```bash
git clone https://github.com/jinya-tonai/sap-calcview-to-dbt
cp -r sap-calcview-to-dbt/skills/sap-calcview-to-dbt ~/.claude/skills/
```

## Usage

In Claude Code, invoke the skill with a slash command and point it at
your calculation view XML:

```
/sap-calcview-to-dbt path/to/calculation.xml
```

You can also just describe the task — the skill triggers automatically
when you ask about converting a calculation view, e.g. "convert this
calculation view to Snowflake SQL" or "generate a dbt project from
these views".

Under the hood the skill runs three Python scripts (stdlib only,
Python 3.10+). They also work standalone:

Inventory:

```bash
python3 skills/sap-calcview-to-dbt/scripts/calcview_parser.py path/to/calculation.xml --out output
python3 skills/sap-calcview-to-dbt/scripts/calcview_parser.py folder/with/views/     --out output
```

SQL generation — one CTE per node, final SELECT per the
logicalModel (base measures are projected, never re-aggregated):

```bash
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_sql.py path/to/calculation.xml --out output_sql
# with a mapping table (datasource_id, sf_database, sf_schema, sf_table):
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_sql.py folder/with/views/ --out output_sql --mapping mapping.csv
```

Expressions are translated per the spec §7.3 dictionary
(`daysbetween(a, b)` → `datediff(day, a, b)`, `adddays(d, n)` →
`dateadd(day, n, d)`, `$$IP_X$$` → dbt `var('ip_x', …)`, …).
Anything unconvertible stays in the SQL as a `-- TODO` header entry —
never silently dropped. Without a mapping table, data sources use
placeholder names and are flagged as TODOs.

dbt project generation — sources, staging, intermediate and
mart models wired with `ref()` / `source()`, plus key tests derived
from `key="true"` attributes. Landing database/schema, layer names and
the intermediate-split rules come from a project profile file (copy
`skills/sap-calcview-to-dbt/references/project-profile-template.yaml` into your working folder as
`project_profile.yaml`; the shipped values are generic dummy defaults):

```bash
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_dbt_project.py folder/with/views/ --out dbt_project \
    --mapping mapping.csv --profile project_profile.yaml
```

```
dbt_project/
├─ dbt_project.yml
├─ ISSUES.md               … all TODOs / manual items as one checkbox backlog
├─ inventory/              … per-view inventory (shared with the team)
└─ models/
   ├─ _sources.yml          … landed tables (from the mapping table)
   ├─ staging/stg_*.sql     … one passthrough model per landed table
   ├─ intermediate/int_*.sql … nodes split out of large views (profile rule)
   └─ marts/<view>.sql      … final model per view, plus _marts.yml (tests)
```

Output, per view:

```
output/<VIEW_NAME>/
├─ ir.json                 … intermediate representation
├─ summary.md              … node counts, lineage resolution, expression coverage
└─ inventory/
   ├─ nodes.csv            … nodes (type, inputs, outputs, filters, joins)
   ├─ datasources.csv      … data sources (basis for the mapping sheet)
   ├─ lineage.csv          … output column → origin table.column chains
   ├─ expressions.csv      … formulas, functions used, unknown functions, variables
   ├─ variables.csv        … variables / input parameters and their usages
   └─ unsupported.md       … items that need manual handling
```

## Examples

`skills/sap-calcview-to-dbt/examples/` contains two fixtures:

- `SALES_ORDER_WORKLIST.hdbcalculationview` — a faithful reconstruction of
  the public openSAP sample (3 joins, 4 data sources, 16 output columns).
  Lineage resolves **16/16** columns back to their origin tables, including
  renames (`PRODUCTDESC ← PRODUCT_DESCRIPTION`).
- `SYNTHETIC_ALL_NODES.hdbcalculationview` — a synthetic view covering all
  node types: Projection (filter + calculated columns), Join, Aggregation,
  Union (with a constant mapping), Rank, an input parameter, and a
  restricted measure (which the parser correctly flags as unsupported).

Expected summaries and generated SQL snapshots are in
`skills/sap-calcview-to-dbt/examples/expected/`. Try it:

```bash
python3 skills/sap-calcview-to-dbt/scripts/calcview_parser.py skills/sap-calcview-to-dbt/examples --out output
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_sql.py skills/sap-calcview-to-dbt/examples --out output_sql
```

## Repository layout

```
.claude-plugin/            … plugin + marketplace manifests
skills/sap-calcview-to-dbt/
├─ SKILL.md                … skill definition (read by Claude Code)
├─ scripts/                … parser, SQL generator, dbt project generator,
│                            expression translator (Python stdlib only)
├─ references/             … the reproduction spec (design source of truth)
│                            and the project profile template
└─ examples/               … fixtures + expected summaries / SQL snapshots
```

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Help is especially wanted with real-world expression functions and
XML structures (synthetic examples only; never commit client XML).

## License

MIT
