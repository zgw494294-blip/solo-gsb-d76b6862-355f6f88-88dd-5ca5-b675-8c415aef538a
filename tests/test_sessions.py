"""播放会话测试：建会/推进/接纳修订，固定已开始字幕分轨。"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.playback import plan_adopt  # noqa: E402


def _sub(id, speaker, start, end, locked=None):
    d = {"id": id, "speaker": speaker, "start_ms": start, "end_ms": end}
    if locked is not None:
        d["locked_track"] = locked
    return d


BASE_SCENE = {
    "track_count": 2,
    "subtitles": [
        _sub("s1", "Alice", 0, 1000),
        _sub("s2", "Bob", 800, 3000, locked=2),
        _sub("s3", "Alice", 1000, 2500),
        _sub("s4", "Alice", 2500, 4000),
    ],
}
# 求解：s1=1, s2=2, s3=1, s4=1（changes=0）


def _create_scene(client, scene_id="scene-play", body=None, revision=0):
    base = BASE_SCENE if body is None else body
    payload = {**base, "expected_revision": revision}
    resp = client.put(f"/scenes/{scene_id}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp


def _open_session(client, scene_id="scene-play"):
    resp = client.post(f"/scenes/{scene_id}/session")
    assert resp.status_code == 200, resp.text
    return resp


def test_create_session_copies_snapshot_cursor_zero():
    client = TestClient(app)
    _create_scene(client)
    body = _open_session(client).json()
    assert body["scene_id"] == "scene-play"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 0
    assert body["track_count"] == 2
    assert body["changes"] == 0
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 1},
    ]
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3", "s4"]
    # 游标 0 处：左闭右开，s1 的 [0,1000) 活动
    assert [a["id"] for a in body["active"]] == ["s1"]
    assert body["active"][0]["track"] == 1


def test_create_session_for_unknown_scene_404():
    client = TestClient(app)
    assert client.post("/scenes/nope/session").status_code == 404


def test_session_is_in_memory_snapshot_copy():
    """场景随后被修订，会话快照不随之改变，直到显式接纳。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)

    rev2 = {
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            _sub("s1", "Alice", 0, 1200),
            _sub("s2", "Bob", 800, 3000, locked=2),
            _sub("s3", "Alice", 1200, 2500),
        ],
    }
    assert client.put("/scenes/scene-play", json=rev2).status_code == 200

    got = client.get("/scenes/scene-play/session").json()
    assert got["source_revision"] == 1
    assert [s["id"] for s in got["subtitles"]] == ["s1", "s2", "s3", "s4"]
    assert got["subtitles"][0]["end_ms"] == 1000


def test_recreate_session_resets_cursor_and_revision():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    assert client.post("/scenes/scene-play/session/advance", json={"time_ms": 900}).status_code == 200

    rev2 = {
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            _sub("s1", "Alice", 0, 500),
            _sub("s2", "Bob", 600, 900),
        ],
    }
    r = client.put("/scenes/scene-play", json=rev2)
    assert r.status_code == 200 and r.json().get("feasible") is not False
    body = _open_session(client).json()  # 再次建会 = 重置
    assert body["cursor_ms"] == 0
    assert body["source_revision"] == 2
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2"]


def test_advance_half_open_intervals_and_tracks():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)

    def active_at(t):
        r = client.post("/scenes/scene-play/session/advance", json={"time_ms": t})
        assert r.status_code == 200
        b = r.json()
        return b["cursor_ms"], [a["id"] for a in b["active"]], b["tracks"]

    assert active_at(0)[1] == ["s1"]
    assert active_at(799)[1] == ["s1"]
    # 800: s2 [800,3000) 开始；s1 仍在
    cur, ids, tracks = active_at(800)
    assert cur == 800
    assert ids == ["s1", "s2"]
    assert tracks[0]["occupied"][0]["id"] == "s1"
    assert tracks[1]["occupied"][0]["id"] == "s2"
    # 1000 是 s1 的右端点：左闭右开，s1 已结束；s3 相接开始
    assert active_at(1000)[1] == ["s2", "s3"]
    # 2500：s3 结束、s4 开始（相接）
    assert active_at(2500)[1] == ["s2", "s4"]
    # 3000：s2 结束
    assert active_at(3000)[1] == ["s4"]
    # 4000 之后无活动字幕，但仍返回各空轨道
    cur, ids, tracks = active_at(4000)
    assert ids == []
    assert [t["track"] for t in tracks] == [1, 2]
    assert tracks[0]["occupied"] == []


