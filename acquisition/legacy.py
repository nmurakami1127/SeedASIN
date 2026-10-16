"""旧履歴の適用・分類・確定・却下・再開（人の判断の記録）。

- 判断は acquisition_legacy_evidence に**行として追記**する（元の観測を上書きしない）。
  状態は最新の判断行を畳み込んだ結果（acquisition.legacy_state）。理由・実行者・日時が全て残る。
- 取得先市場が未確定の観測は**US・JP両方の区画に複製**する（各区画のclaimが自分の区画だけを読んで
  REVIEW_LEGACYを判定できる。RLSを迂回せず、参照専用ロールも不要）。

【確定(confirm)の競合防止】USへの確定とJPへの確定が同時に走っても両方がCONFIRMED_THISに
ならないよう、証拠グループ単位の**セッション・アドバイザリロック**（DB全体で共有される）を保持した
まま、次の手順で区画ごとの短いトランザクションを実行する:
  1. 両区画の現在状態を読む（ロック保持中なので他の確定は走らない）。
  2. 既に別の市場で確定済みなら拒否。同じ市場への確定が途中まで進んでいれば不足分だけ補う（再実行可）。
  3. 確定先の区画に CONFIRM(target=T) を追記 → 他方の区画にも CONFIRM(target=T) を追記
     （他方から見ると CONFIRMED_OTHER）。
  途中で失敗しても、他方の区画は UNKNOWN のまま残る＝**保留が外れる方向には誤らない**。
  同じコマンドを再実行すれば補完される。
1トランザクションにしない理由: 区画の切替は接続ごとのセッション設定(app.current_marketplace)で
行う設計で、1トランザクション内で区画を切り替えるとRLSの前提（1接続=1市場）が崩れ、実装ミスが
市場をまたぐ書き込みになり得るため。

各判断は、その証拠が(分類済みなら)対応する出典の識別ロックにも参加する（claimと直列化）。
"""
from __future__ import annotations

import csv
import getpass
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import Connection, text

from acquisition.legacy_scan import Observation
from acquisition.legacy_state import LegacyGroupState, fold_legacy
from acquisition.locks import group_lock_key, identity_lock
from acquisition.normalize import key_hash, normalize_key
from db.session import SessionFactory

MARKETS = ("US", "JP")


class LegacyError(Exception):
    pass


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def read_selected_groups(csv_path: Path) -> set[str]:
    """dry-run CSVのapply列が Y/y/yes の行のグループIDを返す（利用者が選択する）。"""
    selected: set[str] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("apply") or "").strip().lower() in ("y", "yes", "1", "true"):
                selected.add(r["group"].strip())
    return selected


