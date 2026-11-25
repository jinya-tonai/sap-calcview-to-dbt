# 課題管理

生成器が自動生成した課題一覧。手で編集せず、原因側（mapping.csv / プロファイル / 変換辞書 / 変換器）を直して
再生成すること。対応が終わった課題は再生成で消える。

## サマリー

- 対象ビュー: 2
- 課題のあるビュー: 2
- 課題総数: 8

| 区分 | 件数 |
|---|---|
| マッピング未対応 | 7 |
| Restricted measure | 1 |

## ビュー別

### bw.repro::CV_SALES_BY_ORG

- [ ] [マッピング未対応] SD.VBAK: data source not in mapping table; placeholder table name used （参照: inventory/CV_SALES_BY_ORG/inventory/datasources.csv）
- [ ] [マッピング未対応] SD.VBAP: data source not in mapping table; placeholder table name used （参照: inventory/CV_SALES_BY_ORG/inventory/datasources.csv）
- [ ] [マッピング未対応] SD.LEGACY: data source not in mapping table; placeholder table name used （参照: inventory/CV_SALES_BY_ORG/inventory/datasources.csv）
- [ ] [Restricted measure] **Restricted measure** (logicalModel.GROSS_BW_ONLY): Filtered measure; manual implementation (spec 9) （参照: inventory/CV_SALES_BY_ORG/inventory/unsupported.md）

### sap.hana.democontent.epm.models::SALES_ORDER_WORKLIST

- [ ] [マッピング未対応] SO.Header: data source not in mapping table; placeholder table name used （参照: inventory/SALES_ORDER_WORKLIST/inventory/datasources.csv）
- [ ] [マッピング未対応] SO.Item: data source not in mapping table; placeholder table name used （参照: inventory/SALES_ORDER_WORKLIST/inventory/datasources.csv）
- [ ] [マッピング未対応] sap.hana.democontent.epm.models::PROD: data source not in mapping table; placeholder table name used （参照: inventory/SALES_ORDER_WORKLIST/inventory/datasources.csv）
- [ ] [マッピング未対応] sap.hana.democontent.epm.models::BUYER: data source not in mapping table; placeholder table name used （参照: inventory/SALES_ORDER_WORKLIST/inventory/datasources.csv）