def test_advance_same_time_is_idempotent():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    r1 = client.post("/scenes/scene-play/session/advance", json={"time_ms": 1000})
    r2 = client.post("/scenes/scene-play/session/advance", json={"time_ms": 1000})
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["cursor_ms"] == 1000


def test_advance_backward_is_422_and_session_unchanged():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 2000})
    resp = client.post(
        "/scenes/scene-play/session/advance", json={"time_ms": 1999}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["cursor_ms"] == 2000
    assert detail["time_ms"] == 1999
    # 失败不改会话
    got = client.get("/scenes/scene-play/session").json()
    assert got["cursor_ms"] == 2000
    assert [a["id"] for a in got["active"]] == ["s2", "s3"]


def test_advance_negative_or_non_int_is_422():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    assert client.post(
        "/scenes/scene-play/session/advance", json={"time_ms": -1}
    ).status_code == 422
    assert client.post(
        "/scenes/scene-play/session/advance", json={"time_ms": 1.5}
    ).status_code == 422
    assert client.post(
        "/scenes/scene-play/session/advance", json={}
    ).status_code == 422


def test_endpoints_without_session_404():
    client = TestClient(app)
    assert client.get("/scenes/ghost/session").status_code == 404
    assert client.post(
        "/scenes/ghost/session/advance", json={"time_ms": 1}
    ).status_code == 404
    assert client.post("/scenes/ghost/session/adopt").status_code == 404


# ---------------------------------------------------------------------------
# 接纳（adopt）
# ---------------------------------------------------------------------------

def _revise(client, subs, expected, scene_id="scene-play", track_count=2):
    return client.put(
        f"/scenes/{scene_id}",
        json={
            "track_count": track_count,
            "expected_revision": expected,
            "subtitles": subs,
        },
    )


def test_adopt_updates_snapshot_and_source_revision():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 500})

    # 修订：s1（已开始）完全不动；调整 s3 时间，删除 s4，新增 s5（均在游标后）
    r = _revise(
        client,
        [
            _sub("s1", "Alice", 0, 1000),
            _sub("s2", "Bob", 800, 3000, locked=2),
            _sub("s3", "Alice", 1000, 2600),
            _sub("s5", "Carol", 2600, 3200),
        ],
        expected=1,
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_revision"] == 2
    assert body["cursor_ms"] == 500  # 游标不变
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3", "s5"]
    # 已开始的 s1 固定原分轨 1
    track_of = {a["id"]: a["track"] for a in body["assignments"]}
    assert track_of["s1"] == 1
    assert track_of["s2"] == 2
    # 活动字幕仍是 s1
    assert [a["id"] for a in body["active"]] == ["s1"]

    # 场景本身不受会话操作影响
    scene = client.get("/scenes/scene-play").json()
    assert scene["revision"] == 2
    assert scene["subtitles"] == body["subtitles"]


def test_adopt_recomputes_future_assignments_after_cursor_passes():
    """接纳后继续推进，按新快照重算的分轨生效；已开始字幕不动。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    # 推进到 500：仅 s1 已开始并固定在轨道 1
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 500})

    # 修订未开始部分：缩短 s2（800 才开始，可改），新增锁 1 的 x 与 s3 重叠，
    # 迫使 s3 离开轨道 1 转到轨道 2，s4 与 s3 相接随同到轨道 2。
    r = _revise(
        client,
        [
            _sub("s1", "Alice", 0, 1000),
            _sub("s2", "Bob", 800, 1000),
            _sub("x", "Dave", 1000, 2000, locked=1),
            _sub("s3", "Alice", 1000, 2500),
            _sub("s4", "Alice", 2500, 4000),
        ],
        expected=1,
    )
    assert r.status_code == 200
    adopt = client.post("/scenes/scene-play/session/adopt")
    assert adopt.status_code == 200, adopt.text
    track_of = {a["id"]: a["track"] for a in adopt.json()["assignments"]}
    assert track_of["s1"] == 1  # 已开始：固定原轨
    assert track_of["s2"] == 2
    assert track_of["x"] == 1
    assert track_of["s3"] == 2  # 被 x 挤出轨道 1
    assert track_of["s4"] == 2  # s3->s4 相接同轨更优

    at_1500 = client.post(
        "/scenes/scene-play/session/advance", json={"time_ms": 1500}
    ).json()
    ids_tracks = {a["id"]: a["track"] for a in at_1500["active"]}
    assert ids_tracks == {"x": 1, "s3": 2}


def test_adopt_without_new_revision_is_noop_success():
    """场景未再修订时接纳当前快照：幂等成功，分轨不应变化。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 900})
    body = client.post("/scenes/scene-play/session/adopt").json()
    assert body["source_revision"] == 1
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 1},
    ]


