# HANACV2SQL再現仕様 ― calculation.xml → Snowflake SQL / dbt 変換器

作成: 2026-11-20（MVP-1実装済み: 2026-11-21 / MVP-2実装済み: 2026-11-23、`scripts/calcview_parser.py`・`scripts/calcview_to_sql.py`・`scripts/expr_translate.py`、詳細は同フォルダの README）
位置づけ: HANACV2SQL（外部SaaS）の変換ロジックを、ローカルで動く決定的な変換器として再現するための仕様。
想定ユースケースは自前構成専用に寄せてある:

```
SAP S/4HANA CDS View ──TROCCO SHR / OData API──▶ Snowflake RAW（着地テーブル）
SAP BW / HANA の Graphical Calculation View ──HANA Studio──▶ calculation.xml（本変換器の入力）
Snowflake RAW ──dbt（本変換器の出力モデル）──▶ BW内の加工を再現したマート
```

---

## 0. 基本原則

1. **ローカル処理のみ。** calculation.xmlは業務ロジックそのものなので、外部SaaSにアップロードしない。HANACV2SQLは「ロジックの参考」と「金額感のベンチマーク」にだけ使う（付録A）。
2. **決定的変換を優先する。** ノード→SQLはテンプレートで機械的に変換する。LLM/AIは「未対応の式の変換案」と「レビュー支援」に限定し、生成SQLの本体には使わない（再現性と監査のため）。
3. **ノード = CTE / 中間モデル。** HANACV2SQLの売りは「全体を1本の最適化SQLに組み直す」ことだが、dbt用途では逆にノード単位の分割が保守性・検証可能性で勝る。最適化はSnowflakeのオプティマイザに任せる。
4. **変換できないものは黙って落とさない。** 未対応要素は必ず検出レポートに出し、生成SQLには `-- TODO:` コメントを残す。後工程で「気づいたら違った」を防ぐのが最優先。
5. **マッピングは人が直せる表が正。** テーブル/カラムの対応付けはマッピング表（CSV/Excel）で外出しし、コードに埋め込まない。HANACV2SQLの "SQL Mapping Engine" と同じ分離。

### HANACV2SQLとの対応関係

| HANACV2SQL | 本仕様での再現 |
|---|---|
| Phase 1: XML検証 → ノード数表示 → 標準SQL + メタデータ生成 | §2 パーサ + §3 中間表現（IR）と棚卸し成果物 |
| Phase 2: SQL Mapping Engine（テーブル/カラム名の付け替え、対象プラットフォーム方言で再生成） | §4 マッピング表 + §10 出力（dbt生成） |
| 対象プラットフォーム選択（Snowflake / BigQuery / Databricks / Redshift / Fabric） | Snowflake固定（dbt経由）。方言変換は §7 式変換 + §8 型変換 |
| 出力ZIP（生成SQL + ノード単位ロジックシート） | 生成dbtプロジェクト + `inventory/`（ノード一覧・リネージ・式一覧） |
| クレジット課金（ノード数帯域） | 工数見積もりの物差しとして流用（§12） |

---

## 1. 入力仕様

### 1.1 calculation.xml（必須）

HANA Studioの「Display View XML」/ Exportで取得した Graphical Calculation View のXML。
XS Classic形式（`.calculationview`）とHDI形式（`.hdbcalculationview`、`schemaVersion="3.0"`）の両方を受ける。

ルート要素 `Calculation:scenario` の主な属性:

| 属性 | 用途 |
|---|---|
| `id` | ビュー識別子（例: `package::VIEW_NAME`）。出力モデル名の元 |
| `calculationScenarioType` | `TREE_BASED`（グラフィカル）/ `SCRIPT_BASED`（SQLScript）。処理分岐の判定（§5.7） |
| `dataCategory` | `CUBE` / `DIMENSION` / `DEFAULT`。最終モデルの扱いの参考 |
| `outputViewType` | 最上位ノード種別（`Projection` / `Aggregation` 等） |
| `defaultClient` | `$$client$$` 等。クライアント依存の扱い（§6.3） |
| `enforceSqlExecution` | 式エンジン（SQL / Calculation Engine）の推定材料（§7.5） |

子要素の並び（実サンプルで確認済みの構造）:

```
Calculation:scenario
├─ descriptions            … 表示名・説明
├─ localVariables          … 変数 / 入力パラメータ定義
├─ variableMappings        … 子ビューへのパラメータ引き渡し
├─ dataSources             … DataSource（resourceUri が実体名）
├─ calculationViews        … ノード群（calculationView要素、xsi:typeで種別）
└─ logicalModel            … Semantics。attributes / calculatedAttributes /
                              baseMeasures / calculatedMeasures / restrictedMeasures
```

