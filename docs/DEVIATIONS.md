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

## 44. `tool_intake.cli sync-pending`新設、`seller_performance.cli history`の日付基準を依頼日→取込日へ変更（「未取込」「0件」の区別）（Phase 3, 2026-09-16）

ユーザー依頼: (1) `tool_execution_link`にはあるが`tool_output_asin`が未取込のtool_run_idをまとめて検出し、`processed_file_lookup`で自動検出できるものは取込を実行する新規コマンド`sync-pending`。(2) `seller_performance.cli history`の各行の日付を依頼日ではなく取込日に変更し、「取込ASIN数」列を追加、「未取込」（一度も取込を試みていない）と「0件」（取込済みだが該当ASINが無かった）を区別する。

**実装前にAskUserQuestionで確認した2点**（具体例をpreviewに入れた1問目は機能したが、2問目は選択肢テキストを読んだ上でユーザーが意図を汲んで独自の回答を選んだ——#43の教訓通り、抽象的な選択肢だけでは伝わりにくいことの再確認）:

1. `tool_output_asin`に複数行ある場合の「取込日」の代表値: **MIN(imported_at)**（最初にそのtool_run_idのASINが記録された日）を採用。同一ファイルの再取込で後から行が追加されても、「最初に未取込でなくなった日」を示す。
2. `sync-pending`の対象範囲: 過去に取込を試みて「0件」だったtool_run_idは、**次回以降は対象から除外する**（ユーザー回答: 「0件のファイルがあったら次回以降除外。一度もファイルを取り込んでいなければ含める。」）。真に「未取込」（`tool_output_asin`が0行、かつ取込を試みた記録も無い）のものだけを対象にする。

**「0件」と「未取込」の判定方法（スキーマ変更なし）**: `tool_output_asin`に該当tool_run_idの行が1件も無い場合、`import_run_history`（`import_type='TOOL_OUTPUT'`、`status='COMPLETED'`）に、統合ツールの命名規則「{tool_run_id}_...」のprefixに一致する`source_file_name`を持つレコードがあるかをLIKE検索で判定する（`LIKE (tool_run_id || '\_%') ESCAPE '\'`でtool_run_id中の`_`をエスケープ）。この命名規則は`processed_file_lookup.find_tool_output_files`が既に前提にしているものに加え、`tool_intake/log_ingest.py`が読む統合ツールのログ画面（`log-csv`列）の実データ（`tests/unit/test_log_ingest.py`のフィクスチャ、例: `7137846_latraviata.imports.jp@gmail.com_2026-09-06 14:15:55.csv`）でも同じprefix規則であることを確認できたため、`import_run_history`への列追加（例: `tool_run_id`列）は不要と判断した。

**実装**:
- `tool_intake/pending_sync.py`（新設）: `PendingSyncService.find_pending()`が上記のSQL（`tool_output_asin`に行が無く、かつ`import_run_history`に完了記録も無いもの）を実行し、`sync()`が各tool_run_idについて`processed_file_lookup.find_tool_output_files()`で自動検出→1件見つかれば`IntegrationToolImportService.import_file()`で取込、0件・複数件なら「未検出」のまま残す（複数件を自動選択しないのは`tool_intake.cli import-tool-output`の`--file`省略時と同じ方針）。1件の取込失敗（例外）は`errored`に記録して次のtool_run_idの処理を継続する（CLAUDE.md 6節）。
- `tool_intake/cli.py`に`sync-pending --marketplace=<US/JP>`を追加。対話式メニュー`cli.py`にはメニュー10「未取込ファイルをまとめて確認・取り込む」として追加し、同じ`PendingSyncService`を共通利用する。
- `seller_performance/seller_history.py`: `HistoryEntry`に`asin_count`・`import_date`（`date | None`）を追加（既存の`requested_date`・`listed_count`はdays_since_last_run/recent_average/recent_trendがそのまま使うため維持）。`get_history()`のSQLをCTE構成（`toa_stats`/`attempted`/`listed`）に変更し、`tool_output_asin`側と`import_run_history`側を別々に集計してから`tool_execution_link`にLEFT JOINすることで、複数JOINによるファンアウト（件数の重複カウント）を避けた（`seller_recommender.py`の既存CTEパターンを踏襲）。`format_history()`の各行表示を「取込日 取込ASIN数 出品成果」に変更し、`import_date is None`のときのみ「未取込」「-」と表示する（`asin_count=0`かつ`import_date`が実際の日付を持つ「0件」とは表示が異なる）。

**テスト**: 単体5件新規（`tests/unit/test_pending_sync.py`新設: 自動取込・未検出（0件/複数件）・エラー時の継続・`format()`の3区分表示）、単体3件更新+2件新規（`tests/unit/test_seller_history.py`: `_entry()`ヘルパーにasin_count/import_date引数を追加、format_historyの新しい行表示・「未取込」表示・「0件」と「未取込」の区別を検証する新規2件）、単体2件新規（`tests/unit/test_cli_parsers.py`: `sync-pending`のパース・必須引数）。結合6件新規（`tests/integration/test_pending_sync.py`新設: `find_pending()`のSQL検証——未取込/取込済み/0件（除外）/prefix不一致・status不一致（除外しない）/marketplace分離）、結合3件新規（`tests/integration/test_seller_history.py`: MIN(imported_at)の代表値検証、`import_run_history`経由の「0件」判定、prefix不一致・status不一致時は「未取込」のまま）、結合2件新規（`tests/integration/test_cli.py`: `sync-pending`のCLI経由での自動取込・未検出）、結合2件新規（`tests/integration/test_cli_menu.py`: メニュー10の自動取込・未検出）。既存の`seller_performance.cli history`/対話式メニュー1の結合テストのうち、`tool_output_asin`を挿入していないシナリオ（取込前の状態）は表示が「日付 0件」→「未取込 - 0件」に変わるため、該当箇所を更新した。単体154件・結合199件、全てgreen。

**テスト実行時に発見・修正した点**: `sync-pending`のCLI/メニュー結合テストは、同一テストモジュール内の他のテストが残した「未取込」の`tool_execution_link`行（DBはモジュール単位でしかリセットしない）も`find_pending()`の対象に含まれてしまうため、`find_tool_output_files`のモックを対象tool_run_id以外には空リストを返すよう修正した（他のテストの副作用で件数アサーションが不安定にならないようにするため）。また`tests/integration/test_seller_history.py`の`_insert_tool_output()`はデフォルトで`imported_at`をDBの`now()`任せにしていたため、既存の`test_lists_runs_oldest_first_with_listed_counts`に`import_date`のアサーションを追加した際、実行日（テスト実行時の日付）と食い違って落ちた。`_insert_tool_output()`に`imported_at`を明示指定できる引数を追加し、この種のテストでは明示的な日時を渡すよう修正した。

**実データでの動作確認は未実施**: 本エントリの変更は開発機のディスポーザブルなtest-db（`docker compose up -d test-db`）に対する結合テストのみで検証しており、ローカル開発用DB（`db`サービス）の実データに対する動作確認は行っていない（`sync-pending`はファイル自動検出のためC:\\work\\input\\processed配下の実ファイルに依存し、無条件に実行すると実データへ書き込みが発生するため）。実運用前に、ローカル開発用DBに対して少数の「未取込」tool_run_idを対象に`sync-pending`を試し、想定通りの挙動になることを確認すること。

## 45. `seller_performance.cli history`の日付基準を「取込日」（imported_at）から「取得日」（requested_at）へ再変更——Excel移行データでの不自然な日付表示を修正（Phase 3, 2026-09-17）

ユーザー指摘: #44で`history`の日付表示を依頼日（`tool_execution_link.requested_at`）から取込日（`tool_output_asin.imported_at`）へ変更したが、Excelから移行したセラー実績データ（`scripts/import_legacy_tool_output.py`、#32参照）では、`imported_at`は「移行作業（バッチ）を実際に実行した日」になってしまい、本来の依頼日とは無関係な不自然な日付が表示されていた。一方`recommend`側のsummary（`seller_recommender.py`の`last_requested_date`、#43参照）は元々`requested_at`基準のままだったため、同じデータを見る`history`と`recommend`で日付の基準が食い違ってしまっていた。

**対応**: `history`の日付表示を`tel.requested_at`基準へ戻す。列名は「取込日」から**「取得日」**へ変更した（`recommend`側の「最終取得日」ラベルと表記を揃えるため。「依頼日」も検討したが、既存の集計行「直近の取得日からの経過日数」・`recommend`の「最終取得日」が既に`requested_at`を「取得日」と呼んでいたため、そちらに合わせるのが同じコマンド内・コマンド間の一貫性の観点で自然と判断した）。

**「取込ASIN数」列（未取込「-」/0件/N件の区別）は#44の仕様のまま変更していない**——`tel.requested_at`はtool_execution_linkのNOT NULL列のため常に値が存在し「未取込」にはなり得ない一方、取込ASIN数（`tool_output_asin`基準）は依然として「まだ0行」「試みたが0行」を区別する必要があるため、日付とASIN数という2つの独立した軸は今後もこのまま残す。

**実装**: `seller_performance/seller_history.py`の`_GET_HISTORY_SQL`から`MIN(imported_at)::date`/`MIN(irh.executed_at)::date`という「代表日」の算出を削除し、`HistoryEntry.import_date: date | None`を`import_attempted: bool`（`tool_output_asin`に行がある、または`import_run_history`に完了記録があるかの単純な真偽値）に置き換えた。`format_history()`の各行は常に`tel.requested_at::date`（`requested_date`）を表示し、`import_attempted`が真のときのみ`asin_count`を、偽なら「-」を表示する。0件と取込ASIN数を判定する`import_run_history`のprefix/status一致ロジック自体（#44）は変更していない。

**教訓**: 同じ元データ（`tool_execution_link`）を複数のコマンド（`history`・`recommend`）で表示する場合、日付など基本的な軸の意味づけをコマンドごとに独自変更すると、Excel移行データのような「実際の記録日と業務上の意味が乖離しているデータ」で症状が顕在化するまで気づきにくい。今後`history`側で日付関連の表示を変える際は、`recommend`側の対応する項目（現状は`last_requested_date`＝`requested_at`基準）との整合を都度確認すること。

**テスト**: `tests/unit/test_seller_history.py`の`_entry()`/`_pending_entry()`ヘルパーを`import_date`引数から`import_attempted`引数へ変更し、`format_history()`の行表示アサーションを「取得日」ヘッダー・常時日付表示＋`import_attempted`による「-」/`N件`分岐に更新（未取込・0件を区別する新規テストは維持、日付は両ケースとも表示されるように変更）。`tests/integration/test_seller_history.py`は`import_date`関連のアサーションを`import_attempted`へ置き換え、MIN(imported_at)の代表値検証テスト（#44で追加、この変更で対象の仕組み自体が無くなったため）を削除し、`asin_count`・`import_attempted`のみを検証するテストに置き換えた。`tests/integration/test_cli.py`/`tests/integration/test_cli_menu.py`の「未取込」行表示アサーション（`未取込      -      0件`）を、`requested_date`が常に表示されるようになったことに合わせて更新した（`2026-05-22      -      0件`等）。単体154件・結合199件、全てgreen（テストDBはディスポーザブルなtest-dbのみで検証、ローカル開発用DBの実データでの確認は#44と同様に未実施）。

## 46. `seller_performance.cli history`（対話式メニュー含む）の各行に統合ツールID（tool_run_id）列を追加（Phase 3, 2026-09-18）

ユーザー依頼: セラーIDの表記ミス（余分な文字の混入等）を訂正する際、`delete-seller`/`delete-tool-run`（CLAUDE.md 3節の例外、`admin_role`限定の削除コマンド、#38参照）で削除する前に、紐づいている`tool_run_id`を控えておく必要がある。しかし従来の`history`表示には`tool_run_id`が出ておらず、確認するにはDBへ直接SQLを実行するしかなかった。

**対応**: `HistoryEntry.tool_run_id`は#44・#45のSQL（`_GET_HISTORY_SQL`）で元々取得済み（並び替え・突き合わせのキーとして内部利用していた）だったため、SQL変更は不要で、`format_history()`の表示にこの列を追加するだけで済んだ。

**表示**: ヘッダーを「取得日　　　　　統合ツールID　　取込ASIN数　　出品成果」とし、日付と取込ASIN数の間に統合ツールID列を挿入した（ユーザー提示の出力イメージ通りの列順）。例:

```
セラー: A1ZLNQALRE0XNB& (marketplace=JP)
──────────────────────────────────────────
取得日          統合ツールID    取込ASIN数    出品成果
2026-02-10      6931276         424件         60件
──────────────────────────────────────────
```

列の区切りは既存の固定スペース方式（#44・#45から踏襲、tool_run_idの桁数によって厳密な縦揃えにはならない）をそのまま使い、独自の動的パディング等は実装していない（既存の「取得日」「取込ASIN数」列も同様の簡易表示のため、この列だけ精緻に揃える理由が無いと判断）。

**テスト**: `tests/unit/test_seller_history.py`の`_entry()`ヘルパーに`tool_run_id`引数を追加（省略時は従来通り`f"run-{day}"`）。既存の行表示アサーションに`tool_run_id`を挿入する形で更新し、ユーザー提示の出力イメージ（`tool_run_id=6931276`、`セラー=A1ZLNQALRE0XNB&`）をそのまま再現する新規テストを1件追加した。`tests/integration/test_cli.py`/`tests/integration/test_cli_menu.py`の該当アサーションも、テストが実際に使っている`tool_run_id`変数を埋め込む形で更新した。単体155件・結合199件、全てgreen（ディスポーザブルなtest-dbで検証、ローカル開発用DBの実データでの目視確認は未実施）。

## 47. `storefront_blocked_seller.reason`に3つ目の値`RETRY_LATER`を追加、対話式メニューの理由選択を3択に拡張、メニュー1の削除確認質問を廃止（Phase 3, 2026-09-18）

ユーザー依頼、2点:

1. 既存の2択（PERMANENTLY_EXCLUDED＝今後も取得しない／COMPETITOR_SAME_SERVICE＝同業者利用）では、「同業者利用かは不明だが、何らかの一時的な理由で今は取得できず、いずれ再挑戦したい」というケースを表現できなかった。3つ目の値`RETRY_LATER`を追加する。
2. 対話式メニュー「1. セラーの取得履歴を見る」で、禁止セラーの警告表示後に出ていた「このブロック記録を削除しますか？(y/n)」の質問を削除する。警告表示（理由・登録日・メモ）自体はそのまま残し、質問が無くなった分、すぐ後に通常の取得履歴表示・3択の提示へ進む。`delete-storefront-block`コマンド自体は変更せず、独立したコマンドとして引き続き使える。

