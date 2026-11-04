"""成果表示（読み取り専用）をPCから環境変数なしで実行する入口。

    python -m scripts.outcome_readonly --market ALL --since 2026-11-01
    python -m scripts.outcome_readonly --market US --plan-compare --config config/workflow.json

`python -m workflow.cli outcome ...` と同じ処理（DBはREAD ONLYトランザクション、書き込みなし）。接続は workflow/dbconn.py が解決する
（SEEDASIN_APP_DATABASE_URL があればそれ、無ければローカル開発DBのapp_role）。
"""
from __future__ import annotations

import sys

from workflow.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--plan-compare" in argv:
        argv = ["plan-compare"] + [x for x in argv if x != "--plan-compare"]
    else:
        argv = ["outcome"] + argv
    sys.exit(main(argv))
