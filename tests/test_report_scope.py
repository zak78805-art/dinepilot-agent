import asyncio

import pytest

from app.clients.sky_backend_client import SkyBackendClient
from app.tools import dish_tools, order_tools, sales_tools


@pytest.mark.parametrize('empty', [False, True])
def test_turnover_exposes_completed_order_scope_without_changing_amounts(monkeypatch, empty):
    async def get(self, path, params=None):
        assert path == '/admin/report/turnoverStatistics'
        return {'code': 1, 'data': {
            'dateList': '2026-08-04', 'turnoverList': '0' if empty else '158',
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = asyncio.run(sales_tools.get_turnover_statistics('2026-08-04', '2026-08-04'))
    assert result['daily'] == [{'date': '2026-08-04', 'turnover': 0.0 if empty else 158.0}]
    scope = result['statistics_scope']
    assert scope['time_field'] == 'orders.order_time'
    assert scope['status_filter'] == 5
    assert scope['aggregation'] == 'sum(orders.amount)'
    assert scope['currency'] == 'CNY'


@pytest.mark.parametrize('empty', [False, True])
def test_order_report_distinguishes_not_completed_from_cancelled(monkeypatch, empty):
    async def get(self, path, params=None):
        assert path == '/admin/report/ordersStatistics'
        return {'code': 1, 'data': {
            'dateList': '2026-08-04', 'orderCountList': '0' if empty else '4',
            'validOrderCountList': '0' if empty else '2',
            'totalOrderCount': 0 if empty else 4, 'validOrderCount': 0 if empty else 2,
            'orderCompletionRate': 0 if empty else 0.5,
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = asyncio.run(order_tools.get_order_statistics('2026-08-04', '2026-08-04'))
    assert result['completion_rate'] == (0 if empty else 0.5)
    scope = result['statistics_scope']
    assert scope['time_field'] == 'orders.order_time'
    assert scope['total_orders_status_filter'] is None
    assert scope['valid_orders_status_filter'] == 5
    assert scope['completion_rate_formula'] == 'valid_orders / total_orders'
    assert scope['zero_total_orders_has_meaningful_rate'] is False
    # 第二阶段新增 get_order_list 后，状态明细由该工具提供，此处应指向它。
    assert scope['other_status_breakdown_available'] is True
    assert scope['other_status_breakdown_tool'] == 'get_order_list'


@pytest.mark.parametrize('empty', [False, True])
def test_top_sales_exposes_completed_scope_and_ranking_limits(monkeypatch, empty):
    async def get(self, path, params=None):
        assert path == '/admin/report/top10'
        return {'code': 1, 'data': {
            'nameList': '' if empty else '鮰鱼2斤', 'numberList': '' if empty else '2',
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = asyncio.run(dish_tools.get_top_dishes('2026-08-04', '2026-08-04'))
    assert result['ranking'] == ([] if empty else [{'name': '鮰鱼2斤', 'sales': 2}])
    scope = result['statistics_scope']
    assert scope['time_field'] == 'orders.order_time'
    assert scope['status_filter'] == 5
    assert scope['aggregation'] == 'sum(order_detail.number)'
    assert scope['group_by'] == 'order_detail.name'
    assert scope['max_results'] == 10
    assert scope['catalog_coverage_available'] is False