**1. RETRY_LATER追加**:
- `alembic/versions/0012_add_retry_later_to_storefront_blocked_seller_reason.py`（新規増分マイグレーション）でCHECK制約を`(PERMANENTLY_EXCLUDED, COMPETITOR_SAME_SERVICE)`→`(PERMANENTLY_EXCLUDED, COMPETITOR_SAME_SERVICE, RETRY_LATER)`へ拡張。既存データのUPDATEは不要（値の追加のみ、リネームではない）。
- `seller_ledger/storefront_blocked.py`の`Reason`型・`REASON_LABELS`に`RETRY_LATER: "一時的に取得できない"`を追加。
- 対話式メニュー`cli.py`の`_ask_block_reason()`（メニュー1「2」・メニュー9で共通利用）を3択に拡張し、`_BLOCK_REASON_CHOICES`に`"3": "RETRY_LATER"`を追加。不正な選択肢のメッセージも「1または2を入力してください。」→「1〜3を入力してください。」に更新（2箇所）。
- `seller_ledger.cli mark-storefront-blocked`コマンド自体（reasonはCOMPETITOR_SAME_SERVICE固定）は変更していない——RETRY_LATERは対話式メニューからのみ選択できる。

**downgradeで踏んだ実バグ**: 当初の`0012`のdowngradeは、RETRY_LATER行が残っている場合はCHECK制約違反でそのまま失敗させる設計にしていた（#11のようなリネームと異なり、RETRY_LATERに対応する2値時代の値が存在しないため）。しかし`tests/integration/conftest.py::_migrated_schema`はテストモジュールごとに`alembic downgrade base`を実行する設計であり、あるテストモジュールがRETRY_LATER行を1件でも残したまま終了すると、それ以降に実行される全テストモジュールの`_migrated_schema`セットアップが軒並み失敗する（実際にRETRY_LATER関連のテストを追加した直後、後続の無関係な15個のテストモジュールがエラーになる形で発覚した）。#11と同様、downgrade側でCHECK制約を外す前に`UPDATE ... SET reason = 'PERMANENTLY_EXCLUDED' WHERE reason = 'RETRY_LATER'`を実行してから制約を戻す方式に修正した——「一時的に取得できない」という区別自体を無くすdowngradeなので、意味的な情報が失われることは許容し、必ず成功することを優先した。修正後、実際にRETRY_LATER行を1件挿入した状態で`alembic downgrade 0011`→`upgrade head`のロールバック往復を手動実行し、行が`PERMANENTLY_EXCLUDED`へ正しく変換されることを確認した。

**教訓**: このプロジェクトの結合テストは「モジュールごとに`downgrade base`で完全リセットする」前提のため、CHECK制約の値を追加するだけの一見シンプルなマイグレーションでも、downgrade側は「対象データが残っていても必ず成功する」ように作る必要がある（`DROP TABLE`ベースのdowngradeでは問題にならないが、既存テーブルの制約を狭める形のdowngradeでは常に注意が必要）。

**2. メニュー1の削除確認質問を廃止**:
- `cli.py::_menu_history()`から、`blocked_info is not None`の分岐にあった`_ask_yes_no("このブロック記録を削除しますか？(y/n): ")`とそれに続く`delete_blocked()`呼び出しを削除した。`format_history()`（警告＋履歴表示）を`print()`した直後、そのまま3択の提示に進む。
- `seller_ledger.cli delete-storefront-block`（`confirm_and_delete_storefront_block()`）自体は変更していない——独立したコマンドとして引き続き使える。むしろ、メニュー1から直接削除できなくなった分、誤登録の訂正はこのコマンドを使う一択になった。

**テスト**: 単体1件新規（`tests/unit/test_storefront_blocked.py`: `RETRY_LATER`のreason_label）、結合1件新規（`tests/integration/test_storefront_blocked.py`: `mark_blocked()`がRETRY_LATERを受理すること）。`tests/integration/test_cli_menu.py`: 既存の禁止セラー系テスト3件から削除確認の入力（"n"）を除去、削除確認そのものを検証していた2件（y確認で削除される・n確認で残る）を1件（`test_history_blocked_seller_no_longer_offers_deletion_and_keeps_record`: 削除確認プロンプト自体が出ないこと・記録が残ることを確認）に統合、メニュー1「2」・メニュー9それぞれにRETRY_LATER選択の新規テストを追加、無効な理由選択のテスト2件は選択肢が3つになったことに合わせて無効値を"3"→"4"、メッセージを更新。単体156件・結合201件、全てgreen（ディスポーザブルなtest-dbで検証。上記のdowngrade往復は別途手動で実施）。

## 48. `seller_ledger.cli link-tool-run`の`--source`引数と対話式メニューの「発見経路」質問を廃止、`tool_execution_link.source`をNULL許容化（Phase 3, 2026-09-18）

ユーザー依頼: `--source`（発見経路、'SELLER_PAGE'/'BRAND_STORE'/'COMPETITOR'等）は自由入力のテキストで、毎回何を入力すべきか迷う上、表記ゆれが起きやすい。`SellerService.register()`側の同種の`--source`（discovery_source）は「どのロジックからも参照されない」という理由で既に削除済み（#25参照）で、`link-tool-run`側の`source`も同様に使われていないため削除する。

**「使われていない」ことの確認**: `seller_performance/seller_history.py`・`seller_recommender.py`・`attribution.py`のいずれのSQLも`tel.source`を一切SELECTしていないことをgrepで確認した（#25の`SellerService.register()`の時と同じ判断基準）。

**列自体は削除しない（ユーザー指定）**: `tool_execution_link.source`は既存データが入っている可能性があるため、`alembic/versions/0013_make_tool_execution_link_source_nullable.py`で`NOT NULL`制約のみ外した（`DROP COLUMN`はしない）。

**`tool_intake/log_ingest.py`は対象外（スコープを絞った判断）**: `ingest-log`は統合ツールのログHTMLの`data-type`属性から機械的に`source`を自動記録しており（人が毎回何を書くか迷う自由入力ではない）、ユーザーの依頼文も「`link-tool-run`」と「対話式メニューの発見経路の質問」だけを対象に挙げていたため、この自動記録経路は今回変更していない。そのため`ToolRunLinkService.record()`の`source`引数自体は削除せず、`source: str | None = None`（省略可能）に変更するに留めた——`SellerService.register()`の時（引数ごと削除）とは異なり、今回は「人に自由入力で聞くのをやめる」ことが主眼で、「値を一切記録しなくする」ことは依頼されていないため。

**変更箇所**:
- `db/models.py`: `tool_execution_link.source`列を`nullable=False`→`nullable=True`（デフォルト）。
- `alembic/versions/0013_make_tool_execution_link_source_nullable.py`（新規）: `ALTER COLUMN source DROP NOT NULL`。
- `seller_ledger/tool_run_link.py::ToolRunLinkService.record()`: `source`引数を省略可能に変更。
- `seller_ledger/cli.py`: `link-tool-run`の`--source`引数・`_cmd_link_tool_run()`での使用を削除。実行例からも`--source=SELLER_PAGE`を削除。
- `cli.py`（対話式メニュー）: `_do_link_tool_run()`（メニュー1「1」・メニュー2の両方が共有）から「発見経路（例: SELLER_PAGE, BRAND_STORE, COMPETITOR等）: 」の質問を削除。

**downgradeで#12と同じ教訓を先取り適用**: #12（RETRY_LATER追加）のdowngradeで、対象データが残っていると`tests/integration/conftest.py::_migrated_schema`のテストモジュールごとの`downgrade base`が軒並み失敗する実バグを踏んだばかりだったため、今回は先回りして対応した。`0013`のdowngradeは、`NOT NULL`を復元する前に`UPDATE tool_execution_link SET source = 'UNKNOWN' WHERE source IS NULL`を実行してからCHECK制約ではなくNOT NULL制約を戻す。実際に`source=NULL`の行を1件挿入した状態で`alembic downgrade 0012`→`upgrade head`のロールバック往復を手動実行し、NULL行が`'UNKNOWN'`へ正しく変換され、downgradeが失敗しないことを確認した。

**テスト**: `tests/unit/test_cli_parsers.py`の`test_link_tool_run_parses`を`--source`無しに更新し`not hasattr(args, "source")`を追加、`test_link_tool_run_source_accepts_arbitrary_value_not_restricted_to_choices`（`--source`の存在を前提にしていたテスト）は削除。結合1件新規（`tests/integration/test_seller_ledger.py::test_tool_run_link_record_without_source_defaults_to_null`: `source`省略時にNULLで記録されること）。`tests/integration/test_cli.py`の3箇所・`tests/integration/test_cli_menu.py`の3箇所から`--source=SELLER_PAGE`/`"SELLER_PAGE"`等の入力・引数を除去し、`test_link_tool_run_menu_standalone`は`row.source == "BRAND_STORE"`→`row.source is None`に更新。単体155件・結合202件、全てgreen（ディスポーザブルなtest-dbで検証、downgrade往復は別途手動で実施）。

## 49. `seller_performance.cli recommend`（対話式メニュー5も同様）から`storefront_blocked_seller`にPERMANENTLY_EXCLUDEDとして記録済みのセラーを除外（Phase 3, 2026-09-19）

ユーザー指摘: 「今後も取得しない」（PERMANENTLY_EXCLUDED）と明示的に記録したセラーでも、`tool_execution_link`/`seller_attribution`に過去の実績（Excel移行データ等）が残っていると、`recommend`の一覧に出てきてしまっていた。`recommend`は「次に何を取得すべきか」の提案のため、除外判断済みのセラーは過去の実績に関わらず出すべきではない。COMPETITOR_SAME_SERVICE・RETRY_LATER（いずれも「今後も取得する見込みがある」という意味、#41・#47参照）は対象外とし、引き続き候補として表示する。

**実装**: `seller_performance/seller_recommender.py::SellerRecommenderService.recommend()`の`candidates` CTE（絞り込みの起点、#43のdocstringで「一切変更していない」としてきたまさにこのCTE）のWHERE句に、`storefront_blocked_seller`にreason='PERMANENTLY_EXCLUDED'の行が存在するseller_idを除外する`NOT EXISTS`条件を追加した。`storefront_blocked_seller`はmarketplaceに依存しないテーブル（RLS適用対象外、CLAUDE.md 2節の例外）のため、除外はmarketplaceを問わずseller_id単位で行う——`recommend()`自体はmarketplaceでスコープされたまま（`sa.marketplace = :mp`は変更していない）。対話式メニュー`cli.py`のメニュー5は`SellerRecommenderService.recommend()`を共通利用しているため、同時に反映される。

**実データでの動作確認**: ユーザー報告の`A3Q6T5KGOEMH54`（`storefront_blocked_seller`にPERMANENTLY_EXCLUDED、登録日2026-09-17として実在）について、ローカル開発用DBに対して以下を確認した:
- 修正前の`candidates` CTEに相当する生SQL（`NOT EXISTS`無し）を直接実行したところ、JP marketplaceで`listing_occasions=2`（閾値2を満たす）・`last_requested_at`が2026-02-10（`min_days_since_last`=15日を大幅に超過）となり、修正前ロジックでは候補に該当していたことを確認した（US marketplaceでは元々対象データが無かった）。
- 修正後、`python -m seller_performance.cli recommend --marketplace=US`（76件）・`--marketplace=JP`（287件）のいずれの出力にも`A3Q6T5KGOEMH54`が含まれないことを確認した。
- ローカル開発用DBは本エントリ着手時点でalembicが0011止まりだったため、`alembic upgrade head`で0012（RETRY_LATER追加）・0013（tool_execution_link.source NULL許容化）を適用してから検証した。DBへの書き込みは発生しない読み取り専用コマンドのため、後片付けは不要だった。

**テスト**: 結合2件新規（`tests/integration/test_seller_performance.py::TestSellerRecommenderService`: `test_excludes_seller_marked_permanently_excluded`——絞り込み条件を満たす実績があってもPERMANENTLY_EXCLUDEDなら除外されること、`test_does_not_exclude_seller_marked_with_other_reasons`——COMPETITOR_SAME_SERVICE・RETRY_LATERの2値をパラメタライズし、いずれも除外されず候補に残ることを確認）。単体155件（変更なし）・結合205件（パラメタライズにより新規3ケース）、全てgreen（ディスポーザブルなtest-dbで検証）。

## 50. 【重大】対話式メニュー「2. 統合ツールのIDとひもづける」が未記録のtool_run_idを「既に記録されています」と誤報告し、link-tool-runが握りつぶされる不具合の調査・修正（Phase 3, 2026-09-19）

ユーザー報告: marketplace=US・seller-id=A1UVXHQIDG1C7X・tool-run-id=7170614でメニュー2を実行したところ「既にtool_run_id=7170614が記録されています（スキップしました）。」と表示されたが、DBを直接確認するとUS/JPどちらのtool_execution_linkにも該当行が無かった（0 rows）。紐づけが一度も成功していない可能性がある、実際の記録として重大な不具合、として調査を依頼された。

**該当コードの特定**: `cli.py::_do_link_tool_run()`（メニュー1「1」・メニュー2の両方が共有）が

```python
try:
    with app_factory.get_session(marketplace) as conn, conn.begin():
        ToolRunLinkService().record(conn, marketplace, seller_id, tool_run_id, source)
except IntegrityError:
    print(f"既にtool_run_id={tool_run_id}が記録されています（スキップしました）。")
    return
```

という実装になっており、`tool_execution_link`のUNIQUE制約（`uk_tool_execution_link`、`marketplace`・`tool_run_id`）違反かどうかを一切確認せず、`IntegrityError`を無条件に「既に記録済み」として握りつぶしていた。同じパターンが`tool_intake/log_ingest.py::_process_row()`にも存在した（`SellerService.register()`と`ToolRunLinkService.record()`を同じtry/exceptで囲み、`except IntegrityError`で`summary.already_linked`をインクリメントするだけ）。

**現在のスキーマでは他に候補となる制約が無いことを確認**: `tool_execution_link`テーブルの実際の制約は主キー（`id`、IDENTITY）と`uk_tool_execution_link`のみ（外部キー無し、CHECK制約無し）であることをローカル開発用DBの`\d tool_execution_link`で確認した。またRLSポリシー（`WITH CHECK (marketplace = current_setting(...))`）違反は`ProgrammingError`（`InsufficientPrivilege`、SQLSTATE 42501）として送出され`IntegrityError`にはならないことを実際に再現して確認した。つまり**現行スキーマでは`record()`のINSERT文が`IntegrityError`を送出する経路は`uk_tool_execution_link`違反以外に存在しない**——この一点だけを見ると、今回の個別インシデントが「無関係な例外を誤って握りつぶした」結果ではなく、実際に一時的な重複行が存在した可能性が高いことを示唆する（下記「根本原因について」参照）。しかし、`except IntegrityError`で違反した制約を確認しない実装そのものは、将来スキーマが変わった場合や、DBAPIの挙動差で別の`IntegrityError`が発生した場合に、本来の失敗を無条件に「重複」と誤報告し続ける構造的な欠陥であり、それ自体を修正した。

**根本原因について（判明した事実と、判明しなかった点）**: `tool_execution_link`にはINSERT/DELETEの監査ログが存在しないため、報告された時点で本当に重複行が存在し、その後（`delete-tool-run`等による訂正や、他の未特定の経路で）削除されたのか、あるいは他の要因があったのかを事後的に完全には特定できなかった。`SellerDeletionService.delete()`/`ToolRunDeletionService.delete()`はいずれも`tool_execution_link`と`tool_output_asin`を同じトランザクションで一貫して削除する実装になっており（確認済み）、これらの標準コマンドの使用だけでは「tool_execution_linkだけ消えてtool_output_asinは残る」状態は説明できない。**影響範囲の調査**として、ローカル開発用DBで「`tool_output_asin`に行があるのに対応する`tool_execution_link`が無いtool_run_id」を全件チェックしたところ、US 0件・JP 1件（`7128340`、`imported_at`が2026-09-07で本インシデントよりかなり前、`import_run_history`に対応レコード無し＝`scripts/import_legacy_tool_output.py`による一括移行由来）が見つかった。この1件は既存の削除コマンドの動作と矛盾する古いデータ不整合であり、本件（2026-09-19発生、標準の削除コマンドでは説明不可）とは発生時期・パターンが異なるため、**同一の不具合ではなく別の未解明の事象**として切り分けて記録する（追加調査は本エントリのスコープ外、将来必要になれば着手すること）。それ以外に、この誤判定パターン特有の痕跡（スキップイベント自体を記録する仕組みが無い）は現状のログからは発見できなかった。

