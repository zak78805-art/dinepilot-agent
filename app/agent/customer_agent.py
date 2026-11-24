from agentscope.tool import FunctionTool, Toolkit

from app.agent.merchant_agent import MerchantAgent, _run_tool
from app.agent.customer_prompts import CUSTOMER_SYSTEM_PROMPT
from app.tools import customer_tools


async def _shop() -> dict:
    """查询当前营业状态。"""
    return await _run_tool('get_customer_shop_status', customer_tools.get_customer_shop_status)


async def _categories(item_type: str | None = None) -> dict:
    """查询启用分类；item_type为dish或setmeal，不传查询两类。"""
    return await _run_tool('get_customer_categories', customer_tools.get_customer_categories, item_type=item_type)


async def _menu(category_id: int, item_type: str = 'dish', max_price: float | None = None, limit: int = 20) -> dict:
    """按分类查询在售菜单，单项价格上限max_price为元，不含配送打包费。"""
    return await _run_tool('get_customer_menu', customer_tools.get_customer_menu, category_id=category_id,
                           item_type=item_type, max_price=max_price, limit=limit)


async def _setmeal(setmeal_id: int) -> dict:
    """核对套餐包含的菜品和份数，价格与在售状态需查询菜单。"""
    return await _run_tool('get_customer_setmeal_items', customer_tools.get_customer_setmeal_items, setmeal_id=setmeal_id)


async def _orders(status: int | None = None, limit: int = 10) -> dict:
    """查询登录用户本人的最近订单，status为1到6，不接受其他用户ID。"""
    return await _run_tool('get_my_orders', customer_tools.get_my_orders, status=status, limit=limit)


async def _detail(order_id: int) -> dict:
    """查询本人订单，order_id为get_my_orders返回的数据库主键，不是长订单编号。"""
    return await _run_tool('get_my_order_detail', customer_tools.get_my_order_detail, order_id=order_id)


class CustomerAgent(MerchantAgent):
    """复用模型/执行机制，而不是复用商家工具或会话实例。"""
    actor_role = 'customer'

    def _create_agent(self):
        return self._build_agent(self._create_toolkit(), CUSTOMER_SYSTEM_PROMPT, 'customer_assistant')

    @staticmethod
    def _create_toolkit():
        return Toolkit(tools=[
            FunctionTool(_shop, name='get_customer_shop_status', is_read_only=True),
            FunctionTool(_categories, name='get_customer_categories', is_read_only=True),
            FunctionTool(_menu, name='get_customer_menu', is_read_only=True),
            FunctionTool(_setmeal, name='get_customer_setmeal_items', is_read_only=True),
            FunctionTool(_orders, name='get_my_orders', is_read_only=True),
            FunctionTool(_detail, name='get_my_order_detail', is_read_only=True),
        ])
