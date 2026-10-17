import datetime as dt
import types
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute


EXPRESSION = 'CRON_TZ=UTC 13 19 * * 5'
GUARD = dt.datetime(2026, 10, 15, 19, 9, 12, tzinfo=dt.timezone.utc).timestamp()


def inert_bot():
    bot = object.__new__(TelegramBot)
    bot._user_timezone = ZoneInfo('Europe/Warsaw')
    bot._config = types.SimpleNamespace(vault_path=None)
    bot._schedule_retry_max_attempts = 0
    bot._schedule_retry_delay_seconds = 0
    bot._get_state = lambda *a, **kw: object()
    bot._bot_for_state = lambda state: None
    bot._topic_schedules_by_id = {}
    bot._schedule_ids_by_route = {}
    def register(record):
        bot._topic_schedules_by_id[record.schedule_id] = record
        bot._schedule_ids_by_route.setdefault(record.route, set()).add(record.schedule_id)
    bot._register_topic_schedule = register
    return bot


@pytest.mark.parametrize('base,expected', [('2026-10-10T21:00:00Z','2026-10-16T19:13:00Z'),('2026-10-24T00:00:00Z','2026-10-30T19:13:00Z'),('2027-03-27T00:00:00Z','2027-04-02T19:13:00Z')])
def test_same_evaluator_utc_across_dst(base,expected):
    bot=inert_bot()
    fire=bot._next_cron_fire_ts(cron_expr=EXPRESSION,base_ts=dt.datetime.fromisoformat(base.replace('Z','+00:00')).timestamp(),not_before_ts=GUARD)
    assert dt.datetime.fromtimestamp(fire,dt.timezone.utc).isoformat()==expected.replace('Z','+00:00')


def test_plain_default_unchanged():
    bot=inert_bot()
    base=dt.datetime(2026,10,10,21,tzinfo=dt.timezone.utc).timestamp()
    fire=bot._next_cron_fire_ts(cron_expr='13 19 * * 5',base_ts=base,not_before_ts=GUARD)
    assert dt.datetime.fromtimestamp(fire,dt.timezone.utc).isoformat()=='2026-10-16T17:13:00+00:00'
    assert str(bot._user_timezone)=='Europe/Warsaw'


@pytest.mark.asyncio
async def test_actual_api_roundtrip_and_repair_representation():
    bot=inert_bot()
    route=TelegramRoute(chat_id=-1,thread_id=1)
    args=dict(description='[ops-test] inert timing fixture',schedule_mode='cron',cron=EXPRESSION,prompt='inert evaluator test, not weekly birth',interval_seconds=0,reset_session=True,max_runs=100000,inherit='none',until='',run_mode='',**{'from':'2026-10-15T19:09:12Z'})
    with patch('obs_agent.telegram.time.time',return_value=GUARD-86400):
        result=await bot._cron_create(route=route,args=args)
    assert 'is_error' not in result
    record=next(iter(bot._topic_schedules_by_id.values()))
    assert record.cron_expr==EXPRESSION and record.from_ts==GUARD
    # Exact persisted representation is the input to the same post-fire/restore evaluator.
    record.next_run_at=bot._next_timed_run_at(record,base_ts=dt.datetime(2026,10,24,tzinfo=dt.timezone.utc).timestamp())
    listed=await bot._cron_list(route=route)
    assert 'CRON_TZ=UTC' in str(listed)
    assert dt.datetime.fromtimestamp(record.next_run_at,dt.timezone.utc).isoformat()=='2026-10-30T19:13:00+00:00'
