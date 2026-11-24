"""菜品只读工具：销量排名、菜品目录与上下架状态。"""

from app.clients.sky_backend_client import SkyBackendClient
from app.tools.report_utils import date_range, csv_values, report_data

DISH_STATUS_NAMES: dict[int, str] = {0: '停售', 1: '在售'}


async def get_top_dishes(begin: str, end: str, limit: int = 10) -> dict:
    """按下单日期查询已完成订单的商品销量前10；不是完整商品目录或在售品类数。"""
    if not 1 <= limit <= 10:
        raise ValueError('limit 必须在1到10之间')
    params = date_range(begin, end)
    data = report_data(await SkyBackendClient().get('/admin/report/top10', params=params))
    names = csv_values(data.get('nameList'))
    counts = csv_values(data.get('numberList'), int)
    if len(names) != len(counts):
        raise RuntimeError('销量统计名称与数量不一致')
    return {**params, 'ranking': [dict(name=n, sales=c) for n,c in zip(names,counts)][:limit],
            'statistics_scope': {
                'time_field': 'orders.order_time',
                'time_description': '按下单时间筛选，起止日期均包含。',
                'status_filter': 5,
                'status_description': '仅统计当前已完成订单中的商品明细。',
                'aggregation': 'sum(order_detail.number)',
                'group_by': 'order_detail.name',
                'max_results': 10,
                'catalog_coverage_available': False,
                'catalog_coverage_tool': 'get_dish_catalog',
                'definition': '按商品名称合并销量后降序取前10，可能包含菜品和套餐；同名商品会合并。',
            },
            'note': '排名不是完整商品目录，返回1项不代表店铺只售1款或品类不足；空排名只代表此口径没有销售记录。销量不代表库存或利润。'}


async def get_dish_catalog(status: int | None = None, limit: int = 30) -> dict:
    """查询菜品目录与上下架状态；status 为 1 在售、0 停售、不传为全部。"""
    if not 1 <= limit <= 100:
        raise ValueError('limit 必须在1到100之间')
    if status is not None and status not in DISH_STATUS_NAMES:
        raise ValueError('status 只能是 0（停售）或 1（在售），不传表示全部')

    params: dict[str, object] = {'page': 1, 'pageSize': limit}
    if status is not None:
        params['status'] = status

    payload = await SkyBackendClient().get('/admin/dish/page', params=params)
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('菜品目录接口未返回有效数据')
    records = data.get('records')
    if not isinstance(records, list):
        raise RuntimeError('菜品目录接口未返回记录列表')

    dishes = [
        {
            'id': record.get('id'),
            'name': record.get('name'),
            'category': record.get('categoryName'),
            'price': record.get('price'),
            'status': record.get('status'),
            'status_name': DISH_STATUS_NAMES.get(record.get('status'), f'未知状态({record.get("status")})'),
        }
        for record in records
        if isinstance(record, dict)
    ]

    return {
        'total': data.get('total'),
        'returned': len(dishes),
        'dishes': dishes,
        'statistics_scope': {
            'source': 'dish 表当前记录，是菜品主数据而不是销量。',
            'status_filter': status,
            'status_description': ('未指定状态，返回全部上下架状态的菜品。' if status is None
                                   else f'仅返回{DISH_STATUS_NAMES[status]}（status={status}）的菜品。'),
            'limit': limit,
            'truncated': (data.get('total') or 0) > len(dishes),
            'category_field': 'dish.category_id 关联 category.name。',
            'setmeal_coverage': False,
        },
        'note': ('total 是该条件下的菜品总数，returned 是本轮返回条数。'
                 '本工具只覆盖菜品，不含套餐（setmeal）；目录里没有不代表门店不经营，套餐需另行查询。'),
    }


