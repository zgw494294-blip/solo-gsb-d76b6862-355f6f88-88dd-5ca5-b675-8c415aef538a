"""FastAPI 入口：请求/响应模型与路由。"""
from __future__ import annotations

from threading import Lock
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from . import storage as storage_module
from .playback import (
    AdoptConflict,
    CursorOutOfOrder,
    PlaybackManager,
    PlaybackSession,
    SessionNotFound,
)
from .solver import Subtitle, solve, validate
from .storage import RevisionConflict, SceneRecord, SceneStore

app = FastAPI(
    title="对白字幕分轨 API",
    description=(
        "为离线对白字幕分配屏幕轨道：时间重叠的字幕不同轨，"
        "同一说话人按时间相邻字幕的换轨次数最少，并列取轨道号序列字典序最小者。"
        "场景（scene）可按 scene_id 保存修订快照，并以版本号做乐观并发控制。"
        "另可按 scene_id 建立单进程内存播放会话：游标只增不减，"
        "播放中接纳场景后续修订时固定已开始字幕的分轨。"
    ),
    version="1.2.0",
)


class SubtitleIn(BaseModel):
    id: Union[str, int] = Field(..., description="全局唯一的字幕 id")
    speaker: str = Field(..., description="说话人标识，非空")
    start_ms: int = Field(..., ge=0, description="开始时间（毫秒，非负）")
    end_ms: int = Field(..., ge=0, description="结束时间（毫秒，必须大于 start_ms）")
    locked_track: Optional[int] = Field(
        default=None, ge=1, description="可选：锁定轨道号（从 1 开始）"
    )

    @field_validator("speaker")
    @classmethod
    def _speaker_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("speaker 不能为空")
        return v

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, v: Union[str, int]) -> Union[str, int]:
        if isinstance(v, str) and v == "":
            raise ValueError("id 不能为空字符串")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "SubtitleIn":
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms 必须小于 end_ms")
        return self


class AssignRequest(BaseModel):
    track_count: int = Field(
        ..., ge=2, le=3, description="屏幕轨道数（2～3，轨道号从 1 开始）"
    )
    subtitles: List[SubtitleIn] = Field(
        ..., min_length=1, max_length=12, description="字幕列表（1～12 条，按输入顺序）"
    )

    @model_validator(mode="after")
    def _check_cross_item_rules(self) -> "AssignRequest":
        subs = [
            Subtitle(
                id=s.id,
                speaker=s.speaker,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                locked_track=s.locked_track,
            )
            for s in self.subtitles
        ]
        error = validate(subs, self.track_count)
        if error is not None:
            raise ValueError(error)
        return self


class SceneRevisionRequest(AssignRequest):
    """场景修订请求：沿用 /assign 的全部字段与校验，另带版本号。

    expected_revision：客户端所依据的当前版本。新场景填 0；
    已存在场景必须填写当前版本，成功后版本 +1。
    """

    expected_revision: int = Field(
        ..., ge=0, description="所依据的当前场景版本；新场景填 0"
    )


class TrackAssignment(BaseModel):
    id: Union[str, int]
    track: int


class AssignResponse(BaseModel):
    feasible: bool
    changes: int = Field(..., description="同一说话人按时间相邻字幕的换轨总数")
    assignments: List[TrackAssignment]


class SubtitleOut(BaseModel):
    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None


class SceneResponse(BaseModel):
    """一次成功修订保存后的完整场景快照。"""

    scene_id: str
    revision: int = Field(..., description="本次提交成功后的新版本号")
    track_count: int
    subtitles: List[SubtitleOut]
    changes: int
    assignments: List[TrackAssignment]
    feasible: bool = True


class AdvanceRequest(BaseModel):
    """播放推进请求：非负整数毫秒，只能不减。"""

    time_ms: int = Field(..., ge=0, description="推进到的播放位置（毫秒，非负，只能不减）")


class ActiveSubtitle(BaseModel):
    """游标处正在播放的字幕：区间左闭右开，即 start_ms <= cursor < end_ms。"""

    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None
    track: int = Field(..., description="该字幕在会话当前快照中固定/重算后的轨道")


class TrackState(BaseModel):
    """一条轨道在游标处占用的活动字幕（按快照顺序）。"""

    track: int
    occupied: List[ActiveSubtitle]


