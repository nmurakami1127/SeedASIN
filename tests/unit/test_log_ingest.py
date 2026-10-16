"""tool_intake.log_ingest.parse_log_html()の単体テスト。

フィクスチャHTML（_SAMPLE_HTML）は、ユーザーが実際に共有した統合ツールログ画面
（AP2725アカウント）から抜粋した9パターンをそのまま使用する（2026-09-06提供）。
属性・クラス名・入れ子構造は原文のまま。
"""
from tool_intake.log_ingest import parse_log_html

_SAMPLE_HTML = r"""
<table class="table">
  <thead>
    <tr>
      <th scope="col">検索URL</th>
      <th scope="col">検索種別</th>
      <th scope="col">ID</th>
      <th scope="col">CSV名</th>
      <th scope="col">ステータス</th>
      <th scope="col">メモ</th>
      <th scope="col">取得日時</th>
      <th scope="col">DL</th>
      <th scope="col">再登録</th>
      <th scope="col">削除</th>
    </tr>
  </thead>
  <tbody>

    <!-- パターン1 ASINリスト・輸出_XXX・待機中（CSV未生成） -->
    <tr id="AP2725_latraviata.imports.jp@gmail.com_8e6dd15387523d802068e0b162e95218d79fe95065280e17470003999eedeb48.csv"
        data-type="asin_list" data-text="asin" data-country="jp" data-category=""
        data-repeat="1" data-server=""
        data-seq="7141449">
      <td class="log-url"><img src="/img/no_url.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">ASINリスト</td>
      <td class="log-id">7141449</td>
      <td class="log-csv">-</td>
      <td class="log-status">待機中</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸出_Asbiaxb</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">-</td>
      <td class="log-download"><button class="btn btn-secondary download" disabled>DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7141449">削除</button></td>
    </tr>

    <!-- パターン2 キーワード検索・プレフィックス無し・待機中（CSV未生成）、data-country=jp -->
    <tr id="AP2725_latraviata.imports.jp@gmail.com_3d150bbc29a04f9a29bb29076d07f4b9385c54d8b2f14b6b8176ddb444b02785.csv"
        data-type="keyword" data-text="カメラケース" data-country="jp"
        data-category="2221077051,2127357051,3210981,3371421,16462091"
        data-repeat="1" data-server=""
        data-seq="7141017">
      <td class="log-url"><img src="/img/jp.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">キーワード</td>
      <td class="log-id">7141017</td>
      <td class="log-csv">-</td>
      <td class="log-status">待機中</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">カメラケース</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">-</td>
      <td class="log-download"><button class="btn btn-secondary download" disabled>DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7141017">削除</button></td>
    </tr>

    <!-- パターン3 キーワード検索・プレフィックス無し・data-country=us -->
    <tr id="AP2725_latraviata.imports.jp@gmail.com_5f3ff3545ad89ce4725aeefd6d4b1979d725c8d387d12c7849900e4b00a152c8.csv"
        data-type="keyword" data-text="thimble" data-country="us"
        data-category="2617941011,8090710011,12897221,2237329011,12899091"
        data-repeat="1" data-server=""
        data-seq="7141007">
      <td class="log-url"><img src="/img/us.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">キーワード</td>
      <td class="log-id">7141007</td>
      <td class="log-csv">-</td>
      <td class="log-status">待機中</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">thimble</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">-</td>
      <td class="log-download"><button class="btn btn-secondary download" disabled>DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7141007">削除</button></td>
    </tr>

    <!-- パターン4 キーワード検索・取得完了・CSVあり -->
    <tr id="7137846_latraviata.imports.jp@gmail.com_2026-09-06 14:15:55.csv"
        data-type="keyword" data-text="ゲーミングマウス" data-country="jp"
        data-category="637394,2127209051,2151826051,2151970051,3210981"
        data-repeat="1" data-server="xs142812"
        data-seq="7137846">
      <td class="log-url"><img src="/img/jp.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">キーワード</td>
      <td class="log-id">7137846</td>
      <td class="log-csv">7137846_latraviata.imports.jp@gmail.com_2026-09-06 14:15:55.csv</td>
      <td class="log-status">取得完了</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">ゲーミングマウス</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-09-06 14:15:55</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7137846">削除</button></td>
    </tr>

    <!-- パターン5 ストアフロント・輸入_XXX・取得完了・CSVあり -->
    <tr id="7134234_latraviata.imports.jp@gmail.com_2026-09-05 11:20:20.csv"
        data-type="store_url" data-text="https://www.amazon.co.jp/s?me=A37GRCD0WZ9DP3"
        data-country="jp" data-category="" data-repeat="1" data-server="tealwolf96"
        data-seq="7134234">
      <td class="log-url">
        <a href="https://www.amazon.co.jp/s?me=A37GRCD0WZ9DP3" target="_blank">
          <img src="/img/jp.png">
        </a>
        <br><span class="options-open">設定▼</span>
      </td>
      <td class="log-type">ストアフロント</td>
      <td class="log-id">7134234</td>
      <td class="log-csv">7134234_latraviata.imports.jp@gmail.com_2026-09-05 11:20:20.csv</td>
      <td class="log-status">取得完了</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸入_A37GRCD0WZ9DP3</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-09-05 11:20:20</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7134234">削除</button></td>
    </tr>

    <!-- パターン6 禁止セラー（ステータス=禁止セラー、CSV=-、idが"none"の実例あり） -->
    <tr id="none" data-type="store_url"
        data-text="https://www.amazon.com/s?me=A1JC0VIER13CQ4&marketplaceID=ATVPDKIKX0DER"
        data-country="us" data-category="" data-repeat="1" data-server="navyfawn50"
        data-seq="7119178">
      <td class="log-url">
        <a href="https://www.amazon.com/s?me=A1JC0VIER13CQ4&marketplaceID=ATVPDKIKX0DER" target="_blank">
          <img src="/img/us.png">
        </a>
        <br><span class="options-open">設定▼</span>
      </td>
      <td class="log-type">ストアフロント</td>
      <td class="log-id">7119178</td>
      <td class="log-csv">-</td>
      <td class="log-status">禁止セラー</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸入_A1JC0VIER13CQ4</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-08-29 20:51:26</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7119178">削除</button></td>
    </tr>

    <!-- パターン7 ASINリスト・輸出_ブランド名（日本語・記号入り）・取得完了 -->
    <tr id="7122745_latraviata.imports.jp@gmail.com_2026-08-31 08:01:42.csv"
        data-type="asin_list" data-text="asin" data-country="jp" data-category=""
        data-repeat="1" data-server="xs055341"
        data-seq="7122745">
      <td class="log-url"><img src="/img/no_url.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">ASINリスト</td>
      <td class="log-id">7122745</td>
      <td class="log-csv">7122745_latraviata.imports.jp@gmail.com_2026-08-31 08:01:42.csv</td>
      <td class="log-status">取得完了</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸出_ウィンターチェリー_okamoto_Keia＋</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-08-31 08:01:42</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7122745">削除</button></td>
    </tr>

    <!-- パターン8 ASINリスト・輸入_プレフィックスだが直後にアンダースコア無し（誤記実例）-->
    <tr id="7104425_latraviata.imports.jp@gmail.com_2026-08-26 23:56:54.csv"
        data-type="store_url"
        data-text="https://www.amazon.co.jp/s?me=A1VX7O8D3UFTPU"
        data-country="jp" data-category="" data-repeat="1" data-server="tealwolf96"
        data-seq="7104425">
      <td class="log-url">
        <a href="https://www.amazon.co.jp/s?me=A1VX7O8D3UFTPU" target="_blank">
          <img src="/img/jp.png">
        </a>
        <br><span class="options-open">設定▼</span>
      </td>
      <td class="log-type">ストアフロント</td>
      <td class="log-id">7104425</td>
      <td class="log-csv">7104425_latraviata.imports.jp@gmail.com_2026-08-26 23:56:54.csv</td>
      <td class="log-status">取得完了</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸入A1VX7O8D3UFTPU</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-08-26 23:56:54</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7104425">削除</button></td>
    </tr>

    <!-- パターン9 ASINリスト・輸出_XXX・取得完了・全角文字入り -->
    <tr id="7115330_latraviata.imports.jp@gmail.com_2026-08-29 06:56:07.csv"
        data-type="asin_list" data-text="asin" data-country="jp" data-category=""
        data-repeat="1" data-server="tomatomule1"
        data-seq="7115330">
      <td class="log-url"><img src="/img/no_url.png"><br><span class="options-open">設定▼</span></td>
      <td class="log-type">ASINリスト</td>
      <td class="log-id">7115330</td>
      <td class="log-csv">7115330_latraviata.imports.jp@gmail.com_2026-08-29 06:56:07.csv</td>
      <td class="log-status">取得完了</td>
      <td class="log-memo">
        <textarea style="float:left;" class="memo mr-2">輸出_ＢｅｌｌＦｉｎｅ</textarea>
        <div class="status_save"></div>
        <button class="save_memo btn btn-secondary">保存</button>
      </td>
      <td class="log-time">2026-08-29 06:56:07</td>
      <td class="log-download"><button class="btn btn-secondary download">DL</button></td>
      <td class="log-register"><button class="btn btn-secondary register">再登録</button></td>
      <td class="log-delete"><button class="btn btn-secondary delete 7115330">削除</button></td>
    </tr>

  </tbody>
</table>
"""


