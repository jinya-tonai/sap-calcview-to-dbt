---
name: sap-calcview-to-dbt
description: Parse SAP HANA Graphical Calculation View XML (calculation.xml / .hdbcalculationview exported from HANA Studio) into an intermediate representation and a full inventory — node list, data sources, column-level lineage, expressions, variables/input parameters, and unsupported/manual items — and generate Snowflake SQL (one CTE per node) from it, as the first steps of reproducing SAP BW/HANA calculation-view logic as dbt models on Snowflake. Use when the user provides or mentions a calculation view XML, asks what a calculation view contains or depends on, wants it converted to SQL/dbt, or plans a HANA/BW to Snowflake+dbt migration. All processing is local and deterministic; never upload calculation view XML to external converter services.
license: MIT
metadata:
  version: "0.2.1"
---

# SAP Calculation View → dbt（棚卸し）

HANA Studio等から抜いた Graphical Calculation View の XML を、Snowflake + dbt へ再現するためのスキル。現行バージョンは **MVP-2（パース＋棚卸し＋SQL生成）まで**。dbtプロジェクト形式での生成（MVP-3）は未実装なので、できると言わないこと。

## 原則

- **ローカル完結。** クライアントの calculation.xml を外部SaaS（HANACV2SQL等）にアップロードしない。変換可否の判断材料が要る場合も、XMLそのものではなく構造の要約だけを扱う。
- **決定的処理。** パーサはテンプレート変換のみで、LLMに構造解釈をさせない。
- **黙って落とさない。** 変換できない要素・解決できない参照は `unsupported.md` と IR の `extras` に必ず残す。
- **データ型の正は着地側。** HANA側の宣言ではなく、TROCCO等で着地した側（CDS OData `$metadata` 等）を正とする（詳細は `references/` の再現仕様 §4）。

## 手順

1. **入力を確認する。** 対象は `.hdbcalculationview` / `.calculationview`（ルートが `Calculation:scenario` のXML）。`calculationScenarioType` が `SCRIPT_BASED` のものはノード分解の対象外で、検出して報告するだけに留める。
2. **パーサを実行する**（Python標準ライブラリのみ、追加インストール不要）。

   ```bash
   python3 <このスキルのフォルダ>/scripts/calcview_parser.py <XMLファイル or フォルダ> --out <出力先>
   ```

3. **出力を読む。** ビューごとに `<出力先>/<ビュー名>/` ができる。まず `summary.md` で全体像（ノード数・種別、リネージ解決率、式の自動化候補率、未対応件数）を掴み、次に `inventory/` を見る。
   - `nodes.csv` — ノード一覧（種別・入力・出力カラム数・filter有無・結合情報）
   - `datasources.csv` — DataSource一覧（マッピング表の元データ）
   - `lineage.csv` — 最終出力カラム → 元テーブル.カラムの系譜
   - `expressions.csv` — 式の一覧（使用関数・未知関数・参照変数・変換可否）
   - `variables.csv` — 変数/入力パラメータ（型・必須・デフォルト・使用箇所）
   - `unsupported.md` — 手作業対象（制限メジャー、階層、通貨換算、未知関数、SCRIPT_BASED等）
   - `ir.json` — 中間表現。後段のSQL生成はこれを入力にする
4. **報告する。** 次の観点で短くまとめる。
   - ノード構成（種別ごとの件数）と最終ノード
   - リネージ解決率。ただし終端が `constant`（定数マッピング）/ `calculated`（計算列）のものは「辿れない」のではなく、それが正しい終端である旨を添える
   - 式の自動化候補率と、未知関数があればその一覧
   - 未対応・手作業アイテムの一覧（件数だけでなく中身）
   - 入力パラメータと、その使用箇所
5. **SQLを生成する。**

   ```bash
   python3 <このスキルのフォルダ>/scripts/calcview_to_sql.py <XMLファイル or フォルダ> --out <出力先> [--mapping mapping.csv]
   ```

   - ノード = CTE（依存順）、最終SELECTは logicalModel 準拠。**baseMeasureは再集計しない**（仕様 §5.6）。
   - マッピング表（`datasource_id, sf_database, sf_schema, sf_table`）が無いDataSourceはプレースホルダ名＋TODOで出す。黙って実在しないテーブル名を確定形で出さない。
   - 式は `scripts/expr_translate.py` が仕様 §7.3 の辞書で変換する。`$$VAR$$` は dbt の `var()`（§6.1既定）。未知関数は原形のまま残り、TODOとして数える。
   - 生成SQLの冒頭にTODO件数と内訳が出る。報告ではこの件数と中身を必ず添える。
   - 可能なら sqlglot（Snowflake方言）でパース検証する。`var()` のJinjaは検証時のみリテラルに置換する。
6. **次の段階を案内する。** dbtプロジェクト生成（`stg_/int_/mart` 分割、sources.yml、テスト生成）はMVP-3で未実装。実データ突合（仕様 §11）はBW側の出力サンプルが要る。

## リファレンス

- 設計の正本は `references/hanacv2sql-reproduction-spec.md`（HANACV2SQL再現仕様）。ノード変換ルール・式辞書・検証仕様・未対応範囲はすべてここに従う。
- `examples/` にフィクスチャ2本（公開openSAPサンプルの再構成、全ノード種別を網羅した合成XML）と期待値（`examples/expected/` のサマリー＋生成SQLスナップショット）がある。パーサ／生成器を変更したら両フィクスチャで再実行し、期待値と一致するか確認する。

## 既知の制限

- dbtプロジェクト形式の生成（仕様 §10）は未実装（MVP-3）。現行の出力はCTE連結の単一SQLファイル。
- Rankは基本対応（`row_number` + しきい値）をMVP-3から前倒しで実装済み。ただしRankノードの詳細要素と変数の `variableProperties` は公開実サンプルで裏取りできていないため、決め打ちせず汎用キャプチャ（IRの `extras` / `rawProperties`）で保持している。実XMLで構造が判明したら固定化する。
- マッピング表はテーブル対応のみ対応（`calcview_to_sql.py --mapping`）。カラム対応（仕様 §4.2）は未実装で、`datasources.csv` の `mapped_table` はパーサ単体では空のまま。
- 式辞書は仕様 §7.3 の初期版。`component()` の部品番号対応と `weekday()` の起点補正はTODOフラグ付きで出し、実データ確認が前提。
