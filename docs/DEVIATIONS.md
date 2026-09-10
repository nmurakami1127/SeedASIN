# 実装時の逸脱・補足事項ログ

正本（要件定義書v1.3／基本設計書v1.1／詳細設計書v1.1）および`docs/SeedASIN_Claude_Code実装指示書.md`の記載から、実装上の理由で意図的に変更・具体化した点を記録する。矛盾や迷いが生じた場合は、まずこのファイルを確認すること。

## 1. `logging/` → `applog/` へのディレクトリリネーム（Phase 0, 2026-09-01）

実装指示書2章のディレクトリ構成にある`logging/`は、Python標準ライブラリの`logging`モジュールと名前が衝突する。プロジェクトルート直下の絶対インポートでは標準ライブラリ側が隠蔽され、httpx/sqlalchemy等サードパーティが内部で行う`import logging`まで壊れる（`alembic/env.py`自体が`from logging.config import fileConfig`を使用しており、この衝突は実際に踏む）。

実装では`logging/` → **`applog/`**（`applog/logger.py`、クラス名`StructuredLogger`は変更なし）にリネーム済み。実装指示書側の該当2箇所（2章ディレクトリ構成、Phase 2 M12タスク表）にも参照注記を追記済み。他のディレクトリ名（`config`含む）は標準ライブラリと衝突しないためリネーム対象外。

## 2. RLSポリシー述語: `current_setting('app.current_marketplace')`（`missing_ok`省略）（Phase 1, 2026-09-01）

詳細設計書のサンプルSQLは`current_setting('app.current_marketplace', true)`（`missing_ok=true`）を用いているが、これは`app.current_marketplace`未設定時にNULLを返し、RLS述語が偽と評価されて**黙って0件を返す**動作になる。

CLAUDE.mdの絶対厳守ルール「RLS違反（`app.current_marketplace`未設定でのアクセス）はエラーコード`MP-11-01`でシステムエラーとして即時停止する」「RLSはアプリ層の実装ミスに依存しない二重の安全網」を満たすには、未設定時に**確実にエラーを発生させて処理を中断する**必要がある。

そのため実装では`missing_ok`を省略した`current_setting('app.current_marketplace')`を採用する。GUC未設定時にPostgreSQL側が`unrecognized configuration parameter`エラーを送出してクエリを中断するため、アプリ層のチェック漏れがあってもDBレベルで確実に停止する。`SessionFactory`側でこの例外を捕捉し`MP-11-01`としてラップする。

詳細設計書のSQLサンプルとは異なる実装だが、「即時停止」という要件本文への忠実性を優先した意図的な差分（ユーザー確認済み、2026-09-01）。

## 3. `admin_role`のGRANT範囲はMVPスコープに限定（全面実装ではない）（Phase 1, 2026-09-01）

詳細設計書11.1節は`admin_role`を「5.4節の再処理指示・退避運用等、運用担当者向け管理操作専用」と定義しているが、個別テーブルへのGRANT文までは同節に記載がない（設計書スコープ外、ユーザー確認済み）。

**`admin_role`は現時点でMVPスコープ（再処理指示のみ）に限定する**。実装指示書（Phase 0〜7）に具体的に定義されている管理操作はRETRY_EXHAUSTED→RETRY_PENDING再処理（`retry_attempt_counter`のSELECT/UPDATE）のみのため、Phase 1のGRANTはこの範囲に限定した。これは「11章の`admin_role`定義を全面実装した」わけではない点に注意。「退避運用等」の他の管理操作が後続フェーズで具体化された時点で、都度`admin_role`へのGRANTを拡張する（ロール自体の再設計は不要な形でGRANT文を分離してある）。

## 4. `admin_role`監査ログはDBテーブルではなく構造化ログで実装（永続化・改ざん防止は将来課題）（Phase 1, 2026-09-01）

詳細設計書11.2節は管理操作について個人ひもづけの監査ログ（who/when/what）を必須としているが、指示書3章のDBスキーマ一覧（全22テーブル）に監査専用テーブルは含まれていない。

MVPではDBスキーマを22テーブルのまま変更せず、`applog`の`StructuredLogger`で`event="ADMIN_RETRY_REISSUE"`として operator（SSO認証済みの個人ID）・対象marketplace・対象件数・実行日時を記録する方式とする（12章「初期実装は構造化JSON標準出力」方針と整合）。DB専用の監査テーブルは追加しない。

**TODO（将来対応必須）**: 標準出力ログはPod再起動等で容易に失われ、改ざん防止の仕組みもない。現状のstdout出力だけでは「監査ログ」としての要件を満たすには不十分になり得るため、12章の未確定事項として残っているログ収集基盤（監視ツール連携）の選定時に、admin操作ログの**永続化**と**改ざん防止**まで含めてカバーすること。

## 5. 「設定系テーブル」の内訳は6テーブル（会話中に「5種」と誤記した経緯の記録）

`app_role`の通常CRUD対象16テーブルは以下の内訳（設定系は6テーブル、5テーブルではない）。マイグレーション実装時は下記を直接転記し、抽象的な「設定系N種」という表現に頼って列挙しないこと（1テーブル漏れるとRLSは有効なのにGRANTがない状態になり、気づきにくい権限エラー障害になる）。

```
asin_master, search_context, eligibility_current, known_seller_master,
seller_check, seller_check_detail, seller_usage_result, discovery_run,   -- 8
marketplace_account_config, search_allocation_config, search_condition_config,
processing_limit_config, recheck_interval_config, retry_config,          -- 6（設定系）
retry_attempt_counter, import_run_history                                -- 2
```
合計16テーブル。

## 6. `app_role`は`kpi_snapshot`に一切のGRANTを持たない（追記専用5テーブルとは別扱い）（Phase 1, 2026-09-01）

詳細設計書v1.1 3章のDDLを入手し確認したところ、`REVOKE UPDATE, DELETE ... FROM app_role`が明記されているのは`asin_discovery_event` / `eligibility_history` / `seed_output_history` / `processing_error_log` / `import_error_log`の5テーブルのみで、`kpi_snapshot`にはこの記述が無かった。11.1節の「`kpi_report_role`: SELECT権限のみ（書き込み不可、kpi_snapshotへのINSERTのみ別途許可）」という記述と合わせると、`kpi_snapshot`への書き込みは常に`kpi_report_role`経由（US/JP単体集計・横断集計・書き込みをすべて`kpi_report_role`で実行する）であり、`app_role`はこのテーブルにSELECTもINSERTも持たない設計と判断した。

`db/table_names.py`では`APPEND_ONLY_APP_TABLES`（app_roleがSELECT/INSERTのみ、5テーブル）と`KPI_REPORT_ONLY_TABLES`（app_roleは無権限、kpi_report_roleのみSELECT+INSERT、1テーブル＝`kpi_snapshot`）を明確に分離した。以前提示した「追記専用6テーブルにapp_roleがSELECT/INSERT」という説明はこの点で不正確だったため、ここで訂正する。

## 7. BIGSERIAL主キーのシーケンスに明示的なGRANT USAGEが必要（IDENTITYではなくSERIAL）（Phase 1, 2026-09-01）

Phase 1提案時点では「サロゲートキーはレガシーSERIALではなくGENERATED ALWAYS AS IDENTITYを使う」としていたが、詳細設計書3章のDDLを確認したところ、`asin_discovery_event.discovery_event_id`・`seller_check.seller_check_id`・`seed_output_history.seed_output_id`・`processing_error_log.error_id`・`import_error_log.error_id`・`kpi_snapshot.snapshot_id`はすべて**BIGSERIAL**と明記されていたため、これに従う（詳細設計書のDDLをそのまま移植する方針を優先）。

IDENTITY列と異なりBIGSERIALは列のDEFAULTが背後の独立したシーケンスオブジェクト（`{table}_{column}_seq`という暗黙命名）への`nextval()`呼び出しであり、INSERTするロールにテーブルへのINSERT権限があってもシーケンスへの`USAGE`権限が別途無いと`permission denied for sequence ...`で失敗する。`0004_grant_privileges.py`で該当6テーブルのシーケンスに`GRANT USAGE`を追加した（`kpi_snapshot`用は`kpi_report_role`へ、他5つは`app_role`へ）。

## 8. `alembic.ini`はASCII限定（日本語コメントを書かない）

`alembic.ini`はPythonのconfigparserが`encoding="locale"`で読み込むため、UTF-8で保存されたファイルであっても、実行環境のロケールがUTF-8でない場合（本プロジェクトの開発機を含む、日本語版Windowsの既定コードページcp932など）は非ASCIIバイト列でデコードエラーになる（`UnicodeDecodeError`でalembicコマンドそのものが起動不能になる、実際にこの開発機で再現した）。そのため`alembic.ini`には日本語コメントを書かないこと。説明が必要な場合は`alembic/env.py`や各バージョンスクリプト（通常のUTF-8ソースとして読まれるため問題ない）、またはこのファイルに書く。

## 9. 【本番デプロイ前TODO】マネージドPostgreSQLでの`BYPASSRLS`付与制約（Phase 1, 2026-09-01）

`0001_create_roles.py`の`ALTER ROLE kpi_report_role BYPASSRLS`は、これを実行する接続がスーパーユーザー権限（または少なくとも自身がBYPASSRLSを保有しCREATEROLEも持つロール）である必要がある。

ローカル開発（`docker-compose.yml`の公式`postgres:16`イメージ）では`POSTGRES_USER`（本プロジェクトでは`seedasin`）が真のスーパーユーザーになるため問題なく通る。一方、AWS RDS／Google Cloud SQL等のマネージドPostgreSQLでは、マイグレーション適用に使うマスターユーザーが真のスーパーユーザーではないことが多く（例: RDSの`rds_superuser`）、`BYPASSRLS`属性の付与がプラットフォーム側の制約で失敗する可能性がある。

**TODO（本番デプロイ計画時に必須で検証すること）**: 対象のマネージドPostgreSQL環境で、実際にマイグレーション適用ユーザーが`ALTER ROLE ... BYPASSRLS`を実行できるか事前検証する。できない場合の代替手段（プラットフォーム提供の昇格ロール経由での実行、サポートチケットでの制約解除依頼、初期セットアップのみ手動実行する運用にする等）を本番運用ドキュメントに残す必要がある。Phase 1のローカル実装・レビューをブロックするものではない。

## 10. `tests/integration/test_rls.py`の`_role_url()`が`str(URL)`でパスワードをマスクしていたバグ（Phase 1, 2026-09-02、実PostgreSQL初回実行で発覚）

WSL2/Docker Desktopのセットアップ完了後、初めて実PostgreSQL 16に対してRLS結合テストを実行したところ、全9テストが`password authentication failed for user "app_role"`で失敗した。

原因は`_role_url()`ヘルパーが`str(make_url(...).set(username=role, password=pw))`という実装になっており、SQLAlchemyの`URL.__str__()`はセキュリティ上の理由でパスワードを常に`'***'`にマスクして返す仕様だったため。つまり文字通り`'***'`という文字列をパスワードとしてPostgreSQLに送信しており、認証が失敗するのは正しい挙動だった（DB側にもアプリ側にもバグはなく、テストヘルパーのバグ）。`url.render_as_string(hide_password=False)`に修正した。

調査の過程で「Docker DesktopのWindowsポートフォワーディングのタイミング問題」という誤った仮説を立てて`time.sleep()`を追加したが、真因究明後に不要と判明し削除した。

## 11. `SessionFactory.get_session()`がSQLAlchemy 2.xのautobegin transactionを残し、呼び出し側の`with conn.begin():`を壊すバグ（Phase 1, 2026-09-02、実PostgreSQL初回実行で発覚）

上記#10の修正で実際にDB接続できるようになった結果、`db/session.py`の実装バグが判明した。`get_session()`は接続直後に`conn.execute(text("SET app.current_marketplace = ..."))`を実行するが、SQLAlchemy 2.xの`Connection`は`execute()`呼び出しで暗黙にトランザクションを開始する（autobegin）。この未commit/未rollbackのトランザクションが残ったまま`yield conn`していたため、CLAUDE.mdが要求する`with conn.begin(): ...`パターン（M8/M4/M6のトランザクション境界、2節）を呼び出し側で使うと`sqlalchemy.exc.InvalidRequestError: This connection has already initialized a SQLAlchemy Transaction() object via begin() or autobegin`で必ず失敗する状態だった。

修正: `SET`実行直後に`conn.commit()`を追加した。PostgreSQLの`SET`（`SET LOCAL`ではない）はセッション全体に効き、それを発行したトランザクションのcommit/rollbackに関わらずコネクションが閉じるまで値が保持されるため、ここでcommitしても`app.current_marketplace`の設定値は失われない。commitすることでコネクションが「トランザクションなし」の状態で呼び出し元に渡り、`with conn.begin(): ...`が正常に使えるようになる。

このバグはPhase 1のRLS結合テストが未実行だった間（WSL2導入前）は一切検出できなかった。M8/M4/M6の実装（Phase 2以降）で最初にこのパターンを使った時点で必ず踏んでいたはずのバグであり、実PostgreSQLでの検証が必要だった典型例。

## 12. カスタムGUCは`SET`後に`RESET`しても`unrecognized configuration parameter`エラーには戻らない（Phase 1, 2026-09-02、実PostgreSQL初回実行で発覚）

`test_mp_11_01_raised_when_marketplace_unset`は「`SessionFactory`が一度`SET`した後でも、途中で`RESET`されれば`RLSViolationError`になる」ことを検証しようとしていたが、実PostgreSQLで検証した結果、この前提はPostgreSQLの仕様上成立しないことが判明した（psqlで直接確認: `SET app.current_marketplace='US'; RESET app.current_marketplace; SELECT current_setting('app.current_marketplace');`は空文字列を返し、エラーにはならない）。

カスタム（プレースホルダ）GUCは一度でも`SET`されるとセッション内で「認識済みパラメータ」になり、`RESET`は値を空文字列に戻すだけで、`missing_ok`省略時の「未認識パラメータ」エラー状態には二度と戻らない。そのためRLS述語`current_setting('app.current_marketplace') = 'US'`は`'' = 'US'`でfalseとなり、クエリはエラーではなく**0件の結果を静かに返す**。

この「即時停止しない」動作は、`SessionFactory.get_session()`が呼び出しごとに必ず新規接続直後に`SET`するという実装（実運用ではセッション内で`RESET`が発生する経路が存在しない）の範囲では実害がない。真の安全網は「一度も`SET`していない生接続」のケースであり、これは`test_raw_connection_without_set_fails`で別途検証済みで、正しく`unrecognized configuration parameter`エラーになることを確認している。

**関連する発見（Phase 2着手時、複数の結合テストファイルを同一pytestセッションで実行して発覚）**: `db/session._get_engine()`はDB接続URL文字列単位でSQLAlchemyエンジン（コネクションプール）をプロセス全体で使い回す。結合テストの`_migrated_schema`fixtureはテストモジュール単位で`alembic downgrade base`→`alembic upgrade head`（DROP ROLE→CREATE ROLE含む）を繰り返すため、あるモジュールの実行中にプールに残った物理コネクション（同名だが古いOIDのロールとして認証済み）を、後続モジュールが同じURL文字列経由で再利用してしまうと、新しく作り直された同名ロールへのGRANTが効かず`permission denied`になる（実際に再現・特定済み）。テスト側`tests/integration/conftest.py`の`_migrated_schema`で、スキーマ再作成の前後に`db.session._ENGINES`の全エンジンを`dispose()`して対処した。**本番運用でDROP ROLE/CREATE ROLEを行うことは無いため実運用コードへの影響はないが、`SessionFactory`/`ReportSessionFactory`のプロセス全体エンジンキャッシュという設計自体は把握しておくこと**（同一プロセス内でDB接続URLが変わらない限りエンジンは使い回される）。

**実行順序・CI対応について（ユーザーからの確認依頼、2026-09-02）**: このバグは実行順序（ランダム化やxdist並列）とは無関係で、同一pytestプロセス内でスキーマ再作成fixtureを使うテストモジュールが2つ以上あれば、後から実行される方が必ず踏む決定的なバグである（原因は`_ENGINES`がプロセス全体でグローバル共有されていることそのもの）。現時点で`pytest-randomly`/`pytest-xdist`は未導入・CIワークフローも未整備（`pip list`で確認済み）。対処は`tests/integration/conftest.py`の`_migrated_schema`（テストインフラの共通レイヤー）に一本化してあるため、`-p no:randomly`のようなpytest実行オプションやCI設定側での追加対応は不要。今後`tests/integration/`配下に新しいテストファイルを追加しても、この`conftest.py`の`_migrated_schema`を使う限り自動的に保護される。将来xdistを導入する場合も、別プロセスのワーカーはそもそも`_ENGINES`を共有しないため、同一ワーカー内で複数の結合テストファイルが実行されるケースにのみ本fixtureの保護が効けば足りる。

