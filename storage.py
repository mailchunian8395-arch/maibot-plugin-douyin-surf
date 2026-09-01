from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .quality_gate import is_official_url


_DOUYIN_LIKE_COUNT_PATTERN = re.compile(
    r"点赞\s*[：:]\s*(\d+(?:\.\d+)?)\s*([万wWkK]?)",
    re.IGNORECASE,
)


def _douyin_like_count(candidate: dict[str, Any]) -> int | None:
    """读取候选抓取时保存的抖音点赞量，未知时不得视为满足自动分享热度线。"""

    for key in ("snippet", "full_text"):
        match = _DOUYIN_LIKE_COUNT_PATTERN.search(str(candidate.get(key) or ""))
        if match is None:
            continue
        count = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "万":
            count *= 10_000
        elif unit == "w":
            count *= 10_000
        elif unit == "k":
            count *= 1_000
        return max(0, int(count))
    return None


def normalize_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"spm_id_from", "from", "share_source"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def fingerprint_item(title: str, url: str) -> str:
    stable = normalize_url(url) or re.sub(r"\W+", "", str(title or "").lower())
    return hashlib.sha256(stable.encode("utf-8", errors="ignore")).hexdigest()


class LifeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    snippet TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    found_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    topic TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    interesting_point TEXT NOT NULL DEFAULT '',
                    stance TEXT NOT NULL DEFAULT '',
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0,
                    share_score REAL NOT NULL DEFAULT 0,
                    risk_label TEXT NOT NULL DEFAULT '',
                    share_intent TEXT NOT NULL DEFAULT '',
                    shared_at REAL,
                    shared_stream_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_discoveries_share
                    ON discoveries(status, share_score DESC, found_at DESC);

                -- 候选正文只存一份；每个聊天流独立保存待分享、排队、拒绝和已分享状态。
                -- 这样同一个视频可同时进入多个群，又不会因一个群已发送而从其他群消失。
                CREATE TABLE IF NOT EXISTS discovery_stream_candidates (
                    discovery_id INTEGER NOT NULL,
                    stream_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    assigned_at REAL NOT NULL,
                    queued_at REAL,
                    shared_at REAL,
                    share_defer_until REAL NOT NULL DEFAULT 0,
                    share_decline_count INTEGER NOT NULL DEFAULT 0,
                    quality_reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(discovery_id, stream_id),
                    FOREIGN KEY(discovery_id) REFERENCES discoveries(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_discovery_stream_candidates_ready
                    ON discovery_stream_candidates(stream_id, status, assigned_at DESC);
                CREATE INDEX IF NOT EXISTS idx_discovery_stream_candidates_shared
                    ON discovery_stream_candidates(stream_id, shared_at DESC);

                CREATE TABLE IF NOT EXISTS reactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discovery_id INTEGER NOT NULL,
                    stream_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(discovery_id) REFERENCES discoveries(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            discovery_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(discoveries)").fetchall()
            }
            migrations = {
                "full_text": "TEXT NOT NULL DEFAULT ''",
                "observed_title": "TEXT NOT NULL DEFAULT ''",
                "observed_at": "REAL",
                "observation_attempts": "INTEGER NOT NULL DEFAULT 0",
                "knowledge_score": "REAL NOT NULL DEFAULT 0",
                "knowledge_facts_json": "TEXT NOT NULL DEFAULT '[]'",
                "official_today": "INTEGER NOT NULL DEFAULT 0",
                "heat_score": "REAL NOT NULL DEFAULT 0",
                "memorized_at": "REAL",
                "screenshot_base64": "TEXT NOT NULL DEFAULT ''",
                "screenshot_kind": "TEXT NOT NULL DEFAULT ''",
                "screenshot_reason": "TEXT NOT NULL DEFAULT ''",
                "screenshot_shared_at": "REAL",
                "media_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                "media_forwarded_at": "REAL",
                "video_shared_at": "REAL",
                "queued_stream_id": "TEXT NOT NULL DEFAULT ''",
                "queued_at": "REAL",
                "share_defer_until": "REAL NOT NULL DEFAULT 0",
                "share_defer_stream_id": "TEXT NOT NULL DEFAULT ''",
                "share_decline_count": "INTEGER NOT NULL DEFAULT 0",
                "share_eligible": "INTEGER NOT NULL DEFAULT 1",
                "knowledge_eligible": "INTEGER NOT NULL DEFAULT 1",
                "quality_reason": "TEXT NOT NULL DEFAULT ''",
                "subject_gender": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in discovery_columns:
                    connection.execute(f"ALTER TABLE discoveries ADD COLUMN {column} {definition}")
            screenshot_migration = connection.execute(
                "SELECT value FROM state WHERE key='comment_only_screenshot_migration_v1'"
            ).fetchone()
            if screenshot_migration is None:
                connection.execute(
                    """
                    UPDATE discoveries SET
                        screenshot_base64='', screenshot_kind='', screenshot_reason='', screenshot_shared_at=NULL
                    WHERE screenshot_base64<>'' OR screenshot_kind<>''
                    """
                )
                connection.execute(
                    "INSERT INTO state(key, value) VALUES('comment_only_screenshot_migration_v1', 'done')"
                )

    def add_candidates(self, items: Iterable[dict[str, Any]]) -> list[int]:
        inserted: list[int] = []
        now = time.time()
        with self._connect() as connection:
            for item in items:
                title = str(item.get("title") or "").strip()
                url = normalize_url(str(item.get("url") or item.get("href") or ""))
                if not title or not url:
                    continue
                fingerprint = fingerprint_item(title, url)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO discoveries(
                        fingerprint, source, title, url, snippet, published_at, found_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        str(item.get("source") or "网络").strip(),
                        title[:500],
                        url[:2000],
                        str(item.get("body") or item.get("snippet") or "").strip()[:3000],
                        str(item.get("date") or item.get("published_at") or "").strip()[:100],
                        now,
                    ),
                )
                if cursor.rowcount:
                    inserted.append(int(cursor.lastrowid))
        return inserted

    def get_discoveries(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        normalized = [int(item) for item in ids]
        if not normalized:
            return []
        marks = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM discoveries WHERE id IN ({marks}) ORDER BY found_at DESC", normalized
            ).fetchall()
        return [dict(row) for row in rows]

    def get_discoveries_by_urls(self, urls: Iterable[str]) -> list[dict[str, Any]]:
        """按规范化 URL 查询已收录候选，用于手动搜索复用未分享的作品。"""

        fingerprints = list(
            {
                fingerprint_item("", normalized_url)
                for url in urls
                if (normalized_url := normalize_url(str(url or "")))
            }
        )
        if not fingerprints:
            return []
        marks = ",".join("?" for _ in fingerprints)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM discoveries WHERE fingerprint IN ({marks})",
                fingerprints,
            ).fetchall()
        return [dict(row) for row in rows]

    def known_candidate_urls(self, source: str = "") -> set[str]:
        """返回已处理过的候选链接，供同一站内搜索继续向后翻页。"""

        query = "SELECT url FROM discoveries WHERE url<>''"
        params: tuple[str, ...] = ()
        if source:
            query += " AND source=?"
            params = (source,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return {normalize_url(str(row["url"] or "")) for row in rows if str(row["url"] or "")}

    def pending_curation(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM discoveries WHERE status='new' ORDER BY found_at ASC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_stream_candidate(self, discovery_id: int, stream_id: str) -> None:
        """将已通过筛选的候选加入一个聊天流的独立候选池。"""

        normalized_stream_id = str(stream_id or "").strip()
        if not normalized_stream_id:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO discovery_stream_candidates(discovery_id, stream_id, assigned_at)
                VALUES (?, ?, ?)
                """,
                (int(discovery_id), normalized_stream_id, time.time()),
            )

    def active_candidate_count(self, stream_id: str = "") -> int:
        """返回全局或指定聊天流尚未分享、可继续处理的候选数量。"""

        with self._connect() as connection:
            if stream_id:
                row = connection.execute(
                    """
                    SELECT COUNT(*) FROM discovery_stream_candidates AS candidate
                    INNER JOIN discoveries AS discovery ON discovery.id=candidate.discovery_id
                    WHERE candidate.stream_id=? AND candidate.status IN ('ready', 'queued')
                        AND discovery.status='ready' AND discovery.share_eligible=1
                    """,
                    (str(stream_id),),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) FROM discoveries
                    WHERE shared_at IS NULL AND status IN ('new', 'ready', 'queued')
                    """
                ).fetchone()
        return int(row[0] if row is not None else 0)

    def curate_discovery(self, discovery_id: int, result: dict[str, Any]) -> None:
        keep = bool(result.get("keep", False))
        status = "ready" if keep else "dismissed"
        reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
        knowledge_facts = (
            result.get("knowledge_facts") if isinstance(result.get("knowledge_facts"), list) else []
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discoveries SET
                    status=?, topic=?, summary=?, interesting_point=?, stance=?, reasons_json=?,
                    confidence=?, share_score=?, risk_label=?, share_intent=?, knowledge_score=?,
                    knowledge_facts_json=?, official_today=?, heat_score=?
                WHERE id=?
                """,
                (
                    status,
                    str(result.get("topic") or "").strip()[:300],
                    str(result.get("summary") or "").strip()[:3000],
                    str(result.get("interesting_point") or "").strip()[:2000],
                    str(result.get("stance") or "").strip()[:2000],
                    json.dumps([str(item) for item in reasons[:6]], ensure_ascii=False),
                    max(0.0, min(1.0, float(result.get("confidence") or 0))),
                    max(0.0, min(1.0, float(result.get("share_score") or 0))),
                    str(result.get("risk_label") or "").strip()[:100],
                    str(result.get("share_intent") or "").strip()[:3000],
                    max(0.0, min(1.0, float(result.get("knowledge_score") or 0))),
                    json.dumps([str(item) for item in knowledge_facts[:20] if str(item).strip()], ensure_ascii=False),
                    1 if bool(result.get("official_today")) else 0,
                    max(0.0, min(1.0, float(result.get("heat_score") or 0))),
                    int(discovery_id),
                ),
            )

    def pending_observation(self, limit: int = 1) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM discoveries
                WHERE status='ready' AND observed_at IS NULL AND observation_attempts<3
                ORDER BY MAX(share_score, knowledge_score) DESC, found_at DESC LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_observation_count(self) -> int:
        """返回仍需打开详情页核验的候选数，供补货任务防止积压。"""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM discoveries
                WHERE status='ready' AND observed_at IS NULL AND observation_attempts<3
                """
            ).fetchone()
        return int(row[0] if row is not None else 0)

    def update_deep_observation(self, discovery_id: int, result: dict[str, Any]) -> None:
        facts = result.get("knowledge_facts") if isinstance(result.get("knowledge_facts"), list) else []
        reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
        media_urls = result.get("media_urls") if isinstance(result.get("media_urls"), list) else []
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discoveries SET
                    observed_title=?, full_text=?, observed_at=?, summary=?, interesting_point=?, stance=?,
                    reasons_json=?, confidence=?, share_score=?, risk_label=?, share_intent=?,
                    knowledge_score=?, knowledge_facts_json=?, official_today=?, heat_score=?,
                    screenshot_base64=?, screenshot_kind=?, screenshot_reason=?,
                    media_urls_json=?,
                    share_eligible=?, knowledge_eligible=?, quality_reason=?, subject_gender=?
                WHERE id=?
                """,
                (
                    str(result.get("observed_title") or "").strip()[:500],
                    str(result.get("full_text") or "").strip()[:50000],
                    time.time(),
                    str(result.get("summary") or "").strip()[:3000],
                    str(result.get("interesting_point") or "").strip()[:2000],
                    str(result.get("stance") or "").strip()[:2000],
                    json.dumps([str(item) for item in reasons[:8] if str(item).strip()], ensure_ascii=False),
                    max(0.0, min(1.0, float(result.get("confidence") or 0))),
                    max(0.0, min(1.0, float(result.get("share_score") or 0))),
                    str(result.get("risk_label") or "").strip()[:100],
                    str(result.get("share_intent") or "").strip()[:3000],
                    max(0.0, min(1.0, float(result.get("knowledge_score") or 0))),
                    json.dumps([str(item) for item in facts[:30] if str(item).strip()], ensure_ascii=False),
                    1 if bool(result.get("official_today")) else 0,
                    max(0.0, min(1.0, float(result.get("heat_score") or 0))),
                    str(result.get("screenshot_base64") or "").strip(),
                    str(result.get("screenshot_kind") or "").strip()[:30],
                    str(result.get("screenshot_reason") or "").strip()[:1000],
                    json.dumps([str(item) for item in media_urls[:20] if str(item).strip()], ensure_ascii=False),
                    1 if bool(result.get("share_eligible", True)) else 0,
                    1 if bool(result.get("knowledge_eligible", True)) else 0,
                    str(result.get("quality_reason") or "").strip()[:500],
                    str(result.get("subject_gender") or "").strip().lower()[:16],
                    int(discovery_id),
                ),
            )

    def mark_media_forwarded(self, discovery_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE discoveries SET media_forwarded_at=? WHERE id=?",
                (time.time(), int(discovery_id)),
            )

    def mark_video_shared(self, discovery_id: int) -> None:
        """标记原生视频发送成功，并清除同一候选的重复预览媒体。"""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discoveries
                SET video_shared_at=?, screenshot_base64='', screenshot_kind='', screenshot_reason='',
                    media_urls_json='[]'
                WHERE id=?
                """,
                (time.time(), int(discovery_id)),
            )

    def mark_observation_failed(self, discovery_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discoveries SET
                    observation_attempts=observation_attempts+1,
                    status=CASE WHEN observation_attempts+1>=3 THEN 'dismissed' ELSE status END
                WHERE id=?
                """,
                (int(discovery_id),),
            )

    def mark_memorized(self, discovery_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE discoveries SET memorized_at=? WHERE id=?",
                (time.time(), int(discovery_id)),
            )

    def mark_screenshot_shared(self, discovery_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE discoveries SET screenshot_shared_at=? WHERE id=?",
                (time.time(), int(discovery_id)),
            )

    def reject_urls_for_quality(self, urls: Iterable[str], reason: str) -> int:
        """Quarantine known bad discoveries without deleting their audit history."""

        normalized = [normalize_url(item) for item in urls if normalize_url(item)]
        if not normalized:
            return 0
        marks = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE discoveries SET
                    share_eligible=0, knowledge_eligible=0, quality_reason=?,
                    share_score=MIN(share_score, 0.2), knowledge_score=MIN(knowledge_score, 0.35),
                    heat_score=MIN(heat_score, 0.2)
                WHERE url IN ({marks})
                """,
                (str(reason or "quality_rejected").strip()[:500], *normalized),
            )
            return int(cursor.rowcount or 0)

    def dismiss_discovery(self, discovery_id: int, reason: str) -> None:
        """Dismiss a discovery that cannot enter the observation pipeline."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discoveries SET
                    status='dismissed', share_eligible=0, knowledge_eligible=0, quality_reason=?
                WHERE id=?
                """,
                (str(reason or "policy_rejected").strip()[:500], int(discovery_id)),
            )

    def delete_discovery(self, discovery_id: int) -> None:
        """删除不应保留的候选，避免普通内容长期堆积在冲浪库中。"""

        with self._connect() as connection:
            connection.execute("DELETE FROM discoveries WHERE id=?", (int(discovery_id),))

    def next_share_candidate(
        self,
        minimum_score: float,
        *,
        stream_id: str = "",
        selection_mode: str = "最高分优先",
        source_keyword: str = "",
        exclude_source_keyword: str = "",
    ) -> dict[str, Any] | None:
        now = time.time()
        normalized_stream_id = str(stream_id or "").strip()
        order_by = {
            "随机发送": "RANDOM()",
            "最高分优先": "share_score DESC, found_at ASC, discovery.id ASC",
            "最早收录优先": "found_at ASC, discovery.id ASC",
        }.get(selection_mode)
        if order_by is None:
            raise ValueError(f"未知的候选发送顺序：{selection_mode}")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT discovery.* FROM discoveries AS discovery
                INNER JOIN discovery_stream_candidates AS candidate
                    ON candidate.discovery_id=discovery.id
                WHERE candidate.stream_id=? AND candidate.status='ready'
                    AND discovery.status='ready' AND observed_at IS NOT NULL
                    AND full_text<>'' AND share_eligible=1 AND share_score>=?
                    AND candidate.share_defer_until<=?
                ORDER BY {order_by}
                """,
                (
                    normalized_stream_id,
                    float(minimum_score),
                    now,
                ),
            ).fetchall()
        required_keyword = str(source_keyword or "").strip().lower()
        excluded_keyword = str(exclude_source_keyword or "").strip().lower()
        for row in rows:
            candidate = dict(row)
            source_text = " ".join(
                str(candidate.get(key) or "") for key in ("source", "url")
            ).lower()
            if required_keyword and required_keyword not in source_text:
                continue
            if excluded_keyword and excluded_keyword in source_text:
                continue
            if (
                is_official_url(str(candidate.get("url") or ""))
                or str(candidate.get("risk_label") or "").lower() == "official"
            ):
                continue
            if float(candidate.get("share_score") or 0) >= float(minimum_score):
                return candidate
        return None

    def mark_share_queued(self, discovery_id: int, stream_id: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discovery_stream_candidates
                SET status='queued', queued_at=?
                WHERE discovery_id=? AND stream_id=? AND status='ready'
                """,
                (time.time(), int(discovery_id), str(stream_id)),
            )

    def queued_share_for_stream(self, stream_id: str) -> dict[str, Any] | None:
        """Return the newest unfinished share queued for a specific chat stream."""

        normalized_stream_id = str(stream_id or "").strip()
        if not normalized_stream_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT discovery.* FROM discoveries AS discovery
                INNER JOIN discovery_stream_candidates AS candidate
                    ON candidate.discovery_id=discovery.id
                WHERE candidate.status='queued' AND candidate.stream_id=?
                ORDER BY candidate.queued_at DESC, discovery.id DESC LIMIT 1
                """,
                (normalized_stream_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def restore_share_candidate(self, discovery_id: int, stream_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discovery_stream_candidates
                SET status='ready', queued_at=NULL
                WHERE discovery_id=? AND stream_id=? AND status='queued'
                """,
                (int(discovery_id), str(stream_id)),
            )

    def recover_stale_queued_shares(self, max_age_seconds: float) -> int:
        """恢复没有完成发送的旧队列项，避免重启后永久卡在 queued。"""

        cutoff = time.time() - max(1.0, float(max_age_seconds))
        with self._connect() as connection:
            return int(
                connection.execute(
                    """
                    UPDATE discovery_stream_candidates
                    SET status='ready', queued_at=NULL
                    WHERE status='queued' AND (queued_at IS NULL OR queued_at<?)
                    """,
                    (cutoff,),
                ).rowcount
                or 0
            )

    def defer_share_candidate(
        self,
        discovery_id: int,
        stream_id: str,
        *,
        cooldown_minutes: int,
        max_attempts: int,
        reason: str,
    ) -> str:
        """记录 Planner 拒绝后的退避，避免同一帖子每分钟重复投递。"""

        now = time.time()
        normalized_stream_id = str(stream_id or "").strip()
        cooldown_seconds = max(1, int(cooldown_minutes)) * 60
        maximum_attempts = max(1, int(max_attempts))
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT share_decline_count FROM discovery_stream_candidates
                WHERE discovery_id=? AND stream_id=?
                """,
                (int(discovery_id), normalized_stream_id),
            ).fetchone()
            if row is None:
                return "missing"
            next_attempt = int(row[0] or 0) + 1
            if next_attempt >= maximum_attempts:
                connection.execute(
                    """
                    UPDATE discovery_stream_candidates SET
                        status='dismissed', queued_at=NULL, share_defer_until=0,
                        share_decline_count=?, quality_reason=?
                    WHERE discovery_id=? AND stream_id=? AND status<>'shared'
                    """,
                    (
                        next_attempt,
                        f"主动分享连续被拒绝 {next_attempt} 次：{str(reason)[:300]}",
                        int(discovery_id),
                        normalized_stream_id,
                    ),
                )
                return "dismissed"
            connection.execute(
                """
                UPDATE discovery_stream_candidates SET
                    status='ready', queued_at=NULL, share_defer_until=?,
                    share_decline_count=?, quality_reason=?
                WHERE discovery_id=? AND stream_id=? AND status<>'shared'
                """,
                (
                    now + cooldown_seconds,
                    next_attempt,
                    f"主动分享暂缓 {cooldown_minutes} 分钟：{str(reason)[:300]}",
                    int(discovery_id),
                    normalized_stream_id,
                ),
            )
        return "deferred"

    def mark_shared(self, discovery_id: int, stream_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE discovery_stream_candidates
                SET status='shared', shared_at=?, queued_at=NULL
                WHERE discovery_id=? AND stream_id=?
                """,
                (time.time(), int(discovery_id), str(stream_id)),
            )
            if cursor.rowcount:
                return
            # 手动 /抖音 不属于自动候选池，继续沿用旧的单次发送记录语义。
            connection.execute(
                """
                UPDATE discoveries
                SET status='shared', shared_at=?, shared_stream_id=?,
                    queued_stream_id='', queued_at=NULL
                WHERE id=?
                """,
                (time.time(), str(stream_id), int(discovery_id)),
            )

    def recent_shared(self, stream_id: str, since: float) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT discovery.*, candidate.shared_at AS stream_shared_at
                FROM discovery_stream_candidates AS candidate
                INNER JOIN discoveries AS discovery ON discovery.id=candidate.discovery_id
                WHERE candidate.stream_id=? AND candidate.status='shared' AND candidate.shared_at>=?
                ORDER BY candidate.shared_at DESC
                """,
                (str(stream_id), float(since)),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_reaction(self, discovery_id: int, stream_id: str, message_text: str) -> None:
        text = str(message_text or "").strip()
        if not text:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reactions(discovery_id, stream_id, message_text, created_at) VALUES (?, ?, ?, ?)",
                (int(discovery_id), str(stream_id), text[:2000], time.time()),
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key=?", (str(key),)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_state(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )

    def prune_retained_data(
        self,
        *,
        ordinary_candidate_days: int,
        dismissed_days: int,
        shared_days: int,
        now: float | None = None,
    ) -> dict[str, int]:
        """删除已过保留期的抖音候选与分享记录。"""

        current_time = time.time() if now is None else float(now)
        ordinary_cutoff = current_time - max(1, int(ordinary_candidate_days)) * 86400
        dismissed_cutoff = current_time - max(1, int(dismissed_days)) * 86400
        shared_cutoff = current_time - max(1, int(shared_days)) * 86400
        removed = {
            "ordinary_candidates": 0,
            "dismissed": 0,
            "shared": 0,
        }

        with self._connect() as connection:
            removed["ordinary_candidates"] = int(
                connection.execute(
                    "DELETE FROM discoveries WHERE shared_at IS NULL AND status<>'dismissed' AND found_at<?",
                    (ordinary_cutoff,),
                ).rowcount or 0
            )
            removed["dismissed"] = int(
                connection.execute(
                    "DELETE FROM discoveries WHERE shared_at IS NULL AND status='dismissed' AND found_at<?",
                    (dismissed_cutoff,),
                ).rowcount
                or 0
            )
            removed["shared"] = int(
                connection.execute(
                    "DELETE FROM discoveries WHERE shared_at IS NOT NULL AND shared_at<?",
                    (shared_cutoff,),
                ).rowcount
                or 0
            )
        return removed

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            result = {
                "discoveries": int(connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]),
                "ready": int(connection.execute("SELECT COUNT(*) FROM discoveries WHERE status='ready'").fetchone()[0]),
                "shared": int(connection.execute("SELECT COUNT(*) FROM discoveries WHERE status='shared'").fetchone()[0]),
                "reactions": int(connection.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]),
            }
        return result