**パーサ実装上の注意:**
- 名前空間URIは世代で揺れる（`http://www.sap.com/ndb/BiModelCalculation.ecore` と `https://…` の両方がある）。URI文字列ではなく**ローカル名で要素を判定**する。
- 属性値・式の中の `"` や `$$…$$` はXMLエスケープ（`&quot;` 等）されて入っている。前処理で確実にデコードする。
- `layout` / `shapes` 等の描画情報は変換に不要。無視する（ただし未知要素として数だけ数え、想定外の構造検出に使う）。

### 1.2 付随入力

| 入力 | 必須度 | 用途 |
|---|---|---|
| マッピング表（§4） | 必須 | DataSource → Snowflake着地テーブル、カラム対応 |
| TROCCO着地情報（対象CDS View一覧、各OData `$metadata`） | 必須 | 着地側のカラム名・型の確定。型の正はこちら（§8） |
| 参照先Calculation ViewのXML（dataSourceが別ビューの場合） | 条件付き必須 | 再帰的に展開する。下位が無い場合は「未解決参照」としてレポート |
| BW/HANA側の出力サンプル or 集計結果（キー×期間の件数・金額合計） | 検証用 | §11 突合テストの期待値 |
| 対象ビュー一覧（複数XMLをZIP/フォルダで一括） | 任意 | 一括変換。ビュー間参照の解決にも使う |

### 1.3 入力チェック（変換前に必ず実施）

1. XMLとして well-formed か、ルートが `Calculation:scenario` か
2. `calculationScenarioType` の判定 → `SCRIPT_BASED` は別ルート（§5.7）
3. ノード数のカウント（工数見積もり表示。HANACV2SQLの課金帯域と同じ物差し）
4. 未対応要素の事前スキャン（階層、通貨換算、UOM、制限メジャー、時間結合 等 → §9）
5. dataSourceの解決可否（マッピング表に無いものはエラー一覧へ。黙って素通りしない）

---

## 2. パーサ仕様（Phase 1 前半）

- 実装: Python + lxml（または標準 `xml.etree` + ローカル名正規化）。単一ファイル入力、副作用なし。
- 役割は「XML → 中間表現（IR、§3）」と「棚卸し成果物の出力」まで。SQL生成は後段（§5）に分離する。
- ノードの入出力解決: 各ノードの `<input node="…">` が参照するのは **dataSourceのid** か **他ノードのid**。これでDAGを構築し、トポロジカルソートして変換順を決める。循環・欠落参照はエラー。
- カラムリネージ: `<mapping xsi:type="Calculation:AttributeMapping" target="…" source="…"/>` を全ノード分たどり、最終出力カラム → 元dataSourceカラムの系譜を作る。
  - 参考実装の実績範囲: DataHubのHANAコネクタが同じXMLからカラムレベルリネージを抽出しており、対応ノードは `ProjectionView / JoinView / AggregationView / UnionView / RankView / SqlScriptView`。本パーサの最低対応範囲もこれに合わせる。
  - mappingの `source` は素のカラム名のほか、`PARTNER.PARTNERID` のように入力側オブジェクトで修飾された形で出ることがある（実サンプルで確認）。修飾子は入力ノード/ソースの解決に使う。

### 棚卸し成果物（`inventory/` に出力）

| ファイル | 内容 |
|---|---|
| `nodes.csv` | node_id, type, inputs, 出力カラム数, filter有無, 計算列数 |
| `datasources.csv` | datasource_id, resourceUri, type, マッピング先Snowflakeテーブル, 解決状態 |
| `lineage.csv` | 最終出力カラム, 経由ノード列, 元datasource, 元カラム, 変換種別（直通/計算/集計） |
| `expressions.csv` | 式の出現場所（ノード/論理モデル）, 元式, 変換後式, 変換状態（自動/TODO） |
| `variables.csv` | 変数・入力パラメータ一覧, 型, デフォルト値, 必須, 使用箇所 |
| `unsupported.md` | 未対応要素の一覧と出現場所（§9） |

この棚卸しだけでも「仕様書起こし」の代替になるため、SQL生成より先に価値が出る（実装順序は§12）。

---

## 3. 中間表現（IR）仕様

パーサとSQL生成の間の契約。JSONでシリアライズ可能に保ち、デバッグ・差分確認を容易にする。