def test_adopt_track_count_changed_409():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 100})
    _revise(client, BASE_SCENE["subtitles"], expected=1, track_count=3)
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "track_count_changed"
    # 会话不变
    got = client.get("/scenes/scene-play/session").json()
    assert got["source_revision"] == 1
    assert got["track_count"] == 2


def test_adopt_started_subtitle_deleted_409():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 900})  # s1,s2 开始
    _revise(
        client,
        [
            _sub("s2", "Bob", 800, 3000, locked=2),
            _sub("s3", "Alice", 1000, 2500),
            _sub("s4", "Alice", 2500, 4000),
        ],
        expected=1,
    )
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "started_subtitle_missing"
    assert detail["subtitle_id"] == "s1"
    assert client.get("/scenes/scene-play/session").json()["source_revision"] == 1


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("speaker", "Alice2"),
        ("start_ms", 10),
        ("end_ms", 1100),
        ("locked_track", 2),
    ],
)
def test_adopt_started_subtitle_identity_changed_409(field, new_value):
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 500})  # 仅 s1 开始

    changed = _sub("s1", "Alice", 0, 1000)
    changed[field] = new_value
    subs = [changed] + BASE_SCENE["subtitles"][1:]
    put_resp = _revise(client, subs, expected=1)
    assert put_resp.status_code in (200, 422)
    if put_resp.status_code == 422 or put_resp.json().get("feasible") is False:
        # 非法（422）或合法但无解（feasible=false）的修订不落库：
        # 场景仍是 rev1，接纳旧快照幂等成功，会话保持原样
        body = client.post("/scenes/scene-play/session/adopt").json()
        assert body["source_revision"] == 1
        return

    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "started_subtitle_changed"
    assert detail["subtitle_id"] == "s1"
    # 失败不改会话：已开始字幕的所有身份字段保持原值
    got = client.get("/scenes/scene-play/session").json()
    original = BASE_SCENE["subtitles"][0]
    for name in ("speaker", "start_ms", "end_ms", "locked_track"):
        assert got["subtitles"][0].get(name) == original.get(name)


def test_adopt_started_at_cursor_is_frozen():
    """start_ms == cursor 的字幕也算已开始，必须固定。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 800})  # s2 此刻开始
    changed = _sub("s2", "Bob", 800, 2900, locked=2)
    _revise(client, [BASE_SCENE["subtitles"][0], changed] + BASE_SCENE["subtitles"][2:], 1)
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "started_subtitle_changed"


def test_adopt_ended_subtitle_is_also_frozen():
    """游标前已结束（start_ms <= cursor）的字幕同样不得改动。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 2000})  # s1 已结束
    changed = _sub("s1", "Alice", 0, 900)
    _revise(client, [changed] + BASE_SCENE["subtitles"][1:], 1)
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["subtitle_id"] == "s1"


