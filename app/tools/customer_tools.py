"""Customer tools never accept a user_id and never call administrator APIs."""
from decimal import Decimal, InvalidOperation

from app.clients.customer_backend_client import CustomerBackendClient, current_customer_id

_STATUS = {1: '待付款', 2: '待接单', 3: '已接单', 4: '派送中', 5: '已完成', 6: '已取消'}


def _positive_id(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError('ID必须是有效正整数')
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('limit必须在1到100之间')
    return value


def _money(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 1000000:
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError):
        raise ValueError('金额必须是有效非负数') from None


async def _data(path, params=None):
    result = await CustomerBackendClient().get(path, params=params)
    if not isinstance(result, dict) or result.get('code') != 1 or 'data' not in result:
        raise RuntimeError('用户接口返回异常')
    return result['data']


def _records(value):
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError('用户接口列表格式异常')
    return value


def _owned(record):
    user_id = current_customer_id.get()
    if type(user_id) is not int or type(record.get('userId')) is not int or record['userId'] != user_id:
        raise RuntimeError('订单归属校验失败')


def _order(record):
    _owned(record)
    return {'id': record.get('id'), 'number': record.get('number'), 'status': record.get('status'),
            'status_name': _STATUS.get(record.get('status'), '未知状态'),
            'amount': record.get('amount'), 'order_time': record.get('orderTime')}


async def get_customer_shop_status() -> dict:
    """查询店铺当前营业状态，不修改状态。"""
    status = await _data('/user/shop/status')
    if type(status) is not int or status not in (0, 1):
        raise RuntimeError('营业状态格式异常')
    return {'status': status, 'status_name': '营业中' if status == 1 else '休息中',
            'note': '查询时刻状态，不保证下单时仍营业。'}


async def get_customer_categories(item_type: str | None = None) -> dict:
    """查询在售分类，item_type为dish菜品、setmeal套餐；不传查询两类。"""
    if item_type not in (None, 'dish', 'setmeal'):
        raise ValueError('item_type只能是dish或setmeal')
    params = {} if item_type is None else {'type': 1 if item_type == 'dish' else 2}
    records = _records(await _data('/user/category/list', params))
    return {'categories': [{'id': item.get('id'), 'name': item.get('name'),
                            'type': item.get('type')} for item in records if item.get('status') == 1],
            'scope': '当前启用分类，不代表每个分类都存在在售商品。'}


async def get_customer_menu(category_id: int, item_type: str = 'dish',
                            max_price: float | None = None, limit: int = 20) -> dict:
    """按分类浏览在售菜品或套餐，可按单项价格上限筛选，金额为元、不含额外费用。"""
    _positive_id(category_id)
    _limit(limit)
    if item_type not in ('dish', 'setmeal'):
        raise ValueError('item_type只能是dish或setmeal')
    budget = _money(max_price) if max_price is not None else None
    records = _records(await _data(f'/user/{item_type}/list', {'categoryId': category_id}))
    items = []
    for item in records:
        if type(item.get('status')) is not int or item['status'] not in (0, 1):
            raise RuntimeError('商品上下架状态格式异常')
        if item['status'] != 1:
            continue
        if type(item.get('categoryId')) is not int or item['categoryId'] != category_id:
            raise RuntimeError('商品分类与请求分类不一致')
        try:
            price = _money(item.get('price'))
        except ValueError:
            raise RuntimeError('商品价格格式异常，不能用于推荐') from None
        if budget is not None and price > budget:
            continue
        flavors = _records(item.get('flavors') or [])
        items.append({'id': item.get('id'), 'name': item.get('name'), 'price': float(price),
                      'description': item.get('description'), 'category_id': category_id,
                      'item_type': item_type,
                      'flavors': [{'name': f.get('name'), 'value': f.get('value')} for f in flavors]})
    return {'items': items[:limit], 'matched': len(items), 'returned': min(len(items), limit),
            'scope': {'category_id': category_id, 'item_type': item_type, 'currency': 'CNY',
                      'fees_included': False, 'allergens_verified': False, 'stock_verified': False,
                      'truncated': len(items) > limit},
            'note': '仅当前分类的查询候选；预算是单项价格上限，不是整单实付。推荐组合要按数量相加，打包和配送费另计。口味不等于过敏原信息。'}


async def get_customer_setmeal_items(setmeal_id: int) -> dict:
    """查询套餐包含的菜品与份数，不代表已核实套餐当前起售或过敏原。"""
    _positive_id(setmeal_id)
    records = _records(await _data(f'/user/setmeal/dish/{setmeal_id}'))
    return {'setmeal_id': setmeal_id,
            'items': [{'name': i.get('name'), 'copies': i.get('copies'),
                       'description': i.get('description')} for i in records],
            'note': '套餐在售及价格以菜单查询为准；不能据此保证过敏原安全。'}


async def get_my_orders(status: int | None = None, limit: int = 10) -> dict:
    """查询当前登录用户本人的最近订单，不接受用户ID。status为1到6。"""
    _limit(limit)
    if status is not None and (type(status) is not int or status not in _STATUS):
        raise ValueError('status必须是1到6的订单状态')
    params = {'page': 1, 'pageSize': limit}
    if status is not None:
        params['status'] = status
    data = await _data('/user/order/historyOrders', params)
    if not isinstance(data, dict):
        raise RuntimeError('本人订单列表格式异常')
    orders = [_order(item) for item in _records(data.get('records'))]
    return {'orders': orders, 'total': data.get('total'), 'returned': len(orders),
            'note': '当前登录用户本人的订单，最多返回第一页；total是符合条件的总数。状态为当前快照。'}


async def get_my_order_detail(order_id: int) -> dict:
    """查询本人订单详情。order_id为列表返回的主键，不是长订单编号。"""
    _positive_id(order_id)
    data = await _data(f'/user/order/orderDetail/{order_id}')
    if not isinstance(data, dict):
        raise RuntimeError('本人订单详情格式异常')
    result = _order(data)
    result.update(cancel_reason=data.get('cancelReason') or None,
                  items=[{'name': item.get('name'), 'number': item.get('number'), 'amount': item.get('amount')}
                         for item in _records(data.get('orderDetailList') or [])],
                  note='只有系统记录了取消原因才能说明原因；金额为订单金额，不是逐项价格之和的保证。')
    return result