def test_parses_all_nine_patterns_with_no_errors():
    rows, errors = parse_log_html(_SAMPLE_HTML)
    assert errors == []
    assert len(rows) == 9


def _row_by_tool_run_id(rows, tool_run_id):
    return next(r for r in rows if r.tool_run_id == tool_run_id)


def test_pattern1_asin_list_export_prefix_waiting():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7141449")
    assert row.marketplace == "US"  # 輸出
    assert row.seller_id == "Asbiaxb"
    assert row.status == "待機中"
    assert row.csv_filename is None
    assert row.source == "ASIN_LIST"


def test_pattern2_keyword_no_prefix_uses_data_country_jp():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7141017")
    assert row.marketplace == "JP"
    assert row.seller_id == "カメラケース"
    assert row.csv_filename is None


def test_pattern3_keyword_no_prefix_uses_data_country_us():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7141007")
    assert row.marketplace == "US"
    assert row.seller_id == "thimble"


def test_pattern4_keyword_completed_with_csv():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7137846")
    assert row.marketplace == "JP"
    assert row.seller_id == "ゲーミングマウス"
    assert row.status == "取得完了"
    assert row.csv_filename == "7137846_latraviata.imports.jp@gmail.com_2026-09-06 14:15:55.csv"


def test_pattern5_store_url_import_prefix():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7134234")
    assert row.marketplace == "JP"  # 輸入
    assert row.seller_id == "A37GRCD0WZ9DP3"
    assert row.source == "STORE_URL"