def test_adopt_new_subtitle_at_or_before_cursor_409():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 500})
    # 新增字幕恰好在游标处开始（此时只有 s1 在轨道 1 活动，快照自身有解）
    subs = BASE_SCENE["subtitles"] + [_sub("s9", "Zoe", 500, 600)]
    r2 = _revise(client, subs, expected=1)
    assert r2.status_code == 200 and r2.json().get("feasible") is not False
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "subtitle_starts_before_cursor"
    got = client.get("/scenes/scene-play/session").json()
    assert got["source_revision"] == 1

    # 再落库 rev3：另一条新字幕在游标之前开始（与 s9 同说话人但不重叠）-> 同样冲突
    subs3 = subs + [_sub("s8", "Zoe", 200, 300)]
    r3 = _revise(client, subs3, expected=2)
    assert r3.status_code == 200 and r3.json().get("feasible") is not False
    resp = client.post("/scenes/scene-play/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "subtitle_starts_before_cursor"
    # 按新快照顺序报告越界字幕：s9（500）与 s8（200）都早于游标，首个为 s9
    assert resp.json()["detail"]["subtitle_id"] == "s9"
    # 连续失败都不改动会话
    got = client.get("/scenes/scene-play/session").json()
    assert got["source_revision"] == 1
    assert [s["id"] for s in got["subtitles"]] == ["s1", "s2", "s3", "s4"]


def test_adopt_future_only_change_recomputes_freely():
    """cursor=0 时 start=0 的 s1 仍被固定；其余字幕按新快照自由重算，
    演示接纳结果可以与“从空白直接求解”不同（因为已开始字幕被锁定）。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    # cursor = 0：s1 的 start_ms=0 <= 0，被固定在原轨 1；s2 缩短到 1000 结束，
    # s3 锁 2（与 s2 端点相接不再冲突），s4 与 s3 相接随同到轨道 2
    subs = [
        _sub("s1", "Alice", 0, 1000),
        _sub("s2", "Bob", 800, 1000, locked=2),
        _sub("s3", "Alice", 1000, 2500, locked=2),
        _sub("s4", "Alice", 2500, 4000),
    ]
    r = _revise(client, subs, expected=1)
    assert r.status_code == 200 and r.json().get("feasible") is not False
    body = client.post("/scenes/scene-play/session/adopt").json()
    track_of = {a["id"]: a["track"] for a in body["assignments"]}
    assert track_of["s1"] == 1  # start=0 已开始：固定原轨
    assert track_of["s2"] == 2
    assert track_of["s3"] == 2
    assert track_of["s4"] == 2  # 与 s3 相接同轨，Alice: s1(1)->s3(2) 一次换轨


def test_adopt_infeasible_after_pinning_409():
    """接纳快照本身有解，但固定已开始字幕后全量重算无解 -> 409。"""
    client = TestClient(app)
    scene = {
        "track_count": 2,
        "subtitles": [
            _sub("a", "A", 0, 100),
            _sub("b", "B", 100, 200),
            _sub("c", "C", 200, 300),
        ],
    }
    _create_scene(client, scene_id="scene-infeas", body=scene)
    # 求解：全部轨道 1（端点相接）
    _open_session(client, "scene-infeas")
    client.post("/scenes/scene-infeas/session/advance", json={"time_ms": 150})
    # a、b 已开始并固定在轨道 1。新快照让 c 提前到 150（>cursor? 否：150<=150 不行）
    # c 改为 160 开始（游标之后），并与仍在播放的 b[100,200) 重叠锁 1 -> 无解
    rev2 = {
        "track_count": 2,
        "expected_revision": 1,
        "subtitles": [
            _sub("a", "A", 0, 100),
            _sub("b", "B", 100, 200),
            _sub("c", "C", 160, 300, locked=1),
        ],
    }
    r = client.put("/scenes/scene-infeas", json=rev2)
    assert r.status_code == 200  # 快照自身有解（c 与 b 重叠但未同时锁 1）
    resp = client.post("/scenes/scene-infeas/session/adopt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "infeasible"
    got = client.get("/scenes/scene-infeas/session").json()
    assert got["source_revision"] == 1
    assert [s["id"] for s in got["subtitles"]] == ["a", "b", "c"]
    assert {a["id"]: a["track"] for a in got["assignments"]} == {
        "a": 1,
        "b": 1,
        "c": 1,
    }


def test_illegal_scene_revision_422_does_not_affect_session():
    """非法修订在 PUT 阶段即 422 且不落库；会话接纳不到它，旧版接纳仍幂等成功。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    client.post("/scenes/scene-play/session/advance", json={"time_ms": 500})
    # s1 与 s3 同说话人时间重叠 -> 非法输入 422
    subs = [
        _sub("s1", "Alice", 0, 1000),
        _sub("s2", "Bob", 800, 3000, locked=2),
        _sub("s3", "Alice", 900, 2500),
        _sub("s4", "Alice", 2500, 4000),
    ]
    r = _revise(client, subs, expected=1)
    assert r.status_code == 422

    # 场景仍是 rev1：接纳返回当前会话快照，状态不变
    body = client.post("/scenes/scene-play/session/adopt").json()
    assert body["source_revision"] == 1
    assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3", "s4"]
    assert body["cursor_ms"] == 500


def test_concurrent_advance_and_adopt_serialized():
    """推进与接纳并发：每个完整操作串行生效，状态始终自洽。"""
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)

    # 先准备 revision 2（s1 不动，其余在游标后）
    _revise(
        client,
        [
            _sub("s1", "Alice", 0, 1000),
            _sub("s2", "Bob", 800, 3000, locked=2),
            _sub("s3", "Alice", 1000, 2500),
        ],
        expected=1,
    )

    errors = []

    def worker(kind):
        local = TestClient(app)
        try:
            if kind == "advance":
                r = local.post(
                    "/scenes/scene-play/session/advance", json={"time_ms": 900}
                )
            else:
                r = local.post("/scenes/scene-play/session/adopt")
            if r.status_code not in (200,):
                errors.append((kind, r.status_code, r.text))
        except Exception as exc:  # pragma: no cover
            errors.append((kind, "exc", repr(exc)))

    threads = []
    for i in range(12):
        threads.append(threading.Thread(target=worker, args=("advance",)))
        threads.append(threading.Thread(target=worker, args=("adopt",)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = client.get("/scenes/scene-play/session").json()
    assert final["cursor_ms"] == 900
    # 接纳最终必然生效（幂等），源版本为 2，且 s1 始终在轨道 1
    assert final["source_revision"] == 2
    s1 = next(a for a in final["assignments"] if a["id"] == "s1")
    assert s1["track"] == 1


def test_sessions_do_not_survive_manager_reset():
    client = TestClient(app)
    _create_scene(client)
    _open_session(client)
    from app.main import playback

    playback.reset()
    assert client.get("/scenes/scene-play/session").status_code == 404
    # 场景数据仍在 SQLite
    assert client.get("/scenes/scene-play").status_code == 200


# ---------------------------------------------------------------------------
# plan_adopt 纯函数直接测试
# ---------------------------------------------------------------------------

def test_plan_adopt_pins_started_and_recomputes_rest():
    old_subs = [
        _sub("a", "A", 0, 100),
        _sub("b", "B", 100, 200),
    ]
    old_assignments = [{"id": "a", "track": 2}, {"id": "b", "track": 2}]
    new_subs = [
        _sub("a", "A", 0, 100),
        _sub("b", "B", 100, 200),
        _sub("c", "C", 200, 300, locked=1),
    ]
    subs, assignments, changes = plan_adopt(
        track_count=2,
        cursor_ms=150,
        old_subtitles=old_subs,
        old_assignments=old_assignments,
        new_subtitles=new_subs,
    )
    assert [s["id"] for s in subs] == ["a", "b", "c"]
    assert assignments == [
        {"id": "a", "track": 2},
        {"id": "b", "track": 2},
        {"id": "c", "track": 1},
    ]


def test_plan_adopt_raises_on_changed_started():
    with pytest.raises(Exception):
        plan_adopt(
            track_count=2,
            cursor_ms=50,
            old_subtitles=[_sub("a", "A", 0, 100)],
            old_assignments=[{"id": "a", "track": 1}],
            new_subtitles=[_sub("a", "A", 0, 99)],
        )
