#!/usr/bin/env python3
"""站内通知功能调用示例（纯标准库，无需第三方依赖）。

覆盖：
1) 开封通知：资料开封后，封存人与三名见证人各收到一条 opened 通知；
2) 见证人替换通知：封存人、旧见证人、新见证人（及另两名见证人）各收到
   一条 witness_replaced 通知——旧见证人被移出名单仍固定收到；
3) 交接确认通知：新旧封存人与三名见证人各收到一条 handover_confirmed；
4) 到期清除通知：到点清除后，封存人与三名见证人各收到一条 purged；
5) 收件箱：非负事件序号游标 + 1～100 页大小分页读取，含 next_cursor；
6) 标记已读：按通知编号幂等标记并返回已读状态；他人通知编号统一 403；
7) 失去资料权限（交接后的旧封存人、被替换的旧见证人）仍可读自己的通知，
   但通知不含正文/凭证/密钥，也不能借此访问资料。

用法: python examples/demo_notifications.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def list_all(key: str) -> list[dict]:
    """用游标分页拉完本人全部通知，按事件序号升序返回。"""
    out, cursor = [], 0
    while True:
        _, page = call("GET", f"/notifications?limit=100&cursor={cursor}", key)
        out.extend(page["notifications"])
        if page["next_cursor"] is None:
            return out
        cursor = page["next_cursor"]


def types(key: str) -> list[str]:
    return [n["event_type"] for n in list_all(key)]


def main() -> None:
    users = {}
    for name in ["alice", "bob", "carol", "dave", "erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin = (users[n] for n in
                                     ["alice", "bob", "carol", "dave", "erin"])

    # ------------------------------------------------------------------
    # 1) 开封 -> 封存人 + 三名见证人各一条 opened
    # ------------------------------------------------------------------
    print("== 1) 开封通知 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "通知示例正文：NOTIFY-DEMO-SECRET",
                      "witnesses": ["bob", "carol", "dave"]})
    doc1, digest1 = sealed["document_id"], sealed["digest"]
    call("POST", f"/documents/{doc1}/open-requests", alice,
         {"reason": "家庭会议", "deadline": in_seconds(3600)})
    call("POST", "/open-requests/1/approve", bob, {"digest": digest1})
    call("POST", "/open-requests/1/approve", carol, {"digest": digest1})

    _, inbox = call("GET", "/notifications?limit=20", alice)
    show("alice 的通知", 200, inbox)
    note = inbox["notifications"][0]
    assert note["event_type"] == "opened" and note["document_id"] == doc1
    assert note["read"] is False and "body" not in json.dumps(note)
    for name, key in [("bob", bob), ("carol", carol), ("dave", dave)]:
        assert types(key) == ["opened"], name
    assert types(erin) == []  # 非参与方无通知
    print("   开封通知：alice/bob/carol/dave 各 1 条，erin 0 条")

    # ------------------------------------------------------------------
    # 2) 见证人替换 -> 封存人 + 旧/新见证人
    # ------------------------------------------------------------------
    print("== 2) 见证人替换通知 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "替换通知正文", "witnesses": ["bob", "carol", "dave"]})
    doc2 = sealed["document_id"]
    status, resp = call("POST", f"/documents/{doc2}/witness-replacements", alice,
                        {"old_witness": "dave", "new_witness": "erin"})
    show("dave -> erin", status, resp)
    assert status == 200
    # 旧见证人 dave 已不在名单，仍固定收到一条
    assert types(dave).count("witness_replaced") == 1
    assert types(erin) == ["witness_replaced"]
    assert types(alice).count("witness_replaced") == 1
    print("   替换通知：alice、bob、carol、erin（新）各 1 条，dave（旧）额外保留 1 条")

    # ------------------------------------------------------------------
    # 3) 交接确认 -> 新旧封存人 + 三名见证人
    # ------------------------------------------------------------------
    print("== 3) 交接确认通知 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "交接通知正文", "witnesses": ["bob", "carol", "dave"]})
    doc3 = sealed["document_id"]
    _, inv = call("POST", f"/documents/{doc3}/handover-invitations", alice,
                  {"to_username": "erin", "confirm_deadline": in_seconds(3600)})
    status, resp = call("POST", f"/handover-invitations/{inv['invitation_id']}/confirm", erin)
    show("erin 确认交接", status, resp)
    assert status == 200
    assert types(alice).count("handover_confirmed") == 1
    assert types(erin).count("handover_confirmed") == 1
    for key in (bob, carol, dave):
        assert types(key).count("handover_confirmed") == 1

    # 旧封存人 alice 失去资料权限，但仍可读自己收到的通知
    s, _ = call("GET", f"/documents/{doc3}", alice)
    print(f"-- 交接后 alice 访问资料 -> {s}（403 表示权限已收回）")
    assert s == 403
    s, _ = call("GET", "/notifications", alice)
    assert s == 200  # 通知仍可读

    # ------------------------------------------------------------------
    # 4) 到期清除 -> 封存人 + 三名见证人各一条 purged
    # ------------------------------------------------------------------
    print("== 4) 到期清除通知（安排 2 秒后清除）==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除通知正文", "witnesses": ["bob", "carol", "dave"]})
    doc4 = sealed["document_id"]
    call("PUT", f"/documents/{doc4}/purge", alice, {"purge_at": in_seconds(2)})
    print("   等待清除时刻到达（扫描器/读请求会确定性结算）……")
    time.sleep(2.5)
    call("GET", f"/documents/{doc4}", alice)  # 触发确定性到点清除
    assert types(alice).count("purged") == 1
    for key in (bob, carol, dave):
        assert types(key).count("purged") == 1
    print("   清除通知：alice/bob/carol/dave 各 1 条，且同一变化不重复投递")

    # ------------------------------------------------------------------
    # 5) 游标分页
    # ------------------------------------------------------------------
    print("== 5) 游标分页 ==")
    _, page1 = call("GET", "/notifications?limit=2&cursor=0", bob)
    show("bob 第 1 页（limit=2）", 200, page1)
    assert len(page1["notifications"]) == 2
    _, page2 = call("GET", f"/notifications?limit=2&cursor={page1['next_cursor']}", bob)
    show("bob 第 2 页", 200, page2)
    assert all(n["event_seq"] > page1["next_cursor"] for n in page2["notifications"])
    # 非法游标 / 页大小 -> 422
    assert call("GET", "/notifications?cursor=-1", bob)[0] == 422
    assert call("GET", "/notifications?limit=101", bob)[0] == 422

    # ------------------------------------------------------------------
    # 6) 标记已读（幂等）+ 他人编号 403
    # ------------------------------------------------------------------
    print("== 6) 标记已读 ==")
    nid = page1["notifications"][0]["notification_id"]
    status, r1 = call("POST", f"/notifications/{nid}/read", bob)
    show("bob 标记已读", status, r1)
    assert r1["read"] is True and r1["read_at"]
    status, r2 = call("POST", f"/notifications/{nid}/read", bob)
    show("bob 重复标记（幂等，read_at 不变）", status, r2)
    assert r2["read_at"] == r1["read_at"]
    # 他人通知编号 / 不存在编号统一 403，不改状态
    assert call("POST", f"/notifications/{nid}/read", carol)[0] == 403
    assert call("POST", "/notifications/999999/read", carol)[0] == 403
    print("   已读重试不产生额外记录；他人/不存在编号统一 403")

    print("\n全部通知示例断言通过。")


if __name__ == "__main__":
    main()