**修正**: `seller_ledger/tool_run_link.py`に`DuplicateToolRunError`例外を新設し、`ToolRunLinkService.record()`が`IntegrityError`を捕捉した際、`e.orig.diag.constraint_name`が`uk_tool_execution_link`と一致する場合のみ`DuplicateToolRunError`に変換して送出、それ以外（診断情報が取得できない場合を含む安全側フォールバック）はそのまま再送出するようにした。呼び出し元（`cli.py::_do_link_tool_run()`・`tool_intake/log_ingest.py::_process_row()`）は`except IntegrityError`を`except DuplicateToolRunError`に変更した——これにより、`uk_tool_execution_link`以外の想定外の`IntegrityError`は握りつぶされず、対話式メニューの外側の`except Exception`（`main()`のループ）や`ingest_log()`の1行単位のエラー分離（CLAUDE.md 6節）へ正しく伝播するようになった。

**調査中に発見した関連の不整合（追加修正）**: `seller_ledger.cli link-tool-run`単体（対話式メニューを介さない標準コマンド）には、そもそも重複時の例外処理が一切無く、実際に重複を試すと生の`IntegrityError`トレースバックでクラッシュすることを実地で確認した。対話式メニューと同じ「既にtool_run_id=...が記録されています（スキップしました）。」を表示するよう`seller_ledger/cli.py::_cmd_link_tool_run()`にも同じ`except DuplicateToolRunError`処理を追加した。

**実データでの動作確認**（ローカル開発用DB、`db`サービス）:
1. `SET app.current_marketplace='US'`で`tool_execution_link`を確認し、`tool_run_id='7170614'`が0件であることを再確認した。
2. `python -m seller_ledger.cli link-tool-run --marketplace=US --seller-id=A1UVXHQIDG1C7X --tool-run-id=7170614`を実行し、`linked: marketplace=US seller_id=A1UVXHQIDG1C7X tool_run_id=7170614`が表示され、`tool_execution_link`に実際に1行（`seller_id=A1UVXHQIDG1C7X`, `tool_run_id=7170614`）が記録されたことを確認した。
3. 同じコマンドをもう一度実行し、クラッシュせず「既にtool_run_id=7170614が記録されています（スキップしました）。」が表示され、`tool_execution_link`の該当行数が引き続き1件のまま（重複INSERTされていない）ことを確認した——`DuplicateToolRunError`が真の重複に対しては引き続き正しく機能することも合わせて確認。
4. ローカル開発用DBは本エントリ着手時点でalembicが0011止まりだったため（#49と同じ状況）、`alembic upgrade head`で0012・0013を適用してから上記を実施した。

**テスト**: 単体5件新規（`tests/unit/test_tool_run_link.py`新設: `_violates_unique_tool_run_constraint()`が制約名一致/不一致/診断情報欠如の3パターンを正しく判定すること、`record()`がuk_tool_execution_link違反を`DuplicateToolRunError`に変換すること、それ以外の`IntegrityError`はそのまま再送出し握りつぶさないこと——擬似的な`.orig.diag.constraint_name`を持つフェイク例外・フェイクコネクションで検証、実DB不要）。結合1件更新（`tests/integration/test_seller_ledger.py::test_tool_run_link_duplicate_tool_run_id_raises`: 期待する例外型を`IntegrityError`→`DuplicateToolRunError`に変更し、`marketplace`/`tool_run_id`属性も検証）。結合1件新規（`tests/integration/test_cli.py::test_seller_ledger_link_tool_run_duplicate_via_cli_shows_friendly_message`: `seller_ledger.cli link-tool-run`単体を2回実行し、2回目がクラッシュせず友好的なメッセージを表示し、重複INSERTされないことを確認——上記「調査中に発見した関連の不整合」の回帰テスト）。単体160件・結合206件、全てgreen（ディスポーザブルなtest-dbで検証）。

## 51. `seller_performance.cli recommend`（対話式メニュー5も同様）: COMPETITOR_SAME_SERVICE・RETRY_LATERで記録した直後のセラーが上位に残り続ける問題を修正（Phase 3, 2026-09-18）

ユーザー指摘: 「今回はチェックしたが保留する」という判断（COMPETITOR_SAME_SERVICE・RETRY_LATERで記録）をしたセラーが、いつまでもrecommendの上位に残り続け、同じセラーを何度もチェックしてしまう。PERMANENTLY_EXCLUDEDは#49で既にrecommendから完全除外されているが、それ以外の2つの理由は現状recommendの判定に一切影響していなかった。

**対応方針**: 完全除外（#49のPERMANENTLY_EXCLUDED）ではなく、「保留を記録した日」を「最終取得日」の候補として扱う——`tool_execution_link.requested_at`の最新値と`storefront_blocked_seller.first_noted_at`（reasonがCOMPETITOR_SAME_SERVICE・RETRY_LATERの場合のみ）のうち、より新しい方（`GREATEST`）を採用する。これにより記録直後は既存の`min_days_since_last`判定（前回から15日以上経過）に自然に引っかかり一覧から外れ、期間が経過すれば通常通り再び候補に上がってくる——新しい仕組みを追加するのではなく、既存のmin_days_since_lastの仕組みにそのまま乗せる設計とした。PERMANENTLY_EXCLUDEDは変更なし（#49のNOT EXISTSで先に完全除外されるため、今回のGREATEST計算には到達しない）。

**実装前に確認した前提**（ユーザーからの確認依頼）: 「tool_execution_linkの実績が全く無く、storefront_blocked_sellerの記録だけがあるセラー」（例: ユーザーが例示した「アスカモデル」のケース）がrecommendの対象になってしまわないか。`candidates` CTEは`seller_attribution sa JOIN weekly_listing_snapshot wls ... JOIN tool_execution_link tel`という構成で、`seller_attribution`自体が`tool_execution_link`経由でのみ作られる（`AttributionService`）ため、tool_execution_linkの実績が無い（＝seller_attributionが無い）セラーは`candidates`のJOIN条件そのものを満たせず、今回の変更後も自然に対象外のままであることを実装前にコードレビューで確認した。実データでも、ローカル開発用DBの`known_seller_master`に実在する「アスカモデル(ASUKAMODEL)」（JP、tool_execution_link 1件・seller_attribution 0件・storefront_blocked_seller未記録）が、実際に`recommend --marketplace=JP`（273件）に含まれないことを確認した。

**実装**: `seller_performance/seller_recommender.py::SellerRecommenderService.recommend()`のSQLに`blocked_recency`CTE（`storefront_blocked_seller`のうちreason IN ('COMPETITOR_SAME_SERVICE', 'RETRY_LATER')のみ）を追加。`candidates`CTEのmin_days_since_last判定を`current_date - tel.last_requested_at::date`から`current_date - GREATEST(tel.last_requested_at::date, br.first_noted_at)`に変更（`GREATEST`はPostgreSQLの仕様上NULLを無視するため、対象外の理由や記録自体が無いセラーでは従来通りtel側の値のみが使われる）。表示・並び順に使う`last_requested_date`（`seller_stats`由来）も同様に、最終SELECTで`GREATEST(s.raw_last_requested_date, br.first_noted_at)`として算出するよう変更した（`seller_stats`内部の列名は`raw_last_requested_date`にリネームし、GREATEST後の値と区別できるようにした）。`ORDER BY`も新しい`last_requested_date`（SELECT句のエイリアス）を参照するよう変更。`recent_fetch_days`・`previous_delta`の計算方法（`daily`/`per_run`/`ranked`/`deltas`）は変更していない——これらは実際の取得実績のみを示す指標として据え置く。

**実データでの動作確認**（ローカル開発用DB、alembicは既にhead=0013）:
- 2026-09-17（前日）にCOMPETITOR_SAME_SERVICEで記録済みの実セラー`A1KIRUBS0ZW6G1`（JP、`listing_occasions=14`）について、`tool_execution_link`単体の最終取得日は2026-03-03（199日前）で、旧ロジックなら`min_days_since_last=15`を大きく超えており候補に該当していたはずだが、修正後の`recommend --marketplace=JP`（273件）には含まれないことを確認した。
- `--min-days-since-last=0`で強制的に候補へ含めた場合、表示される「最終取得日」が実際の取得日（2026-03-03）ではなく記録日（2026-09-17）になっていることを確認し、表示・並び順ロジックの変更も正しく機能していることを確認した。
- DBへの書き込みは発生しない読み取り専用コマンドのため、後片付けは不要だった。

**テスト**: 結合7件新規（`tests/integration/test_seller_performance.py::TestSellerRecommenderService`）:
- `test_excludes_seller_recently_marked_competitor_or_retry_later`（reasonをパラメタライズ）: tool_execution_linkの実績自体は十分古くても、直近の保留記録があれば除外されること。
- `test_includes_seller_again_once_enough_days_pass_since_block_record`（同）: 保留記録からmin_days_since_last日以上経過すれば再び候補に上がること。
- `test_last_requested_date_uses_more_recent_of_tool_execution_link_and_block_record`: 表示・並び順に使う`last_requested_date`が保留記録の日付を正しく反映すること。
- `test_permanently_excluded_still_excluded_regardless_of_block_record_age`: PERMANENTLY_EXCLUDEDは記録日の新旧に関わらず引き続き完全除外されること（#49の回帰確認）。
- `test_seller_with_only_block_record_and_no_tool_execution_link_is_not_recommended`: tool_execution_link/seller_attributionが無いセラーは対象外のままであること（上記「実装前に確認した前提」の自動テスト化）。
既存の`test_does_not_exclude_seller_marked_with_other_reasons`は、`first_noted_at`を意図的に古い日付（30日前）に変更した——本テストの主旨（PERMANENTLY_EXCLUDEDのような完全除外はしない）と、今回追加した「直近の記録は除外する」という挙動を混同しないよう分離するため。単体160件（変更なし）・結合213件、全てgreen（ディスポーザブルなtest-dbで検証）。

## 52. `seller_performance.cli recommend`（対話式メニュー5も同様）: min_listing_countの撤廃、保留記録日・保留理由の列分離（Phase 3, 2026-09-30）

ユーザー依頼、3点:

1. `--min-listing-count`による絞り込みを撤廃する。実績（listing_occasions）が0回のセラーも一覧に出す。
2. #51で「最終取得日」に混ぜていた`storefront_blocked_seller.first_noted_at`を、独立した「保留記録日」列へ分離する。「最終取得日」列は`tool_execution_link.requested_at`のみを基準に戻す（実績が無い場合は空欄/「-」）。min_days_since_lastの判定・並び順には、引き続き「最終取得日と保留記録日のうち、より新しい方」を使う。
3. 保留理由（COMPETITOR_SAME_SERVICE/RETRY_LATER、表示ラベルは既存の`REASON_LABELS`を流用）の列を新設する。

PERMANENTLY_EXCLUDEDは引き続き完全除外のまま変更しない。

**実装前に確認した前提**（ユーザーからの確認依頼）: 「tool_execution_linkの実績が全く無く、storefront_blocked_sellerの記録だけがあるセラー」（例えば今回のアスカモデルのケースのように、まだ一度もASINを取得できていない場合）がrecommendの対象になるべきか——ならないはず（min_listing_countの条件を満たさないので元々対象外のはず）という理解が正しいか。#51時点での結論（`candidates`の起点である`tool_execution_link`自体に行が無いセラーは対象外）は、今回`candidates`の起点を`seller_attribution`→`tool_execution_link`へ変更した後も変わらないことをコードレビューで確認した——`tel_agg`サブクエリ（`tool_execution_link`起点）に行が無ければ、そもそも`candidates`に現れようがないため。実データでも、ローカル開発用DBの「アスカモデル(ASUKAMODEL)」の実態（`storefront_blocked_seller`に記録はまだ無く、`tool_execution_link`は1件・`seller_attribution`は0件）を踏まえ、この設計が意図通りであることを確認した。

**1. min_listing_countの撤廃**:
- `candidates` CTEの起点を、`seller_attribution sa JOIN weekly_listing_snapshot wls`（INNER JOIN、listing_occasionsが1件も無いと行自体が存在しない）から、`tool_execution_link`をmarketplace・seller_idで集約した`tel_agg`サブクエリ（そのマーケットプレイスで一度でも実行されていれば必ず1行存在する）に変更した。`listing_occasions`の実績集計（`listing_agg`サブクエリ）はLEFT JOINにし、`COALESCE(listing_agg.listing_occasions, 0)`で0を許容するようにした。`HAVING COUNT(...) >= :min_count`は削除。
- `recommend()`の`min_listing_count`引数、`seller_performance.cli recommend`の`--min-listing-count`、対話式メニューcli.pyのメニュー5内の対応する参照を削除した。`seller_recommendation_config.min_listing_count`列自体（既存データ）は変更していない——`recommend()`がもう読まないだけの未使用列として残る（`db/models.py`にコメントで明記）。
- `lookback_days`によるlisting_occasionsの期間制限（実績のカウント対象を直近lookback_days日に絞る）は維持した——閾値（何回以上必要か）を無くしただけで、「何を数えるか」の定義自体は変えていない。

**2・3. 保留記録日・保留理由の列分離**:
- `SellerRecommendation`データクラスに`hold_recorded_date: date | None`・`hold_reason: str | None`を追加し、`last_requested_date`は純粋に`tool_execution_link`基準の値（`candidates.last_requested_date`、`tel_agg`由来）に戻した。`blocked_recency` CTEに`reason`列を追加し、最終SELECTで`br.first_noted_at AS hold_recorded_date`・`br.reason AS hold_reason`として別々に取得する。
- `min_days_since_last`の判定（`candidates`のWHERE句）・並び順（`ORDER BY`）は、引き続き`GREATEST(tel_agg.last_requested_date, br.first_noted_at)`を使う——#51の絞り込みロジック自体は変更せず、表示のみを分離した。
- `format_recommendation_line()`を、`seller_ledger.storefront_blocked.REASON_LABELS`を使って保留理由をラベル表示するよう変更し、出力順を「seller_id / 最終取得日 / 保留記録日 / 保留理由 / 取得日数(重複排除) / 取得回数 / 合計出品成果 / 前回比」に変更した（ユーザー提示の出力イメージ通り）。`last_requested_date`は現状の`candidates`設計では実質的に常に非NULLだが（`tel_agg`起点のため）、防御的にNoneでも「-」表示できるようにした。