```jsonc
{
  "scenario": {
    "id": "package::CV_NAME",
    "type": "TREE_BASED",            // or SCRIPT_BASED
    "dataCategory": "CUBE",
    "outputViewType": "Aggregation",
    "defaultClient": "$$client$$"
  },
  "dataSources": [
    { "id": "SO.Header", "resourceUri": "SO.Header", "type": "DATA_BASE_TABLE",
      "schemaName": null, "mappedTable": null }   // mappedTableは§4で解決
  ],
  "variables": [
    { "id": "IP_DATE", "kind": "inputParameter", "dataType": "DATE",
      "defaultValue": null, "mandatory": true, "multipleValues": false,
      "usedIn": ["Projection_1.filter", "logicalModel.calc.X"] }
  ],
  "nodes": [
    {
      "id": "Sales", "type": "JoinView",
      "inputs": [
        { "node": "SO.Header",
          "mappings": [ { "target": "SALESORDERID", "source": "SALESORDERID" } ] },
        { "node": "SO.Item",
          "mappings": [ { "target": "PRODUCTID", "source": "PRODUCT.PRODUCTID" } ] }
      ],
      "viewAttributes": ["SALESORDERID", "PRODUCTID"],
      "calculatedAttributes": [
        { "id": "AMOUNT_JPY", "formula": "\"GROSSAMOUNT\" * \"RATE\"", "dataType": "DECIMAL" }
      ],
      "filter": null,                       // { "expression": "..." }
      "join": { "joinType": "inner", "joinAttributes": ["SALESORDERID"],
                "cardinality": null },
      "aggregation": null,                  // AggregationView用
      "union": null,                        // UnionView用
      "rank": null                          // RankView用
    }
  ],
  "logicalModel": {
    "topNode": "Partner",
    "attributes": [
      { "id": "SALESORDERID", "key": true, "order": 1,
        "keyMapping": { "columnObjectName": "Partner", "columnName": "SALESORDERID" } }
    ],
    "calculatedAttributes": [ /* formula持ち */ ],
    "baseMeasures": [
      { "id": "GROSSAMOUNT", "aggregationType": "sum", "measureType": "simple",
        "measureMapping": { "columnObjectName": "Partner", "columnName": "GROSSAMOUNT" } }
    ],
    "calculatedMeasures": [],
    "restrictedMeasures": []
  }
}
```

設計判断:
- IRはXMLの構造をなるべく素直に写す。解釈（SQL化の判断）は生成段に寄せ、IRで意味を捏造しない。
- 解決できなかった項目は `null` + `issues[]` に理由を残す。生成段は `issues` があるノードを `-- TODO` 付きで出す。

---

## 4. マッピング表仕様（Phase 2 = Mapping Engine相当）

### 4.1 テーブルマッピング

| 列 | 内容 |
|---|---|
| `datasource_id` | XML内のDataSource id |
| `resource_uri` | XMLの resourceUri（実体名） |
| `hana_object_type` | テーブル / Calculation View / Attribute View 等 |
| `sf_database` / `sf_schema` / `sf_table` | TROCCO着地先（CDS Viewの着地テーブル） |
| `cds_view_name` | 対応するCDS View名（ODataサービス名） |
| `client_filter` | クライアント列と値（例: `MANDT = '100'`）。§6.3 |
| `note` | 人手判断の記録 |

ルール:
- デフォルトは「resourceUriを正規化した名前 = 着地テーブル名」の自動推定を入れ、人が上書きする。
- **未マッピングのdataSourceが1つでもあれば変換は警告付きで続行し、該当ノードは生成対象から外してレポートに明示**する（部分変換の可否は実行時オプション）。

### 4.2 カラムマッピング

| 列 | 内容 |
|---|---|
| `sf_table` / `source_column` | 着地側カラム（OData `$metadata` のプロパティ名が正） |
| `target_column` | dbt側で使うカラム名。デフォルトは着地名と同一 |
| `sf_data_type` | 着地側の実型（§8） |
| `note` | リネーム理由など |

- デフォルト同一名。BW由来のCV側カラム名（InfoObject名など）とCDS側フィールド名がずれる前提で、**差分だけを表に書く**運用にする。
- マッピングに無いカラムがXML側から要求された場合はエラー一覧へ（型推測でごまかさない）。

---

## 5. ノード変換ルール（Phase 1 後半 = SQL生成）

