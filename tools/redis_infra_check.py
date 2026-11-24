"""Live Redis infrastructure verification against local sky-redis (0 model calls).

Verifies against real Redis server:
1. PING + TTL (`SET EX`)
2. Multi-worker `SessionStore` + `AgentState` round-trip & operator isolation
3. Multi-worker `ReportReviewManager` status persistence & session clear
4. Multi-worker `ApprovalManager` concurrent confirm serialized by real `SET NX PX` + Lua `EVAL`
Cleans up all temporary keys under an isolated prefix before exiting.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import tempfile
import time
from types import SimpleNamespace

from agentscope.message import AssistantMsg, TextBlock, UserMsg
from agentscope.state import AgentState

from app.approval.manager import ApprovalConflictError, ApprovalManager
from app.memory.session_store import SessionStore
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.review.text_reviewer import Fact
from app.storage.redis_store import RedisStateStore


async def run_live_redis_check(redis_url: str) -> dict:
    started = time.monotonic()
    prefix = f'sky_agent_check_{secrets.token_hex(4)}'
    store = RedisStateStore(redis_url=redis_url, key_prefix=prefix, socket_timeout=2.0)

    try:
        ping_ok = await store.ping()
        if not ping_ok:
            return {'passed': False, 'error': 'Redis ping failed', 'real_model_calls': 0}

        # 1. Cross-worker SessionStore AgentState persistence
        worker_a = SessionStore(10, 120, state_store=store)
        worker_b = SessionStore(10, 120, state_store=store)
        async with worker_a.use(('merchant', 7, 'live-s1'), lambda: SimpleNamespace(state=AgentState())) as ag_a:
            ag_a.state.summary = '<conversation-summary>\n- 用户：今日营业额\n</conversation-summary>'
            ag_a.state.context.append(UserMsg(name='merchant', content='看下热销菜'))
            ag_a.state.context.append(AssistantMsg(
                name='merchant_assistant',
                content=[TextBlock(text='招牌酸菜鱼第一。')],
            ))

        session_ttl = await store._get_client().ttl(store._session_key('merchant', 7, 'live-s1'))
        async with worker_b.use(('merchant', 7, 'live-s1'), lambda: SimpleNamespace(state=AgentState())) as ag_b:
            session_restored = (
                '今日营业额' in str(ag_b.state.summary)
                and len(ag_b.state.context) == 2
                and '招牌酸菜鱼' in ag_b.state.context[1].get_text_content()
            )
        session_cleared = await SessionStore(10, 120, state_store=store).clear(('merchant', 7, 'live-s1'))

        # 2. Cross-worker ReportReviewManager
        rev_a = ReportReviewManager(generate=build_mock_review_generator(), state_store=store, ttl_seconds=120.0)
        rev_b = ReportReviewManager(generate=build_mock_review_generator(), state_store=store, ttl_seconds=120.0)
        ref = rev_a.submit(
            operator_id=7,
            session_id='live-rev',
            draft='已完成订单营业额为200元。',
            evidence=(Fact(id='turnover', text='营业额200元'),),
        )
        assert ref is not None and ref.review_id is not None
        for _ in range(40):
            st = rev_a.get(review_id=ref.review_id, operator_id=7, session_id='live-rev')
            if st and st.status not in ('pending', 'running'):
                break
            await asyncio.sleep(0.02)
        remote_rev = await rev_b.aget(review_id=ref.review_id, operator_id=7, session_id='live-rev')
        review_ok = remote_rev is not None and remote_rev.status == 'pass'
        await rev_b.acancel_session(operator_id=7, session_id='live-rev')
        await rev_a.shutdown()
        await rev_b.shutdown()

        # 3. Cross-worker ApprovalManager + real Redis SET NX PX + Lua EVAL distributed lock
        exec_count = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / 'audit.jsonl'
            apr_a = ApprovalManager(audit_log_path=audit_path, state_store=store, ttl_seconds=120.0, retention_seconds=120.0)
            apr_b = ApprovalManager(audit_log_path=audit_path, state_store=store, ttl_seconds=120.0, retention_seconds=120.0)
            ticket = await apr_a.acreate_dish_status_approval(
                operator_id=7,
                session_id='live-apr',
                dish_id=12,
                dish_name='酸菜鱼',
                category='招牌菜',
                price=58.0,
                before_status=1,
                before_status_name='起售',
                after_status=0,
                after_status_name='停售',
                reason='售罄',
            )

            async def exec_once(_t) -> str:
                nonlocal exec_count
                exec_count += 1
                await asyncio.sleep(0.05)
                return '已停售'

            outcomes = await asyncio.gather(
                apr_a.confirm(approval_id=ticket.approval_id, operator_id=7, executor=exec_once),
                apr_b.confirm(approval_id=ticket.approval_id, operator_id=7, executor=exec_once),
                return_exceptions=True,
            )
            ok_list = [o for o in outcomes if not isinstance(o, BaseException)]
            err_list = [o for o in outcomes if isinstance(o, ApprovalConflictError)]
            approval_ok = len(ok_list) == 1 and len(err_list) == 1 and exec_count == 1
            await apr_b.acancel_session(operator_id=7, session_id='live-apr')

        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        passed = bool(ping_ok and session_ttl > 0 and session_restored and session_cleared and review_ok and approval_ok)
        return {
            'check': 'live_redis_state_persistence',
            'passed': passed,
            'real_model_calls': 0,
            'elapsed_ms': elapsed_ms,
            'checks': {
                'ping': ping_ok,
                'session_ttl_seconds': session_ttl,
                'cross_worker_session_restore': session_restored,
                'cross_worker_session_clear': session_cleared,
                'cross_worker_review_persistence': review_ok,
                'cross_worker_approval_distributed_lock': approval_ok,
                'approval_executor_invocations': exec_count,
            },
        }
    finally:
        try:
            client = store._get_client()
            keys = [k async for k in client.scan_iter(match=f'{prefix}:*')]
            if keys:
                await client.delete(*keys)
        except Exception:
            pass
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Verify Redis persistence against local sky-redis')
    parser.add_argument('--redis-url', default='redis://localhost:6379/1')
    parser.add_argument('--output', default='')
    args = parser.parse_args()

    report = asyncio.run(run_live_redis_check(args.redis_url))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    raise SystemExit(0 if report.get('passed') else 1)


if __name__ == '__main__':
    main()
