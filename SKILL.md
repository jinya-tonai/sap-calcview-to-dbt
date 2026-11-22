---
name: sap-calcview-to-dbt
description: Parse SAP HANA Graphical Calculation View XML (calculation.xml / .hdbcalculationview exported from HANA Studio) into an intermediate representation and a full inventory — node list, data sources, column-level lineage, expressions, variables/input parameters, and unsupported/manual items — as the first step of reproducing SAP BW/HANA calculation-view logic as dbt models on Snowflake. Use when the user provides or mentions a calculation view XML, asks what a calculation view contains or depends on, or plans a HANA/BW to Snowflake+dbt migration. All processing is local and deterministic; never upload calculation view XML to external converter services.
license: MIT
metadata:
  version: "0.1.0"
---

# SAP Calculation View → dbt（棚卸し）

HANA Studio等から抜いた Graphical Calculation View の XML を、Snowflake + dbt への再現に先立って棚卸しするスキル。現行バージョン（MVP-1）は **パース＋棚卸しまで**。SQL生成は未実装なので、できると言わないこと。

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
5. **次の段階を案内する。** 棚卸しの次はマッピング表（DataSource → 着地テーブル、カラム差分）の確定が前提。それが無い状態でSQL生成に進まない。

## リファレンス

- 設計の正本は `references/hanacv2sql-reproduction-spec.md`（HANACV2SQL再現仕様）。ノード変換ルール・式辞書・検証仕様・未対応範囲はすべてここに従う。
- `examples/` にフィクスチャ2本（公開openSAPサンプルの再構成、全ノード種別を網羅した合成XML）と期待サマリー（`examples/expected/`）がある。パーサを変更したら両フィクスチャで再実行し、サマリーが期待値と一致するか確認する。

## 既知の制限

- SQL / dbt モデルの生成は未実装（仕様 §12 の MVP-2 以降）。
- Rankノードの詳細要素と変数の `variableProperties` は公開実サンプルで裏取りできていないため、決め打ちせず汎用キャプチャ（IRの `extras` / `rawProperties`）で保持している。実XMLで構造が判明したら固定化する。
- マッピング表の読み込みは未実装。`datasources.csv` の `mapped_table` は現状すべて空。
