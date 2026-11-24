import asyncio

from app.clients.sky_backend_client import SkyBackendClient
from app.tools import sales_tools


def test_admin_token_header_name() -> None:
    client = SkyBackendClient.__new__(SkyBackendClient)
    client.token = "test-token"
    assert client._headers() == {"token": "test-token"}


def test_get_today_sales_extracts_java_result(monkeypatch) -> None:
    expected = {
        "turnover": 1288.0,
        "validOrderCount": 20,
        "orderCompletionRate": 0.8,
        "unitPrice": 64.4,
        "newUsers": 3,
    }

    class FakeClient:
        async def get(self, path: str) -> dict:
            assert path == "/admin/workspace/businessData"
            return {"code": 1, "data": expected}

    monkeypatch.setattr(sales_tools, "SkyBackendClient", FakeClient)
    result = asyncio.run(sales_tools.get_today_sales())
    for key, value in expected.items():
        assert result[key] == value
    scope = result['statistics_scope']
    assert scope['time_field'] == 'orders.order_time'
    assert scope['turnover_status_filter'] == 5
    assert scope['valid_orders_status_filter'] == 5
    assert scope['other_status_breakdown_available'] is False
    assert 'statistics_scope' not in expected  # 不修改后端原始响应对象。


def test_business_error_is_rejected() -> None:
    try:
        SkyBackendClient._raise_for_business_error({"code": 0, "msg": "失败"})
    except RuntimeError as exc:
        assert str(exc) == "失败"
    else:
        raise AssertionError("业务错误必须抛出异常")