共通:
- ノードは依存順に **CTE** 化する。CTE名 = ノードidを snake_case 化したもの。
- dataSource入力は `{{ source('sap_raw', '<着地テーブル>') }}`（または staging モデル、§10）への参照に置換する。
- 各ノードの出力カラムは、そのノードの `viewAttributes` + `calculatedAttributes` の順序どおりに固定する。下流はこの出力だけを参照する（XMLのmappingと一致させる）。
- 生成SQLの先頭に発生源コメントを付ける: `-- node: <id> (<type>) / source: <xmlファイル名>`。

### 5.1 ProjectionView

- 入力は1つ（dataSource または他ノード）。
- `mapping` の target を `select <source> as <target>` にする。sourceが修飾形（`OBJ.COL`）なら該当入力のエイリアスを引く。
- `calculatedViewAttributes` の `<formula>` は §7 で変換して select 項目に追加。
- ノードの `<filter>` 式は `where` に変換する。式中のカラム参照は当ノードの入力カラムに解決する。
- 射影のみで実質素通しのノードも省略せずCTE化する（検証時にBW側ノードと1対1で追えることを優先。最適化は後段の任意パス）。

### 5.2 JoinView

- 入力は2つ。`joinType` の対応:

| XML | Snowflake |
|---|---|
| `inner` | `inner join` |
| `leftOuter` | `left outer join` |
| `rightOuter` | `right outer join` |
| `fullOuter` | `full outer join` |
| `text`（テキスト結合） | 原則 `left join` + 言語条件。言語（SPRAS等）の確定方法が案件依存のため **手作業フラグ** |
| referential（参照結合） | `left join`。参照整合が前提の最適化はSnowflake側では行わず、結果一致を優先 |

- 結合条件は `<joinAttribute name="…"/>` の列挙を `on a.<col> = b.<col>` の AND 結合にする。左右で名前が違う場合は各入力の mapping から実カラムを解決する。
- 結合ノードに filter がある場合は `where`（outer join の場合は条件の置き場所で結果が変わるため、XMLの意図が on か where か判断できないものは TODO フラグを立てる）。
- Star Join（ディメンションCVとの結合束）も JoinView の連続として処理する。専用最適化はしない。

### 5.3 AggregationView

- `viewAttributes` のうち集計対象でないもの = `group by` キー。
- メジャーは `aggregationType` に従って集計関数化する:

| aggregationType | Snowflake |
|---|---|
| `sum` | `sum(x)` |
| `min` / `max` | `min(x)` / `max(x)` |
| `count` | `count(x)`（distinct指定がある場合は `count(distinct x)`） |
| `avg` | `avg(x)`。ただし元が「合計÷件数」由来のBW例外集計に当たる場合は §9 |

- 集計後に効く計算（calculated measure 相当）は外側CTEで計算する（集計前/後の取り違え防止）。
- **例外集計（exception aggregation）は自動変換対象外。** 検出したら §9 レポートへ。

### 5.4 UnionView

- 入力は複数。各入力の mapping で target カラムに揃え、`union all` でつなぐ。
- ある入力に無い target カラムは `cast(null as <型>)` で補う（型は他入力または論理モデルから決める。決められない場合は TODO）。
- 入力ごとに異なる**定数マッピング**（例: 区分値の固定付与）がある場合は、その入力の select に定数列として出す。
- HANAのUnionノードは重複排除をしない前提（= `union all`）。`union`（distinct）への読み替えはしない。

### 5.5 RankView

- partition 属性 → `partition by`、order 属性 + 方向 → `order by`、しきい値 → 外側で `where rn <= N`。
- ランキング関数（`row_number` / `rank`）の別はXML属性から決める。判別できない場合は `row_number` を既定とし、TODOコメントで明示する。

### 5.6 logicalModel（Semantics）→ 最終モデル

- 最終モデルのカラム順・カラム名は logicalModel の `attributes` / `baseMeasures` の `order` どおり。`keyMapping` / `measureMapping` が指す最上位ノードの出力から引く。
- logicalModel の `calculatedAttributes` / `calculatedMeasures` の formula は §7 で変換し、最終 select で計算する。
- **注意: baseMeasure の `aggregationType` は「BIツールが消費するときの既定集計」であって、最終モデルのSQLで再集計する指示ではない。** 最上位ノードが既に明細/集計済みの行を返すなら、最終モデルはそのまま射影する。aggregationType は dbt の semantic layer / メタデータ（`meta` 情報）と突合テスト設計に回す。
- `key="true"` の属性は dbt の `unique` + `not_null` テスト候補として yml に出力する。
- `restrictedMeasures` は §9（手作業）。

### 5.7 SCRIPT_BASED / SqlScriptView の扱い