**【将来のTODO】xdist導入時は本fixtureの前提を再検証すること**（ユーザー指摘、2026-09-02）。上記の「xdistのワーカーは別プロセスなので`_ENGINES`を共有せず問題にならない」という判断は、あくまで**xdist未導入の現時点での分析**であり、実際にxdistを導入した時点で必ず裏取りすること。特に確認すべき点: (1) xdistの各ワーカーが本当に独立したPythonプロセスであり続けるか（`--dist=loadscope`/`--dist=loadgroup`等の分散方式によってはテストの割り当てが変わるだけで前提自体は保たれるはずだが、xdistのバージョンや設定次第で挙動が変わり得るため要確認）、(2) 複数ワーカーが**同一のDocker PostgreSQLインスタンス**へ同時にDROP ROLE/CREATE ROLEを撃つこと自体が新たな競合（あるワーカーのalembic downgradeが別ワーカーのテスト実行中のロールを消してしまう等）を生まないか——これは`_ENGINES`のプロセス内キャッシュとは別次元の問題で、xdist導入時は結合テスト用DBインスタンスをワーカーごとに分離する（`docker compose`側でワーカーID付きのDB名/ポートを払い出す等）対応が別途必要になる可能性が高い。「xdist未導入だからCI対応不要」という現在の結論を、xdist導入時にそのまま流用しないこと。

対応（ユーザー確認済み、2026-09-02）: `test_mp_11_01_raised_when_marketplace_unset`は実際の挙動（エラーではなく空の結果集合になること）を検証するテストに書き換え、`test_reset_after_set_yields_empty_result_not_error`にリネームした。`errorctl.exceptions.RLSViolationError`への変換ロジック自体（`db/session.py`の`ProgrammingError`捕捉）は、`get_session()`の呼び出しパターン上、実質的に到達不能コードである点は将来の設計見直し時に留意すること。

**補足（コネクションプール再利用時のmarketplace漏洩リスクについて、ユーザーからの確認依頼を受けて検証、2026-09-02）**: `_get_engine()`はデフォルトのQueuePool（`pool_pre_ping=True`のみ指定）を使い、物理コネクションは複数の`get_session()`呼び出しをまたいで再利用され得る。`pool_size=1, max_overflow=0`で強制的に同一物理コネクションを使い回させた上でUS→JP→USと切り替える検証を行い、`pg_backend_pid()`で同一物理コネクション（同一PID）であることを確認しつつ、各セッションが自分のmarketplaceの行のみを見ていること（漏洩なし）を確認した。

これは`get_session()`が物理コネクションの新規/再利用を問わず、呼び出しのたびに必ず`SET app.current_marketplace`を最初に実行してから`conn`を呼び出し元へ渡すため、「前回のmarketplace設定を引きずったままアプリコードが触れる」窓が存在しないことによる。**漏洩防止は「毎回無条件SET」が担う別の防御層であり、「未設定時にエラーで即時停止」（MP-11-01、一度もSETされていない生コネクションでのみ発火）はそれとは独立した安全網である**。プーリングはMP-11-01の発火条件を狭める（一度使われたコネクションでは二度と発火しない）が、漏洩防止そのものには影響しない。

## 13. Phase 3 DBマイグレーション: 新しいマイグレーションファイルを追加せず`db/models.py`/`db/table_names.py`の編集のみで対応（Phase 3, 2026-09-02）

2026-09-02にプロジェクトスコープが大きく再確定され（`docs/SeedASIN_スコープ整理_20260902.md`、`docs/phase3_implementation_guide.md`）、`known_seller_master`への列追加、`eligibility_current`/`eligibility_history`/`seller_check`/`seller_check_detail`の2段階廃止、6テーブルの新設が必要になった。

Phase 1の`0002_create_tables.py`は`metadata.create_all()`、`0003_enable_rls.py`/`0004_grant_privileges.py`は`db/table_names.py`のタプルを**実行時に**読んで処理する設計（テーブル定義の二重管理を避けるための意図的な設計、0002のdocstring参照）。本番デプロイ済みのDBがまだ存在しない現時点では、この特性を活かし、**新しいマイグレーションファイル（0005等）を追加せず、`db/models.py`と`db/table_names.py`を直接編集**した。`alembic downgrade base → upgrade head`で再生するだけで新スキーマ（28テーブル）が完成する。

**【本番デプロイ前TODO】**: この運用は「本番DBがまだ存在しない」ことに依存する。一度でも本番デプロイし実データが入ったDBが存在するようになったら、この方式は使えなくなる（`db/models.py`を直接書き換えて再生する方式は既存データを破壊する）。以降のスキーマ変更は、個別の`op.add_column`/`op.create_table`等を持つ独立したマイグレーションファイルとして追加する運用に切り替える必要がある（ユーザー確認済み、2026-09-02）。

**2段階廃止の実装方法**: `eligibility_current`/`eligibility_history`/`seller_check`/`seller_check_detail`を`db/table_names.py`の`NORMAL_CRUD_TABLES`/`APPEND_ONLY_APP_TABLES`から外し、新設した`DEPRECATED_TABLES`タプルへ移動した。ただし`ALL_TABLES`には含め続け、RLSは維持する（「全テーブルでRLS必須」というCLAUDE.md 2節の原則を崩さないため）。GRANT対象リストから外れることで`app_role`は自動的に無権限になり、明示的な`REVOKE`文は不要だった（実PostgreSQLで検証: 4テーブルとも`permission denied`を確認）。テーブル構造・データ自体は残しており、正式な`DROP TABLE`は別フェーズで改めて判断する。

**旧M1アーキテクチャ（`search_context`, `discovery_run`, `asin_discovery_event`, `search_allocation_config`, `search_condition_config`）は今回は一切手を加えていない**。`docs/phase3_implementation_guide.md`に廃止の明記が無いため、ユーザー確認の上、現時点では触らない方針とした（asin_collector実装時に実際の要否を判断する）。

**指示書のCREATE TABLE例からの調整点**:
- 列の型を指示書の`TEXT`から既存21テーブルの規約（`marketplace=CHAR(2)`, `asin=String(20)`, `seller_id=String(40)`）に合わせた
- `collector_asin`に`marketplace`列が指示書のSQL例には無かったが、RLS適用に必須のため追加した（指示書側の記載漏れと判断）
- 主キーは指示書の指定通り`GENERATED ALWAYS AS IDENTITY`を採用（Phase 1のBIGSERIALから意図的に変更）

新設6テーブルの`app_role`向け分類（指示書に明記が無いため独自に判断、ユーザー確認済み）: 通常CRUD＝`tool_execution_link`, `seller_attribution`, `collector_run`（進行中に`status`等が更新されるため）。追記専用＝`tool_output_asin`, `weekly_listing_snapshot`, `collector_asin`（新CLAUDE.md 3節の指定通り）。

## 14. `GENERATED ALWAYS AS IDENTITY`はBIGSERIALと異なりシーケンスへの明示的な`GRANT USAGE`が不要（Phase 3, 2026-09-02、実PostgreSQLで検証）

\#7でBIGSERIAL主キーには`GRANT USAGE ON SEQUENCE ... TO app_role`が別途必要と判明していたため、Phase 3新設テーブル（`GENERATED ALWAYS AS IDENTITY`採用）にも同様の対応が必要と想定し、いったん実装した。しかし実PostgreSQLで検証したところ、**IDENTITY列は非所有者ロールでもシーケンスへのUSAGE権限なしでINSERT可能**であることが判明した（`REVOKE USAGE ON SEQUENCE collector_run_id_seq FROM app_role`を実行し、`\dp`でACLからapp_roleが消えたことを確認した上で、`app_role`としてのINSERTが成功することを確認した）。

PostgreSQLの内部実装として、IDENTITY列の裏側のシーケンスは列に紐づく形で所有され、INSERT時の値生成はテーブル自体のINSERT権限で評価される（BIGSERIALの明示的なシーケンスオブジェクトとは権限モデルが異なる）と考えられる。そのため、いったん追加した`IDENTITY_SEQUENCES`辞書と対応するGRANT/REVOKEコード（`0004_grant_privileges.py`）は削除し、`BIGSERIAL_SEQUENCES`のみを残した。**新しくBIGSERIAL主キーのテーブルを追加する場合は#7の対応が必要だが、IDENTITY主キーのテーブルには不要**という点を、次にテーブルを追加する際に混同しないよう記録しておく。

## 15. `known_seller_master.last_seen_at`と`latest_checked_at`の使い分け（Phase 3, 2026-09-03、seller_ledger方針提示時にユーザー指摘）

`seller_ledger`の方針提示時、`SellerService.register()`が`ON CONFLICT DO UPDATE SET last_seen_at = ...`とする案を出したところ、`last_seen_at`はPhase 1から既に存在する既存列であり、Phase 3で追加した`latest_checked_at`（2.1節）と意味が重複しないか確認された。

整理した結果、以下の理由で**両列とも残し、書き込み元を明確に分離する**：

- `last_seen_at`（Phase 1から存在）: `seller_ledger.SellerService.register()`が更新する。「人がこのセラーを（再）発見し、台帳に登録/再登録した日時」を表す（`docs/SeedASIN_スコープ整理_20260902.md`のフロー⑤〜⑥に対応するイベント）。
- `latest_product_count`/`latest_checked_at`（Phase 3で新設）: `seller_ledger`では一切更新しない。導出元は`tool_output_asin`（統合ツール出力の取込結果、ASINのみでカウント情報は持たない）であり、`tool_execution_link`経由でtool_run_idからセラーへ紐づけた上で件数を集計する必要がある。この集計・反映は`tool_intake`モジュール（統合ツール出力取込、Phase 3実装順序3番目、まだ未実装）が担う想定であり、`seller_ledger`（実装順序2番目）の責務ではない（フロー⑦〜⑧に対応する別イベント）。

`SellerService.register()`は`latest_product_count`/`latest_checked_at`に一切触れないことを`tests/integration/test_seller_ledger.py::test_register_does_not_touch_latest_product_count_or_latest_checked_at`で回帰的に検証している。`tool_intake`実装時に、この2列への書き込みロジックをそちらに実装すること（本DEVIATIONSエントリを読み返してから着手すること）。

## 16. `tool_intake`実装（Phase 3, 2026-09-03）: 列名・エンコーディング判定・列数分岐の方針、および`import_run_id`採番の衝突バグ

`tool_intake`着手前にユーザーから実ファイルのヘッダー列名（統合ツール出力26列、週次出品完了リスト米国21列/日本23列）を提供いただいた。方針は以下の通り確定（コード側のdocstringにも記載）:

- **エンコーディング自動判定**: `tool_intake/csv_utils.py::decode_auto()`でutf-8-sig→cp932→utf-8の順に試す共通ヘルパーを新設し、`integration_tool_import.py`/`weekly_listing_import.py`の両方で共有する（ユーザーからの要望通り、ファイル種別が増えても使い回せる形にした）。
- **週次出品完了リストの列数**: ファイル内容からの自動判別はせず、`marketplace`引数から21列（US）/23列（JP）を決め打ちする。ただし実際のヘッダー列数が想定と食い違えばValueErrorで中断する（サイレントな誤読み込み防止。ユーザー確認済み）。
- **`tool_output_asin`の取込列**: ASIN・取得日時のみ（価格・カテゴリー等は取り込まない）。指示書2.4節の未確定事項をこの実装で確定させた。
- **SKU未設定行の扱い**: `weekly_listing_snapshot`はSKU列が空の行を「出品中でないASIN」としてスキップする（エラーではない）。`docs/SeedASIN_スコープ整理_20260902.md` 9節の「SKU列が付与されている行＝出品中のASIN」という理解に基づく。
- **`snapshot_date`**: CSV内の日時列（登録日時等）ではなく、取込バッチを実行した日付（デフォルト`date.today()`、引数で上書き可）を使う（ユーザー確認済み）。
- **統合ツール出力の空ヘッダー3列**: `column_index()`は列名一致でインデックスを探すため、空文字列の重複列が複数あっても、探している列名（`ASIN`・`取得日時`）自体が空文字列でない限り影響しない。

**実装中に発見した実バグ**: `ImportRunRecorder`が`import_run_id`を`{marketplace}-{import_type}-{yyyyMMddHHmmss}`（旧実装指示書の採番規則、秒単位まで）で生成していたところ、結合テストで同一marketplace×import_typeの取込を同一秒内に2回実行すると`import_run_history`の主キー制約に衝突することが判明した（`test_reimport_is_idempotent`が同一プロセス内で連続して`import_file()`を2回呼ぶ際に実際に再現）。テスト固有の問題ではなく、本番でも同一秒内に同種の取込が2回走れば同じ衝突が起きる設計上の欠陥だったため、4桁のランダムhexサフィックスを採番規則に追加した（`{marketplace}-{import_type}-{yyyyMMddHHmmss}-{suffix}`）。旧採番規則からの意図的な逸脱。

**関連する結合テストの設計上の注意点（今後`tool_intake`系のテストを追加する際に踏まえること）**: `tool_output_asin`/`weekly_listing_snapshot`/`import_error_log`はいずれも追記専用テーブルで`app_role`にDELETE権限が無い。さらに`import_run_history`は`import_error_log`からFK参照されるため、エラー行を記録した取込runの`import_run_history`行は（`import_run_history`自体はNORMAL_CRUD_TABLESでDELETE可能でも）`import_error_log`側の行が残っている限りFK制約でDELETEできない。そのため`tests/integration/test_tool_intake.py`は個別クリーンアップを一切行わず、`uuid4`由来の一意なASIN/tool_run_idで他テストとの衝突を避けた上で、テストDB全体をconftest.pyの`_migrated_schema`のモジュール終了時破棄に任せる設計にした。

## 17. `seller_performance`実装（Phase 3, 2026-09-03）: 責務分担の確認・新設config・初のデータシード用マイグレーション

`seller_performance`方針提示の依頼文で「`known_seller_master.latest_product_count`/`latest_checked_at`の書き込みロジックを含める」という書き方になっていたが、これは#15で確定済みの「この更新は`tool_intake`の責務」という方針と矛盾していた。ユーザーに確認したところ、依頼文の書き方が誤解を招いただけで#15の方針は変更しないことが確定した（`tool_intake/integration_tool_import.py::IntegrationToolImportService.import_file()`にこの更新ロジックを追加し、`seller_performance`側には一切持たせていない）。

**「継続的に使えるセラー」の閾値**: `docs/SeedASIN_スコープ整理_20260902.md` 6節で「直近数ヶ月・複数回」とだけ確定していた部分を、ユーザーが本フェーズで具体化した: 直近90日・異なる`snapshot_date`で2回以上。「設定変更可能にする」という条件付きで確定したため、Pythonコードへハードコードせず、新設テーブル`seller_recommendation_config(marketplace, lookback_days, min_listing_count)`（`retry_config`と同じPK=marketplace単独の設計）に持たせた。

**初めてデータシード専用のマイグレーションファイル（`0005_seed_seller_recommendation_config.py`）を追加した**: Phase 3のここまでの変更（#13）はすべて`db/models.py`/`db/table_names.py`の編集のみで新規マイグレーションファイルを追加しない方針を取ってきたが、これは「スキーマ定義の動的再生（`metadata.create_all()`・`table_names.py`の実行時読み込み）」で表現できる範囲に限られる。US/JPへのデフォルト値（90, 2）投入は**データ**であり、動的再生の対象外（テーブルは空で作られるだけ）のため、この一点だけ実際の`op.execute(INSERT ... ON CONFLICT DO NOTHING)`を持つマイグレーションとして追加した。`#13`で予告していた「本番デプロイ後は個別マイグレーションに切り替える」の考え方を、データシードという性質上ここで部分的に先取りした形になる。

**`AttributionService.attribute()`の設計**: 1本の`INSERT ... SELECT ... ON CONFLICT DO NOTHING`文で完結させ、CLAUDE.md 5節の「適切な単位でトランザクションを区切ること」を、アプリ層でのループ処理を避けPostgreSQLの単一ステートメント内スナップショット一貫性に委ねることで満たした（複数ステートメントに分割してのアプリ層ループより競合状態の余地が無い）。起点セラーの決定は`tool_output_asin`の`DISTINCT ON (marketplace, asin) ORDER BY acquired_at ASC`で「最古のtool_run経由」を採用する（旧M6の「discovered_at昇順で最古行を採用」と同じ考え方を踏襲）。同一ASINが複数セラー経由で観測された場合に最古を採るロジックは`test_uses_earliest_tool_output_row_when_asin_seen_via_multiple_sellers`で検証済み。

## 18. `asin_collector`方針確定（Phase 3, 2026-09-03）: 実行形態・郵便番号設定・CAPTCHA運用をCLAUDE.md 7節の記載から実態に合わせて修正

`asin_collector`着手前の方針整理で、CLAUDE.md 7節の原文（「Playwright起動時、郵便番号を必ず設定する処理を組み込む」「CronJobでの無人実行が前提」）が、実際に踏襲対象としている既存`collect_asins_from_search.py`の運用実態と食い違っていることが判明した。ユーザーに確認したところ、既存スクリプトは以下の通り動いている:

- **完全対話式**: 人が手元でheadedブラウザを見ながら起動し、CAPTCHAもその場で人が解く。無人でのバッチ実行は元々していない。
- **郵便番号設定はプログラムで行わない**: Playwrightの永続プロファイル（`user_data_dir`）を使い、配送先地域は人が事前に手動でそのプロファイルへ設定済みの状態でプログラムを実行する。UI操作による自動設定は作っていない。

