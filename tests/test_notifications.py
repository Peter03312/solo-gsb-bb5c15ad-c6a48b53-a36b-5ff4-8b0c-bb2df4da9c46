"""站内通知功能测试。

规则要点（规格）：
- 资料开封或到期清除时，向当时封存人及三名见证人各记一条通知；
- 见证人替换时通知封存人和新旧见证人（被替换出名单的旧见证人也固定收到）；
- 交接确认时通知新旧封存人及三名见证人；
- 收件人在状态变更的同一事务内固定，通知按事件序号（event_seq）排列，
  含资料编号、事件类型、时刻与已读状态，不含正文、凭证或密钥；
- 账号凭现有 X-API-Key 以非负事件序号游标与 1～100 页大小读取本人通知及
  下一游标，并按通知编号标记已读、返回已读状态；
- 非法游标、页大小或他人通知编号一律拒绝且不改状态；
- 失去资料权限的人仍可读自己收到的通知，但不能借此访问资料；
- 失败或回滚的操作不留通知；扫描器与请求并发结算同一变化不重复投递；
- 已读重试幂等，不产生额外记录。
"""
import datetime as dt
import threading
import time

import pytest

from conftest import auth, register, seal

BODY = "通知绝密资料 NOTIFICATION-SECRET-BODY"


def _in(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def users(client):
    keys = {n: register(client, n) for n in
            ("owner", "w1", "w2", "w3", "w4", "nextowner")}
    return keys


def _seal_open(client, keys, *, earliest=None):
    d = seal(client, keys["owner"], BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]
    payload = {"reason": "通知测试", "deadline": _in(3600)}
    if earliest is not None:
        payload["earliest_open_at"] = _in(earliest)
    r = client.post(f"/documents/{doc_id}/open-requests", json=payload,
                    headers=auth(keys["owner"]))
    assert r.status_code == 201, r.text
    req_id = r.json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                headers=auth(keys["w1"]))
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                    headers=auth(keys["w2"]))
    return doc_id, dg, req_id, r.json()["status"]


def _notifs(client, key, **params):
    return client.get("/notifications", params=params, headers=auth(key)).json()["notifications"]


def _types(client, key, **params):
    return [n["event_type"] for n in _notifs(client, key, **params)]


# ---------------------------------------------------------------------------
# 开封 / 清除：当时封存人 + 三名见证人各一条
# ---------------------------------------------------------------------------


def test_open_notifies_owner_and_three_witnesses(client, users):
    doc_id, _, _, status = _seal_open(client, users)
    assert status == "opened"

    for name in ("owner", "w1", "w2", "w3"):
        ns = _notifs(client, users[name])
        assert len(ns) == 1
        n = ns[0]
        assert n["event_type"] == "opened"
        assert n["document_id"] == doc_id
        assert n["read"] is False and n["read_at"] is None
        assert n["event_seq"] > 0 and n["event_at"]
        # 通知只含规定字段，不含正文/凭证/密钥
        assert set(n) == {
            "notification_id", "document_id", "event_seq",
            "event_type", "event_at", "read", "read_at",
        }
        assert BODY not in str(n)

    # 与资料无关的账号收不到任何通知
    assert _notifs(client, users["w4"]) == []
    assert _notifs(client, users["nextowner"]) == []


def test_open_notification_event_seq_matches_audit_and_is_ordered(client, users):
    doc_id, _, _, _ = _seal_open(client, users)
    audit = client.get(f"/documents/{doc_id}/audit",
                       headers=auth(users["owner"])).json()["records"]
    opened_seq = next(r["id"] for r in audit if r["action"] == "opened")
    n = _notifs(client, users["owner"])[0]
    assert n["event_seq"] == opened_seq
    assert n["event_at"] == next(r["at"] for r in audit if r["id"] == opened_seq)

    # 再制造一个事件（替换见证人），通知按 event_seq 升序
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    client.post(f"/documents/{d['document_id']}/witness-replacements",
                json={"old_witness": "w3", "new_witness": "w4"},
                headers=auth(users["owner"]))
    seq = [n["event_seq"] for n in _notifs(client, users["owner"])]
    assert seq == sorted(seq)


def test_purge_notifies_owner_and_witnesses_via_read_settlement(client, users):
    # 未开封资料安排到点清除，读路径在写事务内确定性结算后投递
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    r = client.put(f"/documents/{doc_id}/purge", json={"purge_at": _in(1)},
                   headers=auth(users["owner"]))
    assert r.status_code == 200, r.text
    time.sleep(1.3)
    got = client.get(f"/documents/{doc_id}", headers=auth(users["owner"])).json()
    assert got["status"] == "purged"

    for name in ("owner", "w1", "w2", "w3"):
        assert _types(client, users[name]).count("purged") == 1
    assert _types(client, users["w4"]) == []