class LegacyService:
    def __init__(self, app_factory: SessionFactory, actor: str | None = None,
                 hook: Callable[[str, str], None] | None = None) -> None:
        self._app = app_factory
        self._actor = actor or _actor()
        self._hook = hook or (lambda point, group: None)  # テスト用: 途中失敗の注入点

    # ------------------------------------------------------------------ 基盤

    @contextmanager
    def _group_lock(self, group: str):
        """証拠グループのセッション・アドバイザリロック。同じ物理接続で取得し、保持したまま区画ごとの
        短いトランザクションを実行する。

        解放の保証: 本文が例外で抜けても、解放は必ず**取得した同じ接続**で行う。解放を確認できない
        （トランザクション中断・切断等）場合は、その接続をプールへ戻さず破棄(invalidate)する——
        セッション・アドバイザリロックはプール返却時のrollbackでは解放されないため、そのまま返すと
        ロックを持ったまま別の処理に再利用されてしまう。接続が切れていればサーバー側でロックは解放済み。
        ロック取得の途中（割り込み含む）で例外になった場合も、接続を破棄する。"""
        key = group_lock_key(group)
        with self._app.get_session("US") as lock_conn:
            try:
                lock_conn.execute(text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
                lock_conn.commit()
            except BaseException:
                self._discard(lock_conn)  # 取得できたか不明なので、この接続は再利用しない
                raise
            try:
                yield
            except BaseException:
                self._release_or_discard(lock_conn, key)
                raise
            else:
                self._release_or_discard(lock_conn, key)

    @staticmethod
    def _discard(lock_conn: Connection) -> None:
        try:
            lock_conn.invalidate()
        except Exception:
            pass

    @classmethod
    def _release_or_discard(cls, lock_conn: Connection, key: str) -> None:
        """取得した同じ接続でロックを解放する。解放を確認できなければ接続を破棄する。"""
        released = False
        try:
            lock_conn.rollback()  # 中断したトランザクションが残っていても解放できる状態に戻す
            released = bool(lock_conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}).scalar())
            lock_conn.commit()
        except BaseException:
            released = False
        if not released:
            cls._discard(lock_conn)

    def _states(self, partition: str, group: str | None = None) -> list[LegacyGroupState]:
        with self._app.get_session(partition) as conn, conn.begin():
            where = " WHERE evidence_group = CAST(:g AS uuid)" if group else ""
            rows = conn.execute(
                text(f"SELECT * FROM acquisition_legacy_evidence{where} ORDER BY evidence_id"),
                {"g": group} if group else {},
            ).all()
        return fold_legacy(rows, partition)

    def group_states(self, group: str) -> dict[str, LegacyGroupState]:
        out = {}
        for p in MARKETS:
            for s in self._states(p, group):
                out[p] = s
        return out

    def _lock_identity_for(self, conn: Connection, partition: str, state: LegacyGroupState | None,
                           extra: tuple[str, str] | None = None) -> None:
        keys = set()
        if state is not None and state.classified:
            keys.add((state.source_type, state.key_hash))
        if extra:
            keys.add(extra)
        for t, h in sorted(keys):  # ソートしてデッドロックを避ける
            identity_lock(conn, partition, t, h.strip())

    def _append(self, partition: str, group: str, decision_type: str, reason: str, *,
                target: str | None = None, classify: tuple[str, str, str, str] | None = None) -> bool:
        """区画に判断行を追記する（冪等: 現在状態が既に同じなら追記しない）。戻り値=追記したか。"""
        with self._app.get_session(partition) as conn, conn.begin():
            rows = conn.execute(
                text("SELECT * FROM acquisition_legacy_evidence WHERE evidence_group = CAST(:g AS uuid) ORDER BY evidence_id"),
                {"g": group},
            ).all()
            states = fold_legacy(rows, partition)
            if not states:
                raise LegacyError(f"証拠グループ {group} は区画{partition}に存在しません")
            state = states[0]
            extra = (classify[0], classify[2]) if classify else None
            self._lock_identity_for(conn, partition, state, extra)
            # ロック取得後に状態を再読込
            rows = conn.execute(
                text("SELECT * FROM acquisition_legacy_evidence WHERE evidence_group = CAST(:g AS uuid) ORDER BY evidence_id"),
                {"g": group},
            ).all()
            state = fold_legacy(rows, partition)[0]
            if decision_type == "CLASSIFY" and classify and (state.source_type, state.key_primary, state.key_qualifier) == (
                classify[0], classify[1], classify[3]
            ):
                return False
            if decision_type == "CONFIRM":
                last_confirm = [d for d in state.decisions if d["decision_type"] == "CONFIRM"]
                if state.market_state in ("CONFIRMED_THIS", "CONFIRMED_OTHER") and last_confirm:
                    if last_confirm[-1]["target_marketplace"] == target:
                        return False  # 既に同じ市場で確定済み（冪等）
                    # 群ロックを迂回した呼び出しでも、別市場への確定を成功させない（二重防御）
                    raise LegacyError(
                        f"この区画では既に{last_confirm[-1]['target_marketplace']}で確定済みです。reopenしてから確定してください"
                    )
            if decision_type == "DISMISS" and state.market_state == "DISMISSED":
                return False
            if decision_type == "REOPEN" and state.market_state == "UNKNOWN":
                return False
            params = {"g": group, "mp": partition, "dt": decision_type, "reason": reason, "actor": self._actor,
                      "target": target, "st": None, "kp": None, "kq": None, "kh": None}
            if classify:
                params.update(st=classify[0], kp=classify[1], kh=classify[2], kq=classify[3])
            conn.execute(
                text(
                    "INSERT INTO acquisition_legacy_evidence (evidence_group, marketplace, row_type, decision_type, "
                    "target_marketplace, source_type, key_primary, key_qualifier, key_hash, reason, actor) "
                    "VALUES (CAST(:g AS uuid), :mp, 'DECISION', :dt, :target, :st, :kp, :kq, :kh, :reason, :actor)"
                ),
                params,
            )
        return True

    # ------------------------------------------------------------------ 適用

    def apply(
        self,
        observations: Iterable[Observation],
        selected_groups: set[str],
        *,
        classify: str = "none",  # none / fact / all（hintのgradeで許可する範囲）
        confirm_recorded_market: bool = False,
        reason: str,
        batch_id: str | None = None,
    ) -> dict[str, int]:
        """利用者が選択したグループだけを適用する。
        - 取得先市場が未確定: 両区画に複製（UNKNOWN）。
        - confirm_recorded_marketを明示し、かつ記録(URLドメイン)で市場が事実のもの: その区画だけに置き、
          CONFIRM(target=市場)を追記する（取得成功の断定ではなく、旧証拠として間隔に数える）。
        - 取得成功が未確認(UNCONFIRMED)のものは、選択されていても適用しない。"""
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        if classify not in ("none", "fact", "all"):
            raise ValueError("classify must be none/fact/all")
        stats = {"selected": 0, "skipped_unconfirmed": 0, "observations": 0, "classified": 0, "confirmed": 0}
        for o in observations:
            if o.group not in selected_groups:
                continue
            stats["selected"] += 1
            if o.success_grade == "UNCONFIRMED":
                stats["skipped_unconfirmed"] += 1
                continue
            confirm_here = confirm_recorded_market and o.market_grade == "FACT" and o.market_hint in MARKETS
            partitions = [o.market_hint] if confirm_here else list(MARKETS)
            for p in partitions:
                with self._app.get_session(p) as conn, conn.begin():
                    n = conn.execute(
                        text(
                            "INSERT INTO acquisition_legacy_evidence (evidence_group, marketplace, row_type, evidence_kind, "
                            "evidence_ref, data_marketplace, evidence_at, evidence_at_basis, asin_count, raw_value, "
                            "success_grade, market_grade, type_grade, market_hint, market_hint_basis, type_hint, "
                            "type_hint_basis, actor, batch_id) VALUES (CAST(:g AS uuid), :mp, 'OBSERVATION', :kind, :ref, "
                            ":dm, :at, :atb, :n, :raw, :sg, :mg, :tg, :mh, :mhb, :th, :thb, :actor, :batch) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        {"g": o.group, "mp": p, "kind": o.kind, "ref": o.ref, "dm": o.data_marketplace, "at": o.evidence_at,
                         "atb": o.evidence_at_basis, "n": o.asin_count, "raw": o.raw_value, "sg": o.success_grade,
                         "mg": o.market_grade, "tg": o.type_grade, "mh": o.market_hint, "mhb": o.market_hint_basis,
                         "th": o.type_hint, "thb": o.type_hint_basis, "actor": self._actor, "batch": batch_id},
                    ).rowcount
                    stats["observations"] += n
            allowed = classify == "all" or (classify == "fact" and o.type_grade == "FACT")
            key = o.proposed_key()
            if (allowed and key is not None) or confirm_here:
                with self._group_lock(o.group):  # 人の判断(classify/confirm等)と同じ群ロックで直列化
                    if allowed and key is not None:
                        for p in partitions:
                            if self._append(p, o.group, "CLASSIFY", f"apply選択({classify}): {o.type_hint_basis} / {reason.strip()}",
                                            classify=(o.type_hint, key[0], key_hash(o.type_hint, key[0], key[1]), key[1])):
                                stats["classified"] += 1
                    if confirm_here:
                        if self._append(o.market_hint, o.group, "CONFIRM",
                                        f"apply選択: 取得先市場=記録された{o.market_hint_basis}（旧証拠として間隔に数える。"
                                        f"取得成功の断定ではない） / {reason.strip()}", target=o.market_hint):
                            stats["confirmed"] += 1
        return stats

    # ------------------------------------------------------------------ 人の判断

    def classify(self, group: str, source_type: str, primary: str, qualifier: str, reason: str) -> list[str]:
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        kp, kq = normalize_key(source_type, primary, qualifier)
        kh = key_hash(source_type, kp, kq)
        done: list[str] = []
        with self._group_lock(group):
            states = self.group_states(group)
            if not states:
                raise LegacyError(f"証拠グループ {group} が見つかりません")
            for p in MARKETS:
                if p in states and self._append(p, group, "CLASSIFY", reason.strip(), classify=(source_type, kp, kh, kq)):
                    done.append(p)
        return done

    def confirm(self, group: str, target: str, reason: str) -> list[str]:
        """取得先市場を確定する。区画をまたぐ競合防止と途中失敗の再実行は、モジュール冒頭の説明を参照。"""
        if target not in MARKETS:
            raise ValueError("target must be US or JP")
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        other = "JP" if target == "US" else "US"
        done: list[str] = []
        with self._group_lock(group):
            states = self.group_states(group)
            if not states:
                raise LegacyError(f"証拠グループ {group} が見つかりません")
            targets = set()
            for p, s in states.items():
                if s.market_state == "CONFIRMED_THIS":
                    targets.add(p)
                elif s.market_state == "CONFIRMED_OTHER":
                    last = [d for d in s.decisions if d["decision_type"] == "CONFIRM"][-1]
                    targets.add(last["target_marketplace"])
            if any(s.market_state == "DISMISSED" for s in states.values()):
                raise LegacyError("却下済みの証拠です。reopen してから確定してください")
            if targets and targets != {target}:
                raise LegacyError(f"既に{sorted(targets)}で確定済みです。変更するには reopen してください（履歴は残ります）")
            if target not in states:
                raise LegacyError(f"この証拠は区画{target}に存在しません（取得先市場の確定済みデータです）")
            if self._append(target, group, "CONFIRM", reason.strip(), target=target):
                done.append(target)
            self._hook("after_target_confirm", group)
            if other in states and self._append(other, group, "CONFIRM", reason.strip(), target=target):
                done.append(other)
        return done

    def dismiss(self, group: str, reason: str) -> list[str]:
        return self._both(group, "DISMISS", reason)

    def reopen(self, group: str, reason: str) -> list[str]:
        return self._both(group, "REOPEN", reason)

    def _both(self, group: str, decision_type: str, reason: str) -> list[str]:
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        done: list[str] = []
        with self._group_lock(group):
            states = self.group_states(group)
            if not states:
                raise LegacyError(f"証拠グループ {group} が見つかりません")
            # 却下は保留が外れる方向なので、確定済みの市場側を最後にする（途中失敗でも保留側が残る）
            order = [p for p in MARKETS if p in states]
            for p in order:
                if self._append(p, group, decision_type, reason.strip()):
                    done.append(p)
                self._hook(f"after_{decision_type.lower()}_{p}", group)
        return done

    # ------------------------------------------------------------------ 一覧

    def list_groups(self) -> list[dict]:
        merged: dict[str, dict] = {}
        for p in MARKETS:
            for s in self._states(p):
                m = merged.setdefault(s.evidence_group, {"group": s.evidence_group, "state": s, "partitions": {}})
                m["partitions"][p] = s.market_state
        return list(merged.values())
