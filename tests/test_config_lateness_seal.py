"""未入组设备缩短 allowed_lateness_seconds 的窗口封存修复测试。

PUT /devices/{id}/config 更新未入组设备配置时：
- 缩短允许迟到秒数后，水位线已越过新封存阈值（watermark >= window_end + 新迟到秒数）
  的已有 open 窗口在同一写事务内封存并记录 sealed_at（阈值边界取等）；
- 只有实际封存才推进窗口检索的状态筛选变更版本：先前 status 筛选游标翻页返回 409，
  未筛状态的游标仍可翻页；未封存任何窗口的缩短/调长不推进版本，幂等重复提交亦然；
- 调长迟到期限绝不重开已封存窗口；
- 后续写入落在新封存窗口的事件按 late 修正处理（修正记录 + correction_count 递增）。
"""


def put_config(client, device_id, size, lateness):
    return client.put(
        f"/devices/{device_id}/config",
        json={"window_size_seconds": size, "allowed_lateness_seconds": lateness},
    )


def ingest(client, device_id, events):
    r = client.post("/ingest", json={"device_id": device_id, "events": events})
    assert r.status_code == 200, r.text
    return r


def seed_open_windows(client):
    """默认 60/30 配置（ingest 自动注册）：

    - d1：窗口 [00:00,00:01)（open：水位线 00:01:20 未越过旧阈值 00:01:30）、
          窗口 [00:01,00:02)（open，水位线事件所在窗口）；
    - d2：窗口 [00:00,00:01)（open）。
    """
    ingest(client, "d1", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    ingest(client, "d1", [{"event_id": "e2", "event_time": "2026-09-22T00:01:20Z", "value": 2.0}])
    ingest(client, "d2", [{"event_id": "e1", "event_time": "2026-09-22T00:00:40Z", "value": 3.0}])


def windows_by_start(client, device_id):
    return {w["window_start"]: w for w in client.get(f"/devices/{device_id}/windows").json()}


def search(client, **params):
    return client.get("/windows/search", params=params)


def keys(body):
    return [(w["window_start"], w["device_id"]) for w in body["windows"]]


# ---------- 缩短允许迟到秒数：同事务封存越过新阈值的 open 窗口 ----------

def test_shorten_lateness_seals_overdue_open_windows(client):
    seed_open_windows(client)
    assert windows_by_start(client, "d1")["2026-09-22T00:00:00Z"]["status"] == "open"

    # 30 → 0：水位线 00:01:20 已越过 [00:00,00:01) 的新封存阈值 00:01:00
    r = put_config(client, "d1", 60, 0)
    assert r.status_code == 200
    body = r.json()
    assert body["allowed_lateness_seconds"] == 0
    # 响应与配置更新、封存在同一事务内完成：统计已反映新封存的窗口
    assert body["sealed_windows"] == 1
    assert body["open_windows"] == 1

    after = windows_by_start(client, "d1")
    sealed = after["2026-09-22T00:00:00Z"]
    assert sealed["status"] == "sealed"
    assert sealed["sealed_at"] is not None
    # 封存不改动既有聚合
    assert (sealed["count"], sealed["sum"]) == (1, 1.0)
    # [00:01,00:02) 新阈值 00:02:00 未被水位线 00:01:20 越过：保持 open
    still_open = after["2026-09-22T00:01:00Z"]
    assert still_open["status"] == "open"
    assert still_open["sealed_at"] is None
    # 其他设备不受影响
    assert windows_by_start(client, "d2")["2026-09-22T00:00:00Z"]["status"] == "open"


def test_shorten_seals_at_exact_threshold_boundary(client):
    seed_open_windows(client)
    # 30 → 20：[00:00,00:01) 新阈值 00:01:20 恰好等于水位线，按 >= 口径封存
    r = put_config(client, "d1", 60, 20)
    assert r.status_code == 200
    assert r.json()["sealed_windows"] == 1
    w = windows_by_start(client, "d1")["2026-09-22T00:00:00Z"]
    assert w["status"] == "sealed"
    assert w["sealed_at"] is not None


def test_shorten_without_overdue_windows_seals_nothing(client):
    seed_open_windows(client)
    # 30 → 25：[00:00,00:01) 新阈值 00:01:25 仍未被水位线 00:01:20 越过
    r = put_config(client, "d1", 60, 25)
    assert r.status_code == 200
    assert r.json()["sealed_windows"] == 0
    assert r.json()["open_windows"] == 2
    w = windows_by_start(client, "d1")["2026-09-22T00:00:00Z"]
    assert w["status"] == "open"
    assert w["sealed_at"] is None


def test_lengthen_lateness_does_not_reopen_sealed_windows(client):
    # 水位线推到 00:02:00：[00:00,00:01) 在旧阈值 00:01:30 下由 ingest 封存
    ingest(client, "d1", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    ingest(client, "d1", [{"event_id": "e2", "event_time": "2026-09-22T00:02:00Z", "value": 2.0}])
    before = client.get("/devices/d1/windows").json()
    assert before[0]["status"] == "sealed"

    # 调长允许迟到秒数 30 → 300：已封存窗口绝不重开，其余窗口原样
    r = put_config(client, "d1", 60, 300)
    assert r.status_code == 200
    assert r.json()["allowed_lateness_seconds"] == 300
    assert client.get("/devices/d1/windows").json() == before


# ---------- 状态筛选变更版本：只有实际封存才推进 ----------

def test_status_filter_cursor_gets_409_after_config_seal(client):
    seed_open_windows(client)
    first = search(client, status="open", limit=1).json()
    assert keys(first) == [("2026-09-22T00:00:00Z", "d1")]
    cursor = first["next_cursor"]
    assert cursor is not None

    # 配置更新实际封存了 (00:00, d1)：状态筛选维度版本推进
    assert put_config(client, "d1", 60, 0).status_code == 200

    r = search(client, status="open", limit=1, cursor=cursor)
    assert r.status_code == 409
    assert "restart" in r.json()["detail"]

    # 不带游标重新检索：按最新状态返回
    fresh = search(client, status="open").json()
    assert keys(fresh) == [
        ("2026-09-22T00:00:00Z", "d2"),
        ("2026-09-22T00:01:00Z", "d1"),
    ]


def test_cursor_without_status_filter_survives_config_seal(client):
    seed_open_windows(client)
    first = search(client, limit=2).json()
    cursor = first["next_cursor"]
    assert cursor is not None

    assert put_config(client, "d1", 60, 0).status_code == 200

    # 未筛状态的游标不绑定状态版本：配置封存不影响翻页（行快照上界不变）
    r = search(client, limit=2, cursor=cursor)
    assert r.status_code == 200, r.text
    assert keys(r.json()) == [("2026-09-22T00:01:00Z", "d1")]
    assert r.json()["next_cursor"] is None


def test_shorten_without_actual_seal_keeps_status_cursor_valid(client):
    seed_open_windows(client)
    cursor = search(client, status="open", limit=1).json()["next_cursor"]
    assert cursor is not None

    # 缩短但没有窗口越过新阈值：未实际封存，不推进版本
    assert put_config(client, "d1", 60, 25).status_code == 200

    r = search(client, status="open", limit=1, cursor=cursor)
    assert r.status_code == 200, r.text
    assert keys(r.json()) == [("2026-09-22T00:00:00Z", "d2")]


def test_lengthen_keeps_status_cursor_valid(client):
    seed_open_windows(client)
    cursor = search(client, status="open", limit=1).json()["next_cursor"]
    assert cursor is not None

    # 调长迟到期限：不重开已封存窗口，也不封存任何窗口，版本不动
    assert put_config(client, "d1", 60, 300).status_code == 200

    r = search(client, status="open", limit=1, cursor=cursor)
    assert r.status_code == 200, r.text
    assert keys(r.json()) == [("2026-09-22T00:00:00Z", "d2")]


def test_repeat_same_config_does_not_seal_again(client):
    seed_open_windows(client)
    assert put_config(client, "d1", 60, 0).status_code == 200
    # 首次封存后发起 status 筛选分页
    cursor = search(client, status="open", limit=1).json()["next_cursor"]
    assert cursor is not None

    # 幂等重复提交：没有可再封存的窗口，不推进版本，游标继续有效
    assert put_config(client, "d1", 60, 0).status_code == 200
    r = search(client, status="open", limit=1, cursor=cursor)
    assert r.status_code == 200, r.text
    assert keys(r.json()) == [("2026-09-22T00:01:00Z", "d1")]


# ---------- 后续写入落在新封存窗口：按 late 修正处理 ----------

def test_events_landing_in_newly_sealed_window_are_late(client):
    seed_open_windows(client)
    assert put_config(client, "d1", 60, 0).status_code == 200

    # 落入刚被配置更新封存的 [00:00,00:01)：按 late 修正处理
    r = ingest(client, "d1", [{"event_id": "e3", "event_time": "2026-09-22T00:00:30Z", "value": 5.0}])
    body = r.json()
    assert body["late"] == 1
    assert body["results"][0]["status"] == "late"
    assert body["results"][0]["window_start"] == "2026-09-22T00:00:00Z"

    w = windows_by_start(client, "d1")["2026-09-22T00:00:00Z"]
    assert w["status"] == "sealed"
    assert (w["count"], w["sum"]) == (2, 6.0)
    assert w["correction_count"] == 1
    assert w["late_event_count"] == 1

    corrections = client.get("/devices/d1/corrections").json()
    assert len(corrections) == 1
    assert corrections[0]["event_id"] == "e3"
    assert corrections[0]["window_start"] == "2026-09-22T00:00:00Z"
    assert corrections[0]["value"] == 5.0


def test_event_within_new_lateness_after_shorten_is_not_late(client):
    seed_open_windows(client)
    # 30 → 25：没有窗口被封存（见上），落入 [00:01,00:02) 的事件仍 accepted
    assert put_config(client, "d1", 60, 25).status_code == 200
    r = ingest(client, "d1", [{"event_id": "e3", "event_time": "2026-09-22T00:01:40Z", "value": 4.0}])
    assert r.json()["results"][0]["status"] == "accepted"
    w = windows_by_start(client, "d1")["2026-09-22T00:01:00Z"]
    assert w["status"] == "open"
    assert w["correction_count"] == 0