def test_purge_by_sweeper_delivers_exactly_once(client, users, sweep):
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    client.put(f"/documents/{doc_id}/purge", json={"purge_at": _in(1)},
               headers=auth(users["owner"]))
    time.sleep(1.2)
    assert sweep() >= 0  # 后台/手动扫描结算
    # 再扫一轮也不重复投递（状态守卫 + UNIQUE(event_seq, recipient_id) 兜底）
    sweep()
    for name in ("owner", "w1", "w2", "w3"):
        assert _types(client, users[name]).count("purged") == 1


# ---------------------------------------------------------------------------
# 见证人替换：通知封存人 + 新旧见证人（其余两名当前见证人也收到）
# ---------------------------------------------------------------------------


def test_witness_replacement_notifies_owner_and_both_old_and_new(client, users):
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    r = client.post(f"/documents/{doc_id}/witness-replacements",
                    json={"old_witness": "w3", "new_witness": "w4"},
                    headers=auth(users["owner"]))
    assert r.status_code == 200, r.text

    owner = _notifs(client, users["owner"])
    assert [n["event_type"] for n in owner] == ["witness_replaced"]
    # 旧见证人 w3 已被移出名单仍固定收到
    old = _notifs(client, users["w3"])
    assert [n["event_type"] for n in old] == ["witness_replaced"]
    # 新见证人 w4 收到
    new = _notifs(client, users["w4"])
    assert [n["event_type"] for n in new] == ["witness_replaced"]
    # 其余两名见证人也收到（“封存人 + 当前三名见证人”）
    for name in ("w1", "w2"):
        assert _types(client, users[name]) == ["witness_replaced"]
    for n in owner + old + new:
        assert n["document_id"] == doc_id and n["read"] is False


def test_handover_confirmation_notifies_both_owners_and_witnesses(client, users):
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    r = client.post(f"/documents/{doc_id}/handover-invitations",
                    json={"to_username": "nextowner", "confirm_deadline": _in(3600)},
                    headers=auth(users["owner"]))
    assert r.status_code == 201, r.text
    inv_id = r.json()["invitation_id"]
    r = client.post(f"/handover-invitations/{inv_id}/confirm",
                    headers=auth(users["nextowner"]))
    assert r.status_code == 200, r.text

    # 旧封存人、新封存人、三名见证人各一条 handover_confirmed
    for name in ("owner", "nextowner", "w1", "w2", "w3"):
        assert _types(client, users[name]) == ["handover_confirmed"]
    assert _notifs(client, users["w4"]) == []


def test_lost_permission_still_reads_notifications_but_not_document(client, users):
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    # 交接后旧封存人失去资料权限
    inv = client.post(f"/documents/{doc_id}/handover-invitations",
                      json={"to_username": "nextowner", "confirm_deadline": _in(3600)},
                      headers=auth(users["owner"])).json()
    client.post(f"/handover-invitations/{inv['invitation_id']}/confirm",
                headers=auth(users["nextowner"]))
    assert client.get(f"/documents/{doc_id}", headers=auth(users["owner"])).status_code == 403
    # 但仍可读自己收到的通知
    ns = _notifs(client, users["owner"])
    assert [n["event_type"] for n in ns] == ["handover_confirmed"]
    assert BODY not in str(ns)

    # 被替换的旧见证人同样：失去资料权限但保留通知
    d2 = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    client.post(f"/documents/{d2['document_id']}/witness-replacements",
                json={"old_witness": "w3", "new_witness": "w4"},
                headers=auth(users["owner"]))
    assert client.get(f"/documents/{d2['document_id']}",
                      headers=auth(users["w3"])).status_code == 403
    types = _types(client, users["w3"])
    assert "witness_replaced" in types


# ---------------------------------------------------------------------------
# 游标分页与参数校验
# ---------------------------------------------------------------------------