**実データでの動作確認**（ローカル開発用DB、alembicは既にhead=0013、スキーマ変更は無いため追加のマイグレーション適用は不要）:
- `recommend --marketplace=JP`が287件（#51時点）→541件に増加し、`取得回数=0`のセラーが多数含まれることを確認した。
- COMPETITOR_SAME_SERVICEで記録済みの実セラー`A1KIRUBS0ZW6G1`について、`--min-days-since-last=0`で強制的に候補へ含めた場合の表示が「最終取得日=2026-03-03　保留記録日=2026-09-17　保留理由=同業者利用」となり、#51時点で1列に混ざっていた表示が正しく分離されていることを確認した。デフォルトの`min-days-since-last=15`では、このセラーが引き続き除外されたまま（#51の絞り込みロジック自体は変わっていないこと）も確認した。
- PERMANENTLY_EXCLUDEDの実セラー`A3Q6T5KGOEMH54`が、今回の変更後も`recommend --marketplace=JP`の541件に含まれないことを確認した（#49の回帰確認）。

**テスト**: 単体5件新規（`tests/unit/test_seller_recommender.py`: 取得回数0の表示、保留記録日・保留理由の表示（RETRY_LATER/COMPETITOR_SAME_SERVICEのラベル差、`REASON_LABELS`経由）、`last_requested_date`がNoneの場合の防御的な「-」表示）。単体1件新規（`tests/unit/test_cli_parsers.py`: `--min-listing-count`自体が存在しない・渡すとargparseエラーになること）。結合テストは`tests/integration/test_seller_performance.py`の`TestSellerRecommenderService`配下を全面的に見直した——既存の`recommend()`呼び出し19箇所から`min_listing_count=2`引数を除去、`test_excludes_seller_below_threshold`は`test_includes_seller_with_single_listing_occasion`へ、`test_excludes_listing_outside_lookback_window`は`test_listing_occasions_excludes_snapshot_outside_lookback_window`へ改名の上アサーションを反転（閾値が無いため「除外される」ではなく「カウントされない」ことを検証）、`test_last_requested_date_uses_more_recent_of_tool_execution_link_and_block_record`（#51）は`test_last_requested_date_and_hold_recorded_date_are_shown_as_separate_columns`へ改名し新しい2列を別々に検証。新規2件（`test_includes_seller_with_zero_listing_occasions`——min_listing_count撤廃の核心、`test_hold_recorded_date_and_reason_are_none_when_not_blocked`）。`tests/integration/test_cli.py`の3箇所から`--min-listing-count=2`/`min_listing_count=2`を除去。単体165件・結合215件、全てgreen（ディスポーザブルなtest-dbで検証、実データでの動作確認は上記の通り別途実施）。

## 53. `seller_performance.cli recommend`の候補拡大・CSV出力化、新規コマンド`asin_collector.cli check-recommendations`の追加（Phase 3, 2026-10-07）

ユーザー依頼、4点:

1. `tool_execution_link`に一度も紐づけていない（register済みだがlink-tool-run未実行の）セラーも`recommend`の候補に含める。`known_seller_master`を候補の基準に広げ、`tool_execution_link`が0件のセラーも出す（最終取得日は「-」/空欄表示）。PERMANENTLY_EXCLUDEDの完全除外・marketplaceの絞り込み（RLS）は変更しない。
2. `recommend`の結果を画面ではなくCSVファイルへ出力する。出力先`exports\recommend\recommend_<marketplace>_<yyyyMMdd_HHmmss>.csv`（フォルダ無ければ自動作成）、画面には件数とファイルの場所のみ表示、列は現在の表示項目と同じ8列、UTF-8 BOM付き、並び順は現状どおり、対話式メニュー5も同じ動作にする。
3. ストアフロント検索URL（ユーザーがブラウザで確認済み）: JP `https://www.amazon.co.jp/s?me=<seller_id>`、US `https://www.amazon.com/s?me=<seller_id>&marketplaceID=ATVPDKIKX0DER`。`check-recommendations`のURLはこの形で確定。
4. （新規実装としての設計要件、下記「経緯」参照）`check-recommendations`が出品商品0件のセラーを正しく判定し、誤判定時は「判定不能」として記録せず一覧に残す。

**経緯（調査→設計への読み替え）**: ユーザーから「`check-recommendations`を実行した結果...」という体裁で不具合報告があったが、調査の結果`check-recommendations`というコマンド自体がリポジトリに一切存在しないこと（全ファイルgrep一致ゼロ、gitログも最初の1コミットのみ）が判明したため、`AskUserQuestion`でユーザーに確認したところ「今回が新規作成」との回答を得た。ユーザーから改めて説明があり、4番の「原因調査」は実装済みコードのデバッグではなく、新規実装する判定ロジックの設計検討に読み替えることになった。ユーザー提示の実セラーID（JP `A26YLYFFSVKSNX`=商品無し、`A3SQM3QBSWZXTB`=商品あり）を使って実機で検証しながら設計した。

**1. candidatesの起点をknown_seller_masterへ拡大**: `seller_performance/seller_recommender.py::SellerRecommenderService.recommend()`の`candidates` CTEの起点を、`tool_execution_link`（#52でそのmarketplaceで一度でも実行されていれば必ず1行存在するとしていた前提）から`known_seller_master`（register済みなら必ず1行存在する）へさらに広げた。`last_requested_date`・`listing_occasions`・`total_listed`・`recent_fetch_days`・`previous_delta`はいずれも`tool_execution_link`起点のLEFT JOIN経由のままのため、実行実績が無いセラーではNone/0になる（#52時点で「理論上しか起こらない」としていたNoneケースが実際に発生するようになった）。`min_days_since_last`の判定は、`last_requested_date`・`hold_recorded_date`の両方がNone（実行実績も保留記録も無い、一度も接点が無いセラー）の場合は無条件に対象へ含める（比較対象の日付が無いため「十分に日数が経過している」とみなす）。並び順（`GREATEST(...) ASC`）は`NULLS FIRST`とし、一度も接点が無いセラーを先頭（最優先）に表示する——ユーザー未指定のため、既存の「古いセラーほど上位」という設計思想から妥当と判断した。`per_run`/`seller_stats`/`deltas`への`JOIN`は、実行実績が無いセラーが最終結果から落ちないよう`LEFT JOIN`＋`COALESCE`に変更した（INNER JOINのままだと、候補には含まれるのに実績サブクエリとマッチせず消えてしまう）。

**実装前にユーザーへ提示した見積り**: ローカル開発用DBでUS/JPとも`known_seller_master`1154件中、`tool_execution_link`に一度も紐づいていないセラーが39件（うちPERMANENTLY_EXCLUDED 0件）。デフォルトの`min_days_since_last=15`の下では、そのうち直近15日以内にCOMPETITOR_SAME_SERVICE/RETRY_LATERの保留記録があるものが7件あり対象外のままとなるため、新規に一覧へ加わるのは約32件/marketplaceと見積もり、ユーザーに提示・実装の了承を得た。実際の変更後、JP `recommend`は541件→583件に増加した。

**2. CSV出力化**: `seller_performance/cli.py`に`write_recommendations_csv()`を新設（`seller_performance.cli recommend`・対話式メニューcli.pyのメニュー5の両方から共通利用）。出力先`exports/recommend/recommend_<marketplace>_<yyyyMMdd_HHmmss>.csv`（`asin_collector.cli export-run`の`exports/collected_asins/`と同じ「無ければ自動作成」方式）。`encoding="utf-8-sig"`でBOM付きにし、Excelでそのまま開けるようにした。列の値は画面表示用の文字列（「-」等）ではなく、空欄/生の数値に統一した——Excelでの並べ替え・フィルタに使いやすくするため（保留理由だけは`REASON_LABELS`のラベルのまま、人が読む情報のため）。`format_recommendation_line()`自体は削除せず残したが、現時点でCLI/メニューのいずれからも呼ばれていない（1行テキスト表示が必要になった場合のための汎用ユーティリティとして維持）。

**3・4. 新規コマンド`asin_collector.cli check-recommendations`**: `asin_collector/check_recommendations.py`を新設。`recommend`が出力したCSVの`seller_id`列（`tool_intake.csv_utils.read_csv_rows`/`column_index`を再利用、cp932/utf-8-sig/utf-8自動判定——Excelで編集後cp932保存され得るため、CLAUDE.md技術スタック参照）を読み、各セラーのストアフロント検索URLを`open_browser()`（`asin_collector.browser_session`、既存の永続プロファイル・CAPTCHA検知の仕組みを再利用）で順に巡回する。

**商品0件の判定方法（実機確認、2026-10-07）**: `asin_collector.asin_extractor.extract_search_card_asins()`（`collect-brand-page`と同じ関数、`div[data-component-type="s-search-result"]`範囲内のみを見るためスポンサー枠・おすすめ商品は含まれない）で抽出したASINが0件、かつAmazon自身の「検索結果0件」ウィジェット（`data-csa-c-content-id="messaging-messages-no-results"`、JP/US双方で同一のウィジェットID文字列であることを確認済み——表示言語に依存しないため判定に使う）が存在する、の両方を満たした場合のみ「商品なし」と判定する。実際にPlaywrightで`https://www.amazon.co.jp/s?me=A26YLYFFSVKSNX`（「該当商品なし」）と`https://www.amazon.co.jp/s?me=A3SQM3QBSWZXTB`（商品あり）を開いて検証した結果:
- `A26YLYFFSVKSNX`: ページ上にスポンサー商品（Dyson ドライヤー、「スポンサー」表示あり）が存在するが、`extract_search_card_asins()`は0件を返す（スポンサー枠は`s-search-result`コンポーネントの外側にあるため）。「検索結果0件」ウィジェットは存在する。→ NO_PRODUCTS。
- `A3SQM3QBSWZXTB`: `extract_search_card_asins()`は16件返し、ウィジェットは存在しない。→ HAS_PRODUCTS。
- 念のため、広い範囲（`[data-asin]`属性全体＋`/dp/`リンク全体）を見る`extract_asins()`（ranking_page_collector等向けに残してある既存の別関数）でも`A26YLYFFSVKSNX`のページを解析したが、スポンサー枠のASINへのリンクが`<a href>`の単純な`/dp/`パターンと一致しない構造だったため、こちらも偶然0件を返した——ただし、この一致は構造的に保証されたものではないため、判定には`extract_search_card_asins()`の方（`s-search-result`コンポーネント境界による構造的な保証がある）を採用した。
- カードが0件でもウィジェットが見つからない場合（想定外のページ構造）、CAPTCHA検知時、ページ読み込み失敗、一時的な500/503エラー（再試行後も解消しない場合）は、いずれも「商品なし」と断定せず「判定不能」として記録し、`storefront_blocked_seller`へは書き込まない（誤ってPERMANENTLY_EXCLUDEDに記録しないため、ユーザー指定）。

**CAPTCHA時に人の対話待ちをしない（CLAUDE.md 7節の例外）**: `brand_page_collector.py`と異なり、CAPTCHA検知時は`wait_for_human_to_solve_captcha()`を呼ばず即座に「判定不能」としてそのセラーをスキップし、次のセラーへ進む。CLAUDE.md 7節の絶対厳守ルール「CAPTCHA発生時は人の手動対応を待つ設計とする」の例外として、同節に明記した（ユーザー確認済み、2026-10-07）——`collect-brand-page`は1回の実行で1つのtarget-urlをページ送りするだけだが、`check-recommendations`は1回の実行で`recommend`の出力（候補拡大後は1marketplaceあたり数百件規模）を順に処理するため、1件ごとに対話待ちでブロッキングする設計は現実的でないと判断した。

**0件確認後の記録**: `StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note="check-recommendationsでストアフロント検索結果0件を確認（自動記録）")`を呼ぶ。既存のmark_blocked()の「初回登録を上書きしない」設計をそのまま踏襲するため、既に何らかの理由で記録済みのセラーがあっても上書きしない。

**実データでの動作確認**（ローカル開発用DB、`db`サービス）: `A26YLYFFSVKSNX`・`A3SQM3QBSWZXTB`の2件のみを含む小さなCSVを作成し、実際に`python -m asin_collector.cli check-recommendations --marketplace=JP --csv=...`を実行した（`recommend`の実出力583件全件に対する実行は、未レビューの大量のPERMANENTLY_EXCLUDED書き込みを伴う破壊的操作になり得るため、意図的に行っていない）。結果: 対象2件、商品あり1件、商品0件（PERMANENTLY_EXCLUDEDへ記録）1件、判定不能0件。DB確認の結果、`A26YLYFFSVKSNX`は`PERMANENTLY_EXCLUDED`として記録済み（実行前に既にユーザー自身が手動で記録済みだったため、今回は「既存記録を上書きしない」動作を確認する形になった）、`A3SQM3QBSWZXTB`は`storefront_blocked_seller`に一切記録が無いことを確認した。`recommend --marketplace=JP`を実際に実行し、583件のCSVが`exports/recommend/`配下にUTF-8 BOM付きで出力され、最終取得日が空欄の行（候補拡大で新たに加わったセラー）が25件含まれることを確認した。

**テスト**: 単体19件新規（`tests/unit/test_check_recommendations.py`新設: `storefront_search_url()`のJP/US形式・URLエンコード・不正marketplace、`classify_search_page()`の3分類、`read_seller_ids_from_csv()`のCSV読み取り・cp932対応・列欠落時のエラー、`_check_one()`のCAPTCHA/サーバーエラー再試行/ページ読み込み失敗/想定外構造の各分岐——`tests/unit/test_brand_page_collector_retry.py`と同じ`_StatefulFakePage`パターンを使用、`check_sellers()`の集計ロジック）。単体7件新規（`tests/unit/test_seller_performance_cli.py`新設: `write_recommendations_csv()`のファイル名パターン・BOM・ヘッダー順・値の空欄化・複数行出力）。単体2件新規（`tests/unit/test_cli_parsers.py`: `check-recommendations`のパース・必須引数）。結合2件新規（`tests/integration/test_check_recommendations.py`新設: `_record_no_products()`がPERMANENTLY_EXCLUDEDを記録すること、既存記録を上書きしないこと——`brand_page_collector.py`の「DB書き込みはブラウザ無しで直接呼び出して検証する」パターンを踏襲）。結合5件新規（`tests/integration/test_seller_performance.py::TestSellerRecommenderService`: register済み・未紐づけセラーの候補化と各フィールドのNone/0確認、未紐づけ＋PERMANENTLY_EXCLUDEDの除外、未紐づけ＋直近の保留記録の除外、未紐づけ＋古い保留記録の候補化とhold列確認、未紐づけセラーが並び順の先頭に来ること）。既存2件（`tests/integration/test_cli.py`の`recommend`テスト）はCSV出力に合わせて画面出力の検証からCSVファイル読み取りの検証に書き換えた。単体192件・結合222件、全てgreen（ディスポーザブルなtest-dbで検証、実データでの動作確認は上記の通り別途実施）。

## 54. `check-recommendations`: 既存の保留記録のPERMANENTLY_EXCLUDEDへの自動更新・判定不能の連続中断、`recommend`候補拡大の増分調査（Phase 3, 2026-10-07）

ユーザー依頼、3点:

