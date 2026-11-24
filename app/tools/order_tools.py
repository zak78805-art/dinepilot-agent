"""订单只读工具：状态概览、订单列表、订单详情。

状态常量与 Java 端 `sky-pojo/com/sky/entity/Orders.java` 保持一致。
"""

from app.clients.sky_backend_client import SkyBackendClient
from app.tools.report_utils import date_range, csv_values, report_data

ORDER_STATUS_NAMES: dict[int, str] = {
    1: '待付款',
    2: '待接单',
    3: '已接单',
    4: '派送中',
    5: '已完成',
    6: '已取消',
}

# 后端 OrderStatisticsVO 只统计三个流转中的状态，不含已完成和已取消。
STATUS_OVERVIEW_SCOPE = {
    'status_definitions': {
        'toBeConfirmed': '当前状态为待接单（status=2）的订单数。',
        'confirmed': '当前状态为已接单、等待派送（status=3）的订单数。',
        'deliveryInProgress': '当前状态为派送中（status=4）的订单数。',
    },
    'excluded_statuses': '已完成（5）、已取消（6）、待付款（1）不计入这三个计数。',
    'snapshot_description': '这是查询时刻的状态快照，不代表历史流转过程。',
}


def _status_name(status) -> str | None:
    if status is None:
        return None
    return ORDER_STATUS_NAMES.get(int(status), f'未知状态({status})')


def _order_summary(record: dict) -> dict:
    """列表场景只保留核对所需的字段，避免把整行订单原样透出。"""
    return {
        'id': record.get('id'),
        'number': record.get('number'),
        'status': record.get('status'),
        'status_name': _status_name(record.get('status')),
        'amount': record.get('amount'),
        'order_time': record.get('orderTime'),
        'phone': record.get('phone'),
        'address': record.get('address'),
        'cancel_reason': record.get('cancelReason') or None,
    }


async def get_order_statistics(begin: str, end: str) -> dict:
    """按下单日期统计全部订单、当前已完成订单及完成率；不提供其他状态明细。"""
    params = date_range(begin, end)
    data = report_data(await SkyBackendClient().get('/admin/report/ordersStatistics', params=params))
    dates = csv_values(data.get('dateList'))
    counts = csv_values(data.get('orderCountList'), int)
    valid = csv_values(data.get('validOrderCountList'), int)
    if not len(dates) == len(counts) == len(valid):
        raise RuntimeError('订单统计字段数量不一致')
    return {**params, 'daily': [dict(date=d, orders=n, valid_orders=v) for d,n,v in zip(dates,counts,valid)],
            'total_orders': data.get('totalOrderCount'), 'valid_orders': data.get('validOrderCount'),
            'completion_rate': data.get('orderCompletionRate'),
            'statistics_scope': {
                'time_field': 'orders.order_time',
                'time_description': '按下单时间筛选，起止日期均包含；状态取查询时的当前状态。',
                'total_orders_status_filter': None,
                'valid_orders_status_filter': 5,
                'completion_rate_formula': 'valid_orders / total_orders',
                'zero_total_orders_has_meaningful_rate': False,
                'other_status_breakdown_available': True,
                'other_status_breakdown_tool': 'get_order_list',
                'definitions': {
                    'total_orders': '全部状态订单数，包含未完成及已取消等状态。',
                    'valid_orders': '当前状态为已完成（status=5）的订单数。',
                    'completion_rate': '0到1的比例；总订单为0时后端返回0，应说明无订单、完成率不适用。',
                },
                'limitation': '总数减已完成数仅代表非已完成订单数；要区分取消、待接单等具体状态，需用 get_order_list 按状态查询。',
            }}


async def get_order_status_overview() -> dict:
    """查询当前待接单、已接单待派送、派送中的订单数量。"""
    payload = await SkyBackendClient().get('/admin/order/statistics')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('订单状态统计接口未返回有效数据')
    return {
        'to_be_confirmed': data.get('toBeConfirmed'),
        'confirmed': data.get('confirmed'),
        'delivery_in_progress': data.get('deliveryInProgress'),
        'statistics_scope': STATUS_OVERVIEW_SCOPE,
        'note': ('这三个计数只覆盖流转中的订单；已完成和已取消的订单需要用订单列表按状态查询。'
                 '计数为0只代表此刻没有该状态的订单，不代表从未有过。'),
    }


