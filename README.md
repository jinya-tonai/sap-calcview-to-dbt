# sap-calcview-to-dbt

An agent skill that parses **SAP HANA Graphical Calculation View** XML
(`calculation.xml` / `.hdbcalculationview`, as exported from HANA Studio)
into an intermediate representation and a complete inventory — the first
step of reproducing SAP BW/HANA calculation-view logic as **dbt models on
Snowflake**.

SAP BW/HANA の Graphical Calculation View を Snowflake + dbt で再現するための
棚卸しスキル。XML をローカルで決定的にパースし、ノード構成・カラム系譜・
式・変数・未対応要素を洗い出す。

## Why

- Converter SaaS tools exist, but uploading a client's `calculation.xml` to
  an external service is often a compliance non-starter. This skill runs
  **entirely locally** — nothing leaves your machine.
- Manual spec-writing from the HANA Studio GUI is the usual bottleneck.
  The parser generates the inventory (nodes, lineage, expressions,
  variables, unsupported items) mechanically, so humans only review.
- Anything the parser cannot handle is **never silently dropped**: it is
  reported in `unsupported.md` and preserved in the IR.

## Status

| Stage | Status |
|---|---|
| MVP-1: parser + inventory (this repository) | ✅ Implemented |
| MVP-2: node → SQL conversion | Planned |
| MVP-3: dbt project generation | Planned |

Design: [references/hanacv2sql-reproduction-spec.md](references/hanacv2sql-reproduction-spec.md)
(a reproduction spec of the HANACV2SQL conversion pipeline, adapted to a
TROCCO → Snowflake → dbt stack).

## Install as a skill

Clone this repository into your agent's skills directory, e.g. for
Claude Code:

```bash
git clone https://github.com/jinya-tonai/sap-calcview-to-dbt ~/.claude/skills/sap-calcview-to-dbt
```

Or just use the parser directly — it only needs Python 3 (stdlib only).

## Usage

```bash
python3 scripts/calcview_parser.py path/to/calculation.xml --out output
python3 scripts/calcview_parser.py folder/with/views/     --out output
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

`examples/` contains two fixtures:

- `SALES_ORDER_WORKLIST.hdbcalculationview` — a faithful reconstruction of
  the public openSAP sample (3 joins, 4 data sources, 16 output columns).
  Lineage resolves **16/16** columns back to their origin tables, including
  renames (`PRODUCTDESC ← PRODUCT_DESCRIPTION`).
- `SYNTHETIC_ALL_NODES.hdbcalculationview` — a synthetic view covering all
  node types: Projection (filter + calculated columns), Join, Aggregation,
  Union (with a constant mapping), Rank, an input parameter, and a
  restricted measure (which the parser correctly flags as unsupported).

Expected summaries are in `examples/expected/`. Try it:

```bash
python3 scripts/calcview_parser.py examples --out output
```

## Repository layout

```
SKILL.md                  … skill definition (read by agents)
scripts/calcview_parser.py … the MVP-1 parser (stdlib only)
references/               … the reproduction spec (design source of truth)
examples/                 … fixtures + expected summaries
```

## License

MIT