1. `check-recommendations`がNO_PRODUCTSと判定したセラーが、すでに`storefront_blocked_seller`にCOMPETITOR_SAME_SERVICEまたはRETRY_LATERで記録されている場合は、PERMANENTLY_EXCLUDEDへ更新する。更新時、noteの先頭に「自動判定: 商品0件（元の理由: <元の理由の表示ラベル>）」を追記し、元のnoteがあればその後ろに残す。`first_noted_at`は変更しない。すでにPERMANENTLY_EXCLUDEDのセラーは何もしない。サマリに新規記録・既存保留からの更新の件数を別々に表示する。
2. 判定不能（CAPTCHA・読み込み失敗・想定外の構造）が連続してN件続いたら処理を中断する。Nの既定値は5、`--max-consecutive-undetermined`で変更可能。中断時は処理済み件数・未処理のseller_id一覧を画面表示する。
3. `recommend`のJP件数が541件→583件（+42件）に増えた件の調査。25件は`tool_execution_link`に一度も紐づいていないセラー（#53で意図した増分）。残り17件の内訳、および`known_seller_master`への候補拡大が意図しない行を生んでいないかの確認。

**1. 既存保留記録の自動アップグレード**: `seller_ledger/storefront_blocked.py`に`StorefrontBlockedSellerService.mark_or_escalate_to_permanently_excluded(conn, seller_id, new_record_note)`を新設（戻り値`EscalateResult.status`: `"NEW"`/`"UPGRADED"`/`"ALREADY_EXCLUDED"`）。これは同モジュールの「既存行は上書きしない」という原則（`mark_blocked()`のON CONFLICT DO NOTHING）に対する唯一の例外として、モジュールdocstringに明記した。`asin_collector/check_recommendations.py::_record_no_products()`はこのメソッドを呼ぶよう変更し、戻り値をそのまま返すようにした（従来の`mark_blocked()`直呼びから変更）。`CheckRecommendationsSummary`に`no_products_new`・`no_products_upgraded`・`no_products_already_excluded`の3カウンタを新設（旧`no_products_recorded`を置き換え）。`asin_collector/cli.py::_cmd_check_recommendations()`は新規記録・既存保留からの更新を別の行で表示し、「既にPERMANENTLY_EXCLUDED（変更なし）」は0件のときは表示しない（通常の運用では`recommend`のCSVに既にPERMANENTLY_EXCLUDEDのセラーは出てこないため、ほぼ発生しない防御的分岐のため）。

**2. 判定不能の連続中断**: `CheckRecommendationsService.check_sellers()`に`max_consecutive_undetermined`引数（既定値5、`_DEFAULT_MAX_CONSECUTIVE_UNDETERMINED`）を追加。HAS_PRODUCTS/NO_PRODUCTSのいずれかを1件でも判定できれば連続回数はリセットされる。閾値に達した時点で残りの`seller_id`を処理せず中断し、`CheckRecommendationsSummary.aborted=True`・`processed_count`・`remaining_seller_ids`を設定する。CLAUDE.md 6節の「1レコードの失敗で全体を止めない」というエラー分離の精神とは別の判断軸——同種の失敗（CAPTCHA等）が連続する場合は、個々の偶発的失敗ではなくブラウザ・ネットワーク・Amazon側の状態異常の可能性が高いとみなし、バッチ単位で早期中断する方が安全と判断した（ユーザー指定）。中断後は同じCSVファイルでそのまま再実行できる——`mark_or_escalate_to_permanently_excluded()`は既にPERMANENTLY_EXCLUDEDのセラーに対して何もしない（べき等）ため、処理済みの行を再度含んでいても安全。`asin_collector/cli.py`に`--max-consecutive-undetermined`（既定値5、int）を追加。

**3. recommend増分調査（結論: known_seller_masterへの候補拡大に意図しない行は無い）**: ローカル開発用DBに対し、#52時点の候補ロジック（`tool_execution_link`起点）と#53時点の候補ロジック（`known_seller_master`起点）を同一のSQLWHERE条件・同一時点（今回の調査時点のcurrent_date）で再実行し、候補集合を直接比較した。
- JP: 旧ロジックを今の時点で実行すると558件、新ロジック（実際の`recommend`出力と一致）は583件。差分は25件で、全件が`last_requested_date IS NULL`（`tool_execution_link`未紐づけ）のセラーであり、既存の紐づけ済みセラーが新たに増えた/消えたケースは0件だった。
- US: 旧ロジック261件、新ロジック268件。差分は+8件（全件未紐づけ）だが、旧ロジックのみに含まれていたseller_id`A5EWCCV7Y5K59&`（末尾に`&`が付いた、実在しない表記）が新ロジックでは候補から落ちることも確認した。調べた結果、`tool_execution_link`（id=1484、tool_run_id=7159378、2026-09-12登録）に`seller_id`の入力ミス（正しい表記`A5EWCCV7Y5K59`は`known_seller_master`にUS登録済み）が原因で、`tool_execution_link.seller_id`には`known_seller_master`への外部キー制約が無いため検出されずに残っていた既存のデータ不整合だった。`known_seller_master`起点に変更したことで、この誤記行は（正しいseller_idとは別の、実在しない“ゴースト”候補として）recommendの一覧に出なくなった——意図した副作用ではないが、害のない方向の変化のため、本対応ではデータ修正は行わず報告のみとする。
- 上記より、**「541件→583件」のうち25件は#53で意図した増分（未紐づけセラー）で説明がつき、known_seller_masterへの候補拡大自体は他に不要な行を増やしていない**ことを確認した。報告された「541件」と、同一ロジックを今回の調査時点で再実行した場合の558件との差（17件）は、`known_seller_master`への拡大とは無関係——`min_days_since_last`（15日）の判定基準は`last_requested_date`・`hold_recorded_date`のいずれか新しい方からの経過日数のため、保留記録・取得記録の年齢が時間の経過や記録の追加・変更によって変わるたびに、候補に出入りするセラーが生じる（通常の挙動）。「541件」測定時点の候補seller_id一覧を保存していなかったため（また本リポジトリはコード変更の履歴をgit管理していないため#52時点の実装を再現する手段が無い）、17件を1件単位で再現することはできなかったが、既存の紐づけ済みセラーについて新旧ロジックの候補集合が完全一致する（新規追加0件・消失0件）ことを直接確認できたため、候補基準の拡大が原因ではないと判断した。

**テスト**: 単体5件新規（`tests/unit/test_check_recommendations.py`: `check_sellers()`の新規/更新/既存除外の3カウンタ振り分け、連続判定不能での中断・処理済み件数・未処理一覧、HAS_PRODUCTS/NO_PRODUCTSによる連続カウントのリセット。`tests/unit/test_cli_parsers.py`: `--max-consecutive-undetermined`の既定値5・上書き）。結合7件新規・1件書き換え（`tests/integration/test_check_recommendations.py`: COMPETITOR_SAME_SERVICE/RETRY_LATERからのアップグレード・既にPERMANENTLY_EXCLUDEDな場合の無変更、旧「既存記録を上書きしない」テストは新しい3分岐のテストに置き換えた。`tests/integration/test_storefront_blocked.py`: `mark_or_escalate_to_permanently_excluded()`の新規/更新/既存除外の4パターン）。単体197件・結合228件、全てgreen（ディスポーザブルなtest-dbで検証）。実データでの追加の書き込み確認は意図的に行っていない——Task Aのロジックは既存の保留記録を実際に書き換える操作のため、ローカル開発用DBの実データ（人が判断した保留理由）を検証目的で上書きするのは不適切と判断した。

## 55. `seller_performance.cli export-all`新設: 全テーブルCSV抽出・分析用の結合済みCSV（Phase 3, 2026-10-08）

ユーザー依頼: 全データを分析用にCSVへ抽出する機能（`python -m seller_performance.cli export-all [--output-dir <フォルダ>]`、既定の出力先`exports/analysis/<yyyyMMdd_HHmmss>/`、対話式メニューへの追加は無し）。読み取り専用（DBは一切変更しない）、US/JP両方を含める（`kpi_report_role`、BYPASSRLS）、UTF-8 BOM付き・列名はDBの列名のまま・日時はJSTへ変換せずDBの値のまま、の3点を要件として明示された。

**新設モジュール**: `seller_performance/export_all.py`（`DataExportService`・`list_exportable_tables()`）。

**1. 全テーブルのダンプ**: 出力対象テーブルの一覧は、`db/table_names.py`のようなPythonコード側の静的な一覧ではなく、`information_schema.tables`（`table_schema='public' AND table_type='BASE TABLE'`）から実行時に取得する——「DB内の全テーブル」という依頼に文字どおり従うほか、`storefront_blocked_seller`（共有metadataに登録されていない、既存の`db/models.py`末尾のコメント参照）のような、Pythonコード側の一覧から漏れやすいテーブルも自動的に含まれる。管理用テーブルとして`alembic_version`のみを除外した。各テーブルは`SELECT * FROM "{table_name}"`で1テーブル=1CSV（`{table_name}.csv`）に出力する。テーブル名はこの関数の戻り値（`information_schema`由来、ユーザー入力を経由しない）であることを前提にSQL文字列へ直接埋め込んでいる——識別子はバインドパラメータ化できないため（`db/session.py`の`SET app.current_marketplace`と同じ考え方、同モジュールdocstring参照）。

**2. 結合済みCSV（分析用）**:
- `seller_summary.csv`: `known_seller_master`（register済みの全セラー）を起点に、`tool_execution_link`・`tool_output_asin`・`seller_attribution`・`storefront_blocked_seller`を集計・左結合したセラー単位の1行。列は依頼どおり`marketplace, seller_id, 初回登録日, 最終取得日, 取得日数(重複排除/全期間), 取得回数, 取込ASIN数, 合計出品成果, 禁止セラー理由, 禁止セラー記録日`。「取得日数(重複排除/全期間)」は`seller_recommender.py`の`recent_fetch_days`（直近90日固定）と異なり全期間のtool_execution_link.requested_atの重複排除日数、「取得回数」はtool_execution_link行数そのもの（`seller_history.py`の「合計取得回数」と同じ定義）。「禁止セラー理由」は表示用ラベル（`REASON_LABELS`）へ変換せず生のコード値（`COMPETITOR_SAME_SERVICE`等）のまま出力する——分析用途ではフィルタ・集計のしやすさを優先した（recommendのCSVが人の判断用に表示ラベルへ変換しているのとは異なる設計判断）。
- `tool_run_detail.csv`: `tool_execution_link`の1行（統合ツールID単位）ごとに、紐づく`tool_output_asin`件数・`seller_attribution`の出品成果件数を結合した1行。列は依頼どおり`marketplace, seller_id, 統合ツールID, 取得日, 取込ASIN数, 出品成果`。`seller_history.py`の1セラー分の履歴表示を全セラー分フラットに展開したものに相当する。

**3. CLI**: `seller_performance/cli.py`に`export-all`サブコマンドを追加（`--output-dir`省略時は`exports/analysis/<yyyyMMdd_HHmmss>/`、無ければ自動作成。指定時はそのディレクトリをそのまま使う）。`ReportSessionFactory.for_kpi_report_role()`を使用（`asin_collector.cli export-run`と同じ使用パターン）。実行完了後、出力したファイルごとの行数一覧と出力先ディレクトリのパスを画面表示する。依頼どおり対話式メニュー`cli.py`への追加は行っていない。

**テスト**: 単体4件新規（`tests/unit/test_export_all.py`新設: `_write_rows_to_csv()`のヘッダー・行数・UTF-8 BOM・NULLの空欄化）。単体2件新規（`tests/unit/test_cli_parsers.py`: `export-all`の`--output-dir`省略時None・上書き）。結合11件新規（`tests/integration/test_export_all.py`新設: `list_exportable_tables()`がalembic_versionを除外し各分類（通常CRUD・追記専用・kpi_report_role専用・廃止予定・RLS非適用）のテーブルを含むこと・昇順ソート、`export_all()`が全テーブル分＋結合済み2CSVのファイルを作ること・行数が実ファイル内容と一致すること・UTF-8 BOM・列名一致、読み取り専用性（前後でknown_seller_master行数が変わらないこと）、app_roleでは本来marketplace単位でしか見えないUS/JP双方のデータが1つのCSVに含まれること（RLS越え確認）、`seller_summary.csv`の集計値（取得日数/取得回数/取込ASIN数/合計出品成果/禁止セラー理由・記録日）・fetch履歴が無いセラーの0埋め、`tool_run_detail.csv`の1 tool_execution_link=1行・集計値）。単体203件・結合240件、うち既存2件は下記の無関係な事象により一時的に失敗（ディスポーザブルなtest-dbで検証）。

**既知の無関係な事象（本対応では対処せず）**: 今回のテスト実行時（JST 2026-10-08 01:37、UTC 2026-10-07 16:37）、`tests/integration/test_seller_performance.py`の既存2件（`test_last_requested_date_and_hold_recorded_date_are_shown_as_separate_columns`・`test_reports_total_listed_last_requested_date_and_recent_fetch_days`）が1日ずれで失敗した。原因はPythonの`date.today()`（ローカルJST基準）とPostgreSQLの`now()`（コンテナがUTC基準）の食い違い——ローカル時刻がJSTの深夜0時〜9時台（UTCではまだ前日）の間だけ再現する、本対応以前からの既存テストの潜在的な時刻依存バグであり、本ターンで変更したファイル（`seller_performance/export_all.py`・関連テスト）とは無関係（該当テストファイルは今回編集していない）。UTCが日付を追い越せば自然に解消する。修正は依頼範囲外のため見送り、事象のみ記録する。


## 56. `origin_selection`新設（段階1）: ビジネスレポート取込・候補順位付け（Phase 4, 2026-10-16）

ユーザー依頼（調査・設計→承認→実装）。起点ASIN選定のため、手動ダウンロードしたビジネスレポートCSV（US=輸出／JP=輸入）を取り込み、注文された商品点数を最優先に候補を順位付けする。出典管理・30日間隔・ASIN取得連携・統合ツール投入は後続段階で、今回の対象外。

**市場判定（確定）**: ファイル名・タイトル列・通貨・列構成からは判定せず、置き場フォルダの**解決済みパス**で決める（`data/business_reports/US|JP`、`origin_selection/paths.py`）。所定フォルダ外は`--marketplace`必須（`market_basis='EXPLICIT'`）、フォルダと矛盾する指定は停止。統合ツールログのメモ判定（`tool_intake/log_ingest.py`）は変更していない。対象期間は利用者指定値（`period_basis='USER_SPECIFIED'`のCHECKで固定）。

**DB（`alembic/versions/0014`）**: `business_report_import`・`business_report_row`（追記専用）・`business_report_import_event`（追記専用）、ビュー`v_business_report_asin`／`v_business_report_candidate`（`security_invoker`、import_id単位でのみ集計）。`import_run_history.ck_import_type`に`BUSINESS_REPORT`を追加。
- 空DB／既存DBの両立: 3表は共有`metadata`にも`ALL_TABLES`にも入れず、0014の生SQL（IF NOT EXISTS／DROP POLICY IF EXISTS）だけでRLS・GRANTまで完結（#25と同じ。0003/0004が実行時にタプルを読むため）。`db/table_names.INCREMENTAL_RLS_TABLES`はテスト・文書専用。
- 有効版は`(marketplace, period_start, period_end) WHERE status='ACTIVE'`の部分一意インデックスで常に1つ。取込同一性は`(marketplace, period_start, period_end, file_sha256)`（ハッシュ単独では判定しない）。
- 行エラーが1件でもあるレポートは有効化不可（アプリ層の拒否＋`ck_bri_active_no_errors`）。取込は常にSTAGED。有効化は`--mode first|replace`の明示が必須で、確認待ちの間はDB接続・ロックを保持せず、開始時に状態を再確認（アドバイザリロック＋`FOR UPDATE`）。
- `app_role`は`business_report_import`の`status`・`activated_at`のみUPDATE可（列限定）。DELETEはどの表も不可、`admin_role`への付与なし。

