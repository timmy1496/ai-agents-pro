"""П'ять прогонів для README: один щасливий і чотири зламані.

Запуск:  .venv/bin/python demos.py            # усі п'ять
         .venv/bin/python demos.py ok         # один
Вивід копіюється в README як є — без редагування.
"""

import os
import sys

import agent


def ok():
    """1/5 ok — повний ланцюжок list → describe/logs → діагноз."""
    return agent.run("Що зламалось у неймспейсі shop-prod? Постав діагноз по одному найгіршому поду.")


def tool_error():
    """2/5 tool_error — кластер недоступний, даних немає взагалі."""
    return agent.run("Перевір неймспейс flaky-ns, там скаржаться на 500-ки.")


def no_tool_used():
    """3/5 no_tool_used — питання спокушає відповісти з голови."""
    return agent.run("Що зазвичай означає CrashLoopBackOff і як його чинять?")


def turns_exhausted():
    """4/5 turns_exhausted — ліміт кроків урізаний до 2."""
    return agent.run("Порівняй стан усіх подів у shop-prod і billing та знайди спільну причину.",
                     max_steps=2)


def api_error():
    """5/5 api_error — свідомо невалідний ключ."""
    os.environ["OPENAI_API_KEY"] = "sk-invalid-key-for-demo"
    return agent.run("Що зламалось у shop-prod?")


DEMOS = [ok, tool_error, no_tool_used, turns_exhausted, api_error]

if __name__ == "__main__":
    picked = [d for d in DEMOS if not sys.argv[1:] or d.__name__ in sys.argv[1:]]
    for demo in picked:
        print(f"\n{'=' * 78}\n$ python demos.py {demo.__name__}\n# {demo.__doc__.splitlines()[0]}\n{'=' * 78}")
        print(demo())
