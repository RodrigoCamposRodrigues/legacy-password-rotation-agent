import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from rotator.browser import AttemptAborted
from rotator.changers import AgentReport, LLMChanger, ScriptedChanger, Target


def target(dashboard, serial):
    record = dashboard.vault.get_device(serial)
    return Target(serial=serial, name=record.name, model=record.model, run_id="run-test")


@pytest.mark.parametrize("serial", ["CT200-0012", "FL9-0012"])
def test_scripted_changer_changes_the_right_device(dashboard, make_session, serial):
    session = make_session(serial)
    report = ScriptedChanger().change(session, target(dashboard, serial))
    assert report.submitted and report.device_serial_seen == serial
    assert dashboard.truth(serial) == session.new_password
    look_alike = "CT200-0112" if serial.startswith("CT") else "FL9-0013"
    assert dashboard.truth(look_alike) == dashboard.passwords[look_alike]


def test_scripted_changer_survives_session_expiry(dashboard, make_session):
    session = make_session("CT200-0012")
    dashboard.faults(session_expiry_requests=3)
    ScriptedChanger().change(session, target(dashboard, "CT200-0012"))
    assert dashboard.truth("CT200-0012") == session.new_password


class ToolCallingFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_llm_changer_wires_tools_and_returns_structured_report(dashboard, make_session):
    report_args = {"device_serial_seen": "", "submitted": False, "notes": "only looked"}
    messages = iter([
        AIMessage(content="", tool_calls=[{"name": "read_page", "args": {}, "id": "1"}]),
        AIMessage(content="", tool_calls=[{"name": "fill", "args": {"element_id": 999, "value": "x"}, "id": "2"}]),
        AIMessage(content="", tool_calls=[{"name": "AgentReport", "args": report_args, "id": "3"}]),
    ])
    session = make_session("CT200-0012", threaded=True)
    report = LLMChanger("fake", model=ToolCallingFake(messages=messages)).change(session, target(dashboard, "CT200-0012"))
    assert report == AgentReport(**report_args)
    assert session.budget.tool_calls == 2


def test_llm_changer_budget_abort_propagates(dashboard, make_session):
    messages = iter([AIMessage(content="", tool_calls=[{"name": "read_page", "args": {}, "id": str(i)}]) for i in range(5)])
    session = make_session("CT200-0012", max_tool_calls=2, threaded=True)
    with pytest.raises(AttemptAborted):
        LLMChanger("fake", model=ToolCallingFake(messages=messages)).change(session, target(dashboard, "CT200-0012"))
