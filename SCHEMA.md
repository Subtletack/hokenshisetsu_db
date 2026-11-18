# データスキーマ（変数一覧・定義）

本リポジトリで配布・管理している公開データセットの各列（変数）の定義です。

- **GitHub Releases 公開全国 CSV**（`hokenshisetsu_nationwide_YYYYMM_public.csv.gz`）：**62 列**（UTF-8 BOM 付き / gzip 圧縮）
- **Git 差分管理用 都道府県別 CSV**（`data/public/by_pref/*.csv`）：**58 列**（UTF-8 / LF / `medical_code_10digit` 昇順）
  - 公開全国 CSV から、毎月全行で差分が発生するメタ 4 列（`snapshot_date`, `source_file`, `source_sheet`, `seq_no`）を除外しています。
- 個人情報への配慮から、原本にある `owner_name`（開設者名）・`manager_name`（管理者名）は公開データ全体から除外しています。

---

## 1. 施設一覧データ（公開全国 CSV 62 列 / 県別 CSV 58 列）

### 1-1. 医療機関コード・出典区分

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `medical_code_10digit` | 文字列（10桁）例: `0110110210` | あり | 医科医療機関コード 10 桁（`pref_code` 2桁 + `"1"` + `medical_code_7digit` 7桁）。全行一意の主キー。 |
| `medical_code_7digit` | 文字列（7桁）例: `0110210` | あり | 医科側の 7 桁コード（`ika_main` では本体コード、`ikaheisetsu_from_shika` では併設医科コード）。 |
| `primary_code_10digit` | 文字列（10桁） | あり | 掲載元シート上の主コード 10 桁（`pref_code` + `tensu_table_code` + `code_7digit`）。`ika_main` では `medical_code_10digit` と一致、`ikaheisetsu_from_shika` では歯科コード側（点数表コード `3`）。 |
| `tensu_table_code` | `"1"` / `"3"` | あり | 掲載元シートの点数表コード（`"1"`: 医科、`"3"`: 歯科）。 |
| `code_7digit` | 文字列（7桁） | あり | 掲載元シートの主コード 7 桁（ハイフン・カンマ除去後）。 |
| `code_raw` | 文字列 例: `"01,1021,0"` | あり | 原本セルに記載されていた生のコード文字列。 |
| `paired_code_7digit` | 文字列（7桁 / 空） | あり | 原本 `c1` に記載された併設先（相手側）の 7 桁コード。 |
| `paired_code_10digit` | 文字列（10桁 / 空） | あり | `paired_code_7digit` から生成した 10 桁コード（医科本体行なら歯科側 `pref_code+"3"+...`、歯科側行なら医科側 `pref_code+"1"+...`）。 |
| `dental_code_7digit` | 文字列（7桁 / 空） | あり | 歯科併設または歯科側由来の場合の歯科 7 桁コード。 |
| `dental_code_10digit` | 文字列（10桁 / 空） | あり | 歯科併設または歯科側由来の場合の歯科 10 桁コード（`pref_code+"3"+dental_code_7digit`）。 |
| `old_gunshiku_code` | 文字列（`\|` 区切り / 空） | あり | 原本 `c1` 内の旧郡市区コード等（併設コード以外の付記番号）。 |
| `record_source_type` | `"ika_main"` / `"ikaheisetsu_from_shika"` | あり | 抽出元区分。`ika_main`: 医科一覧本体、`ikaheisetsu_from_shika`: 歯科一覧の併設情報から抽出した医科併設分。 |
| `is_ikaheisetsu_from_shika` | `True` / `False` | あり | `record_source_type == "ikaheisetsu_from_shika"` の真偽値フラグ。 |

### 1-2. 所在地・管轄（厚生局・都道府県・郵便番号・市区町村）

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `kouseikyoku_code` | 文字列 例: `01_hokkaido` | あり | 管轄地方厚生局コード（`01_hokkaido` 〜 `08_kyushu`）。 |
| `kouseikyoku_name` | 文字列 例: `北海道厚生局` | あり | 管轄地方厚生局名。 |
| `pref_code` | 文字列（2桁）例: `01` | あり | 都道府県コード（JIS X 0401：`01` 〜 `47`）。 |
| `pref_name` | 文字列 例: `北海道` | あり | 都道府県名。 |
| `postal_code_7digit` | 文字列（7桁）例: `0640807` | あり | ハイフンなし 7 桁郵便番号。 |
| `postal_code` | 文字列 例: `064-0807` | あり | ハイフン付き郵便番号（`NNN-NNNN`）。 |
| `municipality_code_5digit` | 文字列（5桁 / 空）例: `01101` | あり | 全国地方公共団体コード（上 5 桁、`utf_ken_all.zip` と住所前方一致で突合）。 |
| `municipality_name` | 文字列 例: `札幌市中央区` | あり | 市区町村名（政令指定都市は区まで含む）。 |
| `municipality_match_method` | 文字列 | あり | 市区町村突合方法（`both_agree_single_zip`, `both_agree_multi_zip_disambiguated`, `addr_prefix_jigyosho_or_unlisted_zip`, `zip_only_resolved` 等）。 |

