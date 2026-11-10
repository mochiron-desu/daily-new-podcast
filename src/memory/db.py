"""Local SQLite state engine with multi-node JSON state export/merge and run locks."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..harvester.dossier_builder import MatchedArcContext
from ..harvester.rss_fetcher import StoryCluster
from ..utils import ensure_dir, slugify

_STATUS_RANK = {
    "failed": 1,
    "running": 2,
    "generated": 3,
    "published": 4,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoryArcRecord:
    topic_slug: str
    title: str
    first_reported_date: str
    last_updated_date: str
    current_status: str
    cumulative_summary: str
    open_questions: str
    updated_at: str
    source_node: str


@dataclass(frozen=True)
class EpisodeRecord:
    episode_date: str
    title: str
    audio_path: str
    remote_media_url: str
    status: str
    summary_text: str
    topics_covered_json: str
    error_message: str
    error_kind: str
    source_node: str
    updated_at: str


@dataclass(frozen=True)
class RunLockRecord:
    run_date: str
    owner_node_id: str
    owner_priority: int
    acquired_at: str
    expires_at: str


class StateDB:
    """Thread/process-safe SQLite repository for story arcs, episodes, and failover locks."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        ensure_dir(db_path.parent)
        self.init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Create schema tables if they do not exist."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS story_arcs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_slug TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    first_reported_date TEXT NOT NULL,
                    last_updated_date TEXT NOT NULL,
                    current_status TEXT NOT NULL,
                    cumulative_summary TEXT NOT NULL,
                    open_questions TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    source_node TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_date TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    audio_path TEXT NOT NULL DEFAULT '',
                    remote_media_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary_text TEXT NOT NULL DEFAULT '',
                    topics_covered_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT NOT NULL DEFAULT '',
                    error_kind TEXT NOT NULL DEFAULT '',
                    source_node TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS run_locks (
                    run_date TEXT PRIMARY KEY,
                    owner_node_id TEXT NOT NULL,
                    owner_priority INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_arc(row: sqlite3.Row) -> StoryArcRecord:
        return StoryArcRecord(
            topic_slug=row["topic_slug"],
            title=row["title"],
            first_reported_date=row["first_reported_date"],
            last_updated_date=row["last_updated_date"],
            current_status=row["current_status"],
            cumulative_summary=row["cumulative_summary"],
            open_questions=row["open_questions"],
            updated_at=row["updated_at"],
            source_node=row["source_node"],
        )

    @staticmethod
    def _row_to_episode(row: sqlite3.Row) -> EpisodeRecord:
        return EpisodeRecord(
            episode_date=row["episode_date"],
            title=row["title"],
            audio_path=row["audio_path"],
            remote_media_url=row["remote_media_url"],
            status=row["status"],
            summary_text=row["summary_text"],
            topics_covered_json=row["topics_covered_json"],
            error_message=row["error_message"],
            error_kind=row["error_kind"],
            source_node=row["source_node"],
            updated_at=row["updated_at"],
        )

    def get_developing_arcs(self) -> list[StoryArcRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT topic_slug, title, first_reported_date, last_updated_date,
                       current_status, cumulative_summary, open_questions,
                       updated_at, source_node
                FROM story_arcs
                WHERE current_status = 'developing'
                ORDER BY last_updated_date DESC, id DESC
                """
            ).fetchall()
        return [self._row_to_arc(r) for r in rows]

    def get_episode_by_date(self, episode_date: date | str) -> EpisodeRecord | None:
        date_str = episode_date.isoformat() if isinstance(episode_date, date) else episode_date
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT episode_date, title, audio_path, remote_media_url, status,
                       summary_text, topics_covered_json, error_message, error_kind,
                       source_node, updated_at
                FROM episodes
                WHERE episode_date = ?
                """,
                (date_str,),
            ).fetchone()
        return self._row_to_episode(row) if row else None

    def list_recent_episodes(self, limit: int = 10) -> list[EpisodeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT episode_date, title, audio_path, remote_media_url, status,
                       summary_text, topics_covered_json, error_message, error_kind,
                       source_node, updated_at
                FROM episodes
                ORDER BY episode_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def upsert_episode(
        self,
        *,
        episode_date: date | str,
        title: str,
        status: str,
        source_node: str,
        audio_path: str = "",
        remote_media_url: str = "",
        summary_text: str = "",
        topics_covered: Sequence[str] | None = None,
        error_message: str = "",
        error_kind: str = "",
    ) -> EpisodeRecord:
        date_str = episode_date.isoformat() if isinstance(episode_date, date) else episode_date
        now_iso = _utc_now_iso()
        topics_json = json.dumps(list(topics_covered or []))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO episodes (
                    episode_date, title, audio_path, remote_media_url, status,
                    summary_text, topics_covered_json, error_message, error_kind,
                    source_node, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_date) DO UPDATE SET
                    title = excluded.title,
                    audio_path = CASE
                        WHEN excluded.audio_path != '' THEN excluded.audio_path
                        ELSE episodes.audio_path
                    END,
                    remote_media_url = CASE
                        WHEN excluded.remote_media_url != '' THEN excluded.remote_media_url
                        ELSE episodes.remote_media_url
                    END,
                    status = excluded.status,
                    summary_text = CASE
                        WHEN excluded.summary_text != '' THEN excluded.summary_text
                        ELSE episodes.summary_text
                    END,
                    topics_covered_json = CASE
                        WHEN excluded.topics_covered_json != '[]' THEN excluded.topics_covered_json
                        ELSE episodes.topics_covered_json
                    END,
                    error_message = excluded.error_message,
                    error_kind = excluded.error_kind,
                    source_node = excluded.source_node,
                    updated_at = excluded.updated_at
                """,
                (
                    date_str,
                    title,
                    audio_path,
                    remote_media_url,
                    status,
                    summary_text,
                    topics_json,
                    error_message,
                    error_kind,
                    source_node,
                    now_iso,
                ),
            )
        record = self.get_episode_by_date(date_str)
        assert record is not None
        return record

    def update_story_arcs_after_episode(
        self,
        *,
        episode_date: date,
        matched_arcs: Sequence[MatchedArcContext],
        all_clusters: Sequence[StoryCluster],
        source_node: str,
        stale_arc_days: int = 7,
        max_new_arcs: int = 5,
    ) -> None:
        """Advance matched developing arcs, retire stale ones, and seed new top arcs."""
        date_str = episode_date.isoformat()
        now_iso = _utc_now_iso()
        stale_cutoff = (episode_date - timedelta(days=stale_arc_days)).isoformat()
        matched_cluster_ids = {m.matched_cluster.cluster_id for m in matched_arcs}

        with self._connect() as conn:
            for matched in matched_arcs:
                update_note = (
                    f"[{date_str}] {matched.matched_cluster.headline}: "
                    f"{matched.matched_cluster.merged_summary or 'Reported follow-up.'}"
                )
                combined = f"{matched.cumulative_summary}\n{update_note}".strip()
                if len(combined) > 1800:
                    combined = "..." + combined[-1790:]
                conn.execute(
                    """
                    UPDATE story_arcs
                    SET last_updated_date = ?,
                        cumulative_summary = ?,
                        open_questions = ?,
                        updated_at = ?,
                        source_node = ?
                    WHERE topic_slug = ?
                    """,
                    (
                        date_str,
                        combined,
                        f"Follow further developments on {matched.title}.",
                        now_iso,
                        source_node,
                        matched.topic_slug,
                    ),
                )

            conn.execute(
                """
                UPDATE story_arcs
                SET current_status = 'stale',
                    updated_at = ?
                WHERE current_status = 'developing'
                  AND last_updated_date < ?
                """,
                (now_iso, stale_cutoff),
            )

            added = 0
            for cluster in all_clusters:
                if added >= max_new_arcs:
                    break
                if cluster.cluster_id in matched_cluster_ids:
                    continue
                slug = slugify(cluster.headline, max_len=60)
                summary = (
                    f"[{date_str}] {cluster.headline}: "
                    f"{cluster.merged_summary or 'Initial report.'}"
                )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO story_arcs (
                        topic_slug, title, first_reported_date, last_updated_date,
                        current_status, cumulative_summary, open_questions,
                        updated_at, source_node
                    )
                    VALUES (?, ?, ?, ?, 'developing', ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        cluster.headline,
                        date_str,
                        date_str,
                        summary,
                        f"Watch for reactions or next steps following {cluster.headline}.",
                        now_iso,
                        source_node,
                    ),
                )
                if cursor.rowcount > 0:
                    added += 1

    def get_lock(self, run_date: date | str) -> RunLockRecord | None:
        date_str = run_date.isoformat() if isinstance(run_date, date) else run_date
        now_iso = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM run_locks WHERE expires_at <= ?", (now_iso,))
            row = conn.execute(
                """
                SELECT run_date, owner_node_id, owner_priority, acquired_at, expires_at
                FROM run_locks
                WHERE run_date = ?
                """,
                (date_str,),
            ).fetchone()
        if not row:
            return None
        return RunLockRecord(
            run_date=row["run_date"],
            owner_node_id=row["owner_node_id"],
            owner_priority=int(row["owner_priority"]),
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    def acquire_lock(
        self,
        run_date: date | str,
        node_id: str,
        node_priority: int,
        ttl_seconds: int = 2400,
        allow_same_owner: bool = False,
    ) -> tuple[bool, RunLockRecord]:
        """Acquire or renew an active lease for run_date; returns (acquired, current_lock)."""
        date_str = run_date.isoformat() if isinstance(run_date, date) else run_date
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()

        with self._connect() as conn:
            conn.execute("DELETE FROM run_locks WHERE expires_at <= ?", (now_iso,))
            row = conn.execute(
                """
                SELECT run_date, owner_node_id, owner_priority, acquired_at, expires_at
                FROM run_locks
                WHERE run_date = ?
                """,
                (date_str,),
            ).fetchone()
            if row is not None and (not allow_same_owner or row["owner_node_id"] != node_id):
                existing = RunLockRecord(
                    run_date=row["run_date"],
                    owner_node_id=row["owner_node_id"],
                    owner_priority=int(row["owner_priority"]),
                    acquired_at=row["acquired_at"],
                    expires_at=row["expires_at"],
                )
                return False, existing

            conn.execute(
                """
                INSERT INTO run_locks (run_date, owner_node_id, owner_priority, acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_date) DO UPDATE SET
                    owner_node_id = excluded.owner_node_id,
                    owner_priority = excluded.owner_priority,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (date_str, node_id, node_priority, now_iso, expires_iso),
            )
            current = RunLockRecord(
                run_date=date_str,
                owner_node_id=node_id,
                owner_priority=node_priority,
                acquired_at=now_iso,
                expires_at=expires_iso,
            )
            return True, current

    def release_lock(self, run_date: date | str, node_id: str) -> bool:
        date_str = run_date.isoformat() if isinstance(run_date, date) else run_date
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM run_locks WHERE run_date = ? AND owner_node_id = ?",
                (date_str, node_id),
            )
            return cur.rowcount > 0

    def export_state(self) -> dict[str, Any]:
        """Serialize story_arcs and episodes for peer/primary synchronization."""
        with self._connect() as conn:
            arcs = [
                asdict(self._row_to_arc(r))
                for r in conn.execute(
                    """
                    SELECT topic_slug, title, first_reported_date, last_updated_date,
                           current_status, cumulative_summary, open_questions,
                           updated_at, source_node
                    FROM story_arcs
                    """
                ).fetchall()
            ]
            episodes = [
                asdict(self._row_to_episode(r))
                for r in conn.execute(
                    """
                    SELECT episode_date, title, audio_path, remote_media_url, status,
                           summary_text, topics_covered_json, error_message, error_kind,
                           source_node, updated_at
                    FROM episodes
                    """
                ).fetchall()
            ]
        return {
            "exported_at": _utc_now_iso(),
            "story_arcs": arcs,
            "episodes": episodes,
        }

    def merge_state(self, payload: dict[str, Any]) -> dict[str, int]:
        """Idempotently merge exported state from Railway or a sibling local peer."""
        arcs_merged = 0
        episodes_merged = 0
        incoming_arcs = payload.get("story_arcs") or []
        incoming_episodes = payload.get("episodes") or []

        with self._connect() as conn:
            for item in incoming_arcs:
                slug = item.get("topic_slug")
                if not slug:
                    continue
                local = conn.execute(
                    "SELECT updated_at FROM story_arcs WHERE topic_slug = ?",
                    (slug,),
                ).fetchone()
                inc_updated = str(item.get("updated_at") or "")
                if local is None:
                    conn.execute(
                        """
                        INSERT INTO story_arcs (
                            topic_slug, title, first_reported_date, last_updated_date,
                            current_status, cumulative_summary, open_questions,
                            updated_at, source_node
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slug,
                            item.get("title", slug),
                            item.get("first_reported_date", ""),
                            item.get("last_updated_date", ""),
                            item.get("current_status", "developing"),
                            item.get("cumulative_summary", ""),
                            item.get("open_questions", ""),
                            inc_updated,
                            item.get("source_node", ""),
                        ),
                    )
                    arcs_merged += 1
                elif inc_updated > str(local["updated_at"] or ""):
                    conn.execute(
                        """
                        UPDATE story_arcs
                        SET title = ?,
                            first_reported_date = ?,
                            last_updated_date = ?,
                            current_status = ?,
                            cumulative_summary = ?,
                            open_questions = ?,
                            updated_at = ?,
                            source_node = ?
                        WHERE topic_slug = ?
                        """,
                        (
                            item.get("title", slug),
                            item.get("first_reported_date", ""),
                            item.get("last_updated_date", ""),
                            item.get("current_status", "developing"),
                            item.get("cumulative_summary", ""),
                            item.get("open_questions", ""),
                            inc_updated,
                            item.get("source_node", ""),
                            slug,
                        ),
                    )
                    arcs_merged += 1

            for item in incoming_episodes:
                ep_date = item.get("episode_date")
                if not ep_date:
                    continue
                local = conn.execute(
                    "SELECT status, updated_at FROM episodes WHERE episode_date = ?",
                    (ep_date,),
                ).fetchone()
                inc_status = str(item.get("status") or "")
                inc_updated = str(item.get("updated_at") or "")

                if local is None:
                    conn.execute(
                        """
                        INSERT INTO episodes (
                            episode_date, title, audio_path, remote_media_url, status,
                            summary_text, topics_covered_json, error_message, error_kind,
                            source_node, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ep_date,
                            item.get("title", ""),
                            item.get("audio_path", ""),
                            item.get("remote_media_url", ""),
                            inc_status,
                            item.get("summary_text", ""),
                            item.get("topics_covered_json", "[]"),
                            item.get("error_message", ""),
                            item.get("error_kind", ""),
                            item.get("source_node", ""),
                            inc_updated,
                        ),
                    )
                    episodes_merged += 1
                    continue

                local_rank = _STATUS_RANK.get(str(local["status"]), 0)
                inc_rank = _STATUS_RANK.get(inc_status, 0)
                should_update = inc_rank > local_rank or (
                    inc_rank == local_rank and inc_updated > str(local["updated_at"] or "")
                )
                if should_update:
                    conn.execute(
                        """
                        UPDATE episodes
                        SET title = ?,
                            audio_path = CASE WHEN ? != '' THEN ? ELSE audio_path END,
                            remote_media_url = CASE WHEN ? != '' THEN ? ELSE remote_media_url END,
                            status = ?,
                            summary_text = CASE WHEN ? != '' THEN ? ELSE summary_text END,
                            topics_covered_json = CASE WHEN ? != '[]' THEN ? ELSE topics_covered_json END,
                            error_message = ?,
                            error_kind = ?,
                            source_node = ?,
                            updated_at = ?
                        WHERE episode_date = ?
                        """,
                        (
                            item.get("title", ""),
                            item.get("audio_path", ""),
                            item.get("audio_path", ""),
                            item.get("remote_media_url", ""),
                            item.get("remote_media_url", ""),
                            inc_status,
                            item.get("summary_text", ""),
                            item.get("summary_text", ""),
                            item.get("topics_covered_json", "[]"),
                            item.get("topics_covered_json", "[]"),
                            item.get("error_message", ""),
                            item.get("error_kind", ""),
                            item.get("source_node", ""),
                            inc_updated,
                            ep_date,
                        ),
                    )
                    episodes_merged += 1

        return {"story_arcs": arcs_merged, "episodes": episodes_merged}

