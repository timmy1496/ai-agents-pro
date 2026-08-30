"""Челендж A: один і той самий запит на наївному (V1) і робочому (V2) описі інструментів.

Логіка інструментів ідентична — змінюється ЛИШЕ текст опису, який читає модель.

Запуск:  .venv/bin/python challenge_a.py
"""

import agent
import tools

QUERIES = [  # лишився запит, на якому V1 справді ламається
    # Людина називає под коротким іменем — спокуса підставити його в аргумент напряму.
    "Покажи логи checkout-api у неймспейсі shop-prod.",
]


def main():
    for query in QUERIES:
        print(f"\n{'#' * 78}\n# ЗАПИТ: {query}\n{'#' * 78}")
        for label, variant in (("V1 (наївний опис)", "v1"),
                               ("V1 + одне речення про ланцюжок", "v1_chain"),
                               ("V2 (робочий опис)", "v2")):
            tools.set_descriptions(variant)
            llm = agent.build_llm()  # перезбираємо: описи вшиваються у схему тулів
            print(f"\n----- {label} -----")
            print(agent.run(query, llm=llm))


if __name__ == "__main__":
    main()