- 本体はSQLScriptなのでノード分解はしない。埋め込みSQLを取り出し、方言変換（§7の関数・型まわり）のみ適用して1モデルにする。
- カラムレベルリネージは自動化対象外（DataHubでも同じく対象外）。テーブルレベルの入出力だけ棚卸しに出す。

---

## 6. 変数・入力パラメータ仕様

出現場所: `localVariables` の定義、filter / formula 内の `$$IP_xxx$$` プレースホルダ、`variableMappings`（子ビューへの引き渡し）。

### 6.1 変換方針（既定）

- ビュー自体はパラメータを持てない（Snowflakeの通常ビュー）ため、**dbtのコンパイル時置換**を既定とする:
  - `$$IP_DATE$$` → `{{ var('ip_date', '<デフォルト値>') }}` のように `var()` 化し、型に応じてクォート/キャストする。
  - 必須パラメータでデフォルトが無いものは、var未指定時にコンパイルエラーになるよう既定値を入れない（黙って空で動かさない）。
- 複数値パラメータ（IN条件）は `var` をリストとして受け、`in (...)` に展開するマクロを使う。
- 子ビューへの `variableMappings` は、子側XMLを再帰展開したときに親の値をそのまま引き継ぐ形で解決する。

### 6.2 代替案（要判断、§13）

- 対象期間などが固定運用なら、パラメータを「設定テーブル + join/where参照」に変える方式もあり得る。運用形態が決まるまでは §6.1 を既定とする。

### 6.3 クライアント（$$client$$ / MANDT）

- `defaultClient="$$client$$"` のビューはクライアント依存が前提。CDS着地データにクライアント列が含まれる場合は、staging段でマッピング表の `client_filter` により固定値で絞る。
- 着地側にクライアント列が無い（OData側で単一クライアントに確定している）場合は、何もしない。その判定はマッピング表に記録する。

---

## 7. 式（formula / filter）変換仕様

### 7.1 式の出現場所

- ノードの `calculatedViewAttributes`（`<formula>`）
- logicalModel の `calculatedAttributes` / `calculatedMeasures`
- ノードの `<filter>` 式
- 変数のデフォルト値式

### 7.2 トークン規則

| 元 | 変換 |
|---|---|
| `"COLUMN"`（二重引用符の識別子） | Snowflakeの識別子として維持 or マッピング表の target_column に置換 |
| `$$VAR$$` | §6 のパラメータ置換 |
| `'文字列'` | そのまま（シングルクォート） |
| 数値リテラル | そのまま |

### 7.3 関数マッピング（初期辞書）

計算列の式エンジンは小文字関数名の独自文法（例: `if("A" <> '00000000', '231', if(…))` のような入れ子、`daysbetween(date("D"), now())` など）。初期辞書は以下とし、実XMLに出てきた関数から継続的に拡張する。

| HANA（式エンジン） | Snowflake | 備考 |
|---|---|---|
| `if(cond, a, b)` | `iff(cond, a, b)` / 複雑なら `case when` | 入れ子は case 化した方が可読 |
| `isnull(x)` | `x is null` | 真偽値として使う文脈でのみ。この置換で式構造が変わる点に注意 |
| `string(x)` | `to_varchar(x)` | |
| `int(x)` / `float(x)` | `to_number(x)` 系 / `to_double(x)` | 精度指定は案件ごとに確認 |
| `date(x)` | `to_date(x)` | 引数が文字列の日付形式に依存。形式が `YYYYMMDD` の8桁文字列なら `to_date(x, 'YYYYMMDD')` |
| `daysbetween(a, b)` | `datediff(day, a, b)` | 符号の向きは a→b で一致するか実データで検証（§11） |
| `secondsbetween(a, b)` | `datediff(second, a, b)` | |
| `adddays(d, n)` | `dateadd(day, n, d)` | 引数順が逆転する |
| `addseconds(d, n)` | `dateadd(second, n, d)` | |
| `now()` | `current_timestamp()` | 型（DATEで使われている場合）は文脈で `current_date()` |
| `leftstr(s, n)` / `rightstr(s, n)` | `left(s, n)` / `right(s, n)` | |
| `midstr(s, start, len)` | `substr(s, start, len)` | |
| `strlen(s)` | `length(s)` | |
| `instr(s, sub)` | `position(sub in s)` 系 | 戻り値仕様（無し=0）を確認 |
| `concat(a, b, …)` | `concat(a, b, …)` | そのまま可 |
| `replace(s, a, b)` | `replace(s, a, b)` | そのまま可 |
| `upper/lower/trim/ltrim/rtrim` | 同名 | そのまま可 |
| `round(x, n)` | `round(x, n)` | |
| `rounddown(x, n)` | `trunc(x, n)` 系 | 負数の扱いを検証 |
| `abs/sign/mod` | 同名 | そのまま可 |
| `component(d, n)` | `extract(<part> from d)` | n の対応（1=年, 2=日, 3=月, 4=時, 5=分, 6=秒 と資料により順序が特殊）— **実XMLで当たったら個別確認** |
| `weekday(d)` | `dayofweek(d)` | 曜日番号の起点（月曜=0 vs 日曜=1）が違うため **要補正** |
| 未知の関数 | 変換しない | 元式を残し `-- TODO: unconverted function <name>` + expressions.csv に記録 |

