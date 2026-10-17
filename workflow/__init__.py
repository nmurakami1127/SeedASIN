"""既存の各工程（起点選定→発見→取得→反対側展開→統合ツール投入→対応記録）をひとつの入口から動かす実行制御。

新しい探索・対応表は作らない。既存の `acquisition`（出典・経路・lease・取得間隔）、`asin_collector`（取得・発見）、
`tool_submission`（投入台帳・マニフェスト）、`tool_intake`（ingest-log・tool_execution_link）を接続するだけ。
"""