これを受けて、CLAUDE.md 7節を実態に合わせて修正した（ユーザー承認済み、2026-09-03）:
1. `asin_collector`はK8s CronJobでの無人実行対象から除外し、人が手元で起動する対話式CLIツール（`headless=False`）として実装する。
2. 郵便番号設定はPlaywrightでのUI自動操作を作らない。永続プロファイル（`launch_persistent_context`の`user_data_dir`、marketplace別）に人が事前に手動設定しておく前提とし、プログラム側は起動時に現在の配送先表示を軽くチェックし、想定と異なれば警告を出すだけに留める。

**理由**: (1)郵便番号設定UIの自動操作もCAPTCHA同様、Amazon側のUI変更で壊れやすい未検証領域であり、既に確立している人手の運用を置き換える理由が無い。(2)実績のあるパターン（既存スクリプトの永続プロファイル方式）に合わせることで、新規に検証していない挙動を持ち込まずに済む。

**今回のスコープ**: `brand_page_collector.py`のみを実装し、`ranking_page_collector.py`は同じ骨格が検証できてから横展開する（ユーザー承認済み、2段階アプローチ）。CAPTCHA検知のマーカー（特定要素・テキスト等）は実際のCAPTCHAページを見た経験が無いため、一般的なパターンで実装し、実運用で調整する前提とする。

**【将来のTODO】`collector_run`/`collector_asin`と`tool_execution_link`/`seller_attribution`の接続は今回未設計**: `docs/SeedASIN_スコープ整理_20260902.md` 7節は、将来的に探索方式（ブランドページ／ランキングページ／セラー起点等）ごとの「最終的に出品まで至った実績」を比較する仕組み（旧設計書KPI-08の考え方）を志向している。しかし`collector_asin`で収集したASINは、人が手動で既存の統合ツールへ「ASINリスト」入力として渡す想定（スコープ整理7.1節）であり、その結果生じる`tool_run_id`には対応する`seller_id`が存在しない。一方`tool_execution_link.seller_id`はNOT NULL制約があり、seller起点のtool_run以外を表現できない。そのため「このASINはこのcollector_runで見つかった」という起点を`seller_attribution`と同じ仕組みで追跡することは、現状のスキーマでは不可能。`asin_collector`の初回実装（`brand_page_collector.py`の骨格検証）ではこの接続を設計しない（ユーザー確認済み、`collector_run.run_type`/`target`列の記録粒度は現状のままで問題ない）が、将来的にKPI-08相当の比較を実装する際は、`tool_execution_link.seller_id`のNULL許容化＋`collector_run_id`への任意リンク追加、または別テーブルでのcollector起点追跡のいずれかが必要になる。着手前に本エントリを読み返すこと。

## 19. `asin_collector`実地確認（Phase 3, 2026-09-03）: 実際のAmazonページで検証し、未検証だったCAPTCHA/サーバーエラー判定の一部を修正

`brand_page_collector.py`実装後、実際に稼働させたことが無い（机上のテストのみ）という点がユーザーから最大のリスクとして指摘され、実際のAmazon.co.jp/Amazon.comのブランド検索結果ページ（ユーザー提供のPORTER検索URL）に対して、Playwrightのブラウザバイナリを導入した上で実地確認を行った。Claude Code自身はブラウザ画面を目視できないため、スクリーンショット保存→Readツールで画像確認、HTML保存→grep確認、という手順で検証した。

**確認できたこと**:
- `extract_asins()`（`data-asin`属性ベースの抽出）は実際のページに対して正常動作した（JP: 117件、US再試行: 62件を正しく抽出）。
- `is_captcha_page()`は少なくとも正常ページ・500系エラーページを誤ってCAPTCHAと判定することはなかった（false positiveなし）。**ただし本物のCAPTCHAには一度も遭遇しなかったため、CAPTCHAを正しく検知できるかは依然未検証のまま**。

**新たに判明し、修正した実バグ**: amazon.comへの初回アクセスで一時的な500/503サーバーエラー（Amazon標準のエラーページ、`ref=cs_503_logo`等が実際のマーカー）に遭遇した（再試行で解消）。旧実装は`is_captcha_page()`のみで判定しており、このケースは「CAPTCHAではない」→そのままASIN 0件で正常終了扱いになってしまっていた。`is_server_error_page()`を追加し、`BrandPageCollectorService._fetch_html()`に自動リトライ（最大2回、5秒待機、人の介在は不要）を実装した。リトライ上限に達してもなお解消しない場合は例外にせず、その時点のHTML（＝結果的にASIN 0件）をそのまま返す（CLIラッパー実装時に、この状況をより分かりやすく運用者へ知らせる余地がある）。

**実地で裏付けられた郵便番号設定の重要性**: `playwright_profiles/us`はまだ人が手動で郵便番号(97005)を設定していない状態だったため、amazon.comへのアクセス時にヘッダーが「Deliver to **Japan**」（JPY価格・「Ships to Japan」表示）になっていた。CLAUDE.md 7節・DEVIATIONS.md #18で「未設定だと正しく表示されない」としていた懸念が、絵に描いた餅ではなく実際に起きることを確認した。**実運用開始前に、`playwright_profiles/us`へ人が米国郵便番号97005を手動設定する作業が必須**（未実施）。

**今回のPORTER検索URL自体の限界**: JPのURLは`i=shoes`（靴カテゴリ）にスコープされており、PORTER（バッグブランド）とは噛み合わず、抽出された117件の大半はPORTER以外の無関係な商品（スポンサー広告のシューズケース等）だった。`p_4:PORTER`フィルタ自体はHTML内に正しく含まれており、コード側の問題ではなくURLのカテゴリスコープ側の問題。より適切な（バッグ系カテゴリの）URLでの再検証が今後必要。

**確認できていないこと（引き続きTODO）**: 本物のCAPTCHAへの対応（検知・対話待ちの実際の動作）、`ranking_page_collector.py`の横展開、CLI/エントリポイントの配線。

**追記（2026-09-03、郵便番号設定・正しいURLでの再検証）**:

- **US郵便番号(97005)の手動設定を実施・確認した**。人が`playwright_profiles/us`へ手動設定する作業自体はユーザーに実施いただき、Claude Code側は別プロセスで同じプロファイルを開いて`Deliver to`表示を確認する形で検証した。**設定直後の1回目の検証は誤って「Japan」のままと報告してしまった**が、これは検証用スクリプトが、設定作業用に開いたままだったブラウザ（バックグラウンドタスクでまだプロファイルをロック中）と同時に同一プロファイルを開こうとして正しく読めていなかったことが原因と判明。バックグラウンドタスクを停止してから再検証したところ、`Beaverton 97005`と正しく表示されることを確認した。**教訓**: 永続プロファイル使用中の別プロセスから同時に同じプロファイルを開いて検証しようとすると、正しい状態が読めないことがある。検証は必ず対象プロファイルを使用中の他プロセスが完全に終了してから行うこと。
- **正しいPORTERバッグ商品のURL（ユーザー提供、カテゴリ絞り込みではなくキーワード検索`porter ショルダーバッグ`(JP)/`porter bag`(US)）で再検証**。US側はスクリーンショットで`Deliver to Beaverton 97005`表示と、実際のPORTER商品（"PORTER(ポーター) 855-05458 Force Shoulder Bag"等）が結果に含まれることを視覚的に確認した（スポンサー広告として無関係ブランドが混ざるのはAmazon検索結果の通常の挙動）。JP側も「PORTER(ポーター)」がブランドフィルタの選択肢として表示され、キーワード検索として妥当な結果だった。前回（#19本文）の`i=shoes`スコープの誤ったURLとは異なり、今回のURLでは`extract_asins()`が実際に意味のある収集対象を拾えていることを確認した。
- 前回発見時に併せて修正した`is_server_error_page()`は今回2ページとも`False`（誤検知なし）。

**【将来のTODO】同一永続プロファイルへの多重起動防止（ユーザー指摘、2026-09-03）**: 上記の検証中の誤検知は、Claude Codeによる一時的な検証スクリプトが原因だったが、ユーザーから「対話式ツールであっても、前回の実行がまだ終わっていないのに次を起動してしまう」ことは実運用でも起こり得ると指摘された。現状の`asin_collector/browser_session.py::open_browser()`は、同じ`user_data_dir`（marketplace別の永続プロファイル）に対する多重起動を一切防止していない。Chromiumは同一プロファイルの多重起動時、通常は新規ウィンドドウを既存プロセスに委譲する（新しいプロセスは起動せずSingletonLock機構で既存インスタンスにコマンドを転送する）ため、致命的なクラッシュにはなりにくいと考えられるが、Playwrightの`launch_persistent_context()`がこの委譲を正しく扱うかは未確認であり、少なくとも「今どのプロセスがこのプロファイルを使用中か」が運用者から見えない・気づきにくいという問題は残る。今すぐの対応は不要（ユーザー確認済み）だが、`asin_collector`のCLIエントリポイント実装時、もしくは実運用で同種の問題に遭遇した際は、シンプルなロックファイル（例: プロファイルディレクトリ内に`.lock`ファイルを作り、既に存在すれば起動時にエラーで弾く）の追加を検討すること。

## 20. CLIエントリポイント実装（Phase 3, 2026-09-03）: サブコマンド構成、および将来の統合ラッパーコマンドの検討事項

各パッケージ（`seller_ledger`/`tool_intake`/`seller_performance`/`asin_collector`）に`cli.py`を追加し、`argparse`のサブコマンド形式（`python -m <package>.cli <subcommand> --marketplace=US|JP ...`）で配線した。全て`SessionFactory.for_app_role()`（`SEEDASIN_APP_DATABASE_URL`環境変数）でDB接続する。

**`--source`（`tool_execution_link.source`/seller発見経路）は`choices`で制限しない自由入力にした**: `db/models.py`のコメントで'SELLER_PAGE'/'BRAND_STORE'/'COMPETITOR'を例示しているだけで、DB側にCHECK制約が無く「等」と将来値の余地を残しているため、CLI側で先回りして選択肢を固定するとDBスキーマより厳しい制約になってしまう。

**【将来のTODO】統合ラッパーコマンドの検討（ユーザー提案、2026-09-03、今回は見送り）**: 運用手順②「統合ツールを実行したら、必ずセットで`seller_ledger.cli link-tool-run`→`tool_intake.cli import-tool-output`を呼ぶ」という流れは、非エンジニアの運用者が毎回2〜3個のコマンドを手打ちする負担・打ち間違いリスクがあるとの指摘を受けた。将来、`tool_intake.cli ingest --marketplace=... --seller-id=... --tool-run-id=... --source=... --file=...`のような、`seller_ledger`（tool_execution_link記録）と`tool_intake`（CSV取込）両方の責務を1コマンドでまとめて呼ぶラッパーを検討する余地がある。**設計上の留意点**: このラッパーは`seller_ledger`と`tool_intake`の両方のロジックを呼ぶため、どちらのパッケージに置くか（あるいは両方に依存する第三の場所に置くか）を決める必要がある。今回は既存の1コマンド=1責務という単純な構成を優先し、様子を見てから判断することにした（ユーザー確認済み、今回のスコープには含めない）。

## 21. `asin_collector.cli collect-brand-page`から`--marketplace`を廃止し、URLドメインから自動判定（Phase 3, 2026-09-04）

`--marketplace`と`--target-url`を別々の引数として持たせていたため、「`--marketplace=US`なのに日本のAmazonのURLを渡してしまう」といった、CLI呼び出し時の人為的な食い違いを構造的に防げていなかった。ユーザー指摘を受け、`--marketplace`オプション自体を廃止し、`--target-url`のホスト名から自動判定する設計に変更した:

- `www.amazon.co.jp` / `amazon.co.jp` → `JP`
- `www.amazon.com` / `amazon.com` → `US`
- それ以外のホスト名（判定不能な文字列を含む）は`SystemExit`で即座に停止する

`asin_collector/cli.py`の`marketplace_from_url()`関数として実装。`BrandPageCollectorService.collect()`自体のシグネチャ（`marketplace`を明示的に受け取る）は変更していない——CLI層でURLから導出した値を渡すだけで、サービスクラス側は今まで通りmarketplaceを信頼できる引数として扱う。

この変更は`asin_collector.cli`のみが対象。他の3モジュール（`seller_ledger`/`tool_intake`/`seller_performance`）のCLIは、対象が単一のURLに紐づかない（セラーIDやCSVファイル等、ドメインから自動判定できる情報を持たない）ため、`--marketplace`を明示的に指定する現行の設計のまま変更していない。

## 22. `alembic/versions/0001_create_roles.py`にローカル開発専用の固定パスワードを設定（Phase 3, 2026-09-04）

`app_role`等のDBロールパスワードは元々マイグレーションに含めない設計だった（「Kubernetes Secret等から別途ALTER ROLE ... PASSWORD '...'で設定する運用とする」、#1参照）。この設計自体は本番運用の観点では正しいが、ローカル開発では`docker compose down -v`（ボリューム削除）のたびにパスワードが消え、その都度Claude Codeに再設定・`SEEDASIN_*_DATABASE_URL`の再通知を依頼する必要があり、地味な運用負担になっていた。

ユーザーに(a)マイグレーションへ固定値として持たせる／(b)現状維持、の2択を提示され、「ローカル開発環境なので(a)で困ることは無さそう」との判断のもと(a)を選択（最終判断はClaude Codeに一任、2026-09-04）。`0001_create_roles.py`の`upgrade()`で、ロール作成直後に固定パスワードを設定するようにした:

```
app_role:         local_dev_app_role_pw
kpi_report_role:  local_dev_kpi_report_role_pw
admin_role:       local_dev_admin_role_pw
```

**【本番デプロイ前に絶対に確認すること】**: このファイルの固定パスワードはGitリポジトリに平文で残るため、本番等の実デプロイでは`alembic upgrade head`直後に必ずKubernetes Secret等から`ALTER ROLE <role> PASSWORD '<本物のシークレット>'`で上書きすること。このファイルの値をそのまま本番で使うことは絶対に避ける（マイグレーションファイル自体にも警告コメントを残してある）。

ローカル開発用の接続文字列（`docker compose up -d db`後、`alembic upgrade head`実行後に有効）:
```
SEEDASIN_APP_DATABASE_URL=postgresql+psycopg://app_role:local_dev_app_role_pw@localhost:5432/seedasin
SEEDASIN_ADMIN_DATABASE_URL=postgresql+psycopg://admin_role:local_dev_admin_role_pw@localhost:5432/seedasin
SEEDASIN_KPI_REPORT_DATABASE_URL=postgresql+psycopg://kpi_report_role:local_dev_kpi_report_role_pw@localhost:5432/seedasin
```
`tests/integration/conftest.py`の結合テスト用フィクスチャは、この固定値とは別に独自のテスト専用パスワード（`test_app_role_pw`等）を`_migrated_schema`内で上書き設定するため、影響を受けない（テスト実行のたびに正しく独立して動作することを確認済み）。

## 23. 結合テストがローカル開発用DBの実データを繰り返し破壊していた問題（Phase 3, 2026-09-04）: `test-db`サービスの分離とfixture側の安全装置

`asin_collector.cli collect-brand-page`をユーザーが実行して135件のASINを収集したにもかかわらず、`collector_asin`テーブルが0件になっている、との報告を受けて調査した。コード自体（`brand_page_collector.py`のDB書き込み処理）は実際に再実行して問題ないことを確認したが、原因は**コードのバグではなく、Claude Codeが直前に`docker compose down -v`を実行していたこと**だった。

より深刻な根本原因は、**`tests/integration/`の結合テストが、`docker-compose.yml`の`db`サービス（ローカル開発用、実際にCLIで登録・収集したデータが溜まっていく想定のDB）をそのままテスト対象にしていたこと**にある。`tests/integration/conftest.py`の`_migrated_schema`フィクスチャはテストモジュールごとに`alembic downgrade base`（全29テーブル・全ロール削除）を実行する設計であり、`docker compose down -v`の有無に関わらず、**結合テストを1回実行するだけで`db`サービスの実データは消える**。このセッション中、Phase 1〜3を通じて100件を超える結合テストを何度も実行してきており、その都度この`db`サービスに対して`alembic downgrade base`が発行されていた可能性が高い（ユーザー指摘。ただし過去にどのタイミングで何が消えたかを遡って調査することは、ログが残っていないため不可能であり、実施しない。今後同じことを二度と起こさない保証を優先する）。

**対応（ユーザー承認済み、選択肢1）**:
1. `docker-compose.yml`に**専用の`test-db`サービス**を新設した（`POSTGRES_DB: seedasin_test`、ポート`5433`、ボリューム`seedasin_test_pgdata`を`db`サービスと完全に分離）。`db`サービス（ポート5432、DB名`seedasin`、実データ用）は今後一切結合テストの対象にしない。
2. `tests/integration/conftest.py`の`_superuser_url()`に**構造的な安全装置**を追加した: `SEEDASIN_TEST_SUPERUSER_DATABASE_URL`のデータベース名が`seedasin_test`と一致しない場合、`alembic`コマンドを一切実行せず`pytest.fail()`で即座に停止する。これにより、今後誰か（Claude Code含む）が誤って`db`サービス（ポート5432）を指すURLで結合テストを実行しようとしても、破壊的な操作が走る前に必ず止まる。実際に検証済み（`db`サービスを指すURLでテストを試みると0.06秒でエラー終了し、その後`test-db`を指す正しいURLで116件全テストが成功、`db`サービスの実データ（135件）は両方の検証を通じて無傷だった）。