### 7.4 演算子

- `<>`, `||`, `and/or/not`, 比較演算子はそのまま使える。
- 文字列連結 `||` と論理和の混同に注意（HANA式エンジンでも `||` は連結）。

### 7.5 エンジン差異

- 式には Calculation Engine 文法（`if` の入れ子中心）と SQL 文法（`case when` 中心）の2系統がある。`enforceSqlExecution` や式の見た目で推定し、どちらでも最終形は Snowflake の `case` / `iff` に正規化する。
- 正規化できない式は「元式コメント + TODO」で出し、人手変換の対象として expressions.csv に残す。変換率（自動/TODOの比率）をビューの難易度指標として棚卸しに出す。

---

## 8. データ型変換仕様

**優先順位: 型の正は TROCCO着地側（CDS の OData `$metadata` の EDM 型 → 着地テーブルの実型）。** HANA型はXML/論理モデル側の宣言であり、着地で実際に決まる型と食い違うことがある。以下の表は、着地型が未確定な段階での推定と、計算列の出力型を決めるときの参考に使う。

| HANA | Snowflake（目安） | 備考 |
|---|---|---|
| TINYINT | NUMBER(3,0) | |
| SMALLINT | NUMBER(5,0) | |
| INTEGER | NUMBER(10,0) | |
| BIGINT | NUMBER(19,0) | |
| DECIMAL(p,s)（p≤38） | NUMBER(p,s) | |
| DECIMAL（無指定）/ DECIMAL(38,38) 等 | 要個別判断 | ツールにより文字列化される例もある特殊枠。黙って決めない |
| SMALLDECIMAL | NUMBER | 精度は元定義を確認 |
| REAL / DOUBLE | FLOAT | |
| VARCHAR(n) | VARCHAR(n) | |
| NVARCHAR(n) | VARCHAR(n) | Snowflakeは文字数ベース。元がバイト前提なら不足に注意 |
| ALPHANUM(n) / SHORTTEXT(n) | VARCHAR(n) | |
| DATE | DATE | |
| TIME | TIME | |
| TIMESTAMP | TIMESTAMP_NTZ | 精度は元に合わせる |
| SECONDDATE | TIMESTAMP_NTZ(0) | |
| VARBINARY(n) | BINARY(n) | |
| BLOB / CLOB / NCLOB | 対象外フラグ | TEXT系は VARCHAR で受ける場合のみ可 |

---

## 9. 手作業範囲（自動変換しないもの）

検出したら `unsupported.md` に「ビュー / ノード / 項目 / 理由」を列挙する。HANACV2SQL自身が手作業と明記している範囲と概ね一致する。

| 項目 | 理由 / 代替方針 |
|---|---|
| 入力パラメータの複雑な使い方（値ヘルプ連動、式内での動的参照など） | プラットフォーム差が大きい。§6 の単純置換を超えるもの |
| 階層（レベル階層 / 親子階層） | Snowflake側は再帰CTEや階層テーブルの別設計が必要 |
| 通貨換算（Currency Conversion） | レートテーブル（TCURR系CDS）+ 換算モデルを別途設計 |
| 単位換算（UOM） | 同上 |
| 例外集計（BW exception aggregation） | ウィンドウ関数等での個別実装 |
| 制限メジャー（restrictedMeasures） | フィルタ条件付きメジャーの個別実装 |
| テキスト結合の言語決定ロジック | 言語列の確定方法が案件依存 |
| 時間結合（temporal join） | 有効期間の semantics が案件依存 |
| SCRIPT_BASED ビュー本体のSQLScript | §5.7。方言変換のみ支援、意味の移植は人手 |
| CE関数（旧Calculation Engine関数）前提の式 | 廃止方向の記法。個別にSQL式へ書き直し |

---

## 10. 出力仕様（dbtプロジェクト）

### 10.1 構成

