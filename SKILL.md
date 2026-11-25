---
name: sap-calcview-to-dbt
description: Parse SAP HANA Graphical Calculation View XML (calculation.xml / .hdbcalculationview exported from HANA Studio) into an intermediate representation and a full inventory — node list, data sources, column-level lineage, expressions, variables/input parameters, and unsupported/manual items — and generate Snowflake SQL (one CTE per node) from it, as the first steps of reproducing SAP BW/HANA calculation-view logic as dbt models on Snowflake. Use when the user provides or mentions a calculation view XML, asks what a calculation view contains or depends on, wants it converted to SQL/dbt, or plans a HANA/BW to Snowflake+dbt migration. All processing is local and deterministic; never upload calculation view XML to external converter services.
license: MIT
metadata:
  version: "0.4.1"
---

# SAP Calculation View → dbt（棚卸し）

HANA Studio等から抜いた Graphical Calculation View の XML を、Snowflake + dbt へ再現するためのスキル。現行バージョンは **MVP-3（パース＋棚卸し＋SQL生成＋dbtプロジェクト生成）まで**。

## 原則

- **ローカル完結。** クライアントの calculation.xml を外部SaaS（HANACV2SQL等）にアップロードしない。変換可否の判断材料が要る場合も、XMLそのものではなく構造の要約だけを扱う。
- **決定的処理。** パーサはテンプレート変換のみで、LLMに構造解釈をさせない。
- **黙って落とさない。** 変換できない要素・解決できない参照は `unsupported.md` と IR の `extras` に必ず残す。
- **データ型の正は着地側。** HANA側の宣言ではなく、TROCCO等で着地した側（CDS OData `$metadata` 等）を正とする（詳細は `references/` の再現仕様 §4）。

## プロジェクトプロファイル（最初に聞く）

着地先や dbt のレイヤ構成は案件ごとに違う。前提を決め打ちしない。**案件の作業フォルダに `project_profile.yaml` が置かれていれば、それを読んで従う**（雛形は `references/project-profile-template.yaml`）。ユーザーが自分で yml を置く運用が基本で、スキルから聞くのはファイルが無い場合の補助手段にすぎない。無い場合は、次をユーザーに聞いて保存するか、仮置きした前提を報告に必ず書いて生成する。

- **出力先**: 棚卸し／SQL／dbtプロジェクトの出力ディレクトリ（`outputs` 節）。コマンド引数で都度指定させるのではなく、プロファイルから読む。無い場合は一度だけ聞いて記録する
- **着地先**: RAW のデータベース名・スキーマ名、着地テーブル命名（同名／接頭辞／ケース変換）
- **ビュー参照の扱い**: 他ビューのデータソースになっている Calculation View を、変換後モデルに接続するか、着地済みテーブルとして扱うか
- **dbt レイヤ構成**: レイヤの名前と接頭辞（`stg_`/`int_`/`marts` 等は一例にすぎない）、`_sources.yml` の置き場
- **INT / MART の分割方針**: 何を intermediate モデルに切り出し、何を mart にするか（例: ノード数しきい値、複数ビューから共有されるノードは int、最終ノードは mart）
- **案件固有の規約**: クライアント（MANDT）フィルタの扱い、通貨・単位換算の方針

聞いた答えは mapping.csv の `sf_database` / `sf_schema` や、MVP-3 のレイヤ分割に反映する。答えが得られなかった項目は空欄＋TODOのままにし、勝手に埋めない。

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
6. **dbtプロジェクトを生成する。**

   ```bash
   python3 <このスキルのフォルダ>/scripts/calcview_to_dbt_project.py <XMLファイル or フォルダ> --out <出力先> [--mapping mapping.csv] [--profile project_profile.yaml]
   ```

   - `models/_sources.yml`（着地テーブル）、`staging/stg_*`（パススルー）、`marts/<ビュー>`（最終モデル）、`marts/_marts.yml`（説明＋`key="true"` 由来の unique/not_null テスト。複合キーはコメントで案内）を生成する。
   - レイヤ名・接頭辞・着地先・INT分割ルールはプロジェクトプロファイルに従う。プロファイルの `node_count > N` ルールに当たるビューはノードを `intermediate/int_<ビュー>__<ノード>` に切り出す。
   - 配線は `ref()` / `source()` で行い、参照先が実在するか（dangling ref が無いか）を生成後に確認する。生成 SQL は Jinja をスタブ化して sqlglot でパース検証できる。
   - dbt 本体の実行（`dbt compile` 等）はこのスキルの範囲外。生成物の検証はパース＋参照整合まで。
7. **次の段階を案内する。** 実データ突合（仕様 §11）はBW側の出力サンプルが要る。通貨換算・階層・制限メジャーは引き続き手作業範囲（仕様 §9）。

## リファレンス

- 設計の正本は `references/hanacv2sql-reproduction-spec.md`（HANACV2SQL再現仕様）。ノード変換ルール・式辞書・検証仕様・未対応範囲はすべてここに従う。
- `examples/` にフィクスチャ2本（公開openSAPサンプルの再構成、全ノード種別を網羅した合成XML）と期待値（`examples/expected/` のサマリー＋生成SQLスナップショット）がある。パーサ／生成器を変更したら両フィクスチャで再実行し、期待値と一致するか確認する。

## 既知の制限

- dbtプロジェクト生成（MVP-3）はプロジェクト一式の生成まで。マテリアライゼーションは既定（staging/intermediate=view、marts=table）で、増分戦略やスナップショットは未対応。`dbt compile` での検証は未実施（生成側のパース検証＋参照整合まで）。
- Rankは基本対応（`row_number` + しきい値）をMVP-3から前倒しで実装済み。ただしRankノードの詳細要素と変数の `variableProperties` は公開実サンプルで裏取りできていないため、決め打ちせず汎用キャプチャ（IRの `extras` / `rawProperties`）で保持している。実XMLで構造が判明したら固定化する。
- マッピング表はテーブル対応のみ対応（`calcview_to_sql.py --mapping`）。カラム対応（仕様 §4.2）は未実装で、`datasources.csv` の `mapped_table` はパーサ単体では空のまま。
- 式辞書は仕様 §7.3 の初期版。`component()` の部品番号対応と `weekday()` の起点補正はTODOフラグ付きで出し、実データ確認が前提。
