"""FastAPI 入口：请求/响应模型与路由。"""
from __future__ import annotations

from threading import Lock
from typing import List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from . import storage as storage_module
from .solver import Subtitle, solve, validate
from .storage import RevisionConflict, SceneRecord, SceneStore

app = FastAPI(
    title="对白字幕分轨 API",
    description=(
        "为离线对白字幕分配屏幕轨道：时间重叠的字幕不同轨，"
        "同一说话人按时间相邻字幕的换轨次数最少，并列取轨道号序列字典序最小者。"
        "场景（scene）可按 scene_id 保存修订快照，并以版本号做乐观并发控制。"
    ),
    version="1.1.0",
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

    subtitles_data = [s.model_dump() for s in req.subtitles]
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