def test_notification_pagination_cursor_and_page_size(client, users):
    # 为 owner 制造 5 个事件（每份资料一次替换见证人）
    for _ in range(5):
        d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
        r = client.post(f"/documents/{d['document_id']}/witness-replacements",
                        json={"old_witness": "w3", "new_witness": "w4"},
                        headers=auth(users["owner"]))
        assert r.status_code == 200

    first = client.get("/notifications?limit=2", headers=auth(users["owner"])).json()
    assert len(first["notifications"]) == 2
    assert first["next_cursor"] == first["notifications"][-1]["event_seq"]

    second = client.get("/notifications",
                        params={"limit": 2, "cursor": first["next_cursor"]},
                        headers=auth(users["owner"])).json()
    assert len(second["notifications"]) == 2
    assert all(n["event_seq"] > first["next_cursor"] for n in second["notifications"])

    # 走完全部分页，条目无重复无遗漏、顺序单调
    seen = []
    cursor = 0
    while True:
        page = client.get("/notifications", params={"limit": 2, "cursor": cursor},
                          headers=auth(users["owner"])).json()
        seen.extend(n["event_seq"] for n in page["notifications"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert seen == sorted(seen)
    full = client.get("/notifications?limit=100", headers=auth(users["owner"])).json()
    assert seen == [n["event_seq"] for n in full["notifications"]]
    assert full["next_cursor"] is None


def test_notification_query_validation_rejected(client, users):
    # 非法游标（负数/非整数）与越界页大小 -> 422，且不改状态
    assert client.get("/notifications?cursor=-1", headers=auth(users["owner"])).status_code == 422
    assert client.get("/notifications?cursor=x", headers=auth(users["owner"])).status_code == 422
    assert client.get("/notifications?limit=0", headers=auth(users["owner"])).status_code == 422
    assert client.get("/notifications?limit=101", headers=auth(users["owner"])).status_code == 422
    assert client.get("/notifications?limit=x", headers=auth(users["owner"])).status_code == 422
    # 无凭据 401
    assert client.get("/notifications").status_code == 401


# ---------------------------------------------------------------------------
# 标记已读：本人、幂等、他人/不存在 403
# ---------------------------------------------------------------------------


def test_mark_read_returns_state_and_is_idempotent(client, users):
    _seal_open(client, users)
    n = _notifs(client, users["owner"])[0]
    assert n["read"] is False

    r = client.post(f"/notifications/{n['notification_id']}/read",
                    headers=auth(users["owner"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read"] is True and body["read_at"]
    assert body["notification_id"] == n["notification_id"]

    # 列表中的已读状态同步
    again = _notifs(client, users["owner"])[0]
    assert again["read"] is True and again["read_at"] == body["read_at"]

    # 重试幂等：保留首次已读时刻，不产生额外记录
    r2 = client.post(f"/notifications/{n['notification_id']}/read",
                     headers=auth(users["owner"]))
    assert r2.status_code == 200
    assert r2.json()["read"] is True
    assert r2.json()["read_at"] == body["read_at"]


def test_mark_read_rejects_others_and_missing_without_state_change(client, users):
    _seal_open(client, users)
    n = _notifs(client, users["owner"])[0]

    # 他人通知编号：统一 403（与不存在不可区分），不改已读状态
    assert client.post(f"/notifications/{n['notification_id']}/read",
                       headers=auth(users["w1"])).status_code == 403
    # 编号不存在：同样 403
    assert client.post("/notifications/999999/read",
                       headers=auth(users["w1"])).status_code == 403
    # 无凭据 401
    assert client.post(f"/notifications/{n['notification_id']}/read").status_code == 401

    # 状态未被改变：owner 侧仍未读
    assert _notifs(client, users["owner"])[0]["read"] is False


# ---------------------------------------------------------------------------
# 失败 / 回滚不留通知
# ---------------------------------------------------------------------------


def test_failed_operations_leave_no_notification(client, users):
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]

    def counts():
        return {name: len(_notifs(client, key)) for name, key in users.items()}

    before = counts()
    # 错误摘要的同意（409）——不开封，不留通知
    r = client.post(f"/documents/{doc_id}/open-requests",
                    json={"reason": "r", "deadline": _in(3600)},
                    headers=auth(users["owner"]))
    req_id = r.json()["request_id"]
    assert client.post(f"/open-requests/{req_id}/approve", json={"digest": "deadbeef"},
                       headers=auth(users["w1"])).status_code == 409
    # 非封存人替换（403）、新见证人未注册（400）
    assert client.post(f"/documents/{doc_id}/witness-replacements",
                       json={"old_witness": "w3", "new_witness": "w4"},
                       headers=auth(users["w1"])).status_code == 403
    assert client.post(f"/documents/{doc_id}/witness-replacements",
                       json={"old_witness": "w3", "new_witness": "ghost"},
                       headers=auth(users["owner"])).status_code == 400
    # 交接受让人错误（400）、非受让人确认（403）
    assert client.post(f"/documents/{doc_id}/handover-invitations",
                       json={"to_username": "ghost", "confirm_deadline": _in(3600)},
                       headers=auth(users["owner"])).status_code == 400
    assert client.post("/handover-invitations/999/confirm",
                       headers=auth(users["nextowner"])).status_code == 403
    # 过期时刻的清除安排（400）
    assert client.put(f"/documents/{doc_id}/purge", json={"purge_at": _in(-10)},
                      headers=auth(users["owner"])).status_code == 400

    assert counts() == before


# ---------------------------------------------------------------------------
# 并发：扫描器与请求结算同一变化不重复投递
# ---------------------------------------------------------------------------


def test_concurrent_read_settlement_purge_delivers_once(client, users):
    # 资料清除时刻已到，多个参与方并发查询（每个查询都在写事务内确定性
    # 结算同一清除）：只有一个事务真正完成清除并投递通知。
    d = seal(client, users["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    assert client.put(f"/documents/{doc_id}/purge", json={"purge_at": _in(1)},
                      headers=auth(users["owner"])).status_code == 200
    time.sleep(1.2)

    barrier = threading.Barrier(4)
    results = []

    def read(key):
        barrier.wait()
        results.append(client.get(f"/documents/{doc_id}", headers=auth(key)).status_code)

    threads = [threading.Thread(target=read, args=(users[n],))
               for n in ("owner", "w1", "w2", "w3")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200, 200, 200]

    for name in ("owner", "w1", "w2", "w3"):
        assert _types(client, users[name]).count("purged") == 1
