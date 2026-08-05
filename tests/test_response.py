from common.response import ok


def test_ok_envelope() -> None:
    assert ok({"a": 1}) == {"code": 0, "msg": "ok", "data": {"a": 1}}
