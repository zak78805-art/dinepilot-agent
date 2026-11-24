"""第二阶段只读工具测试：订单状态概览、订单列表、订单详情、菜品目录、用户统计。"""

import asyncio

import pytest

from app.clients.sky_backend_client import SkyBackendClient, current_backend_token
from app.tools import order_tools, dish_tools, user_tools


def run(coro):
    return asyncio.run(coro)


def _with_token(coro):
    async def inner():
        token = current_backend_token.set('test-token')
        try:
            return await coro
        finally:
            current_backend_token.reset(token)
    return run(inner())


# ---------- 订单状态概览 ----------

def test_order_status_overview_maps_backend_fields(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/admin/order/statistics'
        assert self.token == 'test-token'
        return {'code': 1, 'data': {'toBeConfirmed': 2, 'confirmed': 3, 'deliveryInProgress': 4}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_status_overview())
    assert result['to_be_confirmed'] == 2
    assert result['confirmed'] == 3
    assert result['delivery_in_progress'] == 4
    assert result['statistics_scope']['status_definitions']


def test_order_status_overview_rejects_bad_payload(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': None}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        _with_token(order_tools.get_order_status_overview())


# ---------- 订单列表 ----------

def test_order_list_builds_datetime_range_and_maps_status(monkeypatch):
    seen = {}

    async def get(self, path, params=None):
        seen['path'] = path
        seen['params'] = params
        return {'code': 1, 'data': {
            'total': 7,
            'records': [
                {'id': 7, 'number': '1785838137567', 'status': 6, 'amount': 25.0,
                 'orderTime': '2026-08-04 18:08', 'phone': '13633323323',
                 'address': '212', 'cancelReason': '骑手不足无法配送'},
            ],
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_list(status=6, begin='2026-08-01', end='2026-08-05'))

    assert seen['path'] == '/admin/order/conditionSearch'
    # 后端字段是 LocalDateTime，必须带时分秒才能覆盖整天。
    assert seen['params']['beginTime'] == '2026-08-01 00:00:00'
    assert seen['params']['endTime'] == '2026-08-05 23:59:59'
    assert seen['params']['status'] == 6
    assert result['orders'][0]['status_name'] == '已取消'
    assert result['orders'][0]['cancel_reason'] == '骑手不足无法配送'
    assert result['total'] == 7 and result['returned'] == 1
    assert result['statistics_scope']['truncated'] is True


def test_order_list_without_date_omits_time_params(monkeypatch):
    seen = {}

    async def get(self, path, params=None):
        seen['params'] = params
        return {'code': 1, 'data': {'total': 0, 'records': []}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_list())
    assert 'beginTime' not in seen['params']
    assert 'endTime' not in seen['params']
    assert 'status' not in seen['params']
    assert result['statistics_scope']['time_scope' if False else 'range'] is None


@pytest.mark.parametrize('kwargs', [
    {'status': 99},
    {'limit': 0},
    {'limit': 101},
    {'begin': '2026-08-01'},
    {'end': '2026-08-05'},
])
def test_order_list_validates_arguments(kwargs):
    with pytest.raises(ValueError):
        _with_token(order_tools.get_order_list(**kwargs))


def test_order_list_passes_number_and_phone_filters(monkeypatch):
    seen = {}

    async def get(self, path, params=None):
        seen['params'] = params
        return {'code': 1, 'data': {'total': 1, 'records': [
            {'id': 7, 'number': '1785838137567', 'status': 6, 'amount': 25.0},
        ]}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_list(number=' 1785838137567 ', phone='136'))
    assert seen['params']['number'] == '1785838137567'
    assert seen['params']['phone'] == '136'
    assert result['statistics_scope']['number_filter'] == '1785838137567'
    assert result['statistics_scope']['number_match'].startswith('number 与 phone 均为模糊匹配')


def test_order_list_rejects_bad_records(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'total': 1, 'records': 'oops'}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        _with_token(order_tools.get_order_list())


# ---------- 订单详情 ----------

def test_order_detail_maps_cancel_reason_and_dishes(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/admin/order/details/7'
        return {'code': 1, 'data': {
            'id': 7, 'number': '1785838137567', 'status': 6, 'amount': 25.0,
            'orderTime': '2026-08-04 18:08', 'cancelReason': '骑手不足无法配送',
            'cancelTime': '2026-08-04 18:09', 'rejectionReason': None,
            'orderDetailList': [{'name': '鲍鱼', 'number': 1, 'amount': 25.0}],
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_detail(7))
    assert result['status_name'] == '已取消'
    assert result['cancel_reason'] == '骑手不足无法配送'
    assert result['dishes'] == [{'name': '鲍鱼', 'number': 1, 'amount': 25.0}]
    assert result['statistics_scope']['cancel_reason_available'] is True
    assert result['statistics_scope']['rejection_reason_available'] is False


def test_order_detail_empty_cancel_reason_stays_none(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'id': 5, 'status': 5, 'cancelReason': '',
                                    'orderDetailList': []}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_detail(5))
    assert result['cancel_reason'] is None
    assert result['statistics_scope']['cancel_reason_available'] is False


@pytest.mark.parametrize('order_id', [0, -1])
def test_order_detail_rejects_invalid_id(order_id):
    with pytest.raises(ValueError):
        _with_token(order_tools.get_order_detail(order_id))


def test_order_detail_resolves_id_from_order_number(monkeypatch):
    """用户口语的「订单号」是 number，不是主键；工具应自动反查主键。"""
    calls = []

    async def get(self, path, params=None):
        calls.append((path, params))
        if path == '/admin/order/conditionSearch':
            assert params['number'] == '1785838137567'
            return {'code': 1, 'data': {'total': 1, 'records': [
                {'id': 7, 'number': '1785838137567'},
            ]}}
        assert path == '/admin/order/details/7'
        return {'code': 1, 'data': {'id': 7, 'number': '1785838137567', 'status': 6,
                                    'cancelReason': '骑手不足无法配送',
                                    'orderDetailList': [{'name': '鲍鱼', 'number': 1, 'amount': 25.0}]}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(order_tools.get_order_detail(order_number='1785838137567'))
    assert result['id'] == 7
    assert result['cancel_reason'] == '骑手不足无法配送'
    assert result['statistics_scope']['resolved_from'] == 'order_number'
    assert len(calls) == 2


def test_order_detail_order_number_not_found(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'total': 0, 'records': []}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    with pytest.raises(ValueError, match='未找到订单号'):
        _with_token(order_tools.get_order_detail(order_number='9999999999999'))


def test_order_detail_requires_some_identifier():
    with pytest.raises(ValueError, match='至少要提供一个'):
        _with_token(order_tools.get_order_detail())


def test_order_detail_rejects_missing_data(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': None}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        _with_token(order_tools.get_order_detail(7))


# ---------- 菜品目录 ----------

def test_dish_catalog_maps_status_and_category(monkeypatch):
    seen = {}

    async def get(self, path, params=None):
        seen['path'] = path
        seen['params'] = params
        return {'code': 1, 'data': {
            'total': 24,
            'records': [
                {'id': 57, 'name': '炝炒圆白菜', 'categoryName': '新鲜时蔬',
                 'price': 18.0, 'status': 0},
            ],
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(dish_tools.get_dish_catalog(status=0))
    assert seen['path'] == '/admin/dish/page'
    assert seen['params']['status'] == 0
    assert result['dishes'][0]['status_name'] == '停售'
    assert result['dishes'][0]['category'] == '新鲜时蔬'
    assert result['statistics_scope']['truncated'] is True


def test_dish_catalog_without_status_queries_all(monkeypatch):
    seen = {}

    async def get(self, path, params=None):
        seen['params'] = params
        return {'code': 1, 'data': {'total': 0, 'records': []}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(dish_tools.get_dish_catalog())
    assert 'status' not in seen['params']
    assert result['statistics_scope']['status_filter'] is None


@pytest.mark.parametrize('kwargs', [{'status': 2}, {'limit': 0}, {'limit': 101}])
def test_dish_catalog_validates_arguments(kwargs):
    with pytest.raises(ValueError):
        _with_token(dish_tools.get_dish_catalog(**kwargs))


# ---------- 用户统计 ----------

def test_user_statistics_sums_new_users(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/admin/report/userStatistics'
        assert params == {'begin': '2026-08-01', 'end': '2026-08-07'}
        return {'code': 1, 'data': {
            'dateList': '2026-08-01,2026-08-02',
            'totalUserList': '0,1',
            'newUserList': '0,1',
        }}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    result = _with_token(user_tools.get_user_statistics('2026-08-01', '2026-08-07'))
    assert result['daily'] == [
        {'date': '2026-08-01', 'total_users': 0, 'new_users': 0},
        {'date': '2026-08-02', 'total_users': 1, 'new_users': 1},
    ]
    assert result['new_users_total'] == 1


def test_user_statistics_rejects_mismatched_lengths(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'dateList': '2026-08-01', 'totalUserList': '1,2',
                                    'newUserList': '1'}}

    monkeypatch.setattr(SkyBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        _with_token(user_tools.get_user_statistics('2026-08-01', '2026-08-07'))


def test_user_statistics_rejects_future_dates(monkeypatch):
    with pytest.raises(ValueError):
        _with_token(user_tools.get_user_statistics('2999-01-01', '2999-01-02'))


# ---------- 工具注册约束 ----------

def test_all_registered_tools_are_read_only(monkeypatch):
    """本阶段只允许只读工具；任何写工具混入都应让测试失败。"""
    from types import SimpleNamespace

    import app.agent.merchant_agent as merchant_agent

    monkeypatch.setattr(
        merchant_agent,
        'get_settings',
        lambda: SimpleNamespace(
            dashscope_api_key='test-key',
            dashscope_base_url='http://127.0.0.1:1/v1',
            agent_model='test-model',
            agent_session_idle_seconds=1800, agent_session_max_entries=500,
        ),
    )

    async def collect():
        agent = merchant_agent.MerchantAgent()._create_agent()
        toolkit = agent.toolkit
        schemas = await toolkit.get_tool_schemas()
        result = []
        for schema in schemas:
            name = schema.get('function', schema).get('name')
            tool = toolkit.get_tool(name)
            if hasattr(tool, '__await__'):
                tool = await tool
            result.append((name, tool.is_read_only))
        return result

    pairs = run(collect())
    assert len(pairs) == 11
    not_read_only = [name for name, read_only in pairs if not read_only]
    assert not not_read_only, f'发现非只读工具: {not_read_only}'
