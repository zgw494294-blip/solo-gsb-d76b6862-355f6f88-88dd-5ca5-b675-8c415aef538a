"""播放会话测试：创建/推进/接纳、已开始字幕保轨、冲突 409、并发串行化。"""
import json
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def scene_payload(track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


def make_session(client, scene_id="scene-play"):
    """创建场景（revision=1）并以其创建会话，返回会话状态。"""
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def revise(client, scene_id, expected_revision, subtitles, track_count=2):
    body = {
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    }
    resp = client.put(f"/scenes/{scene_id}", json=body)
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True
    return resp.json()


# ---------------------------------------------------------------------------
# 创建会话
# ---------------------------------------------------------------------------


def test_create_session_copies_full_snapshot():
    client = TestClient(app)
    state = make_session(client)
    assert state["session_id"]
    assert state["scene_id"] == "scene-play"
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 0
    assert state["track_count"] == 2
    assert state["changes"] == 0
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2", "s3"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_create_session_unknown_scene_404():
    client = TestClient(app)
    resp = client.post("/sessions", json={"scene_id": "nope"})
    assert resp.status_code == 404


def test_create_session_blank_scene_id_422():
    client = TestClient(app)
    resp = client.post("/sessions", json={"scene_id": "  "})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


def test_advance_returns_active_left_closed_right_open():
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    # 800：s1（0<=800<1000）与 s2（800<=800<3000，左闭）活动
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 800})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cursor_ms"] == 800
    assert body["source_revision"] == 1
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]

    # 1000：s1 结束（右开），s3 开始（左闭）
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s2", 2),
        ("s3", 1),
    ]

    # 3000：s2、s3 均已结束
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3000}).json()
    assert body["active"] == []


def test_advance_active_entries_carry_full_subtitle_fields():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900}).json()
    s2 = next(a for a in body["active"] if a["id"] == "s2")
    assert s2 == {
        "id": "s2",
        "speaker": "Bob",
        "start_ms": 800,
        "end_ms": 3000,
        "locked_track": 2,
        "track": 2,
    }


def test_advance_same_time_allowed():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    for _ in range(2):
        resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 500})
        assert resp.status_code == 200
        assert resp.json()["cursor_ms"] == 500


def test_advance_regression_422_and_session_unchanged():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})

    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 999})
    assert resp.status_code == 422
    assert resp.json()["detail"]["cursor_ms"] == 1000

    # 失败的推进不改变会话：游标仍是 1000
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert body["cursor_ms"] == 1000
    assert [a["id"] for a in body["active"]] == ["s2", "s3"]


def test_advance_negative_time_422():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": -1})
    assert resp.status_code == 422


def test_advance_unknown_session_404():
    client = TestClient(app)
    resp = client.post("/sessions/nope/advance", json={"time_ms": 0})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 接纳
# ---------------------------------------------------------------------------


def test_accept_keeps_started_tracks_and_recomputes_rest():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # 游标 900：s1、s2 已开始（固定轨道 1、2）；s3 未开始

    # 场景修订：s3 延长、新增 s4（均在游标之后开始）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])

    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert state["source_revision"] == 2
    assert state["cursor_ms"] == 900  # 游标不变
    assert state["track_count"] == 2
    assert state["assignments"] == [
        {"id": "s1", "track": 1},  # 已开始：固定原分轨
        {"id": "s2", "track": 2},  # 已开始：固定原分轨
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 2},
    ]
    assert state["changes"] == 0

    # 接纳不改动场景本身
    scene = client.get(f"/scenes/{scene_id}").json()
    assert scene["revision"] == 2
    assert [s["id"] for s in scene["subtitles"]] == ["s1", "s2", "s3", "s4"]

    # 推进按新快照生效
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3500}).json()
    assert body["source_revision"] == 2
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s3", 1),
        ("s4", 2),
    ]


def test_accept_without_scene_change_is_noop_success():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert state["source_revision"] == 1
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_accept_future_subtitle_deleted_ok():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # 删除未开始的 s3（start 1000 > 游标 900）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
    ])
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
    ]


def test_accept_track_count_changed_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]

    revise(client, scene_id, 1, scene_payload()["subtitles"], track_count=3)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 会话未被失败改变：场景改回 2 轨后接纳成功
    revise(client, scene_id, 2, scene_payload()["subtitles"], track_count=2)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 3
    assert resp.json()["track_count"] == 2