**参照専用経路**: 別市場の同一ハッシュ警告・`--import-id`からの市場解決には`kpi_report_role`（SELECTのみ）を使い、`app_role`のRLSは回避しない。参照専用接続が未設定なら警告を省略し、市場解決は市場別`app_role`セッション（US→JP）にフォールバックする。以降の更新・読み取りは必ず市場別`app_role`セッションで行う。

**CLAUDE.md**: 3節の追記専用リストに`business_report_row`・`business_report_import_event`を追加した（承認済み）。

**CLI通し確認（2026-10-16、使い捨てtest-db・架空CSV）**: list-inbox→import-report→verify-report→activate-report→candidates→list-reportsをUS/JPで確認。`SEEDASIN_KPI_REPORT_DATABASE_URL`は任意——未設定・接続不可でも`--import-id`からの市場解決は市場別app_roleセッションへのフォールバックで動作し、省略されるのは別市場の同一ファイル警告のみ（警告文には接続詳細を出さず例外の種類だけ表示）。

**ACTIVEレポートの無効化（2026-10-16追加、ユーザー依頼）**: `deactivate-report --import-id N --reason "..."`。ACTIVEのみ・理由必須。状態はSUPERSEDEDに変え、`business_report_import_event`に`DEACTIVATED`イベント（理由=note、実行者、日時）を記録する（差替えによる旧版化の`SUPERSEDED`イベントとは種別で区別）。元行・取込履歴は削除せず、過去のレポートは自動で有効化しない（その市場・期間に有効版が無い状態を許す）。無効化後は`--import-id`指定でも通常の`candidates`対象外（`--allow-non-active`のみ）。確認待ちの間はDB接続・ロックを保持せず、実行時にアドバイザリロック（有効化と同一キー）＋行ロックで状態を再確認する。復元は既存の有効化操作で行い、`--mode first`がSUPERSEDEDにも使えるよう緩めた（有効版が無い期間のみ。別の版が有効なら`--mode replace`を明示）。`show-history`を追加（イベント履歴の表示）。
- **マイグレーション方針**: 0014を書き換えず追加の`0015_add_deactivated_event_type.py`にした。理由: 本リポジトリの方針（#13/#25）は、適用され得る環境があるマイグレーションは書き換えず増分で足すこと。0014は実データDBには未適用と聞いているが、未コミット・個人の開発用DBへの適用有無を確認できない。0015は、0014適用済み・未適用のどちらの環境にも同じ手順で成立し（空DBは0014→0015、0014適用済みは0015のみ）、変更も`ck_brie_type`の付け替えと`ck_brie_deactivated_reason`（無効化イベントはnote必須）の追加だけで小さい。downgradeはDEACTIVATEDをSUPERSEDEDへ読み替えて履歴を残す。
- RLS・権限は変更なし（`app_role`は`status`・`activated_at`のみ列限定UPDATE、eventは追記専用）。


## 57. `acquisition`新設（段階2）: 出典管理と再取得間隔（Phase 4, 2026-10-17）

ユーザー承認済みの設計（調査→設計修正→実装）。実コレクタ・ブラウザ・統合ツールへの接続は対象外（段階3以降）。

**スキーマ（`alembic/versions/0016`）**: 新設5テーブル（`acquisition_interval_config`・`acquisition_source`・`acquisition_path`(追記専用)・`acquisition_source_event`(追記専用)・`acquisition_legacy_evidence`(追記専用)）と`collector_run`の列追加。段階1と同じく共有metadata/`ALL_TABLES`に入れず、生SQLで完結（`db/table_names.ACQUISITION_RLS_TABLES`はテスト・文書専用）。
- `marketplace`は**取得先ページの市場**（既存`collector_run.marketplace`と同じ意味）。起点レポートの市場・業務方向は`acquisition_path`の`origin_marketplace`・`direction`・`origin_import_id`で別に持つ。
- 出典キーは`key_primary`＋`key_qualifier`(NOT NULL, 既定'')の構造化キー。一意性はSHA-256の`key_hash`（長いキーワードでもインデックスサイズ制限に当たらない）。元の値も保持し、登録時にhash一致でも元の値が違えば`KeyCollisionError`。種別は`SELLER/BRAND_NAME/BRAND_STORE/KEYWORD`（ブランド名とストアは別種別。表記の自動統合は全角半角・大小文字・空白のみ）。
- 起点の指標（注文点数・セッション・最大獲得率）は経路の登録時に`import_id`指定のレポートからコピー（区画をまたぐ読み取りを順位付けから排除）。planは使うimportを明示指定させ、同じ市場の2つのimportは拒否、指定外のimportの経路は順位に使わない。
- 失敗の連続判定: `FAILED/PARTIAL/UNVERIFIED_EMPTY`のみ数える。ユーザー中断(Ctrl+C)・`abort-run`・lease失効は数えない。3回で自動保留（根拠表示、`release-hold`で明示解除し以降の失敗は数え直し）。冷却60分。初期値は`acquisition_interval_config`（市場×種別、設定変更可。lease 5分/CAPTCHA 30分は仮設定で段階3で見直す）。
- 30日境界は`as_of >= 基準時刻 + n×24時間`（テスト: 30日直前・ちょうど・経過後）。

**lease・競合**: 全操作が`collector_run`行の`FOR UPDATE`を先に取り、DB時刻(`clock_timestamp()`)でleaseの有効性・トークン・状態を判定する。heartbeat/ASIN追記/終了は失効後は`LeaseLost`（復活させない）、回収も同じ行ロックで直列化。新方式の行(`source_id`有り)は終了後UPDATEを保護トリガーが拒否、`source_id`/`marketplace`は全行で不変。**旧経路の行(`source_id IS NULL`)と既存コレクタ・既存テストには影響しない**（旧RUNNINGのid 33・38は変更も回収もしない）。
- claim・保留/除外/解除・間隔変更・自動保留・旧証拠の分類/確定/却下は、(取得先市場, 種別, key_hash)の**識別ロック**に参加。ロック順序: 識別ロック→出典行→run行。
- **保証範囲**: 既存`storefront_blocked_seller`の更新経路（`seller_ledger.storefront_blocked`等）はこのロックに参加しない。claimは判定クエリ実行時点でコミット済みの内容を反映するが、claim処理中に並行してコミットされた更新は反映されない可能性がある（競合の完全防止は保証しない）。

**旧証拠**: 観測と人の判断を同一テーブルに行種別で追記（`row_type`）。状態は最新の判断を畳み込んだ結果で、履歴は上書きされない。取得先市場が未確定の観測はUS・JP両区画に複製し、各区画のclaimが自区画だけを読んで`REVIEW_LEGACY`を判定（RLS迂回・参照専用ロール不要）。種別が未分類の証拠はどの出典も保留しない（一覧で分かる）。
- `legacy-confirm`の競合防止: 証拠グループ単位のセッション・アドバイザリロックを保持して区画ごとの短いトランザクションで追記（確定先→他方）。途中で失敗しても他方はUNKNOWNのまま（保留が外れる方向に誤らない）、同じコマンドの再実行で補完、別市場への確定はreopenまで拒否。1トランザクション内で区画を切り替えない理由は、1接続=1市場というRLSの前提を保つため。
- dry-run(`legacy-dry-run`)はDBに書かない（`SET TRANSACTION READ ONLY`）。事実/推定/未確認を success・market・type の3軸で出し、適用は利用者が選んだ行だけ。旧コレクタはURLのドメインで取得先市場は事実だが、成功は推定のため`--confirm-recorded-market`の明示なしには確定しない。`MAX(acquired_at)`は参考証拠で取得成功日時とは断定せず、確定後も新方式の成功とは区別して表示。セラー側`status.csv`は移行候補、`done/`は一覧のみ。

**未実装・後続**: 実コレクタへの接続（ブラウザ、段階3）、統合ツール投入・`tool_execution_link`への取得先市場の記録（段階4）、人が承認する別名表、`seller_recommender`との統合。

**最終レビューでの追加・訂正（2026-10-17）**:
- 旧証拠の件数の照合: dry-runの観測1,885件 = 統合ツール実行1,764（tel全行。取込あり1,720／取込なし44）+ 旧collector_run 76（件数あり58／未確認18）+ status.csv 45（推定33／未確認12）。除外・重複排除・解析不能による取りこぼしは無い（1行=1観測、グループIDの重複0）。前回報告の「取込あり1,709」はDB時刻2026-10-16 20:02 UTC時点の値で、その後に11実行（US 8・JP 3、imported_at 21:39〜22:05 UTC）の出力が取り込まれて1,720になった（データの変化）。報告書の「1,677」「約117」は集計の誤記で、正しくは1,720と74。
- `legacy-confirm`等の市場横断ロック（`LegacyService._group_lock`）: 取得した同じ接続で解放し、解放を確認できない接続はプールへ戻さず破棄する（セッション・アドバイザリロックはプール返却時のrollbackでは解放されないため）。ロック取得の途中で例外になった接続も破棄。確定は区画ごとの追記時にも「別市場で確定済みなら拒否」を二重に確認する。`apply`の分類・確定も同じ群ロックで直列化。
- 旧証拠の畳み込みはevidence_id（一意の連番）だけで順序を決める（recorded_atは同時刻になり得るため根拠にしない。入力順にも依存しない）。
- 取得成功日時が不明/参考の旧証拠は、確定後も「最終成功」には出さず、「旧証拠(確定)」「基準=旧証拠(参考)」として別に表示する。
- 0016のdowngradeは既存のcollector_run・collector_asinを1行も削除しない。失われるのはacquisition_*の全データとcollector_runの新規列の値、INTERRUPTEDの区別（FAILEDへ読み替え）。
- `legacy-unclassified`を追加（種別が自動で決まらなかった旧証拠の確認用一覧。値ごとの要約と明細。種別は推測せず、decided_*列は空欄）。
- 既存テスト（`tests/integration/test_seller_performance.py`）の基準日をローカル日付からUTC日付に変更（製品コードの業務日付＝DBのセッションタイムゾーン基準は変更していない。業務日付をJST基準に変える場合は製品の仕様変更として別途判断が必要）。

**旧証拠の移行対象の選択（`legacy-select`、2026-10-17）**: 読み取り専用。DB時刻を基準に、(1)種別・出典キーを確認できる（記録/URL由来。セラーID形式の正規表現による推定は含めず「誤分類候補」として別の一覧）(2)証拠時刻が取得間隔内（`acquisition_interval_config`。取得先市場が不明なら両市場の長い方）(3)取得成功の承認済み参考証拠（推定）がある、を満たす観測だけを選ぶ。取得先市場が不明なら両市場のREVIEW_LEGACY用（SELECT_BOTH）、記録で市場が分かれば参考証拠として保持（SELECT_RECORDED_MARKET）。未分類・done/・取得成功の根拠なし・旧RUNNINGは選択しない。除外理由は1件に1つ（優先順）。証拠数・出典数・予定のDB行数を区別して集計し、元のdry-run（apply列は全行未選択）と選択結果CSVは別ファイル。適用は`legacy-apply --plan <選択結果> --classify fact --confirm-recorded-market`（別途指示）。
- 時刻の注意: `tool_output_asin.acquired_at`（統合ツールの「取得日時」）は、DB時刻より未来の値（最大+7.6時間）があり、JSTのローカル時刻がUTCとして保存されている疑いがある（取込時刻との比較から）。統合ツール実行（TOOL_RUN）の証拠時刻は最大9時間の過大評価の可能性があるため、判断・補正は別途（今回の選択対象41件はcollector_run/status.csv由来で影響しない）。

**旧証拠の投入結果（2026-10-17、実DB。docs/DEVIATIONS.md #57追記）**: `legacy-select/legacy-apply`に`--ledger-jp/--ledger-us`を追加。セラー台帳(xlsx)の「seller id」列に（Brand行でなく）登録され、キーワード/ブランドの記録がなく、ASINと同じ10桁でもないID形式の推定SELLERだけを種別確認済み(FACT)にする（236件中208件。残り28件は台帳に根拠なしで未選択）。種別の根拠文は`type_hint_basis`(varchar 60)に収まる短文。取得先市場は不明のままなので両市場REVIEW_LEGACY。種別未確認の履歴(推定のみ28＋未分類45＝73件)は`legacy_observe_only_*.csv`を`--classify none`で観測のみ投入（保留にも未取得扱いにもしない）。`acquire-plan`は未確定の旧履歴件数を参考表示。投入は証拠1,097行（観測601＝選択248件分455＋観測のみ146、判断496＝CLASSIFY455＋CONFIRM41）。初回投入は台帳指定を忘れ41件のみ分類→台帳指定で再実行して不足分を補い、再実行で増加0を確認。

**未分類履歴と時刻（2026-10-17、docs/DEVIATIONS.md #57追記）**:
- 件数の整合: 投入した出典の識別情報は234件。市場不明で両市場に置いた206＋記録の市場で保持した37（US13／JP24）＝243は、**9件が「市場不明」と「市場記録あり」の両方の証拠を持つ**ため二重に数えた値で、243−9=234。観測（248）・識別情報（234）・市場別の区画に置いた行（観測455行など）は別の単位で、投入漏れ・誤重複はない。
- 未分類履歴（種別が確定していない旧証拠）は、以前は件数表示のみで個別の出典を止めなかった。**識別情報（その出典の種別で正規化した値）が一致する出典は、取得間隔の間、REVIEW_LEGACYとして止める**ように変更（`eligibility.decide`。plan・claim共通）。種別は推測せず、人が`legacy-classify`で分類するか`legacy-dismiss`で却下すれば解除される。値が一致しない出典は止めない。既存スキーマの範囲で実装。
- 未分類一覧に、URLの明示的なパラメータ（`me=`、単一の`rh=p_89:`）から言える分類案の列（suggested_*）を追加。案はDBに書かない。名前・文字列の印象からは案を作らない（現在: JPのブランド絞り込みURL2件のみ案あり、他13種は案なし）。
- 時刻（未補正）: `tool_output_asin.acquired_at`にDB時刻より未来の値（最大+7.6時間）があり、JSTのローカル時刻がUTCとして保存されている疑いがある。**事実として確認できたのは「未来の値が存在する」ことまで**で、全行がずれているかは未確認。仮にずれているなら、統合ツール実行（TOOL_RUN）由来の旧証拠の時刻は最大9時間遅く見え、再取得可能と判定される時刻が最大9時間遅くなる（安全側＝取り過ぎない方向）。collector_run・status.csv由来の証拠は対象外。今回は補正しない。
- 再発防止: `legacy-apply`は、計画CSVで種別が確認済み(FACT)の行を同じ条件で再現できない場合（台帳指定の付け忘れ）は書き込み前に中止する。文字列列の長さ上限（60文字等）を超える観測がある場合も、全件を先に検査して1行も書かずに中止する。

## 58. 出典管理に接続した取得（段階3）: `asin_collector.source_runner` / `collect-source` / `collect-next`（Phase 4, 2026-10-17）

