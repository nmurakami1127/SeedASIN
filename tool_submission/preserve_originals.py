"""登録直前に押し出される、取得完了の元CSVだけを保存する。"""
from pathlib import Path
import csv
import hashlib
import json
from datetime import datetime, timezone

from tool_intake.csv_utils import open_text_auto
from tool_submission.retention import LOCAL_ORIGINAL_DIRS, find_local_originals, rows_at_risk, _safe_name


def preserve_at_risk(page, entries, archive):
    archive = Path(archive)
    dirs = [*LOCAL_ORIGINAL_DIRS, archive]
    risk = set(rows_at_risk([e.seq for e in entries]))
    for entry in entries:
        if entry.seq not in risk or entry.status != '取得完了' or find_local_originals(entry, dirs):
            continue
        row = page._p.locator(f'tr[data-seq="{entry.seq}"]')
        archive.mkdir(parents=True, exist_ok=True)
        with page._p.expect_download(timeout=60000) as event:
            row.locator('button.download').click()
        download = event.value
        if download.failure():
            raise RuntimeError(f'元CSVの保存に失敗: {entry.seq}')
        name = _safe_name(entry.csv or download.suggested_filename)
        if not name.startswith(f'{entry.seq}_') or not name.lower().endswith('.csv'):
            raise RuntimeError(f'元CSVのファイル名を確認できません: {entry.seq}')
        target = archive / name
        temporary = archive / (name + '.part')
        download.save_as(temporary)
        # HTMLのログイン応答などをCSV保全として扱わない。
        decoded = open_text_auto(temporary)
        rows = csv.reader(decoded.splitlines())
        header = next(rows, [])
        if len(header) < 2 or any('<html' in x.lower() for x in header):
            raise RuntimeError(f'元CSVの内容を確認できません: {entry.seq}')
        temporary.replace(target)
        with (archive / 'preserved.jsonl').open('a', encoding='utf-8') as out:
            out.write(json.dumps({'ts': datetime.now(timezone.utc).isoformat(), 'run_id': entry.seq,
                'path': str(target), 'bytes': target.stat().st_size,
                'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, ensure_ascii=False) + '\n')
        print(f'元CSVを保存: {entry.seq} ({target.stat().st_size:,} bytes)', flush=True)
    page.retention_dirs = dirs