```
models/
├─ sap_bw_repro/
│  ├─ _sources.yml            … RAW着地テーブル（CDS単位）
│  ├─ staging/
│  │   └─ stg_<datasource>.sql   … 着地テーブルのリネーム/型確定/クライアント絞り込みのみ
│  ├─ intermediate/
│  │   └─ int_<cv>__<node>.sql   … ノード単位（§10.2 の判断でモデル化する場合）
│  └─ marts/
│      └─ <cv_name>.sql          … 最終モデル（logicalModel準拠）
│      └─ <cv_name>.yml          … カラム説明（descriptions由来）、テスト
inventory/                        … §2 の棚卸し成果物
mapping/                          … §4 のマッピング表
```

- 命名は、開発中のTROCCO向けdbtパッケージ（Fivetranのdbtパッケージの命名に倣う方針）と矛盾しないよう `stg_ / int_` 接頭辞を使う。
- カラム説明はXMLの `descriptions`（defaultDescription）を yml に流し込む。

### 10.2 ノードの粒度（既定ルール）

| 条件 | 出力形態 |
|---|---|
| 1ビュー内のノード数が概ね10以下 | 最終モデル1本の中にCTEとして展開 |
| ノード数が多い / 同一ノードを複数ビューが参照 | ノードを `int_` モデルとして独立 |
| staging（dataSource直後） | 常に `stg_` モデルとして独立（着地との境界を固定する） |

### 10.3 生成SQLの規約

- 先頭コメント: 発生源XML、ノードid、生成日時、自動生成である旨（手編集はモデル側でなく変換器/マッピング側で行う運用を明記）。
- TODOが残るモデルはファイル冒頭に `/* TODO: n items - see inventory/expressions.csv */` を出す。

---

## 11. 検証仕様

変換の正しさは「突合」で担保する。生成して終わりにしない。

1. **件数・合計突合モデル**: BW/HANA側の出力（キー × 期間） vs Snowflakeマートで、件数と主要メジャーの合計を比較する dbt モデルを用意し、差分が0でない行を出す。
2. **dbtテスト**: logicalModel の `key="true"` 属性 → `unique` / `not_null`。参照結合先 → `relationships`（候補として生成、採否は人）。
3. **式の単体確認**: 計算列は代表キー数件についてBW側表示値とSnowflake側計算値を並べる確認クエリを棚卸しに添付する。特に符号・起点がズレやすいもの（`daysbetween` の向き、`weekday` の起点、`rounddown` の負数）は必須確認項目とする。
4. **コンパイル検証**: 生成SQLがSnowflake方言としてパースできること（sqlglot等での構文検証 → 可能なら `dbt compile`）。
5. **ゴールデンテスト**: 公開サンプル（openSAP の `SALES_ORDER_WORKLIST`、Join×3構成）を固定フィクスチャにし、パーサ/IRの回帰テストに使う。

---

## 12. 実装計画と工数の物差し

### 段階

| 段階 | 成果物 | 価値 |
|---|---|---|
| MVP-1 | パーサ + 棚卸し（§2）+ マッピング表テンプレ（§4） | 仕様書起こしと影響調査が自動化される。SQL生成なしでも使える |
| MVP-2 | Projection / Join / Aggregation / Union の変換 + 単純式（§5, §7）→ CTE連結SQL | 典型ビューの骨格が自動生成される |
| MVP-3 | dbtプロジェクト生成（§10）+ Rank / 変数（§5.5, §6） | dbtとしてそのまま回せる形 |
| Phase 4 | 突合モデル（§11）、未対応項目の個別対応 | 本番移行の検証基盤 |

### 工数の物差し（HANACV2SQL課金の流用）

HANACV2SQLは 1 credit = $0.50、ノード数帯域で 10〜40 credits（= $5〜$20）/変換。これを「そのビューの変換難易度」の代理指標として流用する:

- ノード数と expressions.csv の TODO比率から、ビューごとの人手残量を見積もる。
- 金額自体は小さい（§付録A）ため、外部SaaSを使う/使わないの判断軸は金額ではなく、**XMLを外部に出せるか**と**dbt向けの形で出るか**になる。

---

## 13. 未決事項（判断が要るもの）