**今後の結合テスト実行例**:
```
docker compose up -d test-db
SEEDASIN_TEST_SUPERUSER_DATABASE_URL="postgresql+psycopg://seedasin:seedasin_local_dev@localhost:5433/seedasin_test" \
    pytest tests/integration -m integration
```
`db`サービス（ポート5432）は今後、実際のCLI操作でのローカル開発専用として扱い、結合テストのために`down -v`することも二度と行わない。

## 24. `asin_collector.cli export-run`の出力形式変更（Phase 3, 2026-09-06）: ヘッダー行廃止・専用出力ディレクトリ

ユーザー指摘により以下2点を変更した:
1. 出力CSVのヘッダー行（`asin`）を廃止し、ASINのみが1行1件で並ぶヘッダー無し形式にした。ASINはカンマ等を含まないためCSVエスケープ自体が不要と判断し、`csv`モジュールではなく`Path.write_text("\n".join(asins))`による単純なテキスト書き込みに変更した。
2. 出力先を`_EXPORT_DIR = Path("exports") / "collected_asins"`（カレントディレクトリ基準の相対パス、無ければ`mkdir(parents=True, exist_ok=True)`で自動作成）にまとめた。ファイル名は`collected_asins_<collector_run_id>.csv`のまま。

修正後、実データ（ローカル開発用`db`サービス）に対して実行して動作確認した。その際、ユーザー自身が別途`collect-brand-page`を実行していたらしく（`collector_run.id=2`、target="FibaTape"検索、2026-09-06実行）、`export-run`（引数省略）は正しくこちらを「直近の実行」として選択してエクスポートした——`docs/DEVIATIONS.md` #20時点で設計した「marketplaceをまたいで直近の実行を選ぶ」ロジックが実際のユーザー操作に対しても正しく機能することを、意図せず実地で確認できた。

## 25. `SellerService.register()`から`discovery_source`（`known_seller_master.first_known_source`列）を削除（Phase 3, 2026-09-06）

`discovery_source`（CLI `--source`）はどのロジックからも参照されない記録専用の値であり、ユーザー確認の上で削除した。影響範囲: `SellerService.register()`のシグネチャから`discovery_source`引数を削除、`seller_ledger.cli register`から`--source`を削除（`link-tool-run`の`--source`は`tool_execution_link.source`という別物のため維持）、`known_seller_master.first_known_source`列自体も削除。

**実データへの影響**: 削除前に確認したところ、ローカル開発用DBの`known_seller_master`には実際にユーザーが登録したデータが1件存在し（`seller_id=FibaTape`, `first_known_source=BRAND_STORE`）、列削除によりこの値は失われることをユーザーに提示した。ユーザーは「列ごと削除する（この値も消える）」ことを明示的に選択した（2026-09-06）。

**マイグレーション運用が初めて本物の「増分マイグレーション」に切り替わった**: `alembic/versions/0006_drop_known_seller_master_first_known_source.py`を新規追加した。これはPhase 3でこれまで踏襲してきた「`db/models.py`/`db/table_names.py`の編集のみ、`alembic downgrade base`→`upgrade head`で全体を作り直す」方式（#13）が使えなくなったことを意味する——ローカル開発用DBには既に`known_seller_master`以外にも`collector_run`（3件）・`collector_asin`（238件）・`tool_execution_link`（2件）の実データが入っており、全体再生方式はこれらを消してしまうため。以後、スキーマ変更は同様に個別マイグレーションとして追加すること。

**実装上の落とし穴（結合テストで発覚・修正済み）**: `0002_create_tables.py`は`db/models.py`の`metadata.create_all()`で動的にテーブルを再生する設計のため、まっさらなDB（結合テスト用の`test-db`）で0001から一気に適用すると、0002の時点で既に`first_known_source`列を持たないテーブルが作られる（`db/models.py`から先に削除済みのため）。その状態で0006が単純に`op.drop_column()`すると「列が存在しない」エラーになった。`ALTER TABLE ... DROP COLUMN IF EXISTS`（生SQL）に変更し、新規DB（列が最初から無い）と既存のローカル開発用DB（列が実際に存在する）の両方で安全に動作するようにした。**今後、既存カラムを操作する増分マイグレーションを書く際は、この「0002が常に最新のdb/models.py状態でまっさらなDBを作る」という前提を踏まえ、`IF EXISTS`/`IF NOT EXISTS`等の冪等な書き方をデフォルトにすること。**

実際にローカル開発用DBへ`alembic upgrade head`を適用し、他テーブル（`collector_run`3件・`collector_asin`238件・`tool_execution_link`2件）のデータが完全に無傷であること、`known_seller_master`の列構成から`first_known_source`が正しく消えていることを確認済み。

## 26. `tool_intake/log_ingest.py`の新規追加: 統合ツールのログ画面（保存HTML）からの一括自動登録（Phase 3, 2026-09-06）

ユーザー依頼により、統合ツールのログ画面を「ページの保存」（HTMLのみ）で保存したファイルを読み込み、`seller_ledger.cli register` / `link-tool-run` / `tool_intake.cli import-tool-output`をまとめて自動実行するモジュールを新規追加した。CLIエントリポイントは`python -m tool_intake.cli ingest-log --file=<保存HTMLファイル>`。

**HTML解析ライブラリの選定**: 依頼文では「BeautifulSoup等の適切なライブラリ」と例示されたが、`asin_collector/asin_extractor.py`で既に採用している`selectolax`（CLAUDE.md固定の技術スタックには明記されていないが、プロジェクト内で既に実績のあるHTML解析ライブラリ）をそのまま使用した。新規依存追加を避けるための判断であり、依頼の趣旨（適切なライブラリでHTMLを解析すること）とは矛盾しないと判断した。

**`seller_ledger`の意味論の一般化（ユーザー確認済み）**: `known_seller_master.seller_id`には、セラーIDに限らずブランド名・キーワード等、統合ツールに投入した文字列が入り得る前提とした。テーブル名・クラス名（`SellerService`等）自体は変更しない。

**行の識別は`data-seq`属性のみを使用（`id`属性は一切参照しない）**: ユーザーが実際に共有したHTMLに、`<tr id="none" data-seq="...">`のように`id`属性が意味を持たない固定文字列になっている行が実在することが判明したため。

**marketplace/seller_id判定ロジックの区切り文字ゆれへの対応**: メモ欄（`<textarea class="memo">`）が「輸出」「輸入」で始まる場合、`marketplace`をUS/JPに、続く文字列を`seller_id`とする。プレフィックス直後の区切り文字`_`は、ユーザーが共有した実データに`輸入A1VX7O8D3UFTPU`（区切りの`_`が欠落）というパターンが実在したため、`lstrip("_")`により0個または1個の`_`のどちらでも受理するようにした。プレフィックスが無い行（純粋なキーワード検索等）のみ`data-country`属性（jp/us）を参照する。

**取込可否とエラー分離**: `log-status="取得完了"`かつ`log-csv`にファイル名がある行のみ`import-tool-output`を試みる。対象ファイルが`tool_output_dir`（既定`C:\work\output\primary_check`）に見つからない場合はエラーにせず「ファイル未検出」として記録し処理を継続する。既に`link-tool-run`済み（`tool_execution_link`のUNIQUE制約違反）の行も、エラーにせずスキップして次の判定へ進む（CLAUDE.md 6節のエラー分離方針に準拠、再実行時の冪等性を確保）。

**エンコーディング処理の再利用**: 保存HTMLファイルの読み込みには、CSV取込で既に使っている`tool_intake/csv_utils.py::open_text_auto`（utf-8-sig→cp932→utf-8自動判定）をそのまま流用した。CLAUDE.md 技術スタック節にこの旨を追記済み。

ユーザー提供の実データ9パターン（プレフィックス無し区切り文字ゆれ、`id="none"`、禁止セラー、待機中/取得中/取得完了の各ステータス等）を単体テスト（`tests/unit/test_log_ingest.py`）にそのまま組み込み、全パターンが期待通り解析されることを確認済み。結合テスト（`tests/integration/test_log_ingest.py`、`test-db`使用）でregister/link/importの一連の流れ・再実行時のスキップ・ファイル未検出時の継続を検証済み。

## 27. `collector_run.target`を`VARCHAR(200)`から`TEXT`へ変更（Phase 3, 2026-09-07）

ユーザー報告により、実際のAmazon URL（追跡パラメータ付き）が261文字に達し、`VARCHAR(200)`の上限を超えてINSERTがエラーになる事象が発生した。`target`列にはブランド名（短い）とURL（トラッキングパラメータ次第で長くなり得る）の両方が入る設計であり、上限を設ける根拠が無いため、`TEXT`型（無制限）に変更した。

`db/models.py`の`collector_run.target`定義を`String(200)`→`Text`に変更し、`alembic/versions/0007_widen_collector_run_target_to_text.py`を追加した（`ALTER TABLE collector_run ALTER COLUMN target TYPE TEXT` / downgradeは`VARCHAR(200)`へ戻す）。#25/#26の「まっさらなDBでは0002の時点で既に変更後の型になっている」問題は、`ALTER COLUMN ... TYPE TEXT`が対象列が既にTEXT型であっても冪等に成功するため、`IF EXISTS`のような分岐は不要だった。

**検証**: `test-db`（まっさらな状態から`alembic upgrade head`で0001〜0007を通しで適用）で列がTEXT型になっていることを確認し、結合テスト76件全て green。さらに`BrandPageCollectorService._start_run()`を実際に呼び出し、333文字の実例に近いトラッキングパラメータ付きURLがtest-db・実際のローカル開発用DBの両方で正しく保存・読み出しできることを確認した（コード経由の検証であり、生SQLでの列レベル確認だけに留めていない）。ローカル開発用DBへは`alembic upgrade head`を実際に適用し、既存データ（`collector_run`3件・`collector_asin`238件）が無傷であることを確認した上で、検証用に挿入したテスト行（`collector_run.id=4`）は削除済み（`collector_run`は追記専用テーブルではなく通常のCRUD対象のため、削除して原状回復した）。

## 28. `asin_collector.cli collect-brand-page`の複数ページ巡回対応（Phase 3, 2026-09-08）

従来の`brand_page_collector.py`は`target_url`を1回`page.goto()`するだけで、検索結果が複数ページにまたがる場合も1ページ目しか収集していなかった（ユーザー指摘により発覚）。`docs/SeedASIN_スコープ整理_20260902.md` 7節に記載の通り、既存の手作業運用ではページ数上限超過対策として「価格帯分割（`p_36`パラメータでの検索条件分割）」を使っていたが、**この価格帯パラメータはセラー検索（`me=`）と組み合わせた場合のみ機能し、ブランド検索（`p_89=`）では技術的に効かないことが実運用で判明している**（理由不明、Amazon側の仕様）。そのため`collect-brand-page`では価格帯分割を実装せず、「次へ」への複数ページ自動巡回のみを実装し、ページ上限に達した分は既存の手作業運用と同様に取得を諦める方針とした（7節に対応方針として明記済み）。

**実装**: ユーザーが実運用で動作確認済みの既存スクリプト（`_reference/amazoncom_seller_asin_collect.py`、確認後に削除済み・別ファイルであるため単なる参考用として一時配置されていたもの）の2つの関数のロジックをそのまま移植した。
- `find_next_url()`（新規`asin_collector/pagination.py`）: `a.s-pagination-next`クラス→`aria-label`に"next"を含むリンクの順に候補を探し、`s-pagination-disabled`クラスまたは`aria-disabled="true"`を持つ場合は無効なリンクとしてスキップ、有効なら`href`を`current_url`基準の絶対URLに解決する。元スクリプトはPlaywrightの`page.locator()`でライブDOMを見ていたが、本プロジェクトの既存設計（`page.content()`の静的HTML文字列をselectolaxで解析、ブラウザ不要でテスト可能にする）に合わせて移植した——判定に使う情報（class名・aria属性・href）は静的HTMLにも含まれるため、動作に差は無い。
- `extract_search_card_asins()`（`asin_collector/asin_extractor.py`に追加）: 検索結果カード（`div[data-component-type="s-search-result"]`）の範囲内のみを見て、カード自身の`data-asin`属性を優先、無ければカード内の`/dp/`または`/gp/product/`リンクへフォールバックする。既存の`extract_asins()`（ページ全体を無差別にスキャンする実装）とは異なり、関連検索・スポンサーカルーセル等ページ内の他セクションのASINを拾わない。`extract_asins()`自体は変更・削除せず残した（将来の`ranking_page_collector.py`はカード構造が異なる可能性があり、そちらで使う可能性があるため）。`collect-brand-page`は新しい`extract_search_card_asins()`を使うよう切り替えた。

**`BrandPageCollectorService`の変更**: `collect()`に`_collect_pages()`ループを追加し、(1)現在ページのHTML取得（既存の`_fetch_html()`のCAPTCHA検知・500/503自動リトライロジックをそのまま再利用、ページ遷移のたびに同じ判定が働く）、(2)そのページのASIN抽出、(3)新規ASINのみ`_insert_asins()`でcollector_asinへ逐次INSERT（ページ単位の短いトランザクション、途中で中断しても収集済み分はDBに残る設計——ユーザー指定）、(4)`find_next_url()`で次ページ判定、を繰り返す。終了条件は（a）そのページのASINが0件、（b）同一URLへの再訪問（「次へ」の誤検出等でループする異常系対策）、（c）`find_next_url()`が`None`を返す、のいずれか。加えて、上記が万一機能しない異常系に備えた絶対上限`_MAX_PAGES_SAFETY_LIMIT=200`を安全装置として設けた（ユーザー指定通り、通常はこの上限に達する前に自然終了する想定）。既存の`_finish_run()`はASINのINSERTを行わなくなり、最終的な`asin_count`を受け取って`collector_run`を`COMPLETED`にするだけの責務に変更した（ASINのINSERTは新設の`_insert_asins()`が担う）。

**無限スクロール（`scroll_to_bottom`）は実装しなかった**: 実際のブランド検索ページ（Amazon.co.jp「porter ショルダーバッグ」）に対し、スクロールなしでの`extract_search_card_asins()`結果（54件）とスクロール後の結果（54件）を比較したところ完全に一致し、スクロールによる追加読み込みは発生しないことを確認した。Amazon検索結果は各ページの商品カードが最初から静的HTMLに含まれており、無限スクロールで追加カードが動的に挿入されるタイプのUIではないため（ユーザー指示通り、実データで判断した）。

**実地検証**: Amazon.co.jp（`porter ショルダーバッグ`検索）で`find_next_url`/`extract_search_card_asins`単体の動作を2ページ分確認（page1: 54件→page2: 59件、ページ間重複8件は正常、`next_url`も正しく計算される）。さらに`BrandPageCollectorService.collect()`をエンドツーエンドで実行し（プロセス内でのみ安全上限を3ページに一時的に下げて検証時間を短縮）、3ページ分・**140件のASINを正しく収集し、`collector_asin`に140件（全て重複無し）、`collector_run.asin_count=140`・`status=COMPLETED`で記録される**ことを確認した（test-db使用、`collector_asin`が追記専用テーブルのためクリーンアップはせず、test-db自体が使い捨てであることに委ねた）。Amazon.com（`porter bag`検索）でも同じロジックでpage1: 60件→page2: 58件、正しい`next_url`計算を確認済み。いずれもCAPTCHA・500系エラーには遭遇しなかった。

**テスト**: 単体テストを`tests/unit/test_pagination.py`（新規）・`tests/unit/test_asin_extractor.py`（`extract_search_card_asins`分を追加）・`tests/unit/test_brand_page_collector_pagination.py`（新規、複数ページ巡回ループの終了条件・重複排除・安全上限を検証）に追加。結合テストは`tests/integration/test_brand_page_collector.py`を`_insert_asins`/`_finish_run`の新シグネチャに合わせて更新し、ページ単位の逐次INSERTが累積することを検証するテストを追加した。単体86件・結合78件、全てgreen。

参照用に一時配置した`_reference/`フォルダ（`amazoncom_seller_asin_collect.py`・`amazon_seller_asin_collect.py`）は、ロジック移植完了後に削除した（プロジェクトの正式なコードではなく、確認用の一時ファイルだったため）。

## 29. `seller_performance.cli history`の新規追加: セラーの取得履歴・出品成果の一覧表示（Phase 3, 2026-09-09）

ユーザー依頼により、指定セラーの`tool_execution_link`（統合ツール実行履歴）を古い順に一覧し、それぞれの実行が最終的に何件の出品完了に至ったかを横に並べて表示するコマンドを追加した。**判断材料の提示のみを行い、自動判定・自動実行は一切行わない**（「また取りに行くべきか」の最終判断は人が行う、ユーザー明記）。

新規モジュール`seller_performance/seller_history.py`。CLIエントリポイントは`python -m seller_performance.cli history --marketplace=<US|JP> --seller-id=<セラーID>`。