**目的**: 既知の出典を明示して取得する入口を、`acquisition`の実行制御（claim・lease・heartbeat・結果の区別）に接続した。セラーの自動発見・反対側市場への展開は含まない（残作業）。DBスキーマの変更なし。

- `collect-source --market US|JP --source-id N` / `collect-next --market ... --origin-import <id> --section EXPORT|IMPORT|ALL [--max-sources 1] [--dry-run]`。**従来の`collect-brand-page`・`export-run`・`check-recommendations`・ルートの対話メニューは変更していない**（旧経路は`source_id`なしの`collector_run`のまま共存。`export-run`は新方式の実行も同じ`collector_asin`から書き出せる）。
- 流れ: `claim`（取得間隔・除外・保留・実行中・旧証拠・未分類履歴の一致を再判定）→ ページ取得 → 1ページごとに`append_asins`（run行ロック＋トークン＋DB時刻のlease確認）→ `finish`。heartbeatは別スレッドで、CAPTCHAの人の待機中も継続（待機中はDB上`CAPTCHA_WAIT`でleaseは長め）。leaseを失ったら以後のページ取得も書き込みも終了確定もしない（`LEASE_LOST`を報告）。Ctrl+Cは`INTERRUPTED(USER)`として確定（失敗に数えない）。
- 結果の区別: `SUCCESS_NONEMPTY` / `SUCCESS_EMPTY`（SELLERで0件ウィジェットを確認した場合のみ）/ `SUCCESS_TRUNCATED`（ページ数・ASIN数の上限。`--max-pages`既定5）/ `PARTIAL`（保存後の失敗・結果の途中の0件ページ・同一URL再訪）/ `FAILED`（1ページ目から取得できない・5xx（2回自動リトライ）・CAPTCHA未解決）/ `UNVERIFIED_EMPTY`（0件に見えるが確認できない。**成功にしない**）。確認済み0件・上限打切りを30日に数えるかは`acquisition_interval_config`の設定どおり。
- 順位と対象: `acquire-plan`の順位（未取得優先→注文点数→セッション→獲得率）をそのまま引き継ぐ。`collect-next`は**注文0点の起点しかない出典・起点のない出典を既定で対象にしない**（経路は残る。`--include-zero-units`/`--include-no-origin`で明示）、実行件数は`--max-sources`（既定1、最大20）。連続して成功しない実行が3件続けば残りを実行しない。
- ブラウザ: 取得先市場の永続プロファイル（`playwright_profiles/us|jp`、headed）。配送先表示の確認は**警告のみ**（郵便番号US 97005 / JP 289-1223、自動操作はしない）。

**実ページで未検証（成功・正常とは説明しない）**: ①KEYWORD/BRAND_NAME/BRAND_STOREの0件判定（ウィジェットがあっても`UNVERIFIED_EMPTY`にしている。検証済みなのはSELLERのみ、#53）②KEYWORD/BRAND_NAMEのURL形式（BRAND_NAMEは旧実行に見られる`rh=p_89`）③配送先表示ブロック(`#glow-ingress-block`)の検出④CAPTCHA・5xxのマーカー（既存の#18/#19）⑤複数ページ巡回での「次へ」の挙動は既存の移植元のとおりで、新方式のURL（セラー店頭）では未確認。
**検証方法**: 疑似ページ・疑似ブラウザ＋使い捨てtest-db（`tests/integration/test_acquisition_runner.py`、`tests/unit/test_source_runner.py`）。外部接続・ブラウザ起動なし。
**残作業（別途）**: 起点ASINからの新規セラー発見、反対側市場（US→JPなど）への展開、統合ツールへの投入（`tool_execution_link`の対象市場）、取得したASINの`collector_asin`から統合ツール入力への接続の自動化。

**#58 追記: 初回の実地確認（2026-10-17、JP 1出典・最大1ページ）**
- 対象は接続確認用の**手動登録**（起点ASINからの発見ではない。経路は「起点なし」）。取得先市場の根拠は、過去の`collector_run`に残る`me=`付きURLのドメイン（JP: run 5 / US: run 6）。`known_seller_master`/`tool_execution_link`の市場は業務方向なので根拠にしなかった（先に提案したA14MFQBNVE95KO・A1G9JTQNOH85WUはページの市場を示す記録がなく不採用）。
- JP: source_id=9 (A2K28GPVO862U1) run=78 `SUCCESS_TRUNCATED`(PAGE_LIMIT) 1ページ16 ASIN（重複なし）。設定どおり30日に数えられ、再取得可能は2026-11-16 08:24 UTC。claim/planとも`TOO_RECENT`で拒否されることを確認。既存の`collector_run`76行はハッシュ一致で無変更。
- **配送先の不一致**: JPプロファイルは「お届け先: 153-0064」（想定289-1223）、USプロファイルは「Deliver to Japan」（想定97005）。表示ブロック`#glow-ingress-block`のセレクタは実ページで有効と確認（上記の検出は実ページ検証済み）。プログラムでは設定しない方針のため、人が設定する。JPのrun 78は想定外の配送先で取得されている（商品一覧の見え方に影響し得る）。USは配送先確認後に実行するため未実行（source_id=10 登録のみ、ELIGIBLE_NEVER）。
- 追加: 標準入力が使えない実行でのCAPTCHAは、画面で人が解くのをポーリングして待つ（回避しない。時間切れは`CAPTCHA_UNSOLVED`）。`--require-delivery-match`（配送先が確認できなければclaim前に中止。既定は警告のみ）。

**#58 追記2: 配送先を手動設定した後のUS実地確認（2026-10-17）**
- 人が手動でお届け先を設定（US: Beaverton 97005 / JP: 山武市埴谷 289-1223）し、表示の一致を確認してから実行（自動変更なし）。US: source_id=10 (AB49KC99NVKLD) run=79 `SUCCESS_TRUNCATED`(PAGE_LIMIT) 1ページ16 ASIN（重複なし・JPのrun 78との重複0）、`--require-delivery-match`付きで警告なし。claim/planとも`TOO_RECENT`、再取得可能は2026-11-16 08:37 UTC。
- **JP run 78 は配送先153-0064（想定外）で取得した結果**。削除・補正・再取得はしていない。接続確認としては有効だが、想定配送先（289-1223）での取得品質は**未検証**のまま（30日制限により当面再取得しない）。
- 既存の`collector_run`（id<=77）はハッシュ一致で無変更。旧RUNNING(33/38)・保留・レポートも無変更。

## 59. 起点ASINからの出典発見と反対側市場への展開（段階4、Phase 4, 2026-10-17）

**目的**: 注文点数上位の起点ASINの商品ページから、他セラー・ブランドを出典として登録し、取得済みの出典のASINから反対側市場の探索経路を作る。**出典の取得は行わない**（登録した出典は既存の plan・claim・30日間隔を通す）。既存スキーマの範囲で実装（マイグレーションなし）。実ページ・実DBでの実行は未実施（疑似ページ＋test-dbで検証）。

- 追加: `asin_collector/product_page.py`（商品ページの抽出＝純粋関数＋取得結果の区別）、`asin_collector/discovery_runner.py`（実行部）、`acquisition/discovery.py`（起点選択・再探索間隔・自社ID・登録）、`SourceRegistry.register_via_path`、CLI `asin_collector.cli discover-origin / discover-cross`、`acquisition.cli own-account-add / own-account-list`。
- 流れ: ①`--origin-import`で明示したACTIVEレポートから注文点数順に起点ASINを選ぶ（注文0点は既定で対象外）②**商品ページの市場(`--page-market`)を明示**して調べる（セラー＝そのページの出品者、ブランド＝bylineのストアリンク／ブランド絞込リンク／詳細表。経路は`--routes SELLER,BRAND`）③自社セラー(`marketplace_account_config`のis_active)とAmazon自身を除外。**自社IDが未設定の市場では実行しない**④`acquisition_source`へ登録、`acquisition_path`にORIGIN_ASIN経路（起点import・ASIN・方向・指標をコピー）。抽出元・セラー範囲・商品ページURL・調査runはnoteのJSON。同じ出典を複数起点から見つけても出典は1つ・経路が増える⑤反対側市場: 取得済み出典の最新の完了した実行のASINを、**反対側市場**の商品ページで調べ、VIA_SOURCE経路（元の出典の代表起点の方向・指標をコピーして順位を引き継ぐ。planの経路条件にVIA_SOURCEを追加）。**同じASINが反対側市場にあるとは仮定しない**: 存在確認(FOUND)・該当なし(HTTP404のみ)・構造不明(UNCONFIRMED)・読込失敗/5xx/CAPTCHAを区別して記録。
- セラー範囲: 購入ボックスの1件だけは`BUYBOX_ONLY`（全セラー取得とは扱わない）、出品一覧パネルは`VISIBLE_OFFERS`（表示範囲。全出品者とは確認していない）。認識した領域の外（おすすめ・広告）のリンクは拾わない。ブランドストア／ブランド名（絞込リンク・詳細表）は別の出典種別で、ストアの文言からブランド名は作らない。
- **再探索間隔（既存構造での管理）**: 商品ページの調査は出典種別に無い（CHECK制約）ため、`collector_run`の行（run_type='ASIN_PAGE'、target='ASIN:<ASIN>|<routes>'、source_id無し）で管理する。完了から30日・失敗後60分の冷却・3回連続失敗で30日停止・実行中の二重起動拒否（stale 30分）・中断は失敗に数えない（`AsinPageGuard`、識別ロック付き）。該当なし(404)は完了として間隔に数え、構造不明・候補0件は**成功にしない**。旧履歴の走査(`legacy_scan`)はASIN_PAGEを除外。**限界**: lease/heartbeat/フェンシングは無い（人が手元で1つずつ起動する対話式の前提）。
- 展開数の上限（CLI引数＋ハード上限）: 起点ASIN数(既定2/最大10)・起点ごとの出典数(3/10)・反対側へ展開するASIN数(2/5)・展開ASINごとの出典数(3/5)。2回連続の調査失敗・CAPTCHA未解決で残りを実行しない。配送先が確認できなければ調査を始めない（上書き不可）。

**実ページで未検証**: 商品ページのセレクタ（bylineInfo / po-brand / merchant-info / aod-offer-list 等）、404の挙動、出品一覧パネルが商品ページ内に出るか（出ない場合は`BUYBOX_ONLY`止まりで、全出品者の取得には別の画面が要る）、Amazon自身の出品者ID（US ATVPDKIKX0DER / JP AN1VRQENFRJN5）。

**スキーマ変更案（保留。不可欠ではない）**: ①出典種別`ASIN_PAGE`の追加（acquisition_source / acquisition_interval_config / 旧証拠のCHECKとシード）で、商品ページ調査を他の出典と同じ claim・lease・間隔に乗せる ②`acquisition_path`に抽出元・セラー範囲・ページ市場・調査runの構造化列（現在はnote JSON）③展開数の上限を設定テーブル化（`processing_limit_config`のlimit_typeはCHECKで固定のため拡張が要る）。


**#59 改訂追記（2026-10-17、実地試行前の修正。上の記述のうち、以下が置き換わる）**

1. **商品ページ調査の実行管理（lease付き）**: 既存のclaimの保護は`source_id`のある行にしか効かない（保護トリガー・ユニーク索引・`AcquisitionRunService`）ため、`run_type`を付けただけでは効かない。`AsinPageGuard`がアプリ層で同等の保護を行う: 全ての書き込み（heartbeat・CAPTCHA待機・登録の直前・終了・中断）で 行ロック(FOR UPDATE)→トークン→状態→DB時刻のlease の順に検証、期限切れの回収（`begin`時と`reap-asin-pages`）、人による中断（`abort-asin-page`）、回収・終了済みの行に旧プロセスが書くと`LeaseLost`、heartbeatは別スレッド（CAPTCHA待機中も継続・待機中はDB上`CAPTCHA_WAIT`でlease長め）、二重起動は識別ロック＋有効なleaseの行で拒否。**DBが強制するものではない**（下のスキーマ変更案を参照）。
2. **連続失敗の自動停止（出典のAUTO_HOLDと同じ意味）**: 失敗(FAILED)が連続3回で`AUTO_STOPPED`。**期限では戻らず、`release-asin-page --page-market --asin --reason`（理由必須）で解除するまで続く**。解除は履歴の行（error_code=RELEASE_HOLD）を追記し、以降それ以前の失敗は数えない。失敗後は60分の冷却。中断(Ctrl+C・手動中断)・lease失効は失敗に数えない。成功（COMPLETED）でも数え直す。停止理由は`show-asin-page`で確認できる。
3. **Amazon自身の除外**: 以前コードに入れていたJPの`AN1VRQENFRJN5`は、リポジトリ・DBに根拠が無い未確認の値だったため**削除**。販売元IDではない**市場ID**を市場ごとに除外する（`MARKETPLACE_IDS`: US `ATVPDKIKX0DER` は利用者が実ブラウザで確認したURLの`marketplaceID=`、JP `A1VC38T7YXB528` はJPの市場ID）。両IDとも、DBの`known_seller_master`・`storefront_blocked_seller`(RETRY_LATER)にセラーとして記録されており、URLの市場IDの誤記録の疑いがある（JP台帳にも店名"toprakk"の行に同じ値）。**JPのAmazon.co.jp自身の販売元IDは確認できていないため、IDでは除外しない**。代わりに、表示名がAmazon小売と完全一致する出品者（`amazon` / `amazon.com` / `amazon.co.jp` / `amazon.com services llc`）は登録せず`NEEDS_REVIEW_AMAZON_NAME`として報告する（IDを人が確認し、販売元なら`storefront_blocked_seller`へ）。**配送元がAmazonなだけのFBA出品者は、販売元IDが別なので除外しない**（`Amazon Fan Shop`のように名前にAmazonを含むだけの出品者も除外しない）。
4. **反対側市場への展開**: 元ASINを取る実行は`COMPLETED`かつ結果が`SUCCESS_NONEMPTY`または`SUCCESS_TRUNCATED`のものだけ（PARTIAL・UNVERIFIED_EMPTY・FAILEDは使わない）。上限打切りの結果を使うときは「ASINの一部だけ」と明示。**過去の取得結果を使うだけで、出典の再取得はしない**（再取得は`collect-source`。30日間隔を通る）。過去の結果の古さは`--max-run-age-days`（既定30日）、実行は`--run-id`で指定可。代表起点は`--origin-import`で明示した**ACTIVEのimportに属する経路だけ**から選び（`register_via_path(allowed_origin_import_ids=)`）、該当が無ければ登録しない。
5. **同じ商品ページの再調査**: 調査を**ページ単位**にした（targetにrouteを含めない）。抽出結果（出品者・ブランド・範囲。自社・Amazon等の除外前の生の結果）を完了した行の`collector_run.error_message`にJSONで保存し、30日以内は後から別route（SELLER→BRAND）を求められても**ページへ再アクセスせず保存済みの結果から登録**する（`REUSED`）。30日を過ぎたら再取得。保存結果が壊れている場合は再取得せず止める。商品ページと確認できても出品者もブランドも抽出できない場合は成功に数えない（`FOUND_NO_DATA`）。同じrouteの再適用は冪等（出典・経路は増えない）。`--after-rank`で調査済みの上位を飛ばして進められる。

