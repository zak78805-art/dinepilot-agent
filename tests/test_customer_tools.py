import asyncio
import pytest

from app.clients.customer_backend_client import CustomerBackendClient, current_customer_id
from app.tools import customer_tools as tools


def run(coro):
    identity = current_customer_id.set(12)
    try:
        return asyncio.run(coro)
    finally:
        current_customer_id.reset(identity)


def test_menu_filters_sale_status_price_and_minimizes_fields(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/user/dish/list' and params == {'categoryId': 3}
        return {'code': 1, 'data': [
            {'id': 1, 'name': '菜A', 'price': '18.50', 'status': 1, 'categoryId': 3, 'createUser': 9},
            {'id': 2, 'name': '停售', 'price': 1, 'status': 0},
            {'id': 3, 'name': '超预算', 'price': 100, 'status': 1, 'categoryId': 3},
        ]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    result = run(tools.get_customer_menu(3, max_price=20))
    assert len(result['items']) == 1
    assert result['items'][0]['price'] == 18.5
    assert 'createUser' not in result['items'][0]
    assert result['scope']['fees_included'] is False
    assert result['scope']['allergens_verified'] is False


def test_setmeal_menu_uses_public_customer_endpoint(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/user/setmeal/list'
        return {'code': 1, 'data': [{'id': 7, 'name': '套餐', 'price': 30, 'status': 1, 'categoryId': 2}]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    assert run(tools.get_customer_menu(2, item_type='setmeal'))['items'][0]['id'] == 7


@pytest.mark.parametrize('kwargs', [{'category_id': True}, {'category_id': -1},
                                    {'category_id': 3, 'max_price': float('nan')},
                                    {'category_id': 3, 'max_price': -1},
                                    {'category_id': 3, 'item_type': 'admin'},
                                    {'category_id': 3, 'limit': 101}])
def test_bad_menu_arguments_fail_without_network(kwargs):
    with pytest.raises(ValueError):
        run(tools.get_customer_menu(**kwargs))


def test_invalid_backend_price_is_not_silently_recommended(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': [{'id': 1, 'status': 1, 'price': 'NaN', 'categoryId': 3}]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        run(tools.get_customer_menu(3))


@pytest.mark.parametrize('status', [True, None, '1', 9])
def test_menu_rejects_malformed_sale_status(monkeypatch, status):
    async def get(self, path, params=None):
        return {'code': 1, 'data': [{'id': 1, 'categoryId': 3, 'status': status, 'price': 10}]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        run(tools.get_customer_menu(3))


@pytest.mark.parametrize('category', [4, None, True])
def test_menu_never_relabels_wrong_or_missing_category(monkeypatch, category):
    async def get(self, path, params=None):
        return {'code': 1, 'data': [{'id': 1, 'categoryId': category, 'status': 1, 'price': 10}]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        run(tools.get_customer_menu(3))


def test_menu_reports_truncation_instead_of_claiming_complete_coverage(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': [{'id': i, 'categoryId': 3, 'status': 1, 'price': 10} for i in (1, 2, 3)]}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    result = run(tools.get_customer_menu(3, limit=1))
    assert result['matched'] == 3 and result['returned'] == 1
    assert result['scope']['truncated'] is True


def test_shop_categories_and_setmeal_contents_are_readonly(monkeypatch):
    async def get(self, path, params=None):
        data = {
            '/user/shop/status': 1,
            '/user/category/list': [{'id': 3, 'name': '热菜', 'type': 1, 'status': 1}, {'id': 4, 'status': 0}],
            '/user/setmeal/dish/7': [{'name': '饭', 'copies': 1, 'description': '米饭', 'internal': 'hidden'}],
        }[path]
        return {'code': 1, 'data': data}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    assert run(tools.get_customer_shop_status())['status'] == 1
    assert len(run(tools.get_customer_categories())['categories']) == 1
    assert run(tools.get_customer_setmeal_items(7))['items'] == [{'name': '饭', 'copies': 1, 'description': '米饭'}]


def test_own_orders_minimize_private_fields(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/user/order/historyOrders'
        assert params == {'page': 1, 'pageSize': 10}
        return {'code': 1, 'data': {'total': 1, 'records': [
            {'id': 8, 'number': 'order8', 'status': 5, 'userId': 12, 'amount': 30, 'phone': 'private', 'address': 'private'}]}}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    result = run(tools.get_my_orders())
    assert result['orders'][0]['id'] == 8
    assert 'phone' not in result['orders'][0] and 'address' not in result['orders'][0]


@pytest.mark.parametrize('owner', [13, None, True])
def test_foreign_or_unowned_order_detail_is_never_returned(monkeypatch, owner):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'id': 8, 'userId': owner, 'phone': 'private'}}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        run(tools.get_my_order_detail(8))


def test_foreign_record_in_list_fails_closed(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'total': 1, 'records': [{'id': 8, 'userId': 13}]}}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    with pytest.raises(RuntimeError):
        run(tools.get_my_orders())


def test_own_order_detail_and_goods_only(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/user/order/orderDetail/8'
        return {'code': 1, 'data': {'id': 8, 'userId': 12, 'status': 6, 'amount': 30, 'cancelReason': '用户取消',
                                   'orderDetailList': [{'name': '饭', 'number': 1, 'amount': 10}], 'phone': 'private'}}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    result = run(tools.get_my_order_detail(8))
    assert result['cancel_reason'] == '用户取消'
    assert result['items'][0]['name'] == '饭'
    assert 'phone' not in result
