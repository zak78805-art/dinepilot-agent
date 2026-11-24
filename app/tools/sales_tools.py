from app.clients.sky_backend_client import SkyBackendClient
from app.tools.report_utils import date_range, csv_values, report_data


async def get_today_sales() -> dict:
    """查询苍穹外卖后台当天的真实经营数据。

    返回营业额、有效订单数、订单完成率、平均客单价和新增用户数。
    本工具只读取数据，不会修改订单、菜品或店铺状态。
    """
    payload = await SkyBackendClient().get("/admin/workspace/businessData")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("苍穹外卖后端未返回今日经营数据")
    return {
        **data,
        'statistics_scope': {
            'time_field': 'orders.order_time',
            'time_description': '按后端当天的下单时间筛选，不是按支付或完成时间；今日数据未结束。',
            'turnover_status_filter': 5,
            'valid_orders_status_filter': 5,
            'definitions': {
                'turnover': '当前状态为已完成（status=5）的订单 amount 合计，单位元；不是全部已支付订单金额，也不是利润。',
                'validOrderCount': '当前状态为已完成的订单数。',
                'orderCompletionRate': '已完成订单数 / 全部状态订单数；无订单时后端返回0，不代表经营差。',
                'unitPrice': '已完成订单金额合计 / 已完成订单数；无已完成订单时后端返回0。',
                'newUsers': '后端当天新增用户数。',
            },
            'other_status_breakdown_available': False,
        },
    }


async def get_turnover_statistics(begin: str, end: str) -> dict:
    """按下单日期查询当前已完成订单金额，非全部实付金额；日期格式 YYYY-MM-DD。"""
    params = date_range(begin, end)
    data = report_data(await SkyBackendClient().get('/admin/report/turnoverStatistics', params=params))
    dates = csv_values(data.get('dateList'))
    amounts = csv_values(data.get('turnoverList'), float)
    if len(dates) != len(amounts):
        raise RuntimeError('营业额统计日期与金额数量不一致')
    return {**params, 'daily': [dict(date=d, turnover=n) for d,n in zip(dates,amounts)],
            'statistics_scope': {
                'time_field': 'orders.order_time',
                'time_description': '按下单时间筛选，起止日期均包含；不是按支付或完成时间。',
                'status_filter': 5,
                'status_description': '仅当前状态为已完成的订单。',
                'aggregation': 'sum(orders.amount)',
                'currency': 'CNY',
                'definition': '已完成订单金额合计，不是全部已支付订单的金额合计，也不是利润。',
            },
            'note': '金额单位元；包含今天时，当日数据尚未结束。零金额只表示此口径无金额，不能确定未营业或经营异常。'}