| # | 論点 | 既定（推し） | 影響 |
|---|---|---|---|
| 1 | 入力パラメータの扱い | dbt `var()` コンパイル時置換（§6.1） | 運用でパラメータを変える頻度が高いと不便 |
| 2 | カラム名の正規化 | stagingまでは着地名を維持し、martでも原則維持（トレーサビリティ優先）。snake_case化はマッピング表の `target_column` で個別に | 全面snake化するとBW側との照合がしづらくなる |
| 3 | クライアント列 | stagingで固定値フィルタ（§6.3） | 着地に列が無い場合は自動的に無効 |
| 4 | ノード粒度の閾値 | §10.2（10ノード以下はCTE） | 大規模ビューでのモデル数爆発とのバランス |
| 5 | 通貨換算・階層の範囲 | 初回対象ビューに含まれる場合のみ設計する（§9） | 含まれる場合はMVP-2の前に個別設計が要る |
| 6 | 最終モデルのマテリアライゼーション | 既定は view。突合と運用を見て table/incremental を個別判断 | BWのADSO相当を物理化するかどうか |

---

## 付録A: HANACV2SQL 調査メモ（2026-11-20確認）

- 形態: XMLアップロード型SaaS。HANA Studioで「Display View XML」して保存 → アップロード → 標準SQL/PySpark + マッピングメタデータを生成 → Mapping Engineでテーブル/カラム名を付け替え、対象プラットフォーム方言で再生成、の2段構成。
- 課金: クレジット制・サブスクなし。**1 credit = $0.50**。消費はノード数帯域: 1–20ノード=10 credits（$5）、20–40=20（$10）、40–60=30（$15）、60超=40（$20）。Free枠は1日5変換まで・1–10ノードのみ。購入クレジットは無期限。Enterpriseは個別見積もり（オンプレ/VM配置、無制限変換）。
  - 注意: 別ページに「1 Credit = 1 HANA CV Conversion」という矛盾する表記がある。金額の正はPricingページと購入画面で確認した $0.50/credit。
- 対象: Graphical Calculation View専用。Procedures / Table Functionsは対象外。
- 向こうが明記する限界: **Input parameters / Hierarchies / Currency Conversion / UOM は手作業**。本仕様 §9 はこれに合わせた。
- 運営: 決済は親会社 CODESKIT 経由（Terms記載）。
- 判断: 金額は無視できる水準だが、クライアントの calculation.xml を外部に上げる可否が実質のボトルネック。使う場合も検証用の小規模ビュー等に限定すべき。

## 付録B: 参考実装・資料

- **DataHub HANAコネクタ**（OSS）: calculation view XMLからカラムレベルリネージを抽出するパーサの実装実績。対応ノード種別と「SCRIPT_BASEDはカラムリネージ対象外」という線引きを本仕様の範囲設定に流用。 https://docs.datahub.com/docs/generated/ingestion/sources/hana
- **Clariba act.in migrate**: 「Calculation Viewはグラフ構造なので、ノードごとにSQL化して組み立てる」という変換方針の公開事例。本仕様 §5 の基本方針と同じ。
- **SAP HANA Calculation View API Reference**（help.sap.com）: オブジェクト/属性の定義（logicalModel、keyMapping、measure の aggregationType など）の確認先。
- **openSAP サンプル** `SALES_ORDER_WORKLIST.hdbcalculationview`: Join×3 + logicalModel の実XML。§11 のゴールデンテスト用フィクスチャ。
- **データ型マッピング**: Informatica / BryteFlow の SAP HANA → Snowflake 対応表を §8 の目安の出典とした（着地型が正、という優先順位は本仕様独自の判断）。

## 付録C: XML要素クイックリファレンス

| 要素 / 属性 | 意味 | 変換での使い方 |
|---|---|---|
| `Calculation:scenario` | ルート | §1.1 |
| `dataSources/DataSource@id, resourceUri` | 入力実体 | §4 テーブルマッピングのキー |
| `calculationView@xsi:type` | ノード種別（ProjectionView 等） | §5 の分岐 |
| `calculationView/input@node` | 入力参照（dataSource or ノード） | DAG構築 |
| `input/mapping@target, @source` | カラム対応 | リネージ + select生成 |
| `viewAttributes/viewAttribute@id` | ノード出力カラム | 出力順の固定 |
| `calculatedViewAttributes/…/formula` | 計算列の式 | §7 |
| `filter/expression` | ノードのフィルタ式 | where化 |
| `joinAttribute@name` | 結合キー | §5.2 |
| `logicalModel@id` | 最上位ノード名 | 最終モデルの元 |
| `logicalModel/attributes/attribute@key, keyMapping` | 属性とキー | 最終select + テスト候補 |
| `logicalModel/baseMeasures/measure@aggregationType, measureMapping` | メジャーと既定集計 | §5.6（再集計しない点に注意） |
| `localVariables/variable` | 変数/入力パラメータ | §6 |
| `variableMappings` | 子ビューへのパラメータ渡し | §6.1 |