def test_accept_started_subtitle_modified_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # s1 已开始（start 0 <= 900）：end_ms 被修改
    subs = scene_payload()["subtitles"]
    subs[0]["end_ms"] = 900
    revise(client, scene_id, 1, subs)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 会话保持旧快照：场景改回原样后接纳成功
    revise(client, scene_id, 2, scene_payload()["subtitles"])
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 3
    assert resp.json()["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_accept_started_subtitle_locked_track_changed_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    subs = scene_payload()["subtitles"]
    subs[1]["locked_track"] = 1  # s2 已开始，locked_track 不得改
    revise(client, scene_id, 1, subs)
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_started_subtitle_deleted_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # 删除已开始的 s2（start 800 <= 900）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_new_subtitle_starting_before_cursor_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    subs = scene_payload()["subtitles"] + [
        {"id": "s4", "speaker": "Dan", "start_ms": 500, "end_ms": 600},
    ]
    revise(client, scene_id, 1, subs)
    # 新增字幕开始于游标之前（500 <= 900）：409
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_infeasible_with_pinned_tracks_409():
    client = TestClient(app)
    scene_id = "scene-inf"
    # 自定义场景：s2 不锁轨，分轨 1/2/1
    client.put(f"/scenes/{scene_id}", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "A", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "B", "start_ms": 800, "end_ms": 940},
            {"id": "s3", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": scene_id}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # 游标 900：s1 固定轨道 1，s2 固定轨道 2

    # 修订：新增 s4[950,980) 锁轨道 1（在游标之后开始，本身合法可保存）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "A", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "B", "start_ms": 800, "end_ms": 940},
        {"id": "s3", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        {"id": "s4", "speaker": "C", "start_ms": 950, "end_ms": 980,
         "locked_track": 1},
    ])
    # s4 与已开始的 s1（固定轨道 1）时间重叠且同轨：无解
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 失败的接纳不改变会话：活动字幕仍来自旧快照
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 901}).json()
    assert body["source_revision"] == 1
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]


def test_accept_unknown_session_404():
    client = TestClient(app)
    assert client.post("/sessions/nope/accept").status_code == 404


# ---------------------------------------------------------------------------
# 多会话与并发
# ---------------------------------------------------------------------------


def test_sessions_are_independent():
    client = TestClient(app)
    client.put("/scenes/scene-play", json=scene_payload())
    a = client.post("/sessions", json={"scene_id": "scene-play"}).json()
    b = client.post("/sessions", json={"scene_id": "scene-play"}).json()
    assert a["session_id"] != b["session_id"]

    body = client.post(
        f"/sessions/{a['session_id']}/advance", json={"time_ms": 2500}
    ).json()
    assert [(x["id"], x["track"]) for x in body["active"]] == [("s2", 2)]

    # 另一会话游标不受影响；0 时刻 s1 活动（左闭）
    body = client.post(
        f"/sessions/{b['session_id']}/advance", json={"time_ms": 0}
    ).json()
    assert [(x["id"], x["track"]) for x in body["active"]] == [("s1", 1)]


def test_concurrent_accepts_serialized_and_identical():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])

    results = []
    barrier = threading.Barrier(6)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/accept")
        results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 完整操作串行生效：全部成功，且响应完全一致（同一源版本、同一分轨）
    assert all(status == 200 for status, _ in results)
    bodies = {json.dumps(body, sort_keys=True) for _, body in results}
    assert len(bodies) == 1
    state = results[0][1]
    assert state["source_revision"] == 2
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 2},
    ]


def test_concurrent_advances_serialized():
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    results = []
    barrier = threading.Barrier(5)

    def worker(time_ms):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/advance", json={"time_ms": time_ms})
        results.append((time_ms, resp.status_code))

    times = [100, 300, 500, 700, 900]
    threads = [threading.Thread(target=worker, args=(t,)) for t in times]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 串行后游标只增不减：提交最大时刻的请求必定成功，其余成功或 422
    assert (900, 200) in results
    assert all(status in (200, 422) for _, status in results)

    # 最终游标为 900：等于游标可推进，小于游标被拒绝
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 900}
    ).status_code == 200
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 899}
    ).status_code == 422
