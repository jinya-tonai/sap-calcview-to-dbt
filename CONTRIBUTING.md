# Contributing

Contributions are welcome — especially from people who work with real
SAP HANA / BW systems. This project converts Graphical Calculation View
XML into an inventory and Snowflake SQL, deterministically and locally.

## Ground rules

- **Never commit a real client's calculation view XML.** Fixtures must be
  public samples (e.g. openSAP) or synthetic files you created. If you
  want to share a real-world structure, reduce it to a minimal synthetic
  example with invented table/column names first.
- **Deterministic only.** Conversion logic must be template/rule based.
  No LLM calls inside the converter, no network access, Python stdlib
  only (so the skill stays dependency-free).
- **Nothing silently dropped.** If your change encounters a construct it
  cannot convert, it must surface as a TODO / `unsupported.md` entry.
- **READMEs come in pairs.** `README.md` (English) is the source of
  truth; update `README.ja.md` in the same commit so they do not drift.

## Development

No build step and no dependencies. Python 3.10+ is enough.

Regression check — run both tools on the fixtures and diff against the
expected outputs before opening a PR:

```bash
python3 skills/sap-calcview-to-dbt/scripts/calcview_parser.py skills/sap-calcview-to-dbt/examples --out /tmp/out_parser
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_sql.py skills/sap-calcview-to-dbt/examples --out /tmp/out_sql
diff /tmp/out_parser/SALES_ORDER_WORKLIST/summary.md skills/sap-calcview-to-dbt/examples/expected/SALES_ORDER_WORKLIST.summary.md
diff /tmp/out_parser/CV_SALES_BY_ORG/summary.md      skills/sap-calcview-to-dbt/examples/expected/CV_SALES_BY_ORG.summary.md
diff /tmp/out_sql/SALES_ORDER_WORKLIST/SALES_ORDER_WORKLIST.sql skills/sap-calcview-to-dbt/examples/expected/SALES_ORDER_WORKLIST.sql
diff /tmp/out_sql/CV_SALES_BY_ORG/CV_SALES_BY_ORG.sql           skills/sap-calcview-to-dbt/examples/expected/CV_SALES_BY_ORG.sql
python3 skills/sap-calcview-to-dbt/scripts/calcview_to_dbt_project.py skills/sap-calcview-to-dbt/examples --out /tmp/out_dbt \
    --profile skills/sap-calcview-to-dbt/references/project-profile-template.yaml
diff -r /tmp/out_dbt skills/sap-calcview-to-dbt/examples/expected_dbt
```

If your change intentionally alters the output, regenerate the expected
files in the same PR and say so in the description.

Generated SQL should also parse as Snowflake dialect (e.g. with
[sqlglot](https://github.com/tobymao/sqlglot), stubbing `{{ var(...) }}`
Jinja with a literal first).

## Where help is most wanted

- **Expression dictionary** (`skills/sap-calcview-to-dbt/scripts/expr_translate.py`): HANA
  calculation-engine functions seen in real views, with their Snowflake
  equivalents and edge cases (argument order, origin/sign differences).
  See the spec §7.3 table in `skills/sap-calcview-to-dbt/references/`.
- **Real-world XML structures**: the Rank node details and
  `variableProperties` are currently captured generically because no
  public sample shows them. Minimal synthetic examples of real export
  shapes are extremely valuable.
- **Node semantics**: text joins, referential joins, temporal joins,
  currency/UOM conversion detection — currently reported as manual work
  (spec §9). Designs or partial implementations welcome.
- **MVP-3 follow-ups**: reconciliation models (spec §11), column-level
  mapping (spec §4.2), and materialisation/incremental strategies for
  the generated dbt project.

## Pull requests

- Keep PRs focused: one construct, one function family, or one fixture.
- Add or update a fixture when you add conversion behaviour, so the new
  behaviour is covered by the regression diff above.
- Describe what real-world view shape motivated the change (without
  sharing any client XML, per the ground rules).

Questions and proposals: open an issue first for anything larger than a
small fix, so the design can be discussed before you invest time.
