"""播放会话：单进程内存态，进程重启即消失。

会话按 scene_id 建立（一个场景同时只有一个会话，重复创建即重置），
建立时复制已保存场景的完整快照（字幕、分轨、changes、版本），
播放游标 cursor_ms 从 0 开始、只增不减。

播放中可以“接纳（adopt）”场景的后续修订快照：
  - 轨道数必须与建会时一致；
  - 会话中 start_ms <= cursor_ms 的字幕视为“已开始”：id、speaker、
    起止时间、locked_track 必须与新快照逐字一致，且分轨固定为会话原分轨；
  - 其余字幕允许修改、删除或新增，但在新快照中都必须在游标之后开始；
  - 以新快照顺序、沿用最少换轨与字典序并列规则，把已开始字幕的固定轨道
    作为锁定约束，对全部分轨重新求解；无解即接纳失败。
接纳失败不改动会话；成功则整体替换会话快照与源版本，已保存的场景本身不变。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Tuple

from .solver import Subtitle, solve, validate

# 已开始字幕在接纳时必须逐字保持不变的身份字段
_STARTED_FIXED_FIELDS: Tuple[str, ...] = (
    "id",
    "speaker",
    "start_ms",
    "end_ms",
    "locked_track",
)


class SessionNotFound(Exception):
    """scene_id 对应的播放会话不存在（尚未创建或进程已重启）。"""


class CursorOutOfOrder(Exception):
    """推进时间小于当前游标（倒退）；负数等非法值由 HTTP 层 Pydantic 拦截。"""

    def __init__(self, cursor_ms: int, time_ms: int) -> None:
        self.cursor_ms = cursor_ms
        self.time_ms = time_ms
        super().__init__(
            f"播放游标只能不减：当前 cursor_ms={cursor_ms}，收到 time_ms={time_ms}"
        )


class AdoptConflict(Exception):
    """新快照无法被当前会话接纳。

    reason 取值：
      track_count_changed            轨道数发生变化
      duplicate_id                   新快照内字幕 id 重复
      started_subtitle_missing       已开始字幕在新快照中被删除
      started_subtitle_changed       已开始字幕的身份字段被改动
      subtitle_starts_before_cursor  非已开始字幕在游标处或游标之前开始
      invalid_snapshot               新快照与固定轨道组合后不满足输入合法性
      infeasible                     固定已开始字幕后全量重算无解
    """

    def __init__(
        self,
        reason: str,
        message: str,
        subtitle_id: Any = None,
    ) -> None:
        self.reason = reason
        self.subtitle_id = subtitle_id
        super().__init__(message)


def plan_adopt(
    *,
    track_count: int,
    cursor_ms: int,
    old_subtitles: List[Dict[str, Any]],
    old_assignments: List[Dict[str, Any]],
    new_subtitles: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """纯函数：计算接纳新快照后的完整分轨。

    返回 (新快照顺序的字幕数据, 新分轨, 换轨总数)；
    任何接纳冲突或无解都抛出 AdoptConflict，调用方负责保证会话不被改动。
    """
    old_track_by_id = {a["id"]: a["track"] for a in old_assignments}
    started = {s["id"]: s for s in old_subtitles if s["start_ms"] <= cursor_ms}

    new_by_id: Dict[Any, Dict[str, Any]] = {}
    for s in new_subtitles:
        sid = s["id"]
        if sid in new_by_id:
            raise AdoptConflict(
                "duplicate_id", f"接纳快照中字幕 id 重复: {sid!r}", sid
            )
        new_by_id[sid] = s

    # 1) 已开始字幕：身份字段逐字不变，分轨固定（求解时以锁定约束注入）
    for sid, old_s in started.items():
        new_s = new_by_id.get(sid)
        if new_s is None:
            raise AdoptConflict(
                "started_subtitle_missing",
                f"已开始字幕 {sid!r} 不得在接纳快照中删除",
                sid,
            )
        for field_name in _STARTED_FIXED_FIELDS:
            if new_s.get(field_name) != old_s.get(field_name):
                raise AdoptConflict(
                    "started_subtitle_changed",
                    f"已开始字幕 {sid!r} 的 {field_name} 不得在播放中修改",
                    sid,
                )

    # 2) 其余字幕（改/留/新增）都必须在游标之后开始
    for s in new_subtitles:
        if s["id"] not in started and s["start_ms"] <= cursor_ms:
            raise AdoptConflict(
                "subtitle_starts_before_cursor",
                f"字幕 {s['id']!r} 的 start_ms={s['start_ms']} 必须晚于当前游标 "
                f"cursor_ms={cursor_ms}",
                s["id"],
            )

    # 3) 已开始字幕以原分轨作为锁定轨，全量重算
    forced_subs: List[Subtitle] = []
    for d in new_subtitles:
        sid = d["id"]
        if sid in started:
            forced_subs.append(
                Subtitle(
                    id=sid,
                    speaker=d["speaker"],
                    start_ms=d["start_ms"],
                    end_ms=d["end_ms"],
                    locked_track=old_track_by_id[sid],
                )
            )
        else:
            forced_subs.append(
                Subtitle(
                    id=sid,
                    speaker=d["speaker"],
                    start_ms=d["start_ms"],
                    end_ms=d["end_ms"],
                    locked_track=d.get("locked_track"),
                )
            )

    error = validate(forced_subs, track_count)
    if error is not None:
        raise AdoptConflict("invalid_snapshot", f"接纳快照与已开始字幕冲突: {error}")

    tracks, changes = solve(forced_subs, track_count)
    if tracks is None:
        raise AdoptConflict(
            "infeasible",
            "固定已开始字幕的原分轨后，按接纳快照全量重算分轨无解",
        )

    # 归一化输出：与 /scenes 保存的字幕数据一致，始终带 locked_track 键
    subtitles_data = [
        {
            "id": d["id"],
            "speaker": d["speaker"],
            "start_ms": d["start_ms"],
            "end_ms": d["end_ms"],
            "locked_track": d.get("locked_track"),
        }
        for d in new_subtitles
    ]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(forced_subs, tracks)
    ]
    return subtitles_data, assignments_data, changes


@dataclass
class PlaybackSession:
    """一个场景的内存播放会话。subtitles/assignments 仅在整体替换时变更。"""

    scene_id: str
    source_revision: int
    cursor_ms: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    assignments: List[Dict[str, Any]]
    changes: int

    @classmethod
    def from_record(cls, record: Any) -> "PlaybackSession":
        """从已保存场景的完整快照复制建会，游标从 0 毫秒开始。"""
        return cls(
            scene_id=record.scene_id,
            source_revision=record.revision,
            cursor_ms=0,
            track_count=record.track_count,
            subtitles=deepcopy(record.subtitles),
            assignments=deepcopy(record.assignments),
            changes=record.changes,
        )

    def active_subtitles(self) -> List[Dict[str, Any]]:
        """游标处活动字幕：左闭右开 [start_ms, end_ms)，按快照顺序。"""
        track_of = {a["id"]: a["track"] for a in self.assignments}
        active: List[Dict[str, Any]] = []
        for s in self.subtitles:
            if s["start_ms"] <= self.cursor_ms < s["end_ms"]:
                item = dict(s)
                item["track"] = track_of[s["id"]]
                active.append(item)
        return active


class PlaybackManager:
    """进程内会话表。推进与接纳等完整操作在同一把锁下串行生效。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, PlaybackSession] = {}
        self._lock = RLock()

    def create(self, record: Any) -> PlaybackSession:
        """按 scene_id 复制快照建会；同 scene_id 已存在会话则整体重置。"""
        with self._lock:
            session = PlaybackSession.from_record(record)
            self._sessions[record.scene_id] = session
            return session

    def get(self, scene_id: str) -> Optional[PlaybackSession]:
        with self._lock:
            return self._sessions.get(scene_id)

    def require(self, scene_id: str) -> PlaybackSession:
        session = self.get(scene_id)
        if session is None:
            raise SessionNotFound(scene_id)
        return session

    def advance(self, scene_id: str, time_ms: int) -> PlaybackSession:
        """推进（或原地停留）游标；倒退抛 CursorOutOfOrder，会话不变。"""
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                raise SessionNotFound(scene_id)
            if time_ms < session.cursor_ms:
                raise CursorOutOfOrder(session.cursor_ms, time_ms)
            session.cursor_ms = time_ms
            return session

    def adopt(self, scene_id: str, get_record: "Callable[[], Any]") -> PlaybackSession:
        """接纳场景当前快照：成功则整体替换会话快照与源版本，游标不变。

        get_record 在本会话的串行锁内被调用（回调负责读取已保存场景），
        因此“读取场景快照 + 接纳计算 + 替换”是一个完整串行操作，
        与其它推进/接纳互斥，并保证读到某次成功提交的完整快照。
        """
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                raise SessionNotFound(scene_id)

            record = get_record()
            if record is None:
                raise AdoptConflict(
                    "scene_snapshot_unavailable",
                    "无法接纳：场景当前快照不可用",
                )
            if record.track_count != session.track_count:
                raise AdoptConflict(
                    "track_count_changed",
                    f"接纳会话的轨道数不得改变：会话为 {session.track_count} 轨，"
                    f"快照为 {record.track_count} 轨",
                )

            # plan_adopt 失败会抛 AdoptConflict，此时 self._sessions 尚未被触碰
            subtitles, assignments, changes = plan_adopt(
                track_count=session.track_count,
                cursor_ms=session.cursor_ms,
                old_subtitles=session.subtitles,
                old_assignments=session.assignments,
                new_subtitles=record.subtitles,
            )
            new_session = PlaybackSession(
                scene_id=session.scene_id,
                source_revision=record.revision,
                cursor_ms=session.cursor_ms,
                track_count=session.track_count,
                subtitles=subtitles,
                assignments=assignments,
                changes=changes,
            )
            self._sessions[scene_id] = new_session
            return new_session

    def reset(self) -> None:
        """清空全部会话（测试隔离用；生产中进程重启等价于此）。"""
        with self._lock:
            self._sessions.clear()
