# ジオコーディング仕様・アルゴリズム（`hokenshisetsu_geocode_YYYYMM.csv.gz`）

本ドキュメントは、年 1 回（1 月版、および任意月の手動実行）で生成する別添座標アセット `hokenshisetsu_geocode_YYYYMM.csv.gz` の処理アルゴリズム・正規化規則・妥当性検証・制約事項をまとめたものです。

---

## 1. 基本方針

1. **Git 差分用 CSV と分離（案B）**：
   - 住所辞書の更新や小数点以下の座標変動で `data/public/by_pref/*.csv` の Git Diff が埋まらないよう、座標は `release/<snapshot>/hokenshisetsu_geocode_<snapshot>.csv.gz` として分離配布します。
   - `medical_code_10digit`（10 桁医科医療機関コード）で公開全国 CSV および都道府県別 CSV と 1 対 1 で結合できます。
2. **ローカル・政府オープンデータ系辞書の利用**：
   - 商用 API（再配布制限のあるサービス）は使用せず、Python パッケージ `jageocoder`（`2.2.x`）およびローカル辞書（`jukyo_all_v22.20260417`）をオフライン実行して座標を付与します。
3. **元データの保持**：
   - 原本由来の `address` 列自体は書き換えず、検索用クエリ候補のみを段階的に生成して照合します。

---

## 2. 照合アルゴリズム（[geocode_nationwide.py](subprojects/hokenshisetsu/scripts/geocode_nationwide.py)）

各施設レコードの `(medical_code_10digit, kouseikyoku_code, pref_name, municipality_name, address)` を入力とし、以下の手順で座標を付与します。

### 2-1. 検索候補の生成順序（`build_query_candidates`）

まずベースライン候補で検索し、**住居番号レベル（`level == 8`）で整合一致した場合は以降の候補生成・再検索をスキップ**します。`level < 8` または未一致の場合のみ、以下の順で正規化候補を生成・検索し、整合性チェックを通過した中で **最も詳細な階層（`level` 最大）** の結果を採用します。

1. **ベースライン候補**：`address` を NFKC 正規化・前後空白除去し、`pref_name` で始まっていない場合のみ先頭に `pref_name` を補完。
2. **空白除去**：住所途中の全空白を除去（例：`熊本市 東区保田窪5-10-23` → `熊本県熊本市東区保田窪5-10-23`）。
3. **自治体名表記ゆれ補正**：公的自治体名の異体字等を正規化（例：`塩釜市` → `塩竈市`、`龍ヶ崎市` → `龍ケ崎市`、`鎌ヶ谷市` → `鎌ケ谷市`、`袖ヶ浦市` → `袖ケ浦市`、`桧原村` → `檜原村`、`梼原町` → `檮原町`、`須惠町` → `須恵町`）。
4. **根拠付き・スコープ限定の外字（PUA）変換**（[pua_mapping.json](subprojects/hokenshisetsu/scripts/pua_mapping.json)）：
   - Unicode 私用領域文字（`category == "Co"`）を含む場合、**`review_status == "verified"` かつ `source_scope`（`kouseikyoku_code`）が一致する登録済み文字のみ** を検索用候補へ置換します。
   - 未同定の PUA や異なる `source_scope` の PUA は推測変換・削除を行いません。
5. **長音・ダッシュ統一**：全角ハイフン・長音記号（`ー`, `－`, `―`, `−` 等）を半角 `-` に統一。
6. **京都通り名からの末尾町名抽出**（`extract_kyoto_formal_candidate`）：
   - `pref_name == "京都府"` かつ `municipality_name` が `京都市` で始まる場合のみ、最後の方向語（`上る` / `下る` / `上ル` / `下ル` / `東入る` / `西入る` / `東入` / `西入`）の直後に「町名＋数字（番地）」が続く場合に限り、`京都府<municipality_name><町名><番地...>` を候補に追加します。
   - 番地を伴わない通り名のみの住所（例：`京都市上京区丸太町通大宮東入`）は曖昧性回避のため抽出を行わず、市区町村レベルに留めます。