**スキーマ変更案（具体案。実DBには未適用・保留）**
- A. 商品ページ調査の実行管理をDBに強制させる（マイグレーション0017案）: ①`collector_run_guard()`を拡張し、`run_type='ASIN_PAGE'`の行も 終了済みは書き換え不可・`attempt_token`/`marketplace`は不変 にする ②`CHECK (run_type <> 'ASIN_PAGE' OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_seconds IS NOT NULL))` ③部分ユニーク索引`uk_collector_run_one_active_asin_page ON collector_run (marketplace, target) WHERE run_type = 'ASIN_PAGE' AND status IN ('RUNNING','CAPTCHA_WAIT')`（二重起動をDBが拒否）。いずれも追加のみで既存行に影響しない。downgradeで制約・索引を削除しトリガーを元に戻す。
- B. 抽出結果の保存先を`error_message`から専用列（`page_extraction JSONB`）へ。
- C. （代替案）出典種別`ASIN_PAGE`を追加して、claim・lease・間隔を出典と完全に共通化（acquisition_source / acquisition_interval_config / 旧証拠のCHECKとシード、planの対象から除外する処理が要る）。A+Bで足りるなら不要。
- D. `acquisition_path`の抽出元・セラー範囲・ページ市場・調査runの構造化列（現在はnote JSON）、展開数の上限の設定テーブル化。

**#59 追記2（2026-10-17、初回試行の直前に改訂）**
- **Amazon小売の扱い（上の「Amazon自身の除外」を置き換え）**: 「リポジトリ・DBに根拠が無い」ことと「そのIDが誤りである」ことは別なので、`ATVPDKIKX0DER`（USの市場ID）や`A1VC38T7YXB528`（JPの市場ID）が**販売元識別子としても使われ得ないとは断定しない**。除外ではなく**登録を保留して根拠を報告**する（`HELD_AMAZON_RETAIL_UNDETERMINED`。根拠＝市場IDとの一致／表示名がAmazon小売と完全一致、リンクの役割・領域・表示名を`Skipped.detail`に残す）。確認できないIDは推測で追加しない（未確認の`AN1VRQENFRJN5`は削除済み）。判定は人が行い、販売元がAmazon小売なら`storefront_blocked_seller`へ（既存記録・`storefront_blocked_seller`はこのコードでは変更しない）。
- **販売元リンク・販売元表示・配送元表示の区別**: 候補にするのは`seller=`付きのリンク（販売元リンク）だけ。配送元の表示内（fulfillerInfo / shipsFrom 等の目印）のリンクは`shippers`に分けて出典にしない（FBAは配送元がAmazonなだけ）。販売元の表示にIDのリンクが無いものはIDが無いので登録できない。役割の目印が見つからないリンクは`UNSPECIFIED`（決めつけない。目印のマークアップは実ページ未検証）。
- **抽出結果を`collector_run.error_message`へ保存する方式は暫定**（誤用に近い。`error_message`は本来エラー文）。今回の試行のための追加改修はしないが、**大量運用の前に、専用の保存先（`page_extraction JSONB`列。案B）と、DB側の保護（トリガー拡張・CHECK・部分ユニーク索引。案A）を整える**。AsinPageGuardの保証はアプリ層だけで、生SQL等で保護を迂回できる限界は残る。

**#59 追記3: 初回の実地試行（2026-10-17、各市場の起点1 ASIN・商品ページ1ページ・SELLER最大3件）**
自社IDを設定（US: A5FRT2XHYO43T / JP: A3R9V8IM8MACMH、`own-account-add`）。試行前に、test-dbでAsinPageGuardの二重起動拒否・期限切れ後の登録拒否・回収後の旧プロセスの終了確定拒否を確認済み（アプリ層だけの保証）。
- US: 起点B0076AZHC8（注文9点）→ 商品ページ run=80 `SUCCESS_NONEMPTY` / 抽出範囲`BUYBOX_ONLY`（購入ボックスの1件のみ。全セラー取得ではない）→ SELLER A3AJLR7DMJUOQG（DLC international、リンクの役割=SOLD_BY）を source_id=11 に登録（出典新規・経路ORIGIN_ASIN/EXPORT）。同じページからブランドストア（Panduit）とブランド名（Panduit、詳細表）も抽出して保存（今回はrouteがSELLERのため未登録）。自社除外・Amazon小売の保留・配送元表示: 該当なし。再探索可能（ページ）: 2026-11-16 09:46 UTC。
- JP: 起点B0B46VP7NS（注文1点）→ run=81 `SUCCESS_NONEMPTY` / `BUYBOX_ONLY` → SELLER A1MN7UZC7MPZTE（TOPSWING2、SOLD_BY）を source_id=12 に登録（IMPORT）。ブランド名「HD Ridez」（詳細表）を保存。自社除外・保留・配送元: 該当なし。再探索可能: 2026-11-16 09:47 UTC。
- 実ページで確認できたこと: 商品ページの判定マーカー、購入ボックスの販売元リンク（seller=）、販売元表示の役割目印（SOLD_BY）、詳細表のブランド行（po-brand）、bylineのブランドストアリンク。**出品一覧パネル（VISIBLE_OFFERS）は商品ページ内に出なかった**（購入ボックスの1件のみ）ため、他セラーの網羅は別の画面が要る。配送元（SHIPS_FROM）の目印は、この2ページには該当がなく未検証。
- 出典の取得（セラー取扱ASIN）、反対側展開、統合ツール投入、既存データの変更は行っていない（collector_run id<=77のハッシュ一致、保留・旧RUNNING・レポート無変更）。

**#59 追記4: 取扱ASINの少量取得と反対側市場への展開（2026-10-17）**
- 開始時にID・市場・経路を照合: US source_id=11 A3AJLR7DMJUOQG（ORIGIN_ASIN/EXPORT/import=1 B0076AZHC8 注文9点）、JP source_id=12 A1MN7UZC7MPZTE（ORIGIN_ASIN/IMPORT/import=2 B0B46VP7NS 注文1点）。planでは両方「未取得」の1位、claimで可。
- 取得（配送先一致必須・最大2ページ）: US run=82 2ページ32 ASIN `SUCCESS_TRUNCATED`(PAGE_LIMIT) 再取得可能 2026-11-16 10:08 UTC / JP run=83 2ページ32 ASIN `SUCCESS_TRUNCATED` 再取得可能 2026-11-16 10:09 UTC。いずれも**2ページ目への巡回を確認**（次ページがあり上限で打切り）。ASINは重複なし。元の起点ASINは取得結果に含まれていなかった。
- 反対側展開（取得結果の保存順で、元の起点ASIN以外の最初の1件。決定的）: US→JP は B09DGF5WCD を **JPの商品ページ**で調査（run=84、FOUND、`BUYBOX_ONLY`）→ source_id=13 SELLER A3RXLX7WSXLBCF（【迅速発送】くるみ商店、SOLD_BY）を登録。JP→US は B0058BDP80 を **USの商品ページ**で調査（run=85、FOUND、`BUYBOX_ONLY`）→ source_id=14 SELLER A1G9JTQNOH85WU（Harutan【Japan Import】、SOLD_BY）を登録。いずれも「上限打切りの一部の結果からの展開」（`derived_from_partial_result=true`）。
- 経路: VIA_SOURCE。13 = 起点 US import=1 B0076AZHC8（注文9点, EXPORT）→ 経由出典 11 の取得 run=82 → 経由ASIN B09DGF5WCD → 調査先 JP。14 = 起点 JP import=2 B0B46VP7NS（注文1点, IMPORT）→ 経由出典 12 の取得 run=83 → 経由ASIN B0058BDP80 → 調査先 US。planでは両方「未取得」で、起点の注文実績を引き継いで並ぶ。反対側で見つけた出典（13・14）の取扱ASINは**取得していない**。
- 実ページで確認できたこと（追加）: 複数ページ巡回（「次へ」リンク）、配送元の表示（`SHIPS_FROM`）を販売元と区別して出典にしない動作（JP: A26YONAZMFTVSE、US: AGM3OIPIW0FJG を配送元として検出、登録せず）。**ブランドは反対側の2ページでは抽出0件**（HTMLを保存していないため原因は特定できない。ブランド行が無い商品か、マークアップ差か不明）。今後に備え、ブランド未抽出時はbylineInfo／po-brandの有無を抽出結果のnotesに残すようにした。自社除外・Amazon小売の保留は該当なし。
- 既存データ: collector_run id<=77のハッシュ一致、保留・旧RUNNING・レポート・storefront_blocked_seller無変更。


## 60. 取得ASINを統合ツールへ渡す接続（入力ファイル・投入計画・投入台帳・実行ID確定）（Phase 4, 2026-10-17）

**範囲**: 統合ツールへのログイン・アクセス・投入は**していない**。実DBへの新規記録もしていない（DBは読み取りのみ）。新パッケージ`tool_submission`（`python -m tool_submission.cli plan / ledger / reconcile`）。実画面を操作するアダプターは未実装（画面構造が未確認のため）。

**既存の統合ツールの規約（調査で確認）**
- ログ画面(`logs.php`)の`<tr data-seq>`が**統合ツールの実行ID**。メモ欄の接頭辞`輸出_`/`輸入_`が業務方向（US/JP）で、残りが`tool_execution_link.seller_id`になる（`tool_intake/log_ingest.py`）。同じメモ接頭辞が出品判定の振り分けにも使われる（`輸入_`→primary_check、`輸出_`→ex_primary_check。ローカルの`import_tools_automation/automation.py`）。投入後の確定済みCSVは`取得完了`の行から取得される。
- ログ画面の`data-country`（jp/us）は**統合ツールがASINを調べる国**で、メモの業務方向とは別の属性。サンプルでは`ASINリスト`+`輸出_`がjpだったが、1例では決められないため**自動では決めない**（計画はDRAFTで、利用者の確認後にREADY）。
- `data-text`にASINの内容は出ない（ASINリストは`asin`のみ）。そのため、ログからは入力内容の一致を確認できない（メモと「投入前に無かった行」が根拠）。

**設計**
- 出典ごと・業務方向ごとに入力ファイルを分ける（混合投入しない）。ファイルは1行1ASIN・ヘッダー無し・UTF-8（`export-run`と同じ形式）。業務方向は**出典の発見経路**(`acquisition_path.direction`)から決め、取得先市場からは決めない。経路に方向が無い出典は`--direction`の明示が必要（経路と矛盾する指定は拒否）。1つの取得runに複数方向の経路があれば方向ごとに別の入力・別のメモ・別の投入キー（それぞれの起点・経路を保存）。
- 入力にできる取得は`COMPLETED`かつ`SUCCESS_NONEMPTY`/`SUCCESS_TRUNCATED`のみ。上限打切りは`input_is_partial=true`＋理由を計画に明記。ASINの重複はファイル内で1回にするが、取得行数・除去数を計画に残す（証拠の保持とは別）。
- 追跡項目（`exports/tool_submission/manifest_<キー>.json`と台帳）: 取得run・取得先市場・出典・業務方向・メモ・起点import/ASIN/注文点数・全経路（経由出典・経由ASIN）・入力ファイルのsha256・ASIN数・部分取得・ツールの国設定・統合ツール実行ID。投入キー＝sha256(run, 方向, 入力のsha256)の先頭16桁（同じ内容の再実行は同じキー→二重投入・二重記録にならない）。
- 台帳は追記専用のJSONL（`ledger.jsonl`）。状態: PLANNED → SUBMITTING（クリックの前に記録）→ UNKNOWN / CONFIRMED(実行ID) / NOT_SUBMITTED / AMBIGUOUS。同じキーの同時実行はロックファイルで拒否。
- **実行IDの確定**: 投入前にログ画面の既存の実行IDを控え、投入後に「メモ・種別asin_list・国設定が一致する、投入前に無かった、他の投入で確定済みでない行」がちょうど1件のときだけ確定。複数ならAMBIGUOUS（二重投入の疑い）。手動で投入した場合は`reconcile --log-html`（保存したログ画面）で同じ判定を行い、続けて既存の`tool_intake.cli ingest-log`が`tool_execution_link`へ記録する。
- **結果不明の扱い（クリック後に待機だけ失敗する場合を含む）**: 例外の種類にかかわらず**再クリックしない**。ログ画面を読み直して（反映遅れを考慮して複数回）照合する。見つからない場合、アダプターが「クリック前の失敗（何も送っていない）」と明示したときだけ、ログを読めたことを条件にNOT_SUBMITTED（再試行は合計2回まで）。それ以外は結果不明(UNKNOWN)のまま再投入せず、後から照合し直す。ログを読めないときは投入しない。クリックの途中でプロセスが死んでも（SUBMITTING）未実行と決めつけない。
- 疑似画面のテスト（`tests/unit/test_tool_submission.py`）: 正常、クリック完了後の遷移待ちタイムアウト（2種）、反映遅れ、ログ読取不可、原因不明の失敗、クリック前の失敗＋再試行1回、上限、二重投入の疑い、DRAFT・基準なし・ロックの拒否、クラッシュ後の再開、他の投入で確定済みの行を採用しない。

**出典13・14（反対側で発見・未取得）**: 取得後に`plan --fetch-market JP|US --run-id <取得run>`で同じ経路（VIA_SOURCE。起点→経由出典→経由ASIN）から業務方向を決めて接続できる（`tests/integration/test_tool_submission_plan.py`で検証）。今回は新しい外部取得をしていない。

**生成した入力（実DBのrun 82・83。DRAFT）**: `exports/tool_submission/`（Git対象外）。run 82 → 出典11 A3AJLR7DMJUOQG・EXPORT・メモ`輸出_A3AJLR7DMJUOQG`・32 ASIN・部分取得（起点 US import=1 B0076AZHC8 注文9点）／ run 83 → 出典12 A1MN7UZC7MPZTE・IMPORT・メモ`輸入_A1MN7UZC7MPZTE`・32 ASIN・部分取得（起点 JP import=2 B0B46VP7NS 注文1点）。

**不足しているもの（スキーマ変更案。保留）**: `collector_run` ↔ 統合ツール実行IDの対応はDBに無く、台帳（ファイル）は暫定。最小案: 表`tool_submission`（submission_key UNIQUE、collector_run_id、source_id、fetch_marketplace、direction、business_marketplace、memo、tool_country、origin_import_id/origin_asin、input_sha256、asin_count、input_is_partial、status、tool_run_id、attempts、各時刻、UNIQUE(business_marketplace, tool_run_id)）＋追記専用の履歴表`tool_submission_event`。RLSは業務方向の市場（tool_execution_linkと同じ）で、app_roleはINSERT・状態列のUPDATEのみ。`tool_execution_link`へ(business_marketplace, tool_run_id)で結ぶ。適用前に実装方針とマイグレーション構成案を提示して確認を得る（CLAUDE.md）。

**未確認（利用者の確認が必要）**: ①統合ツールの投入画面の構造（入力欄・国設定・メモ欄の有無・送信ボタン）—ログ画面ではメモ欄に「保存」ボタンがあり、投入後にメモを付ける運用の可能性があるため、投入時にメモを入力できるか ②ASINリスト投入時の`data-country`（ツールの国設定）の決め方 ③投入の際のその他のオプション（カテゴリ・繰り返し等）④投入の1回あたりのASIN上限。
