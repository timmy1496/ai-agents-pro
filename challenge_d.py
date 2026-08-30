"""Челендж D: дія з наслідками — той самий запит двічі, другий не створює дублікат.

Підтвердження людини тут автоматизоване, щоб прогін можна було зафіксувати в README:
питання друкується дослівно і відповідь «так» дається скриптом. У звичайному запуску
(`agent.py`, `demos.py`) CONFIRM — це справжній input() у терміналі.

Запуск:  .venv/bin/python challenge_d.py
"""

import json
import pathlib

import agent
import tools

QUERY = "Розберись з подом checkout-api-7d9f6c4b8-x9k2p у shop-prod і заведи інцидент."


def main():
    tools.INCIDENTS = pathlib.Path("incidents.json")
    tools.INCIDENTS.unlink(missing_ok=True)  # чистий старт демо
    tools.CONFIRM = lambda q: print(f"\n[ПІДТВЕРДЖЕННЯ ЛЮДИНИ] {q} [y/N] y  <- відповідь скрипта") or True

    for attempt in (1, 2):
        print(f"\n{'#' * 78}\n# ПРОГІН {attempt}: {QUERY}\n{'#' * 78}")
        print(agent.run(QUERY))

    print(f"\n{'#' * 78}\n# ЖУРНАЛ ІНЦИДЕНТІВ ПІСЛЯ ДВОХ ПРОГОНІВ\n{'#' * 78}")
    records = json.loads(tools.INCIDENTS.read_text())
    print(f"записів у файлі: {len(records)}")
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