async def lookup_dish_for_status_change(
    *,
    dish_id: int | None = None,
    dish_name: str | None = None,
) -> dict:
    """只读查询单个菜品当前快照，供生成起售/停售审批单使用，不执行任何修改。"""
    if dish_id is not None:
        if type(dish_id) is not int or dish_id <= 0:
            raise ValueError('dish_id 必须是正整数')
        payload = await SkyBackendClient().get(f'/admin/dish/{dish_id}')
        data = payload.get('data')
        if not isinstance(data, dict) or type(data.get('id')) is not int:
            raise ValueError(f'未找到 ID 为 {dish_id} 的菜品')
        current_status = data.get('status')
        if type(current_status) is not int or current_status not in DISH_STATUS_NAMES:
            raise RuntimeError('菜品当前状态格式异常')
        return {
            'id': data['id'],
            'name': str(data.get('name') or '').strip() or f'菜品#{data["id"]}',
            'category': data.get('categoryName'),
            'price': float(data['price']) if isinstance(data.get('price'), (int, float)) and not isinstance(data.get('price'), bool) else None,
            'status': current_status,
            'status_name': DISH_STATUS_NAMES[current_status],
        }

    if not isinstance(dish_name, str) or not dish_name.strip():
        raise ValueError('dish_id 和 dish_name 至少要提供一个')
    target_name = dish_name.strip()
    if len(target_name) > 64:
        raise ValueError('dish_name 长度不能超过 64 个字符')

    payload = await SkyBackendClient().get(
        '/admin/dish/page',
        params={'page': 1, 'pageSize': 20, 'name': target_name},
    )
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('records'), list):
        raise RuntimeError('菜品查询接口未返回有效记录列表')
    records = [r for r in data['records'] if isinstance(r, dict)]
    exact = [r for r in records if str(r.get('name') or '').strip() == target_name]
    matches = exact if exact else records
    if not matches:
        raise ValueError(f'未找到名称包含「{target_name}」的菜品，请先查菜品目录核对准确名称')
    if len(matches) > 1:
        names = '、'.join(str(item.get('name')) for item in matches[:5])
        raise ValueError(f'「{target_name}」匹配到多个菜品（{names}），请提供准确菜品名称或菜品 ID')

    record = matches[0]
    found_id = record.get('id')
    current_status = record.get('status')
    if type(found_id) is not int or found_id <= 0:
        raise RuntimeError('菜品主键无效')
    if type(current_status) is not int or current_status not in DISH_STATUS_NAMES:
        raise RuntimeError('菜品当前状态格式异常')
    return {
        'id': found_id,
        'name': str(record.get('name') or '').strip() or f'菜品#{found_id}',
        'category': record.get('categoryName'),
        'price': float(record['price']) if isinstance(record.get('price'), (int, float)) and not isinstance(record.get('price'), bool) else None,
        'status': current_status,
        'status_name': DISH_STATUS_NAMES[current_status],
    }


async def execute_dish_status_change(
    *,
    dish_id: int,
    target_status: int,
    expected_current_status: int,
) -> dict:
    """仅由管理员显式确认审批单后调用：执行前再次核对当前状态，避免并发误覆盖。"""
    from app.approval.manager import ApprovalConflictError

    if type(dish_id) is not int or dish_id <= 0:
        raise ValueError('dish_id 必须是正整数')
    if type(target_status) is not int or target_status not in DISH_STATUS_NAMES:
        raise ValueError('target_status 只能是 0（停售）或 1（起售）')

    latest = await lookup_dish_for_status_change(dish_id=dish_id)
    if latest['status'] != expected_current_status:
        raise ApprovalConflictError(
            f'菜品「{latest["name"]}」当前状态已变为「{latest["status_name"]}」，与审批单创建时不一致'
        )
    await SkyBackendClient().post(f'/admin/dish/status/{target_status}', params={'id': dish_id})
    return {
        'dish_id': dish_id,
        'dish_name': latest['name'],
        'before_status': expected_current_status,
        'after_status': target_status,
        'after_status_name': DISH_STATUS_NAMES[target_status],
    }


async def change_dish_status(dish_id: int, status: int) -> dict:
    """保留旧符号：直接写操作必须走人工审批接口，禁止模型直接调用。"""
    raise RuntimeError('菜品状态变更必须通过 request_dish_status_change 发起审批并经管理员确认执行')
