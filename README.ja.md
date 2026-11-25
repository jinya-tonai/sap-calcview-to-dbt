# sap-calcview-to-dbt

[English](README.md) | **日本語**

**SAP HANA Graphical Calculation View** の XML（`calculation.xml` /
`.hdbcalculationview`、HANA Studio からエクスポートしたもの）を中間表現と
棚卸し一式にパースし、Snowflake SQL を生成するエージェントスキル。
SAP BW/HANA の Calculation View ロジックを **Snowflake 上の dbt モデル**
として再現するための第一歩です。

## なぜ作ったか

- 変換 SaaS は存在しますが、クライアントの `calculation.xml` を外部サービスに
  アップロードするのはコンプライアンス上できないことが多い。本スキルは
  **完全にローカルで動作** し、何も外部に出ません。
- HANA Studio の GUI から手作業で仕様書を起こすのが通常のボトルネックです。
  パーサが棚卸し（ノード、リネージ、式、変数、未対応項目）を機械的に生成する
  ので、人間はレビューに集中できます。
- パーサが扱えない要素は **黙って落としません**。`unsupported.md` に報告され、
  中間表現（IR）にも保持されます。

設計: [references/hanacv2sql-reproduction-spec.md](references/hanacv2sql-reproduction-spec.md)
（HANACV2SQL の変換パイプラインを TROCCO → Snowflake → dbt 構成向けに
再現した仕様書。日本語）

## スキルとしてインストール

エージェントのスキルディレクトリに clone してください（例: Claude Code）。

```bash
git clone https://github.com/jinya-tonai/sap-calcview-to-dbt ~/.claude/skills/sap-calcview-to-dbt
```

パーサ単体でも使えます。必要なのは Python 3 だけです（標準ライブラリのみ）。

## 使い方

棚卸し:

```bash
python3 scripts/calcview_parser.py path/to/calculation.xml --out output
python3 scripts/calcview_parser.py folder/with/views/     --out output
```

SQL 生成 — ノードごとに 1 つの CTE を作り、最終 SELECT は
logicalModel に従います（ベースメジャーは再集計せず、そのまま射影します）。

```bash
python3 scripts/calcview_to_sql.py path/to/calculation.xml --out output_sql
# マッピング表を使う場合（datasource_id, sf_database, sf_schema, sf_table）:
python3 scripts/calcview_to_sql.py folder/with/views/ --out output_sql --mapping mapping.csv
```

式は仕様 §7.3 の辞書で変換します
（`daysbetween(a, b)` → `datediff(day, a, b)`、`adddays(d, n)` →
`dateadd(day, n, d)`、`$$IP_X$$` → dbt の `var('ip_x', …)` など）。
変換できないものは SQL の冒頭に `-- TODO` として残ります。黙って落とす
ことはありません。マッピング表がない場合、データソースはプレースホルダ名
になり、TODO としてフラグが立ちます。

dbt プロジェクト生成 — sources / staging / intermediate / marts を
`ref()` / `source()` で配線し、`key="true"` 属性からキーテストも生成します。
着地先 DB・スキーマ、レイヤ名、intermediate への分割ルールはプロジェクト
プロファイルで決めます（`references/project-profile-template.yaml` を
作業フォルダに `project_profile.yaml` としてコピー。収録値は汎用ダミー）。

```bash
python3 scripts/calcview_to_dbt_project.py folder/with/views/ --out dbt_project \
    --mapping mapping.csv --profile project_profile.yaml
```

```
dbt_project/
├─ dbt_project.yml
└─ models/
   ├─ _sources.yml          … 着地テーブル（マッピング表から）
   ├─ staging/stg_*.sql     … 着地テーブルごとのパススルーモデル
   ├─ intermediate/int_*.sql … 大きいビューから切り出したノード（プロファイルのルール）
   └─ marts/<view>.sql      … ビューごとの最終モデル＋ _marts.yml（テスト）
```

出力（ビューごと）:

```
output/<ビュー名>/
├─ ir.json                 … 中間表現
├─ summary.md              … ノード数、リネージ解決率、式のカバレッジ
└─ inventory/
   ├─ nodes.csv            … ノード（種別、入力、出力、フィルタ、結合）
   ├─ datasources.csv      … データソース（マッピング表の元）
   ├─ lineage.csv          … 出力カラム → 元テーブル.カラムの系譜
   ├─ expressions.csv      … 式、使用関数、未知関数、変数
   ├─ variables.csv        … 変数 / 入力パラメータと使用箇所
   └─ unsupported.md       … 手作業が必要な項目
```

## サンプル

`examples/` にフィクスチャが 2 本あります。

- `SALES_ORDER_WORKLIST.hdbcalculationview` — 公開されている openSAP
  サンプルを忠実に再構成したもの（Join × 3、データソース × 4、出力
  16 カラム）。リネージは **16/16** が元テーブルまで解決します
  （`PRODUCTDESC ← PRODUCT_DESCRIPTION` のようなリネームを含む）。
- `SYNTHETIC_ALL_NODES.hdbcalculationview` — 全ノード種別を網羅した
  合成ビュー。Projection（フィルタ + 計算列）、Join、Aggregation、
  Union（定数マッピング付き）、Rank、入力パラメータ、制限メジャー
  （パーサが未対応として正しく検出します）。

期待サマリーと生成 SQL のスナップショットは `examples/expected/` に
あります。試し方:

```bash
python3 scripts/calcview_parser.py examples --out output
python3 scripts/calcview_to_sql.py examples --out output_sql
```

## リポジトリ構成

```
SKILL.md                  … スキル定義（エージェントが読む）
scripts/calcview_parser.py … パーサ（標準ライブラリのみ）
scripts/calcview_to_sql.py … SQL ジェネレータ（標準ライブラリのみ）
scripts/calcview_to_dbt_project.py … dbt プロジェクト生成（標準ライブラリのみ）
scripts/expr_translate.py  … HANA 式 → Snowflake 変換（仕様 §7）
references/               … 再現仕様（設計の正本）
examples/                 … フィクスチャ + 期待サマリー・SQL スナップショット
```

## コントリビューション

歓迎します。進め方は [CONTRIBUTING.md](CONTRIBUTING.md)（英語）を見て
ください。特に、実務で出てくる式の関数や XML 構造の知見がありがたいです
（フィクスチャは合成例のみ。クライアントの XML は絶対に commit しないで
ください）。

## ライセンス

MIT