def test_pattern6_forbidden_seller_prefix_wins_over_data_country():
    """id属性が"none"でも、data-seqで正しく識別できること。
    輸入_プレフィックスがあるため、data-country="us"であってもmarketplace=JPになる
    （ルール1の行ではdata-countryを一切参照しない）。"""
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7119178")
    assert row.marketplace == "JP"
    assert row.seller_id == "A1JC0VIER13CQ4"
    assert row.status == "禁止セラー"
    assert row.csv_filename is None


def test_pattern7_export_prefix_with_underscores_and_japanese():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7122745")
    assert row.marketplace == "US"
    assert row.seller_id == "ウィンターチェリー_okamoto_Keia＋"


def test_pattern8_import_prefix_without_underscore_typo():
    """「輸入」の直後にアンダースコアが無い実データでも正しくパースできること。"""
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7104425")
    assert row.marketplace == "JP"
    assert row.seller_id == "A1VX7O8D3UFTPU"


def test_pattern9_export_prefix_fullwidth_characters_preserved():
    rows, _ = parse_log_html(_SAMPLE_HTML)
    row = _row_by_tool_run_id(rows, "7115330")
    assert row.marketplace == "US"
    assert row.seller_id == "ＢｅｌｌＦｉｎｅ"


def test_row_without_data_seq_is_ignored():
    html = "<table><tbody><tr><td>no data-seq here</td></tr></tbody></table>"
    rows, errors = parse_log_html(html)
    assert rows == []
    assert errors == []


def test_unresolvable_country_is_a_parse_error():
    html = """
    <table><tbody>
      <tr data-seq="999" data-country="uk" data-type="keyword">
        <td class="log-status">待機中</td>
        <td class="log-csv">-</td>
        <td class="log-memo"><textarea class="memo">something</textarea></td>
      </tr>
    </tbody></table>
    """
    rows, errors = parse_log_html(html)
    assert rows == []
    assert len(errors) == 1
    assert errors[0].tool_run_id == "999"


def test_empty_seller_id_after_prefix_removal_is_a_parse_error():
    html = """
    <table><tbody>
      <tr data-seq="888" data-country="jp" data-type="keyword">
        <td class="log-status">待機中</td>
        <td class="log-csv">-</td>
        <td class="log-memo"><textarea class="memo">輸出_</textarea></td>
      </tr>
    </tbody></table>
    """
    rows, errors = parse_log_html(html)
    assert rows == []
    assert len(errors) == 1
    assert errors[0].tool_run_id == "888"