**「出品成果」の算出方法**: 各実行（`tool_execution_link.tool_run_id`）に対し、`seller_attribution.first_seen_via_tool_run_id`でその実行に帰属したASINのうち、`listed_at`が`NOT NULL`（＝`seller_performance.cli attribute`実行時点で`weekly_listing_snapshot`との突き合わせにより出品確認済みと判定されたASIN）の件数を数える。`seller_attribution.listed_at`は`attribute`コマンド実行時に一度計算・保存される値のため、**このコマンドの表示内容は直近の`attribute`実行時点のもの**であり、`attribute`を実行していない・古い場合は出品成果が実態より少なく表示され得る。既存の運用ランブック（`attribute`→`recommend`の順で定期実行）と同じ前提であり、`history`もこの後に位置づける（`recommend`同様、独自に`weekly_listing_snapshot`を再突き合わせすることはしない）。

**判断材料として追加表示する内容（ユーザー指定）**:
- 直近の取得日からの経過日数（`today - 最後の実行のrequested_at`の日数差）
- 直近3回（実行回数が3回未満ならその回数分）の出品成果の平均、および傾向（増加傾向/減少傾向/横ばい、実行回数1回のみなら「データ不足」）。傾向判定は窓内の最初と最後の値を単純比較するだけの簡易ロジック（複雑な統計は不要というユーザー指定に基づく）。

**セラーが見つからない場合**: `tool_execution_link`に該当行が1件も無ければ「該当するセラーが見つかりません」と表示して終了する（`SystemExit`、`asin_collector.cli export-run`の「該当無し」ケースと同じ慣習）。

**設計上の分離**: DBクエリ（`SellerHistoryService.get_history()`、結合テスト対象）と、経過日数・平均・傾向・表示整形（`days_since_last_run()`/`recent_average()`/`recent_trend()`/`format_history()`、いずれも`HistoryEntry`のリストのみで完結する純粋関数、単体テスト対象）を分離した。既存の`tool_intake/log_ingest.py::IngestSummary.format()`と同じ「表示整形は独立した関数としてテストする」パターンを踏襲。

テスト: 単体9件（`tests/unit/test_seller_history.py`、経過日数・平均・傾向の各パターン、表示整形の期待形状）、結合5件（`tests/integration/test_seller_history.py`、古い順ソート・出品成果の帰属先の正確性・marketplace分離）、CLI結合2件（`tests/integration/test_cli.py`、正常系・該当無しの`SystemExit`）を追加。単体95件・結合85件、全てgreen。

## 30. `storefront_blocked_seller`テーブルの新設: 統合ツールのストアフロントURLから取得できないセラーの記録（Phase 3, 2026-09-09）

ユーザー依頼により、統合ツールのストアフロントURLから取得できないセラーを恒久的に記録する仕組みを追加した。記録経路は2つ:
1. **HIGH_RISK_COUNTRY（自動）**: `tool_intake.cli ingest-log`がログのステータス「禁止セラー」の行を処理する際に自動記録する。
2. **COMPETITOR_SAME_SERVICE（手動）**: 新規コマンド`python -m seller_ledger.cli mark-storefront-blocked --seller-id=<ID> [--seller-name=<名前>] [--note=<メモ>]`で、人が気づいた時点で記録する。reasonはこのコマンド専用の固定値のため引数には無い。

新規モジュール`seller_ledger/storefront_blocked.py::StorefrontBlockedSellerService`（`mark_blocked()`・`get_blocked_info()`）。

**CLAUDE.md 2節（RLS必須）の明示的な例外**: `storefront_blocked_seller`はmarketplaceに依存しない情報（セラーは米国・日本のいずれのAmazonでも同一人物/同一事業者であり、統合ツールから取得できない理由もmarketplace非依存）のため、ユーザーの明示的な指示によりRLSを適用しない。CLAUDE.md 2節に例外の一文を追記し、`db/table_names.py`に新設した`NO_RLS_TABLES`タプルで対象を明示した（`ALL_TABLES`には含めない——`ALL_TABLES`はRLSポリシー生成対象の唯一の正であり、このテーブルは`marketplace`列自体を持たないため、含めるとポリシー作成SQL自体が失敗する）。

**マイグレーション実装上の落とし穴（結合テストで発覚・修正済み）**: 当初、`db/models.py`の共有`metadata`にも`Table`オブジェクトとして`storefront_blocked_seller`を追加していたが、これは`0002_create_tables.py`の`downgrade()`（`metadata.drop_all(bind=..., checkfirst=False)`——現在の`db/models.py`の状態を無条件でDROPする設計）と衝突する。真っさらなDBで`downgrade base`まで戻す際、新設マイグレーション`0008`のdowngradeが先に`storefront_blocked_seller`をDROPし、その後`0002`のdowngradeが同じテーブルを再度DROPしようとして`UndefinedTable`エラーで失敗した。**「`db/models.py`を編集するだけで`0002`のcreate_all/drop_allに反映される」という設計（#13）と、#25以降の「新規テーブルは独立した増分マイグレーションで完結させる」という設計は本質的に両立しない**（前者は現在のmetadata状態を「常に正」として扱い、後者は個々のマイグレーションが自分の作った物だけを責任持って作成・削除する前提のため）。対応として、`storefront_blocked_seller`は共有`metadata`へ一切登録せず（`db/models.py`にはコメントのみ残す）、`alembic/versions/0008_create_storefront_blocked_seller.py`の生SQL（`CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS`）のみでDDLのライフサイクルを完結させた。**今後、全く新規のテーブルを追加する場合はこの方針（共有metadataに含めない）を踏襲すること。**

**GRANT**: `app_role`へSELECT/INSERT/UPDATE/DELETE（通常CRUD、追記専用にはしない——「既存の情報を上書きしない」はアプリ層の`ON CONFLICT DO NOTHING`ロジックで保証し、DBロール権限としては将来の手動修正の余地を残す）。`kpi_report_role`へは他の全テーブルと同様の慣習でSELECTのみ付与した。

**重複防止の実装**: `mark_blocked()`は`INSERT ... ON CONFLICT (seller_id) DO NOTHING RETURNING ...`で新規/既存を判定し、既存の場合は元の`reason`/`first_noted_at`をそのまま返す（上書きしない）。`mark-storefront-blocked`コマンドは既存の場合エラーにせず「既に登録済みです（理由: XXX、初回登録日: YYYY-MM-DD）」と表示し正常終了する。`ingest-log`側は同様のケースをサマリの`storefront_blocked_already_recorded`件数に含める。`ingest-log`内の記録はregister/link-tool-runとは独立したトランザクションで実行する——同じログファイルを再取込した際、既にlink-tool-run済み（`IntegrityError`）でも`storefront_blocked_seller`への記録が未実施なら記録されるようにするため（自己修復的に、再実行のたびに両方の状態を正しい状態へ収束させる設計）。

**`seller_performance.cli history`の拡張**: 指定した`seller_id`が`storefront_blocked_seller`に登録済みの場合、出力の先頭に「※このセラーは統合ツールのストアフロントURLから取得できません（理由: XXX、登録日: YYYY-MM-DD）」を表示する。reasonの日本語表示ラベル（`REASON_LABELS`辞書、`seller_ledger/storefront_blocked.py`）は`seller_performance`側からもimportして再利用し、表示文言を一箇所に集約した。

**`mark-storefront-blocked`コマンドの接続方式**: `storefront_blocked_seller`はmarketplaceに依存しないが、`SessionFactory.get_session()`はCLAUDE.md 2節の一元管理方針によりUS/JPいずれかの指定を必須とする。このコマンドには`--marketplace`引数が無いため、内部で固定値`"US"`を接続確立の形式的な指定として使う（このテーブルにはRLSポリシー自体が存在しないため値そのものに実質的な意味は無い、コード上に理由をコメントで明記済み）。

テスト: 単体3件（`tests/unit/test_storefront_blocked.py`、reason表示ラベルのマッピング）+ `format_history`のblocked_info表示3件を`tests/unit/test_seller_history.py`に追加。結合6件（`tests/integration/test_storefront_blocked.py`、新規記録・重複防止・RLS非適用・CHECK制約違反）+ `ingest-log`自動記録2件（`tests/integration/test_log_ingest.py`）+ CLI結合3件（`mark-storefront-blocked`新規/既存、`history`のブロック通知表示）を追加。単体100件・結合96件、全てgreen。ローカル開発用DBへも`alembic upgrade head`を実際に適用し、既存データ（`collector_run`4件・`collector_asin`264件）が無傷であることを確認した上で、`mark-storefront-blocked`を実際に実行して動作確認（検証用の行は削除して原状回復済み）。

## 31. `scripts/import_legacy_seller_ledger.py`の新規追加: 過去のExcelセラー台帳の一括移行（Phase 3, 2026-09-09）

ユーザー依頼により、過去のExcel台帳（JP: `セラー管理台帳.xlsx`/`セラーリスト`シート、US: `輸出セラー台帳.xlsx`/`セラー台帳`シート）を`known_seller_master`・`tool_execution_link`・`storefront_blocked_seller`へ振り分けて移行する、1回限りの移行作業用スクリプトを追加した。`seller_ledger`モジュールのdocstring（#15参照）で「将来必要になれば別の一回限りのスクリプトとして対応する」と予告していたタスクの実施にあたる。

**着手前に実データを確認した（ユーザー指示通り）**: 依頼文にはExcelファイルが会話ログに添付済みとあったがこのセッションには引き継がれていなかったため、着手前にユーザーへ再共有を依頼し、実際に受け取ったファイルをopenpyxl（新規依存追加、pandasは引き続き使用禁止のため）で直接読み込んで検証してから実装した。これにより、依頼文の記述だけでは分からなかった以下の実データの実態が判明した:

1. **「Bland」ではなく「Brand」（依頼文の誤記を実データで訂正）**: 依頼文には"Bland"と書かれていたが、実際のExcelのブランド行マーカーは正しい綴りの"Brand"だった（JPファイルに28行、USファイルに0行）。実データに合わせて"Brand"で判定するよう実装した。
2. **「状況」列には依頼文にない値が2種類存在した**: 「見送る」（統合tool ID='-'、JP147行・US271行）と空欄（統合tool ID・日付とも空欄、JP73行・US97行）。依頼文が明示していたのは「禁止セラー」（rule 1）のみで、これら2種類は文字通りには「rule 4の通常行」に該当するが、統合tool IDが実在するIDではない（'-'や空欄）ため、そのままファイル名探索を行うと**'-'のような1文字の値はほぼ任意のファイル名に偶然含まれ得り、誤ってmarketplaceを確定させてしまうリスクがある**と判断した。対応として、`is_plausible_tool_id()`（4文字未満は探索自体を行わない）を導入し、これらの行は自動的に「ファイル未検出」として扱うようにした（実データでの目視確認・結合テストの両方で、'-'を含む実在ファイル名に対して誤検出しないことを確認済み）。
3. **「#N/A」等のExcelエラー文字列がseller id/seller name列にそのまま残っている行**があった（JP46行・US15行、いずれも他の列も全て空欄の末尾ゴミ行）。`_BLANK_LIKE_VALUES`に含めることで、rule 3（両方空欄）の対象外判定に正しく含まれるようにした。
4. **seller id列がExcelの数値型として保存されている行**が2件あった（例: `26286483051`、`6901782`）。`str()`変換で対応（DB列はVARCHARのため問題なく書き込める）。
5. **統合tool IDに誤記・自由記述が混じっている行**（`'6644032q'`という末尾に文字が付いた誤記、`'拡張'`/`'拡張にしよう'`/`'拡張取得済み'`という自由記述メモ、USファイルのみ計7行）があったが、これらは実在するIDではないため、rule 4の通常のファイル名探索を素通りして自然に「ファイル未検出」になった（特別扱い不要と判断、実際に確認済み）。

**ファイル名探索の実装**: 対象2フォルダ（`primary_check`=JP、`ex_primary_check`=US）を移行スクリプト実行時に1回だけ走査し、「`{tool_run_id}_...`」形式のファイル名からプレフィックスを取り出した辞書を構築してから、行ごとに辞書引きする（`tool_intake/log_ingest.py`が前提とするのと同じ命名規則、既存の統合ツール出力ファイル名の実例で確認済み）。単体テストでこの実装の当初のバグ（アンダースコアを含まない旧形式のファイル名が誤って全体をプレフィックスとしてインデックスされてしまう）を発見・修正した。

**`StorefrontBlockedSellerService.mark_blocked()`を拡張**: `first_noted_at`を明示的に指定できる任意引数を追加した（省略時はDBのDEFAULT `CURRENT_DATE`のまま、既存の2経路の挙動は変わらない）。移行スクリプトはExcel内で同じseller_idが複数回「禁止セラー」として登場する場合の最古日付を計算し、この引数で渡す。

**tool_execution_link.sourceは`'LEGACY_IMPORT'`固定**: 既存の`SELLER_PAGE`等のライブ運用による記録と区別できるようにするため。

**known_seller_masterへのマッピング**: `known_before_seedasin=true`固定（過去データのため）、`known_at_reliable=true`固定（実際の過去日付という根拠があるため信頼できると判断）、`first_known_at`=同一seller_idの最古の日付、`last_seen_at`=最新の日付、`source_asin`=最古の行の起点ASIN。既存行（marketplace単位）があれば`ON CONFLICT DO NOTHING`で上書きしない。

**`--dry-run`オプション**: 依頼文には無かったが、本番データへの一回限りの書き込みという性質上、実行前に集計結果だけを確認できる安全装置として追加した。実際にユーザー提供の実ファイル2件に対してdry-runを実行し、以下の結果を確認済み（本番DBへの実書き込みはユーザー確認後に実施する）:
```
読込: セラー管理台帳.xlsx (セラーリスト) - 1404行（対象外45件）
読込: 輸出セラー台帳.xlsx (セラー台帳) - 902行（対象外15件）
known_seller_master新規登録数: 1051件
tool_execution_link登録数: 1387件
storefront_blocked_seller登録数: 284件
ブランド行取込数: 26件
ファイル未検出数: 601件
対象外件数: 60件
スキップ数（既存のため）: 0件
```
内訳の整合性を検算済み（対象外60=45+15、禁止セラー318行→ユニークseller_id 284件、残り1988候補行=found 1387+not_found 601）。

**tool_output_asinへの取込は行わない**（ユーザー確認済み、別タスクとして後日対応）。

テスト: 単体19件（`tests/unit/test_import_legacy_seller_ledger.py`、resolve_seller_id/is_plausible_tool_id/build_tool_id_index/determine_marketplace/ImportSummary.format/実際のExcel構造を模したフィクスチャでのparse_excel_rows）+ 結合10件（`tests/integration/test_import_legacy_seller_ledger.py`、known_seller_master最古/最新日付・上書き防止、tool_execution_link重複スキップ、禁止セラーの振り分けと最古日付、ファイル未検出、ブランド行、dry-run）+ `mark_blocked`拡張の結合1件を追加。単体119件・結合107件、全てgreen。

## 32. `scripts/import_legacy_tool_output.py`の新規追加: tool_output_asinへの一括移行（Phase 3, 2026-09-09）

\#31で移行した`tool_execution_link`全件（marketplaceごと）を対象に、対応する統合ツール出力ファイルの中身（ASIN・取得日時）を`tool_output_asin`へ一括取込するスクリプトを追加した。ファイル名探索ロジック（`build_tool_id_index()`/`is_plausible_tool_id()`）は#31のスクリプトから再利用した（`scripts/import_legacy_seller_ledger.py`のこの2関数と2つの既定フォルダ定数は、この時点で`_`プレフィックスを外しモジュール間の公開インターフェースとして扱うよう改名した）。

**着手前に処理時間を実測してから方針を提示した（ユーザー指示通り）**: 対象1351件の`tool_execution_link`のうち、実ファイルが見つかったのは1349件、合計約537MB・約218万ASIN行だった。ファイルの読込・デコード・パース自体は全体で約11秒と高速だが、既存の`IntegrationToolImportService.import_file()`は「1行ごとに個別トランザクションでコミット」する設計（日次の少量ファイル運用が前提）であり、実測（ローカルPostgresへの実ベンチマーク）で1行あたり約2ms、218万行に単純適用すると**約75分**かかることが判明した。

**対応（ユーザー承認済み）**: `IntegrationToolImportService.import_file()`自体は変更せず、この移行専用に「1ファイルにつき1回のバッチINSERT」方式の別ロジックを実装した。ASIN・取得日時の抽出（`tool_intake.csv_utils`のcp932/utf-8-sig/utf-8自動判定含む）と`import_run_history`/`import_error_log`への記帳（`ImportRunRecorder`）はそのまま再利用し、不正な行はPython側で事前に弾いてから正常な行だけをまとめて1回の`execute()`で送るようにした。同条件のベンチマークで1行あたり約0.06ms、全体で**3〜5分**程度に短縮される見込みを提示し、ユーザー承認を得てから実行した。

**`known_seller_master.latest_product_count`/`latest_checked_at`の更新はスキップした**（ユーザー確認済み）: `import_file()`はこの更新を伴うが、過去のファイルを「たった今チェックした」ことにしてしまうため、この移行では意図的に行わない。今後、実際に統合ツールを再実行すれば自然に最新化される。

