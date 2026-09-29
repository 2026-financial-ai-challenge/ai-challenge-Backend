import asyncio
from types import SimpleNamespace

from app.services import call_service


def _fake_outbound(agent, call_session):
    """Stand in for _create_outbound_call, which would place a real call."""

    async def _create(_session_id):
        return agent, call_session, SimpleNamespace(id="bank_security_hold")

    return _create


def test_managed_call_is_not_handed_to_the_in_process_monitor(monkeypatch):
    """A managed call has no agent, and _monitor_call must not see it.

    In managed mode _create_outbound_call returns the API's Call model rather
    than a CallSession, so _monitor_call's call_session.wait() would raise
    AttributeError straight into the catch-all that marks the session failed
    and asks the scheduler to retry -- while ClawOps is still happily running
    the conversation. The status and transcript webhooks own a managed call.
    """
    # Only call_id: a Call model has no wait(), which is the whole point.
    api_call = SimpleNamespace(call_id="CAmanaged")
    monkeypatch.setattr(
        call_service, "_create_outbound_call", _fake_outbound(None, api_call)
    )

    monitored = []
    async def _monitor(*args):
        monitored.append(args)

    monkeypatch.setattr(call_service, "_monitor_call", _monitor)

    asyncio.run(call_service._start_and_monitor_call("ses_managed"))

    assert monitored == []


def test_in_process_call_is_still_monitored(monkeypatch):
    """The pipeline/realtime paths do own their call and must be monitored."""
    agent = SimpleNamespace(name="in-process")
    call_session = SimpleNamespace(call_id="CAsdk")
    monkeypatch.setattr(
        call_service, "_create_outbound_call", _fake_outbound(agent, call_session)
    )

    monitored = []
    async def _monitor(session_id, got_agent, got_session, _scenario):
        monitored.append((session_id, got_agent, got_session))

    monkeypatch.setattr(call_service, "_monitor_call", _monitor)

    asyncio.run(call_service._start_and_monitor_call("ses_sdk"))

    assert monitored == [("ses_sdk", agent, call_session)]