async def get_order_list(status: int | None = None, begin: str | None = None,
                         end: str | None = None, number: str | None = None,
                         phone: str | None = None, limit: int = 10) -> dict:
    """按状态、下单日期、订单号或手机号查询订单列表。"""
    if not 1 <= limit <= 100:
        raise ValueError('limit 必须在1到100之间')
    if status is not None and status not in ORDER_STATUS_NAMES:
        raise ValueError(f'status 必须是 {sorted(ORDER_STATUS_NAMES)} 之一')

    params: dict[str, object] = {'page': 1, 'pageSize': limit}
    if status is not None:
        params['status'] = status
    # 后端按 number/phone 做 like 模糊匹配，这里原样透传，由调用方决定精度。
    if number:
        params['number'] = str(number).strip()
    if phone:
        params['phone'] = str(phone).strip()

    time_scope: dict[str, object]
    if begin or end:
        from app.tools.report_utils import date_range
        if not (begin and end):
            raise ValueError('按日期查询时 begin 和 end 必须同时提供')
        span = date_range(begin, end)
        # 后端 beginTime/endTime 是 LocalDateTime，需要带上时分秒才能覆盖整天。
        params['beginTime'] = f"{span['begin']} 00:00:00"
        params['endTime'] = f"{span['end']} 23:59:59"
        time_scope = {
            'time_field': 'orders.order_time',
            'time_description': '按下单时间筛选，起止日期均包含整天。',
            'range': span,
        }
    else:
        time_scope = {
            'time_field': 'orders.order_time',
            'time_description': '未限定日期，返回当前全部订单中最近的部分。',
            'range': None,
        }

    payload = await SkyBackendClient().get('/admin/order/conditionSearch', params=params)
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('订单查询接口未返回有效数据')
    records = data.get('records')
    if not isinstance(records, list):
        raise RuntimeError('订单查询接口未返回记录列表')

    return {
        'total': data.get('total'),
        'returned': len(records),
        'orders': [_order_summary(record) for record in records],
        'statistics_scope': {
            **time_scope,
            'status_filter': status,
            'status_name': _status_name(status),
            'status_description': ('未指定状态，返回全部状态订单。' if status is None
                                   else f'仅返回当前状态为{_status_name(status)}（status={status}）的订单。'),
            'number_filter': params.get('number'),
            'phone_filter': params.get('phone'),
            'number_match': 'number 与 phone 均为模糊匹配（like %值%），不是精确等于。',
            'order_by': 'order_time desc',
            'limit': limit,
            'truncated': (data.get('total') or 0) > len(records),
        },
        'note': ('total 是该条件下的订单总数，returned 是本轮实际返回的条数；'
                 '两者不等说明结果被截断。状态是查询时快照。'),
    }


async def get_order_detail(order_id: int | None = None, order_number: str | None = None) -> dict:
    """查询单个订单的详情，包含取消原因和商品明细。

    用户口语里的「订单号」通常是 number（如 1785838137567），而 /admin/order/details/{id}
    要的是数据库主键。两个入口都支持：只给 order_number 时先用订单列表反查主键。
    """
    resolved_from = None
    if order_id is None:
        if not order_number or not str(order_number).strip():
            raise ValueError('order_id 和 order_number 至少要提供一个')
        number = str(order_number).strip()
        payload = await SkyBackendClient().get(
            '/admin/order/conditionSearch', params={'page': 1, 'pageSize': 50, 'number': number})
        data = payload.get('data') or {}
        records = data.get('records') or []
        matches = [r for r in records if str(r.get('number')) == number]
        if not matches:
            raise ValueError(f'未找到订单号为 {number} 的订单')
        if len(matches) > 1:
            raise ValueError(f'订单号 {number} 匹配到多笔订单，无法唯一定位')
        order_id = matches[0].get('id')
        resolved_from = 'order_number'
        if not isinstance(order_id, int) or order_id <= 0:
            raise RuntimeError(f'订单号 {number} 对应的主键无效')

    if not isinstance(order_id, int) or order_id <= 0:
        raise ValueError('order_id 必须是正整数')

    payload = await SkyBackendClient().get(f'/admin/order/details/{order_id}')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError(f'未查询到订单 {order_id} 的详情')

    details = data.get('orderDetailList') or []
    if not isinstance(details, list):
        raise RuntimeError('订单详情中的商品明细格式错误')

    return {
        'id': data.get('id'),
        'number': data.get('number'),
        'status': data.get('status'),
        'status_name': _status_name(data.get('status')),
        'amount': data.get('amount'),
        'order_time': data.get('orderTime'),
        'checkout_time': data.get('checkoutTime'),
        'pay_status': data.get('payStatus'),
        'pay_method': data.get('payMethod'),
        'consignee': data.get('consignee'),
        'phone': data.get('phone'),
        'address': data.get('address'),
        'remark': data.get('remark') or None,
        'cancel_reason': data.get('cancelReason') or None,
        'cancel_time': data.get('cancelTime'),
        'rejection_reason': data.get('rejectionReason') or None,
        'delivery_status': data.get('deliveryStatus'),
        'dishes': [
            {
                'name': item.get('name'),
                'number': item.get('number'),
                'amount': item.get('amount'),
            }
            for item in details
            if isinstance(item, dict)
        ],
        'statistics_scope': {
            'status_description': '这是查询时刻的订单状态快照。',
            'resolved_from': resolved_from,
            'id_vs_number': ('id 是数据库主键，number 是面向用户的订单号；'
                             '用户说的「订单号」一般指 number，工具会自动换成 id 再查详情。'),
            'cancel_reason_available': bool(data.get('cancelReason')),
            'rejection_reason_available': bool(data.get('rejectionReason')),
            'definitions': {
                'cancel_reason': '仅当该字段有值时才能说明取消原因；为空表示系统没有记录原因，不能推测。',
                'rejection_reason': '仅当该字段有值时才能说明商家拒单原因。',
                'amount': '订单金额，单位元，含打包费等；不是利润。',
            },
        },
        'note': '取消或拒单原因为空时，不得推测原因，只能说明系统未记录。',
    }
