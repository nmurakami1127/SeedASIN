from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_submission.preserve_originals import preserve_at_risk
from tool_submission.reconcile import LogEntry


def test_completed_original_is_downloaded_and_pending_is_not(tmp_path):
    clicked = []
    class Download:
        suggested_filename = '1_result.csv'
        def failure(self): return None
        def save_as(self, path): Path(path).write_bytes('ASIN,取得日時\nB000000001,2026-10-29\n'.encode('cp932'))
    class BrowserPage:
        def locator(self, selector):
            clicked.append(selector)
            return self
        def click(self): pass
        @contextmanager
        def expect_download(self, **kwargs): yield SimpleNamespace(value=Download())
    page = SimpleNamespace(_p=BrowserPage())
    entries = [LogEntry(i, 'asin_list', 'jp', '', '取得完了', f'{i}_result.csv') for i in range(1, 301)]
    preserve_at_risk(page, entries, tmp_path)
    assert (tmp_path / '1_result.csv').exists()
    assert (tmp_path / 'preserved.jsonl').exists()
    preserve_at_risk(page, entries, tmp_path)
    assert len(clicked) == 2  # 行とDLボタン。保存済みは再ダウンロードしない
    pending = [LogEntry(e.seq, e.data_type, e.country, e.memo, '取得中', e.csv) for e in entries]
    preserve_at_risk(page, pending, tmp_path / 'pending')
    assert not (tmp_path / 'pending').exists()


def test_html_response_is_not_recorded_as_preserved(tmp_path):
    class Download:
        def failure(self): return None
        def save_as(self, path): Path(path).write_text('<html>login</html>', encoding='utf-8')
    class BrowserPage:
        def locator(self, selector): return self
        def click(self): pass
        @contextmanager
        def expect_download(self, **kwargs): yield SimpleNamespace(value=Download())
    entries = [LogEntry(i, 'asin_list', 'jp', '', '取得完了', f'{i}_result.csv') for i in range(1, 301)]
    with pytest.raises(RuntimeError):
        preserve_at_risk(SimpleNamespace(_p=BrowserPage()), entries, tmp_path)
    assert not (tmp_path / '1_result.csv').exists()