class AdvanceResponse(BaseModel):
    """推进（或接纳后查看）返回：源版本、游标与活动字幕/轨道。"""

    scene_id: str
    source_revision: int = Field(
        ..., description="会话当前快照所来自的场景修订版本"
    )
    cursor_ms: int
    active: List[ActiveSubtitle]
    tracks: List[TrackState]


class SessionResponse(AdvanceResponse):
    """会话完整状态：快照 + 游标 + 活动字幕。"""

    track_count: int
    subtitles: List[SubtitleOut]
    changes: int
    assignments: List[TrackAssignment]


# ---------------------------------------------------------------------------
# SQLite 存储：惰性单例，路径可用环境变量 SCENES_DB_PATH 覆盖
# ---------------------------------------------------------------------------

_store: Optional[SceneStore] = None
_store_lock = Lock()


def get_store() -> SceneStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SceneStore(storage_module.DEFAULT_DB_PATH)
    return _store


def reset_store() -> None:
    """重置单例（供测试重新指定 SCENES_DB_PATH 后使用）。"""
    global _store
    with _store_lock:
        _store = None


# ---------------------------------------------------------------------------
# 播放会话：单进程内存态，无需跨重启保留（进程重启即清空）
# ---------------------------------------------------------------------------

playback = PlaybackManager()


def _subtitles_from_request(req: AssignRequest) -> List[Subtitle]:
    return [
        Subtitle(
            id=s.id,
            speaker=s.speaker,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            locked_track=s.locked_track,
        )
        for s in req.subtitles
    ]


def _check_scene_id(scene_id: str) -> None:
    if scene_id.strip() == "":
        raise HTTPException(status_code=422, detail="scene_id 不能为空")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/assign", response_model=AssignResponse)
def assign(req: AssignRequest) -> AssignResponse:
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 锁定冲突 / 轨道不足：无解，方案为空（HTTP 仍为 200；非法输入才是 422）
        return AssignResponse(feasible=False, changes=0, assignments=[])
    return AssignResponse(
        feasible=True,
        changes=changes,
        assignments=[
            TrackAssignment(id=s.id, track=t) for s, t in zip(subs, tracks)
        ],
    )


@app.put(
    "/scenes/{scene_id}",
    response_model=Union[SceneResponse, AssignResponse],
)
def revise_scene(
    scene_id: str, req: SceneRevisionRequest
) -> Union[SceneResponse, AssignResponse]:
    """创建或修订场景。

    - 新场景：expected_revision=0，成功后 revision=1；
    - 修订：expected_revision 必须等于当前版本，成功后版本 +1；
    - 合法但无解：沿用 /assign 的无解响应（feasible=false），不保存、不改版本；
    - 非法输入：422，不改变已有版本；
    - 版本不符（含同一版本的并发竞争落败方）：409，不改变已有版本。
    """
    _check_scene_id(scene_id)

    # 校验由 Pydantic 模型在进入本函数前完成（非法 -> 422）。
    # 求解沿用 /assign 的同一套校验、最少换轨与并列规则。
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 合法但无解：与 /assign 完全相同的响应，且不落库
        return AssignResponse(feasible=False, changes=0, assignments=[])

    subtitles_data = [
        {
            "id": s.id,
            "speaker": s.speaker,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "locked_track": s.locked_track,
        }
        for s in req.subtitles
    ]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        new_revision = get_store().commit_revision(
            scene_id=scene_id,
            expected_revision=req.expected_revision,
            track_count=req.track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return SceneResponse(
        scene_id=scene_id,
        revision=new_revision,
        track_count=req.track_count,
        subtitles=[s.model_dump() for s in req.subtitles],
        changes=changes,
        assignments=[
            TrackAssignment(id=a["id"], track=a["track"]) for a in assignments_data
        ],
    )


def _record_to_scene_response(record: SceneRecord) -> SceneResponse:
    return SceneResponse(
        scene_id=record.scene_id,
        revision=record.revision,
        track_count=record.track_count,
        subtitles=record.subtitles,
        changes=record.changes,
        assignments=record.assignments,
    )


@app.get("/scenes/{scene_id}", response_model=SceneResponse)
def read_scene(scene_id: str) -> SceneResponse:
    """读取场景最近一次成功修订的完整快照；不存在返回 404。"""
    _check_scene_id(scene_id)
    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"场景 {scene_id!r} 不存在")
    return _record_to_scene_response(record)


