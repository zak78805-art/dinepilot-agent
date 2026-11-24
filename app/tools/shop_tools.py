async def get_shop_status() -> dict:
    """查询店铺营业状态。"""
    raise NotImplementedError("等待接入 Java 店铺状态接口")


async def change_shop_status(status: int) -> dict:
    """修改店铺营业状态；调用前必须经过审批。"""
    raise NotImplementedError("等待接入 Java 店铺状态接口")