**実行結果（ローカル開発用DBへの実書き込み）**:
```
tool_execution_link対象件数: 1351件
取込成功件数（tool_output_asinへ新規追加したASIN行数）: 2183472件
処理ファイル数: 1348件
ファイル未検出件数: 2件
行単位の解析エラー件数（ASIN空欄・日付形式不正等）: 10件
その他エラー: 1件
  - marketplace=JP tool_run_id=6929966 file=..._判定結果.csv: required column '取得日時' not found in header
```
「その他エラー」の1件は、該当tool_run_idに対応するファイルが（本来の統合ツール出力ではなく）出品可否判定結果ファイル（`primary_check.py`系、SeedASINのスコープ外）しか存在しなかったケース。ヘッダー列検証で正しく検知・スキップされ、他のファイルの取込には一切影響しなかった（CLAUDE.md 6節のエラー分離が意図通り機能した実例）。事後に調査し、代替となる本来のファイルは存在しない（=データが実際に欠落している）ことを確認済み——コード側の不具合ではない。

処理ファイル数1348に対し、実際に行が入った`tool_run_id`は1325件（差分23件）だったため、該当ファイルを個別に確認したところ、いずれもヘッダー行のみでデータ行が0件の実ファイル（統合ツールが0件ヒットで終わった実行）であることを確認した——バグではなく正常なケース。

**冪等性**: `tool_output_asin`の既存UNIQUE制約`uk_tool_output_asin (marketplace, tool_run_id, asin)`により、同一内容を誤って複数回取り込んでもエラーにも重複行にもならない（`ON CONFLICT DO NOTHING`、結合テストで再取込0件になることを確認済み）。

テスト: 単体4件（`tests/unit/test_import_legacy_tool_output.py`、`ImportSummary.format()`とdry-runの純粋な集計ロジック）+ 結合7件（`tests/integration/test_import_legacy_tool_output.py`、実ファイルからの取込・ファイル未検出・実装らしくない短いtool_idの安全側スキップ・冪等性・1ファイル内の行単位エラー分離・latest_checked_at不更新の確認・dry-run）を追加。単体123件・結合115件、全てgreen。

**`seller_performance.cli history`のメッセージ改善**（同日、ユーザー依頼）: 指定したseller_idが`tool_execution_link`に記録が無い場合、これまでは`storefront_blocked_seller`への登録有無を問わず一律「該当するセラーが見つかりません」と表示していた。これだと「一度も扱ったことがないセラー」と「禁止セラーとして最初から統合ツールに投入しなかったセラー」の区別が付かないため、後者の場合は「このセラーは実行履歴を持ちません（理由: 統合ツールのストアフロントURLから取得できないセラーとして記録済み。理由: XXX）」と表示するよう`seller_performance/cli.py::_cmd_history()`を修正した（`SellerHistoryService`/`format_history()`自体は変更不要、CLI側の分岐のみ）。結合テスト1件を追加し、上記の単体123件・結合115件に含まれている。

## 33. `tool_intake.cli import-weekly-listing`の仕様変更: snapshot_dateをCSV内「登録日時」列から行ごとに決定（Phase 3, 2026-09-09）

これまで`weekly_listing_snapshot.snapshot_date`は、取込バッチを実行した日付（`date.today()`、または`--snapshot-date`で明示指定した日付）を全行共通で使っていた（2026-09-03時点の方針、旧`docs/phase3_implementation_guide.md`§2のDDLコメント「週次取込のタイミング」に対応）。ユーザー指摘により、バッチ実行日は「実際にそのASINが登録された日」の不正確な代用品でしかなく、ファイル内の「登録日時」列に正確な日時が既に存在するため、そちらを正として使う方針に変更した。

**実データで列構成を確認済み**: 米国分（`listing-KW02887...csv`、21列）・日本分（`listing-AP2725...csv`、23列）の両方の実ファイルを直接確認し、いずれも列インデックス10（0始まり）に同名の「登録日時」列（形式: `YYYY-MM-DD HH:MM:SS`、例: `2026-07-02 23:51:32`）が存在することを確認した。列名・位置ともに米国分・日本分で共通のため、`column_index(header, "登録日時")`による名前ベース解決のみで両marketplace対応でき、marketplace別の分岐は不要だった（依頼文は「列名・列位置が違う場合はそれぞれ対応する」としていたが、実データはASIN/SKU同様に共通だった）。

**実装**: `tool_intake/weekly_listing_import.py`の`import_file()`から`snapshot_date`引数を削除し、行ごとに「登録日時」列を`parse_datetime_flexible()`（既存の`tool_intake/csv_utils.py`のロジックをそのまま再利用）でパースし、`.date()`で日付部分のみを取り出して使うようにした。「登録日時」列が空欄・不正な形式の行は、ASIN列が空の行と同じ扱い（`ImportRunRecorder.record_error()`でエラーコード`IM-WEEKLY-01`を`import_error_log`へ記録し、その行だけスキップして処理を継続。CLAUDE.md 6節のエラー分離）とした——既存の`tool_intake/integration_tool_import.py`が複数の検証失敗理由を1つのエラーコードで共有している慣習に倣った（理由ごとにコードを増やさない）。UNIQUE制約`(marketplace, asin, snapshot_date)`はスキーマ変更無しでそのまま機能する（結合テストで再取込時に重複行が増えないことを確認済み）。

**CLIから`--snapshot-date`引数を削除した**: snapshot_dateがCSV自身から決まる以上、この引数は意味を持たなくなるため（黙って無視される設定値を残すと事故のもとになる）。`tool_intake/cli.py`の`_cmd_import_weekly_listing()`・`import-weekly-listing`サブコマンドの両方から削除し、モジュールdocstringの実行例も更新した。

**テスト**: `tests/integration/test_tool_intake.py`の`_weekly_us_row()`/`_weekly_jp_row()`ヘルパーに`registered_at`引数（既定値`"2026-09-01 10:00:00"`）を追加し、これまで登録日時列を空欄のまま使っていた既存テストが新仕様でも成立するよう更新した。「snapshot_dateはバッチ実行日をデフォルトにする」ことを検証していた`test_snapshot_date_defaults_to_today`は新仕様と矛盾するため削除し、代わりに以下を追加した: 行ごとに異なる登録日時→異なるsnapshot_dateになることの検証、登録日時が空欄/不正な形式の行がエラー記録の上でスキップされ他の行の取込は継続することの検証（2パターン）、UNIQUE制約による重複防止が維持されていることの検証。`tests/unit/test_cli_parsers.py`の`--snapshot-date`関連2テストも新しい引数構成に合わせて1テストへ統合した。単体122件・結合118件、全てgreen。

実際に本物の週次出品完了リストファイル（米国分`listing-KW02887...csv`・日本分`listing-AP2725...csv`）を使って動作確認した。

## 34. `seller_performance.cli recommend`に「前回取得からの経過日数」条件を追加（Phase 3, 2026-09-10）

ユーザー依頼により、既存の条件（直近lookback_days日以内にmin_listing_count回以上の出品実績）に加えて、「前回の取得（`tool_execution_link.requested_at`の最新値）からmin_days_since_last日以上経過していること」を新しい絞り込み条件として追加した。満たさない（直近すぎる）セラーは一覧から除外する。増加傾向の判定は組み込まない——`history`コマンドの傾向表示を見て人が最終判断する（ユーザー確認済み、既存方針のまま）。

**新しい閾値のデフォルト値の置き場所（ユーザー確認済み）**: 既存の`lookback_days`/`min_listing_count`と同じ「ハードコード禁止、DBで設定変更可能」方針（docs/DEVIATIONS.md #17）に揃え、`seller_recommendation_config`テーブルに`min_days_since_last`列（`INTEGER NOT NULL DEFAULT 15`）を追加した。新規マイグレーション`alembic/versions/0009_add_min_days_since_last_to_seller_recommendation_config.py`（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`、#25以降の冪等マイグレーション方針を踏襲）。既存のUS/JP行にも`DEFAULT 15`により自動的に値が入る（実際にローカル開発用DBで確認済み）。`config.config_cache.ConfigCache.get()`は`db.models`のTableオブジェクトから動的に全列を`SELECT`する実装のため、この列追加に伴うコード変更は不要だった。

**実装**: `SellerRecommenderService.recommend()`に`min_days_since_last: int`引数を追加し、`tool_execution_link`を`(marketplace, seller_id)`単位で`MAX(requested_at)`集計したサブクエリとJOINし、`current_date - 最新のrequested_at::date >= min_days_since_last`を`WHERE`条件に追加した。同じセラーに複数の`tool_execution_link`がある場合は最新の1件で判定される（結合テストで、古い実行が1件あるだけでは除外を回避できないことを確認済み）。`seller_performance.cli recommend`に`--min-days-since-last`引数を追加し、省略時は他の2閾値と同様に`seller_recommendation_config`から解決する。

**テスト**: 単体2件（`test_cli_parsers.py`の`recommend`パーサーテストに`min_days_since_last`のデフォルトNone・明示指定の検証を追加）+ 結合4件（`test_seller_performance.py`に、直近すぎるセラーの除外・ちょうど境界日数での包含・複数`tool_execution_link`がある場合の最新優先を追加、既存3テストは`_seed_seller_and_tool_run()`ヘルパーを拡張して`requested_at`を意図的に古い日付にできるようにした上で全て`min_days_since_last`引数を追加）+ CLI結合1件（`--min-days-since-last`の上書き）を追加。単体122件・結合122件、全てgreen。

**実データでの動作確認**: ローカル開発用DBへマイグレーション適用後、`recommend --marketplace=US`を実行したところ、既定値（15日）で86件、`--min-days-since-last=1`で98件のセラーが該当した——閾値を緩めると直近に取得したばかりのセラー12件が新たに候補に加わることを実際に確認し、機能が実データ上でも意図通りに働くことを検証した。

## 35. ルート直下`cli.py`の新規追加: 日々の操作をまとめた対話式メニュー（Phase 3, 2026-09-10）

ユーザー依頼により、SeedASINの日々の操作を対話式メニュー（`python -m cli`）としてまとめた。既存の各`X.cli`コマンド（`seller_ledger.cli`・`tool_intake.cli`・`asin_collector.cli`・`seller_performance.cli`）は一切変更せず、このモジュールはそれらの裏にあるサービスクラスを直接呼び出す「まとめ役」として実装した（サブプロセスとして各CLIを再起動する方式ではなく、Pythonの関数呼び出しとして直接連携する——エラーハンドリング・引数の受け渡しが素直になるため）。既存コマンドをそのまま使う上級者向けの操作方法もそのまま残っている。

**実装前にユーザーへ提示し承認を得た、既存コードへの小さな変更2点**:
1. `SellerService.exists(conn, marketplace, seller_id) -> bool`を新設した。メニュー1（取得履歴を見る）で「`known_seller_master`に未登録」を判定するために必要だったが、既存の`SellerService`には登録有無を調べるメソッドが無かった（`register()`はINSERT/UPDATEのみ）。
2. `BrandPageCollectorService.collect()`の戻り値を、ASIN総件数のみの`int`から、`run_id`と`asin_count`を両方持つ`CollectResult`（新設dataclass）に変更した。メニュー4（収集後に任意でCSV出力）で「今回のcollector_run_idをそのまま使う」ために、収集直後にrun_idを受け取る手段が必要だったため。**`asin_collector.cli collect-brand-page`自体の起動コマンド・画面出力は一切変わらない**（`_cmd_collect_brand_page`内の1行を`result.asin_count`に書き換えただけ）——ユーザーからもこの点を明示的に確認された。あわせて、`export-run`のCSV書き出しロジックを`asin_collector/cli.py::export_collector_run(factory, collector_run_id) -> tuple[Path, int]`として切り出し、既存の`_cmd_export_run`と新しい対話式メニューの両方から共通利用できるようにした（ロジックの重複を避けるため）。

**メニュー1（取得履歴を見る）の分岐ロジックで、依頼文に無かった実装上の判断が1つ必要だった**: 「未登録」「登録済み・禁止でもない」の2ケースに加えて、**「登録済みだが`tool_execution_link`が1件も無い」**という第三の状態が実際にあり得る（例: `seller_ledger.cli register`だけ実行してまだ一度も統合ツールに投入していないセラー）。この状態で既存の`format_history()`をそのまま呼ぶと、`entries`が空リストのため`days_since_last_run()`が`entries[-1]`でIndexErrorになる。対応として、`entries`が空の場合は「登録済みですが、取得履歴がまだありません」と表示した上で、依頼文通りの3択（ひもづける/取得不可と記録/何もしない）をそのまま提示するようにした。

**メニュー2（ひもづける）の実装範囲**: 依頼文の指示通り、今回は既存の`link-tool-run`相当の処理（marketplace・seller-id・tool-run-id・sourceを聞いて`ToolRunLinkService.record()`を呼ぶだけ）にとどめた。メニュー1の「未登録セラー」からの遷移時のみ、`SellerService.register()`を先に呼んでから同じ処理につなげる（`auto_register`フラグで分岐）。それ以外の自動register等の詳細仕様は別タスクで改めて確認する（ユーザー指定通り）。

**エラー分離**: メインループは各メニュー処理の呼び出しを`try/except (SystemExit, Exception)`で囲み、失敗時は分かりやすいメッセージを表示してメインメニューへ戻る（アプリ全体は落とさない、CLAUDE.md 6節の精神をCLI層にも適用）。`SystemExit`も明示的に捕捉している点に注意——`marketplace_from_url()`や`export_collector_run()`の「対応外ドメイン」「collector_run_id不在」等は`SystemExit`で表現されており、対話式アプリでこれを未捕捉のまま伝播させるとプロセス自体が終了してしまうため。`EOFError`/`KeyboardInterrupt`はメインループの外側で捕捉し、Ctrl+D/Ctrl+Cでも通常終了する。

**テスト方針**: `builtins.input`をmonkeypatchし、回答列を注入した上で実際に`cli.main()`を呼び出し、実PostgreSQL（test-db）に対して結果を検証する（既存のCLI結合テストと同じ「実際にmain()を呼ぶ」流儀）。メニュー4（ブラウザ必須の収集処理）は既存の`BrandPageCollectorService.collect()`同様に自動テスト対象外だが、`marketplace_from_url()`が対応外ドメインで`SystemExit`を送出し、メインループがそれを捕捉してメインメニューへ戻ることは検証した。結合テスト15件（`tests/integration/test_cli_menu.py`）: メインメニュー表示・終了、不正な選択、禁止セラー分岐、未登録セラーの自動登録、登録済み・履歴無しの分岐、登録済み・履歴有りからの取得不可記録、メニュー2単体、重複tool_run_idの処理継続、import-tool-output、不正なファイルパスでのエラー継続、collect-brand-pageの対応外ドメインエラー、import-weekly-listing、continuous-sellers確認（単一/両marketplace）、1メニューのエラーが後続の別メニュー操作をブロックしないことの確認。単体122件・結合137件、全てgreen。

## 36. `seller_performance.cli history`（および対話式メニュー`cli.py`「1」）: 禁止セラーでも取得履歴を併記するよう修正（Phase 3, 2026-09-10）

#35実装直後にユーザーから不具合指摘があった。`storefront_blocked_seller`に登録済みのセラーについて、警告メッセージだけを表示して処理を終了し（`seller_performance.cli history`は`SystemExit`、対話式メニューはメインメニューへ即座に戻る）、通常の取得履歴（`tool_execution_link`・出品成果）を一切表示していなかった。

**正しい仕様**: `storefront_blocked_seller`の理由（`HIGH_RISK_COUNTRY`/`COMPETITOR_SAME_SERVICE`）に関わらず、そのセラーの取得履歴は独立して存在しうる——禁止セラー（同業者利用等）であっても、独自プログラムでASINを取得し、統合ツールの「ASINリスト」機能経由で`tool_execution_link`に登録することは技術的に可能なため。したがって「禁止セラーである」ことと「取得履歴が0件かどうか」は別々の情報であり、両方とも常に提示すべきだった。

**実装**: `format_history()`（`seller_performance/seller_history.py`）を、`entries`が空リストでもクラッシュしない（`days_since_last_run()`等が`entries[-1]`でIndexErrorになっていた）よう拡張した。`entries`が空の場合は「取得履歴はまだありません。」とだけ表示し、経過日数・傾向等の統計行は出力しない（算出不能なため）。`blocked_info`が与えられていれば、`entries`の有無に関わらず先頭に警告を表示してから、この「セラー: .../取得履歴」ブロックを続けて表示する。

`seller_performance.cli`の`_cmd_history()`から「`entries`が空かつ`blocked_info`がある場合はSystemExit」という分岐を削除し、「`entries`が空かつ`blocked_info`も無い場合のみ」`SystemExit("該当するセラーが見つかりません。")`とするよう単純化した（それ以外は常に`format_history()`の出力をそのまま表示する）。

対話式メニュー`cli.py`の`_menu_history()`も同様に、禁止セラー分岐（`blocked_info is not None`）で`return`していた早期終了を削除し、`format_history()`の出力を表示した後に他の分岐（登録済み・履歴有り/無し）と合流して、既存の3択（ひもづける/取得不可と記録/何もしない）を引き続き提示するようにした。「登録済みだが禁止でもなく履歴も無い」場合の既存メッセージ（「seller_id=... は登録済みですが、取得履歴がまだありません。」）は変更していない——これは`format_history()`の汎用メッセージとは別に、`cli.py`側で`SellerService.exists()`により「登録済み」であることを確認済みの、より具体的な文言のため、そのまま維持した（ユーザー指定通り）。

