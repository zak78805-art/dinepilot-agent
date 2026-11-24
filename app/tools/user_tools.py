"""用户只读工具：新增用户与累计用户统计。"""

from app.clients.sky_backend_client import SkyBackendClient
from app.tools.report_utils import date_range, csv_values, report_data


async def get_user_statistics(begin: str, end: str) -> dict:
    """按下单日期查询每日累计用户数与新增用户数，日期格式 YYYY-MM-DD。"""
    params = date_range(begin, end)
    data = report_data(await SkyBackendClient().get('/admin/report/userStatistics', params=params))
    dates = csv_values(data.get('dateList'))
    total = csv_values(data.get('totalUserList'), int)
    new = csv_values(data.get('newUserList'), int)
    if not len(dates) == len(total) == len(new):
        raise RuntimeError('用户统计日期与数量数量不一致')
    return {
        **params,
        'daily': [
            dict(date=d, total_users=t, new_users=n)
            for d, t, n in zip(dates, total, new)
        ],
        'new_users_total': sum(new),
        'statistics_scope': {
            'time_field': 'user.create_time',
            'time_description': '按用户注册时间筛选，起止日期均包含。',
            'definitions': {
                'total_users': '截至该日期为止的累计用户总数。',
                'new_users': '该日期当天新增注册的用户数。',
            },
            'growth_rate_available': False,
            'limitation': ('只统计注册时间，不代表下单用户数或活跃用户数；'
                           '累计值首日之前的存量用户不在本查询范围内。'),
        },
        'note': ('用户数是注册口径，与订单里的下单用户数不是同一指标。'
                 '新增为0只表示当天没有新注册，不代表没有下单。'),
    }