### 1-3. 歯科併設・施設区分・稼働状況

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `is_shika_heisetsu` | `True` / `False` | あり | 歯科併設フラグ（`has_paired_code_c1` または `in_shikaheisetsu_list` が真）。 |
| `is_shika_heisetsu_or_dental_dept` | `True` / `False` | あり | `is_shika_heisetsu` または `has_dental_dept_c8`（診療科に歯科系を含む）が真。 |
| `has_paired_code_c1` | `True` / `False` | あり | 原本 `c1` に併設コードの記載があるか。 |
| `in_shikaheisetsu_list` | `True` / `False` | あり | 厚生局の歯科併設一覧と突合されたか。 |
| `has_dental_dept_c8` | `True` / `False` | あり | 原本の診療科列（`c8`）に歯科・矯正歯科・小児歯科・歯科口腔外科を含むか。 |
| `facility_type` | `"病院"` / `"診療所"` / `""` | あり | 施設区分（原本記載および病床数に基づく病院・診療所の別）。 |
| `hospital_special_type` | 文字列 / 空 | あり | 代表病院機能区分（優先順位：`特定機能 > 地域支援 > 総合病院 > 一般病院等`）。 |
| `hospital_special_types` | 文字列（`\|` 区切り / 空） | あり | 該当する病院機能区分をすべて `\|` 区切りで保持。 |
| `is_tokutei_kinou_byoin` | `True` / `False` | あり | 特定機能病院フラグ。 |
| `is_chiiki_iryo_shien_byoin` | `True` / `False` | あり | 地域医療支援病院フラグ（※原本 `c9` に「地域支援」と明記された施設のみ `True` となる点に注意）。 |
| `is_sougou_byoin` | `True` / `False` | あり | 総合病院フラグ。 |
| `status` | `"現存"` / `"休止"` / `""` | あり | 稼働状況（原本記載に基づく現存・休止の別）。 |
| `has_ryoyo_byosho_note` | `True` / `False` | あり | 療養病床に関する注記が原本にあるか。 |

### 1-4. 基本情報・指定情報

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `facility_name` | 文字列 | あり | 保険医療機関名称。 |
| `address` | 文字列 | あり | 所在地住所。 |
| `phone` | 文字列 | あり | 電話番号。 |
| `designated_date` | `YYYY-MM-DD` / 空 | あり | 指定年月日。 |
| `designation_reason` | 文字列 例: `新規`, `更新` | あり | 指定事由（新規・更新・組織変更・移転等）。 |
| `valid_period_start` | `YYYY-MM-DD` / 空 | あり | 指定有効期間開始日。 |

### 1-5. 病床数・従事者数・診療科

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `beds_general` | 整数（文字列格納） | あり | 一般病床数。 |
| `beds_ryoyo` | 整数 | あり | 療養病床数。 |
| `beds_seishin` | 整数 | あり | 精神病床数。 |
| `beds_kekkaku` | 整数 | あり | 結核病床数。 |
| `beds_kansen` | 整数 | あり | 感染症病床数。 |
| `beds_total` | 整数 | あり | 病床数合計（上記 5 区分の合算）。 |
| `has_bed_entry` | `True` / `False` | あり | 原本に病床数の記載があるか。 |
| `has_staff_info` | `True` / `False` | あり | 原本に従事者数の記載があるか。 |
| `doctors_ft` | 数値 / 空 | あり | 常勤医師数（Full-Time）。 |
| `doctors_pt` | 数値 / 空 | あり | 非常勤医師数（Part-Time）。 |
| `dentists_ft` | 数値 / 空 | あり | 常勤歯科医師数。 |
| `dentists_pt` | 数値 / 空 | あり | 非常勤歯科医師数。 |
| `pharmacists_ft` | 数値 / 空 | あり | 常勤薬剤師数。 |
| `pharmacists_pt` | 数値 / 空 | あり | 非常勤薬剤師数。 |
| `staff_ft_total` | 数値 / 空 | あり | 常勤従事者数合計（医師・歯科医師・薬剤師）。 |
| `staff_pt_total` | 数値 / 空 | あり | 非常勤従事者数合計（医師・歯科医師・薬剤師）。 |
| `departments` | 文字列 例: `内 小` | あり | 診療科名一覧（原本記載）。 |

