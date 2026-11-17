# 全国保険医療機関データベース（自動取得・統合パイプライン）

厚生労働省の全国8地方厚生局が公表している「コード内容別医療機関一覧表（医科・医科併設・歯科併設）」および日本郵便の郵便番号データ（`utf_ken_all.zip`）を取得・統合し、47都道府県・全保険医療機関の機械可読データセットを構築・検証するパイプラインです。

## 配布データ（GitHub Releases）

データファイル本体はリポジトリの Git 履歴には含めず、各スナップショット（`YYYYMM`）ごとに **GitHub Releases** のアセットとして配布します。

- `hokenshisetsu_nationwide_YYYYMM_public.csv.gz`
  - 全国施設一覧（gzip 圧縮 UTF-8 BOM 付き CSV）。`pandas.read_csv(..., compression="gzip")` でそのまま読み込めます。
  - **個人情報への配慮**：個人開業医等の氏名を含み得る `owner_name`（開設者名）・`manager_name`（管理者名）列は公開版から除外しています。
- `prefecture_summary_YYYYMM.csv`
  - 都道府県別の施設数・病床数・病院区分別集計（47都道府県）。
- `municipality_summary_YYYYMM.csv`
  - 市区町村別の施設数・病床数・病院区分別集計。
- `build_qa_report_YYYYMM.json`
  - 構築時の件数・突合率・基準月検証結果（`snapshot_verification`）を含む QA レポート。
- `SHA256SUMS.txt`
  - 各アセットの SHA-256 チェックサム。

## 主なデータ仕様・注意事項

- **対象範囲**：全国8地方厚生局の「コード内容別医療機関一覧表」に掲載された医科病院・医科診療所（歯科併設側の医科併設分を含む）。
- **病院区分フラグ**：
  - `is_tokutei_kinou_byoin`（特定機能病院）
  - `is_chiiki_iryo_shien_byoin`（地域医療支援病院）
    - **注意**：本列は地方厚生局原本の `c9`（特記事項等）に「地域支援」と記載されている施設（2026年11月版では21都道府県・173施設のみ、26府県は0件）を示すフラグであり、全国の地域医療支援病院（約736施設）を網羅するフラグではありません。
  - `is_sougou_byoin`（総合病院）
  - 複数該当する場合は `hospital_special_types` に `|` 区切りで全値を保持し、`hospital_special_type` は優先順位（`特定機能 > 地域支援 > 総合病院 > 一般病院等`）で代表1値を格納します。
- **スナップショット整合保証**：
  - 取得時に8厚生局・必須ロール（全22対象）の網羅性と対象年月を確認し、構築時に全ワークシート先頭ヘッダーの基準月一致を検証します。

## ローカルでの再現・実行方法

必要パッケージ（Python 3.9+ / 3.12 動作確認済み）：

```bash
pip install -r requirements.txt
```

取得・構築・検証・公開用アセット出力を一括実行する場合（例：`202611`）：

```bash
python3 subprojects/hokenshisetsu/scripts/download_raw.py --snapshot 202611 --mode all --download-japanpost
python3 subprojects/hokenshisetsu/scripts/build_nationwide.py --snapshot 202611
python3 subprojects/hokenshisetsu/scripts/verify_nationwide.py --snapshot 202611
python3 subprojects/hokenshisetsu/scripts/export_public_release.py --snapshot 202611
```

出力先は `release/YYYYMM/` に作成されます。

## 出典・利用上の注意

- 本データは厚生労働省 各地方厚生局が公表している「保険医療機関・保険薬局の指定一覧（コード内容別医療機関一覧表）」および日本郵便株式会社「郵便番号データ（`utf_ken_all.zip`）」をもとに機械的に整形・統合したものです。
- 二次利用にあたっては、各地方厚生局・厚生労働省および日本郵便のウェブサイトに掲載されている利用規約・著作権表示をご確認ください。
- 原資料の表記ゆれ・PDF/Excel 由来の制約により、施設属性や郵便番号の突合に誤差が含まれる場合があります。重要な集計・意思決定には原資料での確認を推奨します。