7. **先頭建物名等の除去**：`municipality_name` が住所先頭より後ろに現れる場合（例：`レインボーテラス内 長野市大字吉1827－1`）、`municipality_name` 以降を切り出した候補を追加します。

### 2-2. 座標フォールバックと自治体整合性チェック

- **座標欠損ノードの親ノード参照（`resolve_node_with_coords`）**：
  - `jageocoder` が座標未登録の番兵値（`999.9`）を返した場合は、`node.parent` を遡って有効な経緯度（`|x| <= 180`, `|y| <= 90`）を持つ最も詳細な祖先ノードを採用し、`geocode_level` もその祖先ノードの階層に合わせます。
- **自治体整合性チェック（`is_consistent_match`）**：
  - 一致ノードの `get_fullname()` を検証し、`pref_name` が不一致の場合、または `level >= 3` で `municipality_name`（`釜`/`竈`、`ヶ`/`ケ`、`惠`/`恵`、離島表記 `三宅島三宅村`→`三宅村`・`八丈島八丈町`→`八丈町` を正規化）が一致結果に含まれない場合は誤照合とみなして棄却します。

---

## 3. 出力列と精度区分（`geocode_level`）

| `geocode_level` | `geocode_level_code` | 対応する住所階層 |
|---|---:|---|
| `house_number` | `8` | 住居番号・号レベル |
| `gaiku` | `7` | 街区・番レベル |
| `aza` | `6` | 小字・丁目レベル |
| `oaza_cho` | `5` | 大字・町丁目レベル |
| `municipality` | `3` / `4` | 市区町村・政令市の区代表点 |
| `prefecture` | `1` / `2` | 都道府県・郡代表点 |
| `unmatched` | 空欄 | 有効な整合一致なし（`latitude` / `longitude` は空欄） |

全列の定義は [SCHEMA.md](SCHEMA.md) を参照してください。

---

## 4. 鮮度・チェックサム整合の仕組み（[export_public_release.py](subprojects/hokenshisetsu/scripts/export_public_release.py)）

- `geocode_nationwide.py` は入力データ（`medical_code_10digit`, `pref_name`, `municipality_name`, `kouseikyoku_code`, `address`）の SHA-256 署名（`input_signature`）、`rules_version`、出力ファイルの SHA-256 を `subprojects/hokenshisetsu/output/geocode_meta_<snapshot>.json` に保存します。
- `export_public_release.py` の再実行時、既存の `hokenshisetsu_geocode_<snapshot>.csv.gz` が現在の公開データと整合していれば実ファイルから再計算して `SHA256SUMS.txt` に含め、元データのコード・住所が更新されて陳腐化していた場合は古い座標ファイルを自動除外・削除します。

---

## 5. 実績値（2026年11月スナップショット・全 97,514 件）

- 辞書：`jukyo_all_v22.20260417`（`rules_version: 2026-11-18.g3`）
- 街区以上（`house_number` + `gaiku`）：**93,171 件（95.5%）**
  - `house_number`: 57,668 件 / `gaiku`: 35,503 件 / `aza`: 1,076 件 / `oaza_cho`: 3,039 件 / `municipality`: 228 件 / `prefecture`・`unmatched`: 0 件

## 6. 注意事項・限界

- `geocode_level` は**住所辞書と一致した階層**を示すものであり、医療機関の建物玄関等の実測位置を保証するものではありません。
- 研究・分析用途に応じて、`geocode_level`（例：`house_number` / `gaiku` のみ、または `oaza_cho` 以上）でフィルタリングして利用することを推奨します。
- `municipality` 止まりのレコードには、原本側の大字省略（地番直結）、辞書更新後の新町名・住居表示変更、番地なし通り名、辞書未収録の大字等が含まれます。
