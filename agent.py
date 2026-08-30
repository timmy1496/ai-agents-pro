"""SRE-агент: цикл tool-calling з явними термінальними станами.

Цикл написаний руками поверх ChatOpenAI.bind_tools (без AgentExecutor) — сенс
задачі в тому, щоб керування кроками, ліміт і обробка помилок були видимі.

П'ять термінальних станів, кожен з визначеною поведінкою:
    ok              — фінальна відповідь після ≥1 успішного виклику інструмента
    tool_error      — модель відповіла, але ЖОДЕН інструмент не віддав даних
    turns_exhausted — вичерпано ліміт кроків, повертаємо часткові знахідки
    api_error       — LLM API недоступний після ретраїв
    no_tool_used    — модель хотіла відповісти «з голови», не торкнувшись кластера
"""

import json
import os
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tools import TOOLS, ToolError

load_dotenv()

MAX_STEPS = int(os.getenv("SRE_MAX_STEPS", "6"))
API_RETRIES = 2
_BY_NAME = {t.name: t for t in TOOLS}

SYSTEM = """Ти SRE-асистент. Дебажиш проблеми в Kubernetes-кластері.

ЗАЛІЗНІ ПРАВИЛА:
1. Кожен факт у відповіді — з результату інструмента. Немає в tool-результаті — не пишеш.
   Не вгадуй імена подів, образи, exit code, вміст логів. Ніколи.
2. Порядок розслідування: list_unhealthy_pods → describe_pod → get_pod_logs.
   Імена подів беруться ТІЛЬКИ з list_unhealthy_pods.
3. Якщо інструмент повернув found=false — так і кажеш: даних немає. Це нормальна відповідь.
4. Якщо інструмент повернув помилку — кажеш, що саме зламалось, і не робиш висновків про
   те, чого не побачив.
5. Ти НЕ МОЖЕШ нічого змінювати в кластері: інструментів запису не існує. Фікс віддаєш
   людині як конкретну команду і чесно позначаєш, що НЕ виконав її.
6. Не питай дозволу, не обіцяй «зараз перевірю» — просто виклич інструмент.

Формат фінальної відповіді:
ДІАГНОЗ: <що зламано, з посиланням на namespace/pod>
ДОКАЗ: <конкретний рядок з логів або поле з describe>
ФІКС (НЕ ВИКОНАНО, виконай сам): <команда або дія>
"""


@dataclass
class Result:
    state: str
    answer: str
    steps: list = field(default_factory=list)
    tool_calls: int = 0
    tool_errors: int = 0

    def __str__(self) -> str:
        trace = "\n".join(f"  {i}. {s}" for i, s in enumerate(self.steps, 1))
        return (f"--- TRACE ---\n{trace}\n--- STATE: {self.state} "
                f"(tools: {self.tool_calls}, errors: {self.tool_errors}) ---\n{self.answer}")


def build_llm():
    """Дефолтна модель. Імпорт усередині, щоб тести працювали без ключа й без мережі."""
    from langchain_openai import ChatOpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY не заданий. Скопіюй .env.example у .env.")
    # base_url дозволяє OpenAI-сумісні шлюзи (OpenRouter тощо) — той самий клієнт.
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0,
                      base_url=os.getenv("OPENAI_BASE_URL") or None).bind_tools(TOOLS)


def _invoke(llm, messages, steps) -> AIMessage:
    """Виклик LLM з backoff. Кидає останню помилку, якщо всі спроби провалились."""
    for attempt in range(API_RETRIES + 1):
        try:
            return llm.invoke(messages)
        except Exception as e:
            steps.append(f"LLM api_error (спроба {attempt + 1}/{API_RETRIES + 1}): {type(e).__name__}: {e}")
            if attempt == API_RETRIES:
                raise
            time.sleep(2 ** attempt)


def _call_tool(call: dict) -> tuple[str, bool]:
    """Повертає (текст результату, is_error). Помилка інструмента НЕ валить процес —
    вона повертається в модель, щоб та могла виправитись або чесно здатись."""
    tool = _BY_NAME.get(call["name"])
    if tool is None:
        return f"ERROR: інструмента {call['name']} не існує. Доступні: {list(_BY_NAME)}", True
    try:
        return tool.invoke(call["args"]), False
    except ToolError as e:
        return f"ERROR: {e}", True
    except Exception as e:  # напр. невалідні аргументи від моделі
        return f"ERROR: {type(e).__name__}: {e}", True


def run(question: str, llm=None, max_steps: int = MAX_STEPS) -> Result:
    llm = llm or build_llm()
    messages = [SystemMessage(SYSTEM), HumanMessage(question)]
    res = Result(state="", answer="")
    facts: list[str] = []  # успішні tool-результати, для часткової відповіді

    for step in range(1, max_steps + 1):
        try:
            ai = _invoke(llm, messages, res.steps)
        except Exception as e:
            res.state = "api_error"
            res.answer = (f"Не можу відповісти: LLM API недоступний після {API_RETRIES + 1} спроб "
                          f"({type(e).__name__}: {e}). Дані з кластера не зібрані — жодних висновків не роблю.")
            return res
        messages.append(ai)

        if not ai.tool_calls:
            if res.tool_calls == 0:
                res.state = "no_tool_used"
                res.steps.append(f"крок {step}: модель відповіла без жодного інструмента — відповідь відкинуто")
                res.answer = ("Відмовляюсь відповідати: модель спробувала відповісти без звернення до кластера, "
                              "а такій відповіді вірити не можна. Уточни неймспейс — і я почну з list_unhealthy_pods.\n"
                              f"[відкинутий чернетковий текст моделі: {ai.content!r}]")
            elif not facts:
                res.state = "tool_error"
                res.answer = ("Не можу поставити діагноз: жоден інструмент не віддав даних "
                              f"({res.tool_errors} помилок). Останнє, що казала модель:\n{ai.content}")
            else:
                res.state = "ok"
                res.answer = ai.content
            return res

        for call in ai.tool_calls:
            res.tool_calls += 1
            out, is_error = _call_tool(call)
            res.tool_errors += is_error
            res.steps.append(f"крок {step}: {call['name']}({json.dumps(call['args'], ensure_ascii=False)}) "
                             f"-> {'ERROR ' if is_error else ''}{out[:160]}")
            if not is_error:
                facts.append(f"{call['name']}{call['args']} -> {out}")
            messages.append(ToolMessage(content=out, tool_call_id=call["id"],
                                        status="error" if is_error else "success"))

    res.state = "turns_exhausted"
    partial = "\n".join(f"- {f[:300]}" for f in facts) or "- нічого"
    res.answer = (f"Ліміт кроків вичерпано ({max_steps}). Діагноз НЕ поставлений — не вигадую його.\n"
                  f"Що встиг зібрати:\n{partial}\n"
                  f"Бракує: фінального висновку. Підніми SRE_MAX_STEPS або звузь питання до одного пода.")
    return res


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Що зламалось у неймспейсі shop-prod?"
    print(run(q))