**テスト修正**: 「禁止セラーは警告だけで終了する」ことを前提にしていた既存テストを新仕様に合わせて書き換えた——`tests/integration/test_cli.py`の`test_seller_performance_history_distinguishes_blocked_seller_with_no_history_via_cli`（`SystemExit`を期待）を`test_seller_performance_history_blocked_seller_with_no_history_shows_warning_and_empty_history_via_cli`（正常終了し、警告＋「取得履歴はまだありません。」の両方を確認）に置き換え、既存の`test_seller_performance_history_shows_blocked_notice_via_cli`（禁止セラー＋履歴有り）に履歴部分の表示も確認するアサーションを追加した。`tests/integration/test_cli_menu.py`の`test_history_blocked_seller_shows_message_and_no_further_options`（3択が出ないことを確認していた）を`test_history_blocked_seller_with_no_entries_still_offers_options`（3択が出ることを確認）に置き換え、禁止セラー＋履歴有りのケース（`test_history_blocked_seller_with_entries_shows_both_warning_and_history`）を新規追加した。単体3件（`tests/unit/test_seller_history.py`に`format_history()`の空`entries`処理を追加）を追加。単体125件・結合138件、全てgreen。

実際にローカル開発用DBの実データ（`AZWAN07F20T1D`、`HIGH_RISK_COUNTRY`で登録済み・`tool_execution_link`無し）で`seller_performance.cli history`を実行し、以前は`SystemExit`（終了コード1）で打ち切られていたのが、警告表示に続けて「取得履歴はまだありません。」が表示され、終了コード0で正常終了することを確認した。

## 37. `tool_intake.cli import-tool-output`・対話式メニュー`cli.py`「3」に統合ツール出力ファイルの自動検出を追加（Phase 3, 2026-09-10）

ユーザー依頼により、対話式メニュー「3. 統合ツールで取得したファイルを取り込む」でファイルパスの手入力を省略できるようにした。marketplace・tool-run-idが分かれば、`C:\work\input\processed\primary_check`（JP）または`ex_primary_check`（US）配下から「{tool-run-id}_...」形式のファイル名を自動検出する——`scripts/import_legacy_seller_ledger.py`のファイル名探索ロジック（#31）と同じ考え方（prefix一致、4文字未満のtool_run_idは検索しない安全策）。

**新規モジュール`tool_intake/processed_file_lookup.py`**: `find_tool_output_files(marketplace, tool_run_id, ...) -> list[Path]`（一致する全ファイルを返す。0件・1件・複数件のいずれも呼び出し元が判定する）と`is_plausible_tool_id()`を実装した。`scripts/`側の`build_tool_id_index()`（数千件のtool_run_idを一括で処理するため、フォルダを1回だけ走査してO(1)辞書引きできるようインデックス化する設計、かつ最初の1件のみ採用）とは異なるアルゴリズム形状（1回の呼び出しにつき対象は1つのtool_run_idのみ、複数一致を検出して一覧提示する必要がある）のため、共通化はせず独立した新規実装とした。加えて、`scripts/`は1回限りの移行作業専用コードであり、恒久的なCLIの依存先として参照するのは設計上望ましくないと判断した（`scripts/`側のコードは一切変更していない）。

**ユーザー指示通り、`tool_intake.cli import-tool-output`自体にも同じ改善を適用した**: `--file`を省略可能にし（`resolve_tool_output_file()`）、省略時は自動検出を試みる。0件の場合は「自動検出できませんでした。--fileで指定してください」でエラー終了、複数件見つかった場合は候補一覧を表示した上で「--fileで指定してください」でエラー終了する——非対話のバッチCLIのため、対話式メニューのように一覧から選ばせることはできない（この違いは意図的な設計判断）。`--file`を明示指定した場合は常にそちらが優先され、自動検出は行われない。

**対話式メニュー`cli.py`「3」**: 0件→従来通りファイルパスの手入力にフォールバック、1件→自動的にそのファイルを使用（`ファイルを自動検出しました: {path}`と表示）、複数件→番号付き一覧を表示して選択させる（`_choose_from_candidates()`、不正な番号は再入力を促す）。

**テスト**: 単体9件（`tests/unit/test_processed_file_lookup.py`、prefix一致・実在しないID・複数一致・アンダースコア無しファイル名との非一致・marketplace別ディレクトリ選択・フォルダ不在）+ 単体4件（`tests/unit/test_tool_intake_cli.py`、`resolve_tool_output_file()`の明示指定優先・自動検出単一/0件/複数件の分岐）+ `test_cli_parsers.py`に`--file`省略時のNoneデフォルト確認1件を追加。結合3件（`tests/integration/test_cli.py`に自動検出経由の実DB取込1件、`tests/integration/test_cli_menu.py`にメニュー3の自動検出単一一致・複数一致からの選択2件、既存の0件フォールバックテストの名称・アサーションを明確化）を追加。単体139件・結合141件、全てgreen。

**実データでの動作確認**: ローカル開発用DBではなくtest-dbに対してだが、実際に`C:\work\input\processed\primary_check`配下の実ファイル（`tool_run_id=6546082`）を対象に`python -m tool_intake.cli import-tool-output --marketplace=JP --tool-run-id=6546082`（`--file`省略）を実行し、実ファイルが正しく自動検出され（`ファイルを自動検出しました: C:\work\input\processed\primary_check\6546082_...csv`）、18行が正常に取り込まれることを確認した。

## 38. `seller_ledger.cli delete-seller`/`delete-tool-run`の新規追加: 入力ミス訂正用の削除コマンド（Phase 3, 2026-09-11）

ユーザー依頼により、セラーID・marketplace・tool-run-idの入力ミスに気づいた際に、間違った記録を取り消すための削除コマンドを2つ追加した。物理削除（DELETE文）で、実行前に削除される件数を表示した上でy/nの確認を必ず取る。対話式メニュー`cli.py`にも「7. セラーの記録を削除する」「8. 紐づけの記録を削除する」として追加した。

**実装前にユーザーへ提示し承認を得た3点**:

1. **依頼文の事実誤認を訂正**: 依頼文は「これらのテーブルはCLAUDE.mdの追記専用テーブルには該当しない」としていたが、実際には`tool_output_asin`はCLAUDE.md 3節の追記専用テーブル一覧に明記されており、`app_role`には既に`REVOKE UPDATE, DELETE`が適用済みだった。そのままでは`app_role`接続で`DELETE FROM tool_output_asin`が権限エラーになる。ユーザーに選択肢を提示し、**`admin_role`に限定的な削除権限を付与する**方針で進めることの承認を得た。CLAUDE.md 3節に例外として明記し、`app_role`自体の制約（追記専用の原則）は変更していない。
2. **`storefront_blocked_seller`はdelete-sellerの削除対象に含めない**（ユーザー確認済み）: 禁止セラーの記録はknown_seller_masterとは独立した別の事実であり、セラー記録の訂正では自動的には消さない。
3. **`delete-tool-run`は`seller_attribution`（`first_seen_via_tool_run_id`が一致する行）も削除対象に含める**（ユーザー確認済み）: 含めないと、間違ったtool-runが原因で帰属したASINが`attribute()`のON CONFLICT DO NOTHING（一度帰属したASINは再評価しない）により永久に正されないままになるため。`delete-seller`は元々`seller_attribution`をseller_id基準で削除対象に含んでいる（依頼文通り）。

**新規モジュール`seller_ledger/deletion.py`**: `SellerDeletionService`（`known_seller_master`/`tool_execution_link`/`tool_output_asin`（tool_execution_link経由）/`seller_attribution`の4テーブル）と`ToolRunDeletionService`（`tool_execution_link`/`tool_output_asin`/`seller_attribution`の3テーブル、`known_seller_master`は削除しない）。それぞれ`count()`（件数取得、確認プロンプト用）と`delete()`（実削除）を持つ。

**実装中に判明した2つの実際の落とし穴（実PostgreSQLでの結合テストで発覚・修正済み）**:
- 当初`admin_role`にDELETEのみをGRANTしたが、`permission denied`で失敗した。`DELETE ... WHERE marketplace=... AND tool_run_id=...`のWHERE句・RLSのUSING句の評価にはSELECT権限も必要という、PostgreSQLの一般的な仕様（DELETE権限が暗黙のSELECT権限を含むわけではない）を踏んでいた。`GRANT SELECT, DELETE`に修正した。
- `SellerDeletionService.delete()`は当初、`admin_role`接続内で`tool_execution_link`をSELECTするサブクエリ（対象tool_run_idの絞り込み）を使っていたが、`admin_role`は`tool_output_asin`以外のテーブルへのアクセス権を一切持たないため、これも`permission denied`になった。対応として、対象tool_run_idの一覧を先に`app_role`接続で取得し、`admin_role`接続へは具体的な値のリスト（`tool_run_id = ANY(:ids)`）として渡す設計に変更した——`admin_role`の権限footprint を`tool_output_asin`単体に限定したまま保つため。

**2つのロールをまたぐため完全な原子性は無い**（設計上の制約として明記）: `tool_output_asin`（`admin_role`接続）とその他のテーブル（`app_role`接続）は別々のDB接続・別々のトランザクションで処理される。異なるロールの接続を1つのトランザクションにまとめることはできないため。削除は必ず件数を人に確認させてから実行するため、確認後に片方だけ失敗する可能性は低いが、この制約は将来同種の機能を作る際も踏まえること。

**新規マイグレーション`alembic/versions/0010_grant_admin_role_delete_on_tool_output_asin.py`**: `GRANT SELECT, DELETE ON tool_output_asin TO admin_role`。docs/DEVIATIONS.md #3で予告されていた「admin_roleへのGRANTを拡張する」の実例。

**確認フローの共通化**: `seller_ledger/cli.py::confirm_and_delete_seller()`/`confirm_and_delete_tool_run()`として、件数表示→y/n確認→削除→結果表示の一連の処理を関数化し、`seller_ledger.cli delete-seller`/`delete-tool-run`コマンドと対話式メニュー`cli.py`「7」「8」の両方から同じ関数を呼ぶ（既存の`export_collector_run()`/`resolve_tool_output_file()`と同じDRY方針）。該当データが0件の場合はy/n確認をスキップし「該当するデータがありません。」と表示する（依頼文通り）。

**テスト**: 結合9件（`tests/integration/test_deletion.py`、count/deleteの正確性・他セラー/他marketplaceへの影響が無いこと・storefront_blocked_sellerが削除されないこと・known_seller_masterが残ること）+ CLI結合4件（`delete-seller`のy確認/n確認/該当無し、`delete-tool-run`のy確認）+ 対話式メニュー結合4件（menu 7のy確認/n確認/該当無し、menu 8のy確認）を追加。単体139件・結合158件、全てgreen。

**実データでの動作確認**: ローカル開発用DBへ`alembic upgrade head`を実際に適用し、`admin_role`の`SELECT, DELETE`権限を確認した上で、検証用のセラー（`DELETE_SMOKE_TEST`/`DELETE_SMOKE_RUN`/`B0DELETESM01`）を実際に挿入・`python -m seller_ledger.cli delete-seller`で削除し、正しく削除されること（`known_seller_master`1件・`tool_execution_link`1件・`tool_output_asin`1件）、既存の実データ（`known_seller_master`1102件・`tool_output_asin`2,231,276件）が無傷であることを確認した。

## 39. 対話式メニュー`cli.py`に、メニュー1を経由しない独立した「取得できないセラーとして記録する」（メニュー9）を追加（Phase 3, 2026-09-11）

ユーザー指摘: 従来はメニュー1「セラーの取得履歴を見る」を経由し、その中で（未登録の場合は自動register→link-tool-runの流れを経て）初めて「このセラーは取得できないと分かった」の選択肢が出る設計だった。統合ツールを一度も試していない段階（例: セラーの住所を見ただけで真贋リスクが高いと分かった場合）で禁止セラーとして記録したいケースに対応できていなかった。

**追加内容**: 新規メニュー項目「9. 取得できないセラーとして記録する」。marketplace・seller-idを聞いた後、理由を1（`HIGH_RISK_COUNTRY`）/2（`COMPETITOR_SAME_SERVICE`）から選ばせ、続けてメモ（任意）を聞いて`StorefrontBlockedSellerService.mark_blocked()`を直接呼ぶ。**メニュー1を経由しない**独立した入口であり、`SellerService.register()`（known_seller_masterへの新規登録）は意図的に一切呼ばない——ユーザー指定通り「known_seller_masterへの新規登録は行わない」を厳密に満たす設計。既に登録済みの場合は「既に登録済みです（理由: XXX、登録日: YYYY-MM-DD）」と表示する（元の理由・日付は上書きしない、`mark_blocked()`のON CONFLICT DO NOTHING挙動をそのまま利用）。

**既存の`seller_ledger.cli mark-storefront-blocked`コマンド自体は変更していない**（reason=COMPETITOR_SAME_SERVICE固定のまま、#30時点の設計を維持）。この新メニューはreasonを選択式にした対話式メニュー独自の機能であり、標準コマンド体系には反映していない（依頼が対話式メニューへの追加のみだったため）。

**リファクタリング**: メニュー1「2. このセラーは取得できないと分かった」の処理（`mark_blocked()`呼び出し＋結果表示）と、新設メニュー9の処理は、結果表示ロジックが同一のため`_mark_seller_blocked()`として共通化した。この際、既に登録済みの場合のメッセージの列名を「初回登録日」から「登録日」に統一した（ユーザーが今回の依頼文で明示した表示形式に合わせた。既存の`seller_ledger.cli mark-storefront-blocked`コマンド側の同種メッセージは「初回登録日」のまま、対話式メニュー側とは独立しているため変更していない——依頼のスコープを対話式メニューのみに限定するため）。

副次的な修正: 既存の`test_invalid_choice_shows_message_and_returns_to_menu`テストが「9」を無効な選択肢の例として使っていたが、メニュー9の新設により「9」が有効な選択肢になったため、テストの入力値を「99」に修正した。

**テスト**: 結合4件（`tests/integration/test_cli_menu.py`、HIGH_RISK_COUNTRY選択・COMPETITOR_SAME_SERVICE選択＋メモ入力・既存登録済みセラーでの表示・不正な理由選択時のメッセージ、いずれもknown_seller_masterへ登録されないことを確認）を追加。単体139件・結合162件、全てgreen。

**実データでの動作確認**: ローカル開発用DBに対して実際にメニュー9を実行し（`MENU9_SMOKE_TEST`、理由=HIGH_RISK_COUNTRY）、`storefront_blocked_seller`には登録されるが`known_seller_master`には一切登録されないことを確認した上で、検証用の行を削除して原状回復した。

## 40. 対話式メニュー`cli.py`「1. セラーの取得履歴を見る」の不具合修正: 未登録セラーでも3択を提示する（Phase 3, 2026-09-11）

ユーザー指摘: メニュー1は、未登録セラーの場合registerが実行された直後、3択（統合ツールと紐づける／取得できないと記録する／何もしない）を出さず、いきなり統合ツールIDの入力を求めていた。これは登録済みセラーの場合の動き（3択を提示してから選択に応じて分岐する）と異なっており、当初の依頼（#35）と食い違っていた。

**原因**: `_menu_history()`の分岐が`elif not is_registered: ...; _do_link_tool_run(..., auto_register=True); return`という構造になっており、未登録セラーは3択の表示コードに一切到達せず、`_do_link_tool_run()`内部の`if auto_register:`ブロックで登録した直後、そのまま同じ関数内で統合tool IDの入力を続けて求めてしまっていた。

**修正**: 未登録セラーの場合は、3択を提示する**前**にその場で`SellerService.register()`を実行するだけに変更し、以降の表示・3択は登録済みセラー（かつ取得履歴が無い状態）と完全に同じコードパスに合流させた。これにより、未登録セラーで選択肢1（ひもづける）・2（取得できないと記録する）・3（何もしない）のいずれも登録済みセラーと同様に機能する。

**副次的なリファクタリング**: 上記の結果、`_do_link_tool_run()`の`auto_register`引数はどの呼び出し元からも渡されなくなった（常にFalse相当）ため、死んだ分岐として削除し、`_do_link_tool_run(app_factory, marketplace, seller_id)`（3引数）に簡素化した。

**テスト**: 既存の`test_history_unregistered_seller_routes_to_register_and_link`（旧仕様——3択を経ずに直接ひもづけへ進むことを検証していた）を`test_history_unregistered_seller_registers_then_shows_same_three_choices_as_registered`に置き換え、3択が実際に表示されること・選択肢1を選んだ場合にひもづけまで完了することを検証するようにした。加えて、未登録セラーで選択肢2（取得できないと記録する）・選択肢3（何もしない）もそれぞれ正しく機能することを確認する新規テスト2件を追加した（従来は選択肢1の経路しか検証されておらず、この不具合そのものは主に選択肢2・3が到達不能だったことに起因するため、両方の直接的な検証を追加する意味は大きい）。単体139件・結合164件、全てgreen。

**実データでの動作確認**: ローカル開発用DBに対して未登録セラー（`MENU1_UNREG_SMOKE`）でメニュー1を実行し、登録直後に3択が正しく表示され、「3. 何もしない」を選んでも登録自体は完了していることを確認した上で、検証用の行を削除して原状回復した。