### 1-6. スナップショット・出典メタ列（公開全国 CSV のみ）

| 列名 | 型・例 | 県別CSV | 説明 |
|---|---|---|---|
| `snapshot_date` | `YYYY-MM-01` 例: `2026-11-01` | なし | スナップショット基準日（対象月の 1 日）。 |
| `source_file` | 文字列 | なし | 抽出元の厚生局 Excel ファイル名。 |
| `source_sheet` | 文字列 | なし | 抽出元のワークシート名。 |
| `seq_no` | 整数（文字列格納） | なし | 原本シート上の連番。 |

---

## 2. 集計 CSV（GitHub Releases）

### 2-1. `prefecture_summary_YYYYMM.csv`（47 都道府県別集計）

- `pref_code`, `pref_name`, `kouseikyoku_code`, `kouseikyoku_name`: 都道府県・管轄厚生局
- `total_facilities`: 施設総数（`ika_main_count + ikaheisetsu_from_shika_count`）
- `ika_main_count`, `ikaheisetsu_from_shika_count`: 抽出元区分別施設数
- `hospitals_count`, `clinics_count`: 病院数・診療所数
- `tokutei_kinou_byoin_count`, `chiiki_iryo_shien_byoin_count`, `sougou_byoin_count`: 病院機能区分別施設数
- `active_genzon_count`, `suspended_kyushi_count`: 現存施設数・休止施設数
- `shika_heisetsu_strict_count`, `shika_heisetsu_or_dental_dept_count`, `in_shikaheisetsu_list_count`, `has_dental_dept_c8_count`: 歯科併設・歯科標榜関連施設数
- `beds_general_sum`, `beds_ryoyo_sum`, `beds_seishin_sum`, `beds_kekkaku_sum`, `beds_kansen_sum`, `beds_total_sum`: 病床数合計
- `unique_municipalities_count`: 当該都道府県内で施設が存在する市区町村数
- `municipality_matched_count`: 市区町村コード突合済み施設数

### 2-2. `municipality_summary_YYYYMM.csv`（市区町村別集計）

- `pref_code`, `pref_name`, `municipality_code_5digit`, `municipality_name`: 都道府県・市区町村
- `total_facilities`, `ika_main_count`, `ikaheisetsu_from_shika_count`, `hospitals_count`, `clinics_count`, `tokutei_kinou_byoin_count`, `chiiki_iryo_shien_byoin_count`, `sougou_byoin_count`, `active_genzon_count`, `suspended_kyushi_count`, `shika_heisetsu_strict_count`, `shika_heisetsu_or_dental_dept_count`, `beds_total_sum`: 都道府県別集計と同定義の市区町村単位集計値

---

## 3. コード変更追跡 CSV（`data/public/transitions/`）

前月以前のスナップショットが存在する場合、廃止されたコードと新規出現したコードのうち、正規化後（NFKC・ハイフン統一・空白除去）の `(facility_name, address)` が 1 対 1 で完全一致する施設を同一施設のコード変更として記録します。

### 3-1. `YYYYMM_code_changes.csv`・`facility_code_history.csv`

| 列名 | 説明 |
|---|---|
| `root_code` | 多段変更（A → B → C）を遡った最初の 10 桁コード（A）。 |
| `prev_snapshot` | 変更前コードが最後に確認された年月（`YYYYMM`）。 |
| `curr_snapshot` | 新コードが出現した年月（`YYYYMM`）。 |
| `old_medical_code_10digit` | 変更前の 10 桁医療機関コード。 |
| `new_medical_code_10digit` | 変更後の 10 桁医療機関コード。 |
| `pref_code`, `pref_name`, `facility_name`, `address`, `phone` | 変更後スナップショット時点の施設基本情報。 |
| `old_designated_date`, `new_designated_date` | 変更前・変更後の指定年月日。 |
| `new_designation_reason` | 変更後の指定事由（例：開設者変更、移転等）。 |
| `match_rule` | 照合ルール識別子（`exact_norm_name_and_address`）。 |

### 3-2. `removed_pool.csv`（未照合の廃止コードプール）

休止・再指定などで 1 か月以上空いて再出現した場合に備えて廃止コードを保持するテーブルです。

- `last_seen_snapshot`: 最後に確認された年月（`YYYYMM`）
- `removed_at_snapshot`: 消失を検出した年月（`YYYYMM`）
- `matched_in_snapshot`: 後続月で再出現・照合された年月（未照合の間は空欄）
- `medical_code_10digit`, `pref_code`, `pref_name`, `facility_name`, `address`, `phone`, `designated_date`: 消失前時点の施設情報