# ---------------------------------------------------------------------------
# 播放会话：建会 / 推进 / 接纳 / 查看（单进程内存态）
# ---------------------------------------------------------------------------

def _session_response(session: PlaybackSession) -> SessionResponse:
    active = session.active_subtitles()
    return SessionResponse(
        scene_id=session.scene_id,
        source_revision=session.source_revision,
        cursor_ms=session.cursor_ms,
        active=active,
        tracks=_track_states(session.track_count, active),
        track_count=session.track_count,
        subtitles=session.subtitles,
        changes=session.changes,
        assignments=session.assignments,
    )


def _advance_response(session: PlaybackSession) -> AdvanceResponse:
    active = session.active_subtitles()
    return AdvanceResponse(
        scene_id=session.scene_id,
        source_revision=session.source_revision,
        cursor_ms=session.cursor_ms,
        active=active,
        tracks=_track_states(session.track_count, active),
    )


def _track_states(track_count: int, active: List[Dict[str, Any]]) -> List[TrackState]:
    by_track: Dict[int, List[ActiveSubtitle]] = {t: [] for t in range(1, track_count + 1)}
    for item in active:
        by_track[item["track"]].append(ActiveSubtitle(**item))
    return [TrackState(track=t, occupied=by_track[t]) for t in range(1, track_count + 1)]


def _require_session_or_404(scene_id: str) -> PlaybackSession:
    try:
        return playback.require(scene_id)
    except SessionNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"场景 {scene_id!r} 的播放会话不存在，请先 POST 建会",
        )


@app.post("/scenes/{scene_id}/session", response_model=SessionResponse)
def create_session(scene_id: str) -> SessionResponse:
    """按 scene_id 创建单进程内存播放会话：复制场景当前完整快照，游标从 0 开始。

    对同一 scene_id 重复调用即按最新场景快照整体重置（游标归零）；
    场景不存在返回 404。会话仅存于内存，进程重启后消失。
    """
    _check_scene_id(scene_id)
    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"场景 {scene_id!r} 不存在")
    session = playback.create(record)
    return _session_response(session)


@app.post("/scenes/{scene_id}/session/advance", response_model=AdvanceResponse)
def advance_session(scene_id: str, req: AdvanceRequest) -> AdvanceResponse:
    """推进播放游标：time_ms 为非负整数且只能不减。

    返回源版本、游标、活动字幕（左闭右开）与各轨道占用。
    倒退返回 422 且不改动会话；会话不存在返回 404。
    """
    _check_scene_id(scene_id)
    _require_session_or_404(scene_id)
    try:
        session = playback.advance(scene_id, req.time_ms)
    except CursorOutOfOrder as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "播放游标只能不减，不接受倒退的 time_ms",
                "scene_id": scene_id,
                "cursor_ms": exc.cursor_ms,
                "time_ms": exc.time_ms,
            },
        )
    return _advance_response(session)


@app.post("/scenes/{scene_id}/session/adopt", response_model=SessionResponse)
def adopt_session(scene_id: str) -> SessionResponse:
    """接纳场景当前已保存快照，保持已开始字幕的轨道。

    - 轨道数不得变；start_ms <= cursor 的字幕 id/speaker/起止/locked_track
      必须不变且固定原分轨；其余字幕可改删或加入，但均须在游标之后开始；
      按新快照顺序与原最少换轨、并列规则全量重算分轨。
    - 接纳冲突或重算无解返回 409，会话保持原样；成功整体替换会话快照与
      源版本（source_revision），游标不变，场景本身不被修改。
    """
    _check_scene_id(scene_id)
    _require_session_or_404(scene_id)

    def _load_snapshot() -> Optional[SceneRecord]:
        return get_store().get_scene(scene_id)

    try:
        # 读取场景快照与接纳计算在同一会话锁内作为完整操作串行生效
        session = playback.adopt(scene_id, _load_snapshot)
    except AdoptConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"接纳修订失败：{exc}",
                "scene_id": scene_id,
                "reason": exc.reason,
                "subtitle_id": exc.subtitle_id,
            },
        )
    return _session_response(session)


@app.get("/scenes/{scene_id}/session", response_model=SessionResponse)
def read_session(scene_id: str) -> SessionResponse:
    """查看播放会话完整状态（快照、游标、活动字幕）；不存在返回 404。"""
    _check_scene_id(scene_id)
    session = _require_session_or_404(scene_id)
    return _session_response(session)