## 41. `storefront_blocked_seller.reason`の整理: `HIGH_RISK_COUNTRY`→`PERMANENTLY_EXCLUDED`へリネーム・意味を拡張、noteが表示されない不具合を修正（Phase 3, 2026-09-09）

ユーザー指摘: 旧`HIGH_RISK_COUNTRY`（真贋リスクが高い国のセラー）は狭すぎる名前だった。実際には「低単価で利益にならない」等、真贋リスク以外の理由でも「今後も取得しない」と判断するケースがある。これらは全て「今後も取得しない」という同じ意味のグループに属するため、reasonの値そのものに理由の詳細をエンコードするのをやめ、`PERMANENTLY_EXCLUDED`（今後も取得しない。詳細はnoteに書く）へ改名・意味を拡張する。`COMPETITOR_SAME_SERVICE`（いずれ別手段でASINを取得できる）は意味が異なるため変更しない。

**スキーマ変更（`alembic/versions/0011_rename_storefront_blocked_seller_reason.py`）**: `ck_storefront_blocked_seller_reason`のCHECK制約を`DROP`→**実データのUPDATE**（`reason='HIGH_RISK_COUNTRY'`の行を`'PERMANENTLY_EXCLUDED'`へ）→新しい許容値でCHECK制約を再`ADD`、の順で実行する。この順序が必須——新制約を先に追加すると、その時点でまだ残っている`HIGH_RISK_COUNTRY`の既存行が新制約に違反してマイグレーションが失敗する。`storefront_blocked_seller`はdb/models.pyの共有metadataに登録されていないテーブル（#30参照）のため、素の生SQLによる増分マイグレーションとして実装した。

**実データでの検証**: ローカル開発用DBには`scripts/import_legacy_seller_ledger.py`（#31）による実データが既に284件`HIGH_RISK_COUNTRY`として登録されていた。マイグレーション適用前後で`SELECT reason, count(*) FROM storefront_blocked_seller GROUP BY reason`を実行し、284件が過不足なく`PERMANENTLY_EXCLUDED`へ移行し、`COMPETITOR_SAME_SERVICE`の6件は影響を受けないことを確認した。`downgrade 0010`→`upgrade head`のロールバック往復も実データに対して実行し、件数が両方向で正しく保持されることを確認した。

**コード側の変更**:
- `seller_ledger/storefront_blocked.py`: `Reason`型・`REASON_LABELS`のキーをリネーム（新しいラベルは「今後も取得しない」）。
- `tool_intake/log_ingest.py`: 「禁止セラー」ステータス行の自動記録が`PERMANENTLY_EXCLUDED`固定で記録するよう変更（トリガー条件・処理経路自体は変更なし）。
- `scripts/import_legacy_seller_ledger.py`: 「禁止セラー」の移行先reasonも同様に変更（このスクリプト自体は#31で既に実行済みの1回限りの移行スクリプトだが、将来の再実行や別データでの再利用に備えて追随させた）。
- 対話式メニュー`cli.py`のメニュー1「2. このセラーは取得できないと分かった」を、メニュー9（#39）と同じ2択（`PERMANENTLY_EXCLUDED`/`COMPETITOR_SAME_SERVICE`）から選ばせる方式に変更した（以前は`COMPETITOR_SAME_SERVICE`固定だった）。プロンプト文言・選択肢マッピングは`_ask_block_reason()`として新設し、メニュー1「2」・メニュー9の両方から共通利用する（重複排除、メニュー9側の選択肢文言もこの機会に新しい意味に合わせて更新した）。

**noteが表示されない不具合の修正**: `BlockedSellerInfo`データクラスに`note`フィールドが存在せず、`StorefrontBlockedSellerService`の各SQL（`mark_blocked()`のRETURNING句・`get_blocked_info()`のSELECT句）もnote列を取得していなかったため、DBに記録されていても画面には一切表示されていなかった。`note`フィールドを追加し、両クエリでnoteも取得するよう修正した上で、以下の3箇所の表示メッセージにメモを追記する処理を加えた（noteがNoneの場合はメモ部分自体を出力しない）:
1. `seller_performance/seller_history.py::format_history()`の警告メッセージ（`seller_performance.cli history`・対話式メニュー1の両方がこの関数を共有しているため一箇所の修正で両方に反映される）
2. `cli.py::_mark_seller_blocked()`の「既に登録済みです」メッセージ（メニュー1「2」・メニュー9共通）
3. `seller_ledger/cli.py::_cmd_mark_storefront_blocked()`の「既に登録済みです」メッセージ（ユーザー依頼の直接のスコープ外だが、`BlockedSellerInfo`にnoteが追加された以上、この同種メッセージだけ古いままにするのは一貫性を欠くため合わせて修正した）

**テスト**: 単体2件追加（`test_reason_label_for_permanently_excluded`に改名、`format_history`のnote表示あり/なし各1件）、結合9件追加（旧`HIGH_RISK_COUNTRY`がCHECK制約で拒否されること、`get_blocked_info`のnote往復2件、メニュー1「2」でnoteが表示されること・`PERMANENTLY_EXCLUDED`も選べること・不正な理由選択のメッセージ、メニュー9のnote付き既存登録メッセージ）。既存テストのうち`HIGH_RISK_COUNTRY`/旧ラベルを前提にしていたものは全て新しい値・ラベルに更新し、メニュー1「2」の入力シーケンスには理由選択の1ステップを追加した。単体141件・結合170件、全てgreen。

**実データでの動作確認**: ローカル開発用DBに対して実際に対話式メニュー1からセラーを新規登録→「2」→理由1（`PERMANENTLY_EXCLUDED`）→メモ「low margin」で記録し、`seller_performance.cli history`の警告メッセージに「メモ: low margin」が正しく表示されることを確認した上で、検証用の行（`storefront_blocked_seller`・`known_seller_master`双方）を削除して原状回復した。

## 42. `storefront_blocked_seller`の誤登録を訂正する専用コマンド`delete-storefront-block`を追加、対話式メニューにも反映（Phase 3, 2026-09-09）

ユーザー指摘: 禁止セラーの理由を間違えて登録してしまった場合（例: 本来`PERMANENTLY_EXCLUDED`のはずが`COMPETITOR_SAME_SERVICE`で登録されていた）、訂正する手段が無かった。`seller_ledger.cli delete-seller`は意図的に`storefront_blocked_seller`を削除対象から除外している（#38、独立した事実のため）ため、別の専用コマンドが必要だった。

**新規コマンド**: `python -m seller_ledger.cli delete-storefront-block --marketplace=<US/JP> --seller-id=<ID>`。`storefront_blocked_seller`はmarketplaceに依存しないテーブルだが、コマンド体系の一貫性のため`--marketplace`を受け取る——実際の検索・削除はseller_idのみで行い、`--marketplace`は`SessionFactory.get_session()`の接続確立に使うだけ（表示メッセージ上も特に使わない、ユーザー指定通り「表示メッセージ用に使う程度でよい」だが、実際には既存の`mark-storefront-blocked`の固定`"US"`接続と違い、素直に`args.marketplace`をそのまま接続に使う実装にした——このテーブルにRLSが無いため、どちらのmarketplaceで接続してもDELETE対象は同じ1行になる）。

**新しいサービスメソッド**: `StorefrontBlockedSellerService.delete_blocked(conn, seller_id) -> BlockedSellerInfo | None`（`seller_ledger/storefront_blocked.py`）。物理DELETE、`RETURNING`で削除された行の内容を返す。未登録なら`None`を返し、呼び出し元がエラーにせず「登録されていません」と表示できるようにする——`mark_blocked()`と対称的な設計。**admin_role接続は不要**: `storefront_blocked_seller`は追記専用テーブルではなく、`app_role`は既にINSERT/UPDATE/DELETEをフルに持っている（alembic/versions/0008で最初から`GRANT SELECT, INSERT, UPDATE, DELETE ON storefront_blocked_seller TO app_role`済み）ため、`delete-seller`/`delete-tool-run`（#38）のような`admin_role`分岐は発生しない——この機能がそれらより単純である理由。

**確認フロー**: `seller_ledger/cli.py::confirm_and_delete_storefront_block()`。`get_blocked_info()`で存在確認→無ければ「seller_idは登録されていません。」で終了→あれば「本当に削除しますか？（理由: XXX、登録日: YYYY-MM-DD、メモ: XXX）」を表示しy/n確認→yesなら`delete_blocked()`を呼び「削除しました」を表示、noなら「削除を中止しました。」。`confirm_and_delete_seller()`/`confirm_and_delete_tool_run()`（#38）と同じ「count/info→表示→y/n→実行」の型を踏襲。

**対話式メニュー`cli.py`のメニュー1への反映**: 禁止セラーの警告表示（`format_history()`の出力）に続けて「このブロック記録を削除しますか？(y/n)」を追加し、yesならその場で`StorefrontBlockedSellerService().delete_blocked()`を直接呼んで削除する。**あえて`confirm_and_delete_storefront_block()`を再利用しなかった**: その関数は削除前に理由・登録日・メモを改めて表示してから確認するが、メニュー1では直前の警告メッセージ自体が既にそれらを表示済みのため、同じ詳細を繰り返す確認は冗長——メニュー側は「このブロック記録を削除しますか？(y/n)」という、既に見えている情報を前提にした簡潔な確認のみとし、標準コマンド側の詳細な確認文言とは別物として実装した（両者を無理に共通化するとどちらかの文言・情報量が損なわれるため、これまでの「本当に同一の処理のときだけ共通化する」方針——#39の`_mark_seller_blocked()`共通化とは逆に、あえて共通化しない判断）。削除後（およびnoを選んだ場合）は、そのまま既存の（未登録/登録済みの）3択の提示に進む——ブロック記録の削除は`known_seller_master`の登録状態に一切影響しない独立した操作のため。

**テスト**: 結合3件追加（`tests/integration/test_storefront_blocked.py`: `delete_blocked()`が削除して削除内容を返すこと・未登録ならNoneを返すこと・削除後に正しい理由で再登録できること）、結合3件追加（`tests/integration/test_cli.py`: `delete-storefront-block`のy確認・n確認・未登録時の無確認メッセージ、`delete-seller`系の既存テストと同じ型）、結合2件追加（`tests/integration/test_cli_menu.py`: メニュー1でyesを選ぶと削除されthen3択に進むこと・noを選ぶと削除されず3択に進むこと）。加えて、メニュー1が禁止セラーの警告を表示する既存の結合テスト3件（`test_history_blocked_seller_with_no_entries_still_offers_options`等）は、新しい削除確認y/nプロンプトが割り込むようになったため、入力シーケンスに`"n"`を1つ挿入する形で更新した。単体141件（変更なし）・結合178件、全てgreen。

**実データでの動作確認**: ローカル開発用DBに対して実際に`mark-storefront-blocked`でテスト行を登録→`delete-storefront-block`をn（中止、行が残ることを確認）→再度y（削除、行が消えることを確認）→再々度実行（「登録されていません」を確認）を一通り実行。続けて対話式メニュー1からも同様に登録→メニュー1で警告表示→「y」でその場から削除→通常の3択（「3. 何もしない」）まで正しく進むことを確認した。いずれも検証用の行はコマンド自体の削除処理で既に消えており、追加の後片付けは不要だった。

## 43. `seller_performance.cli recommend`の一覧表示に判断材料を追加、並び順を変更（Phase 3, 2026-09-09）

ユーザー依頼: 一覧に「合計出品成果」「最終取得日」「前回比」「直近90日以内の取得日数（重複排除）」を追加し、並び順を「最終取得日の昇順（古い順）」優先→同点なら「取得日数（重複排除）の昇順」に変更する。既存の絞り込み条件（`lookback_days`・`min_listing_count`・`min_days_since_last`）は一切変更しない。

**実装前に方針を確認**: 「前回比」の計算方法についてAskUserQuestionで提案したところ、1問目（個々のtool_execution_link行同士で比較 vs 同じ日の複数回をまとめてから比較）は後者（まとめてから比較）を選択。2問目（直近90日の基準を固定90日にするか--lookback-daysに連動させるか）は質問の意図が伝わらなかった（「二つ目は意味不明」）ため、AskUserQuestionを使わずプレーンテキストで方針を提示し直し、固定90日案で確認を取ってから実装した。**教訓**: AskUserQuestionは、選択肢の意味が一目で分かる具体的な対比（コード例・出力例）を伴わないと機能しない——1問目はpreviewで具体例を示せたので機能したが、2問目は抽象的な二択のみだったため伝わらなかった。次回以降、抽象的な設計判断はプレーンテキストで提示する方が確実な場合がある。

**確定した仕様**:
- `total_listed`（合計出品成果）: そのセラーに帰属する出品完了済みASINの総数。全期間（lookback_days非依存）。
- `last_requested_date`（最終取得日）: `tool_execution_link.requested_at`の最新の日付（全期間）。
- `recent_fetch_days`（直近90日以内の取得日数、重複排除）: `--lookback-days`の値に関わらず常に固定90日でカウントする（異なる`--lookback-days`指定をまたいで同じ意味で比較できる安定した指標にするため）。同じ日の複数回取得は1日として数える。
- `previous_delta`（前回比）: `tool_execution_link`を取得日（日付）単位でグルーピングし、その日の出品成果（同日複数回は合算）を直近の取得日とその1つ前の取得日で比較した差分。取得日が1日しかない（比較対象が無い）場合は`None`（表示は「-」）。
- 出力イメージの「取得回数」は新規項目ではなく、既存の`listing_occasions`（絞り込みにも使う、直近lookback_days日以内の異なる出品確認日数）を日本語ラベルで表示しただけ——計算方法・絞り込みへの影響は変更していない。
- 並び順: `last_requested_date ASC, recent_fetch_days ASC, seller_id`（最終取得日が古い順優先、同点ならrecent_fetch_daysが少ない順）。

**実装**: `seller_performance/seller_recommender.py::SellerRecommenderService.recommend()`のSQLをCTE構成に拡張（`candidates`＝既存の絞り込みクエリを完全に無変更のまま維持、`per_run`＝tool_execution_link単位の出品成果、`daily`＝同日合算、`ranked`＝日付降順の順位付け、`seller_stats`＝合計・最終取得日・直近90日カウント、`deltas`＝rn=1とrn=2の差分）。`candidates`で既に絞り込まれた集合に対してのみ`per_run`以降を計算するため、既存の絞り込みロジック自体には一切触れていない。`SellerRecommendation`データクラスに4フィールド追加。表示整形は`format_recommendation_line()`として新設し、`seller_performance.cli recommend`と対話式メニューcli.pyのメニュー5の両方から共通利用する（両画面が同じサービスを呼んでいるため、元々ほぼ同一の表示だった）。

**実データで発見した実バグ**: 実装直後にローカル開発用DBの実データ（JP 263件・US 88件のrecommendations）に対して`recommend`を実行したところ、263件中の途中で`ValueError: invalid format string`でクラッシュした。原因はPostgreSQLの`SUM(bigint)`が`numeric`型を返すこと——psycopgはこれをPythonの`Decimal`にマッピングするため、`f"{delta:+d}件"`（`d`書式指定子はintのみ対応）が失敗していた。取得日が1日しかない（`previous_delta`が`None`）セラーだけが先に263件中十数件表示された後、2日以上の取得日を持つ最初のセラー（`previous_delta`が実際に整数の差分になる、かつDecimal型になる）でクラッシュしていた。**この種のバグは単体テスト・多くの結合テストでは検出できなかった**——`SellerRecommendation`データクラスのフィールドを直接assertするテストはPythonのint同士の比較で常に成功してしまい（`Decimal(-1) == -1`はTrueのため）、実際に`format_recommendation_line()`のフォーマット処理を通す経路（CLI経由の`print()`）でしか再現しない。修正: SQLの`daily`・`seller_stats`CTEで`SUM(...)::bigint`に明示キャストし、`format_recommendation_line()`自体にも防御的に`int()`変換を追加（二重の対策——SQL側のキャストが将来別の集計に置き換わってもこの関数単体では壊れないように）。

**テスト**: 単体4件新規（`tests/unit/test_seller_recommender.py`新設、`format_recommendation_line()`の全項目表示・符号付き前回比・比較不可時のダッシュ表示・Decimal型が渡っても壊れないことを直接検証——このDecimalケースが上記の実バグを再現する回帰テスト）、結合6件新規（`tests/integration/test_seller_performance.py`: 新規4項目の値検証、前回比の同日合算検証、前回比が1日しかない場合None、`recent_fetch_days`が`--lookback-days`を広げても固定90日のままであること、並び順検証）、結合2件新規（`tests/integration/test_cli.py`: CLI経由での新項目表示、**DBを実際に経由して前回比が非Noneになる経路**——これが無ければ上記の実バグは今後も再発する）。単体145件・結合186件、全てgreen。

**実データでの動作確認**: 修正後、ローカル開発用DBに対して`recommend --marketplace=JP`（263件）・`recommend --marketplace=US`（88件）を実行し、いずれも最後まで正常終了・新項目が正しく表示され、最終取得日の昇順に並んでいることを確認した。対話式メニュー5からも同じ表示（`format_recommendation_line()`共通利用）になることを確認した。DBへの書き込みは発生しない読み取り専用コマンドのため、後片付けは不要だった。
