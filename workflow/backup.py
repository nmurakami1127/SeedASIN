"""workflow状態・投入台帳・マニフェスト（と入力ファイル）をまとめてバックアップ/復元する。

zipに全ファイルのsha256つきの目録（BACKUP_MANIFEST.json）を入れ、復元時に検証する。既存のファイルは上書きしない
（復元先に同名があれば拒否。`force=True`のときは`.pre_restore`へ退避してから置く）。ロックファイルは含めない。
実データ・台帳はGit管理外（exports/）。バックアップ先もGitに入れないこと。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "BACKUP_MANIFEST.json"


class BackupError(Exception):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _files(roots: dict[str, Path]) -> list[tuple[str, Path]]:
    out = []
    for label, root in roots.items():
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix not in (".lock", ".tmp", ".part"):
                out.append((f"{label}/{p.relative_to(root).as_posix()}", p))
    return out


def create_backup(state_dir: str | Path, submission_dir: str | Path, out_zip: str | Path) -> dict:
    roots = {"workflow": Path(state_dir), "tool_submission": Path(submission_dir)}
    files = _files(roots)
    if not files:
        raise BackupError("バックアップ対象のファイルがありません")
    out = Path(out_zip)
    if out.exists():
        raise BackupError(f"既に存在します（上書きしません）: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = {}
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, p in files:
            data = p.read_bytes()
            entries[arc] = {"sha256": _sha(data), "size": len(data)}
            z.writestr(arc, data)
        z.writestr(MANIFEST, json.dumps({"created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": entries},
                                        ensure_ascii=False, indent=1))
    tmp.replace(out)
    return {"zip": str(out), "files": len(entries)}


def verify_backup(zip_path: str | Path) -> dict:
    try:
        return _verify(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"バックアップを読めません（破損）: {exc}") from exc


def _verify(zip_path: str | Path) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        try:
            man = json.loads(z.read(MANIFEST))
        except KeyError as exc:
            raise BackupError("目録(BACKUP_MANIFEST.json)がありません") from exc
        for arc, meta in man["files"].items():
            if _sha(z.read(arc)) != meta["sha256"]:
                raise BackupError(f"ハッシュが一致しません（破損）: {arc}")
    return man


def restore_backup(zip_path: str | Path, state_dir: str | Path, submission_dir: str | Path, *, force: bool = False) -> dict:
    man = verify_backup(zip_path)
    dest = {"workflow": Path(state_dir), "tool_submission": Path(submission_dir)}
    plan = []
    for arc in man["files"]:
        label, rel = arc.split("/", 1)
        plan.append((arc, dest[label] / rel))
    clash = [str(t) for _, t in plan if t.exists()]
    if clash and not force:
        raise BackupError(f"復元先に既存のファイルがあります（上書きしません。--force で退避して復元）: {clash[:3]}…（{len(clash)}件）")
    with zipfile.ZipFile(zip_path) as z:
        for arc, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(target.with_name(target.name + ".pre_restore"))
            target.write_bytes(z.read(arc))
    return {"restored": len(plan), "moved_aside": len(clash)}
