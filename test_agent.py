"""Перевірка всіх п'яти станів циклу — без мережі й без ключа (LLM підмінена скриптом)."""

import json

import pytest
from langchain_core.messages import AIMessage

import agent
from tools import ToolError, describe_pod, get_pod_logs, list_namespaces, list_unhealthy_pods

NS = "shop-prod"
CRASHER = "checkout-api-7d9f6c4b8-x9k2p"


class ScriptedLLM:
    """Віддає заздалегідь задані відповіді моделі. Рядок = фінальна відповідь,
    (tool, args) = виклик інструмента, Exception = падіння API."""

    def __init__(self, *script, usage=None):
        self.script = list(script)
        self.calls = 0
        self.usage = usage  # {"input_tokens": .., "output_tokens": .., "total_tokens": ..}

    def _usage(self):
        return dict(self.usage) if self.usage else None

    def invoke(self, messages):
        self.calls += 1
        item = self.script.pop(0) if self.script else "нема що казати"
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            name, args = item
            return AIMessage(content="", usage_metadata=self._usage(), tool_calls=[
                {"name": name, "args": args, "id": f"call_{self.calls}", "type": "tool_call"}])
        return AIMessage(content=item, usage_metadata=self._usage())


# --- інструменти ------------------------------------------------------------

def test_list_finds_only_unhealthy():
    out = json.loads(list_unhealthy_pods.invoke({"namespace": NS}))
    names = {p["name"] for p in out["pods"]}
    assert out["found"] and CRASHER in names
    assert "checkout-api-7d9f6c4b8-b4tzr" not in names  # здоровий под не потрапляє


def test_healthy_namespace_is_found_false_not_error():
    out = json.loads(list_unhealthy_pods.invoke({"namespace": "billing"}))
    assert out["found"] is False and out["pods"] == []


def test_list_namespaces_returns_real_names():
    out = json.loads(list_namespaces.invoke({}))
    assert out["found"] and {"shop-prod", "billing", "flaky-ns"} == set(out["namespaces"])


def test_unknown_namespace_points_at_list_namespaces():
    """Челендж B/b4: помилка має вести до інструмента, а не заохочувати вгадувати далі."""
    with pytest.raises(ToolError, match="Call list_namespaces"):
        list_unhealthy_pods.func("no-such-ns")


def test_unknown_namespace_raises():
    with pytest.raises(ToolError, match="not found"):
        list_unhealthy_pods.func("no-such-ns")


def test_unreachable_cluster_raises():
    with pytest.raises(ToolError, match="i/o timeout"):
        list_unhealthy_pods.func("flaky-ns")


def test_guessed_pod_name_raises():
    with pytest.raises(ToolError, match="do not guess"):
        describe_pod.func(NS, "checkout-api-nope")


def test_missing_logs_are_found_false():
    out = json.loads(get_pod_logs.invoke(
        {"namespace": NS, "pod_name": "image-worker-5c8d94f7-qq1mn"}))
    assert out["found"] is False and "failing to pull image" in out["reason"]


# --- п'ять станів -----------------------------------------------------------

def test_state_ok():
    llm = ScriptedLLM(
        ("list_unhealthy_pods", {"namespace": NS}),
        ("get_pod_logs", {"namespace": NS, "pod_name": CRASHER, "previous": True}),
        "ДІАГНОЗ: ...",
    )
    res = agent.run("що зламалось", llm=llm)
    assert res.state == "ok" and res.tool_calls == 2 and res.tool_errors == 0


def test_state_no_tool_used():
    res = agent.run("що зламалось", llm=ScriptedLLM("Схоже, у вас впала база даних."))
    assert res.state == "no_tool_used"
    assert "Відмовляюсь" in res.answer
    assert "впала база даних" in res.answer  # чернетка показана як відкинута, не як відповідь


def test_state_tool_error():
    """Помилка інструмента не валить процес, і в модель іде саме текст причини.

    Перевіряється не лише стан: у трейсі має бути рівно те повідомлення, яке модель
    прочитає. Без цієї частини тест не ловив би підміну обробника на загальний
    except Exception — див. розділ «Мутаційна перевірка» в README.
    """
    llm = ScriptedLLM(("list_unhealthy_pods", {"namespace": "flaky-ns"}), "Мабуть, все добре.")
    res = agent.run("що з flaky-ns", llm=llm)
    assert res.state == "tool_error" and res.tool_errors == 1
    assert "ERROR: Unable to connect to the server" in res.steps[0]
    assert "ToolError" not in res.steps[0]  # клас винятку — шум, модель має бачити причину


def test_state_turns_exhausted():
    llm = ScriptedLLM(*[("list_unhealthy_pods", {"namespace": NS})] * 5)
    res = agent.run("що зламалось", llm=llm, max_steps=3)
    assert res.state == "turns_exhausted"
    assert llm.calls == 3  # ліміт справді зупиняє цикл
    assert "НЕ поставлений" in res.answer


def test_state_api_error(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda _: None)
    llm = ScriptedLLM(*[RuntimeError("503 upstream unavailable")] * 3)
    res = agent.run("що зламалось", llm=llm)
    assert res.state == "api_error"
    assert llm.calls == agent.API_RETRIES + 1  # ретраї відпрацювали й зупинились


# --- бюджет і вартість (челендж C) ------------------------------------------

USAGE = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}


def test_cost_is_counted_per_call():
    llm = ScriptedLLM(("list_unhealthy_pods", {"namespace": NS}), "ДІАГНОЗ: ...", usage=USAGE)
    res = agent.run("що зламалось", llm=llm, model="gpt-4o-mini")
    # два виклики моделі: 2*(1000 in + 500 out) за ставками 0.15 / 0.60 за 1M
    assert res.input_tokens == 2000 and res.output_tokens == 1000
    assert res.cost_usd == pytest.approx(2000 / 1e6 * 0.15 + 1000 / 1e6 * 0.60)


def test_unknown_model_is_flagged_not_free():
    llm = ScriptedLLM("відповідь", usage=USAGE)
    res = agent.run("щось", llm=llm, model="some/unknown-model-v9")
    assert res.cost_usd > 0 and "ПРИПУЩЕННЯ" in res.price_note


def test_missing_usage_is_flagged_not_free():
    res = agent.run("щось", llm=ScriptedLLM("відповідь"))  # usage=None
    assert res.cost_usd == 0 and "не повернув usage_metadata" in res.price_note


def test_budget_stops_the_run_before_next_llm_call():
    """Ліміт у доларах жорсткіший за ліміт кроків: кроки ще є, гроші вже ні."""
    llm = ScriptedLLM(*[("list_unhealthy_pods", {"namespace": NS})] * 10, usage=USAGE)
    res = agent.run("що зламалось", llm=llm, max_steps=10, max_usd=0.001, model="gpt-4o-mini")
    assert res.state == "budget_exhausted"
    assert llm.calls < 10  # зупинились раніше, ніж вичерпались кроки
    assert res.cost_usd >= 0.001 and "Бюджет вичерпано" in res.answer
    assert "checkout-api-7d9f6c4b8-x9k2p" in res.answer  # часткові знахідки не втрачені
