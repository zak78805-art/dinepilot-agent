from datetime import date, datetime, timedelta, timezone


def today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def date_range(begin: str, end: str) -> dict[str, str]:
    start, finish = date.fromisoformat(begin), date.fromisoformat(end)
    if start > finish or (finish-start).days >= 366 or finish > today():
        raise ValueError('日期范围需满足开始不晚于结束、最多366天、结束不超过今天')
    return {'begin': start.isoformat(), 'end': finish.isoformat()}


def csv_values(value, convert=str):
    if not isinstance(value, str):
        raise RuntimeError('统计接口返回字段格式错误')
    if not value.strip():
        return []
    try:
        return [convert(v.strip()) for v in value.split(',')]
    except ValueError:
        raise RuntimeError('统计接口返回字段格式错误') from None


def report_data(payload):
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('统计接口未返回有效数据')
    return data
