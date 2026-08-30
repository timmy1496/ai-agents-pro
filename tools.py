"""Інструменти SRE-агента поверх фікстур кластера.

Усе, що стосується самого кластера, — read-only: інструмента, який ЗМІНЮЄ кластер
(rollout restart, delete pod), тут немає навмисно. Це спроєктований стан «немає
потрібного інструмента» — агент віддає команду фіксу людині, а не вдає, що виконав її.

Єдина дія з наслідками — create_incident: створює запис у журналі інцидентів.
Вона незворотна (запис бачать чергові), тому проходить два бар'єри: перевірку на
дублікат і явне підтвердження людини — саме в такому порядку.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

FIXTURES = Path(os.getenv("SRE_FIXTURES", Path(__file__).parent / "fixtures" / "cluster.json"))
INCIDENTS = Path(os.getenv("SRE_INCIDENTS", Path(__file__).parent / "incidents.json"))

# Що вважаємо нездоровим: не Running, або не всі контейнери ready, або є перезапуски.
_UNHEALTHY_PHASES = {"Pending", "Failed", "Unknown"}


class ToolError(Exception):
    """Інструмент не зміг виконатись (аналог kubectl exit != 0)."""


def _confirm_via_stdin(question: str) -> bool:
    return input(f"\n[ПІДТВЕРДЖЕННЯ ЛЮДИНИ] {question} [y/N] ").strip().lower() in {"y", "yes", "т", "так"}


# Підміняється в тестах і в демо. Дефолт — реальне питання в термінал.
CONFIRM = _confirm_via_stdin


def _load() -> dict:
    try:
        return json.loads(FIXTURES.read_text())
    except FileNotFoundError:
        raise ToolError(f"fixtures not found at {FIXTURES}")
    except json.JSONDecodeError as e:
        raise ToolError(f"fixtures are not valid JSON: {e}")


def _namespace(name: str) -> dict:
    ns = _load()["namespaces"].get(name)
    if ns is None:
        raise ToolError(
            f'namespace "{name}" not found. Error from server (NotFound): '
            f'namespaces "{name}" not found. '
            f"Call list_namespaces to get the real list — do not guess namespace names."
        )
    if "_error" in ns:  # імітація недоступного кластера
        raise ToolError(ns["_error"])
    return ns


def _pod(namespace: str, pod_name: str) -> dict:
    pod = _namespace(namespace)["pods"].get(pod_name)
    if pod is None:
        raise ToolError(
            f'pod "{pod_name}" not found in namespace "{namespace}". '
            f"Get a real pod name from list_unhealthy_pods first — do not guess names."
        )
    return pod


def _is_unhealthy(pod: dict) -> bool:
    ready, _, total = pod["ready"].partition("/")
    return pod["phase"] in _UNHEALTHY_PHASES or ready != total or pod["restarts"] > 0


@tool
def list_namespaces() -> str:
    """Перелік усіх неймспейсів кластера (як `kubectl get namespaces`).

    Виклич це, якщо користувач НЕ назвав конкретний неймспейс або просить перевірити
    «весь кластер». Не вгадуй імена неймспейсів — їх не можна вивести з голови.

    Returns:
        JSON: {"found": bool, "namespaces": ["shop-prod", ...]}
    """
    names = sorted(_load()["namespaces"])
    return json.dumps({"found": bool(names), "namespaces": names}, ensure_ascii=False)


@tool
def list_unhealthy_pods(namespace: str) -> str:
    """Список нездорових подів у неймспейсі: не Running, не всі контейнери ready, або з перезапусками.

    ЦЕ ПЕРШИЙ КРОК будь-якого розслідування. Імена подів беруться ТІЛЬКИ звідси —
    describe_pod і get_pod_logs потребують точного імені пода з цього списку.

    Args:
        namespace: точна назва неймспейсу, напр. "shop-prod". Не вгадуй — питай користувача.

    Returns:
        JSON: {"namespace", "found": bool, "unhealthy_count", "pods": [{"name","phase","ready","restarts","age","reason"}]}
        found=false означає, що нездорових подів НЕМАЄ — це валідна відповідь, не помилка.

    Raises:
        ToolError: неймспейсу не існує або кластер недоступний.
    """
    pods = _namespace(namespace)["pods"]
    bad = [
        {"name": name, "phase": p["phase"], "ready": p["ready"],
         "restarts": p["restarts"], "age": p["age"], "reason": p["reason"]}
        for name, p in pods.items() if _is_unhealthy(p)
    ]
    return json.dumps({"namespace": namespace, "found": bool(bad),
                       "unhealthy_count": len(bad), "pods": bad}, ensure_ascii=False)


@tool
def describe_pod(namespace: str, pod_name: str) -> str:
    """Деталі пода: статуси контейнерів, причина падіння, exit code, події (як `kubectl describe pod`).

    Потребує ТОЧНОГО імені пода, отриманого з list_unhealthy_pods. Вигадане ім'я поверне помилку.

    Args:
        namespace: неймспейс пода.
        pod_name: повне ім'я пода з list_unhealthy_pods, напр. "checkout-api-7d9f6c4b8-x9k2p".

    Returns:
        JSON: {"namespace","pod","phase","containers":[{name,image,ready,restartCount,state,lastState}],"events":[...]}
        Поле containers[].lastState.terminated.exitCode — головний сигнал для CrashLoopBackOff.

    Raises:
        ToolError: под або неймспейс не знайдено, кластер недоступний.
    """
    pod = _pod(namespace, pod_name)
    return json.dumps({"namespace": namespace, "pod": pod_name, "phase": pod["phase"],
                       "containers": pod["containers"], "events": pod["events"]}, ensure_ascii=False)


@tool
def get_pod_logs(namespace: str, pod_name: str, previous: bool = False) -> str:
    """Логи контейнера пода (як `kubectl logs`). Для CrashLoopBackOff бери previous=true.

    Потребує ТОЧНОГО імені пода з list_unhealthy_pods.

    Args:
        namespace: неймспейс пода.
        pod_name: повне ім'я пода з list_unhealthy_pods.
        previous: true — логи попереднього (вбитого) запуску контейнера. Для пода, що
            падає в циклі, поточні логи часто порожні, а причина саме в previous.

    Returns:
        JSON: {"namespace","pod","previous","found": bool, "logs": str|null, "reason": str|null}
        found=false + reason означає, що логів фізично немає (напр. контейнер ще не стартував).
        НЕ вигадуй вміст логів, якщо found=false.

    Raises:
        ToolError: под або неймспейс не знайдено, кластер недоступний.
    """
    logs = _pod(namespace, pod_name)["logs"]
    text = logs["previous"] if previous else logs["current"]
    if text is None:
        return json.dumps({"namespace": namespace, "pod": pod_name, "previous": previous,
                           "found": False, "logs": None,
                           "reason": logs.get("_unavailable", "no logs for this container")},
                          ensure_ascii=False)
    return json.dumps({"namespace": namespace, "pod": pod_name, "previous": previous,
                       "found": True, "logs": text, "reason": None}, ensure_ascii=False)


def _incident_key(namespace: str, pod_name: str, reason: str | None) -> str:
    """Природний ключ ідемпотентності: той самий под з тією ж причиною — той самий інцидент.

    Свідомо НЕ включає summary: якщо ключем зробити текст від моделі, дублікат
    створиться від будь-якого перефразування, і вся перевірка стане декорацією.
    """
    return f"{namespace}/{pod_name}/{reason or 'unknown'}"


def _load_incidents() -> list:
    try:
        return json.loads(INCIDENTS.read_text())
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise ToolError(f"журнал інцидентів пошкоджений ({INCIDENTS}): {e}")


@tool
def create_incident(namespace: str, pod_name: str, summary: str, evidence: str) -> str:
    """Створює запис в журналі інцидентів. ЄДИНА дія з наслідками — усе інше read-only.

    Створення незворотне: запис бачать чергові інженери. Тому інструмент сам, ДО запису:
      1. перевіряє, чи інцидент для цього пода з цією причиною вже існує (ідемпотентність);
      2. якщо ні — питає підтвердження в людини.
    Обидва бар'єри всередині інструмента, не в твоїй відповідальності. Не проси дозволу
    в тексті — просто виклич, людину спитають без тебе.

    Викликай ТІЛЬКИ після того, як зібрав докази через describe_pod / get_pod_logs.
    Не створюй інцидент на здоровий под і не вигадуй pod_name.

    Args:
        namespace: неймспейс пода.
        pod_name: повне ім'я пода з list_unhealthy_pods.
        summary: один рядок — що зламано.
        evidence: конкретний рядок з логів або поля describe, на якому стоїть висновок.

    Returns:
        JSON: {"created": bool, "incident": {...}, "reason": str}
        created=false з reason="duplicate" — інцидент уже є, повертається існуючий (це УСПІХ, не помилка).
        created=false з reason="declined_by_human" — людина відмовила. Не обходь це і не повторюй виклик.

    Raises:
        ToolError: под не знайдено або журнал пошкоджений.
    """
    pod = _pod(namespace, pod_name)  # інцидент на неіснуючий под не створюється
    key = _incident_key(namespace, pod_name, pod["reason"])
    incidents = _load_incidents()

    existing = next((i for i in incidents if i["key"] == key), None)
    if existing:  # перевірка ПЕРЕД підтвердженням: не смикаємо людину на no-op
        return json.dumps({"created": False, "incident": existing, "reason": "duplicate"},
                          ensure_ascii=False)

    if not CONFIRM(f"Створити інцидент для {namespace}/{pod_name} — {summary}?"):
        return json.dumps({"created": False, "incident": None, "reason": "declined_by_human"},
                          ensure_ascii=False)

    incident = {"id": f"INC-{len(incidents) + 1:04d}", "key": key, "namespace": namespace,
                "pod": pod_name, "reason": pod["reason"], "summary": summary,
                "evidence": evidence, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    incidents.append(incident)
    INCIDENTS.write_text(json.dumps(incidents, ensure_ascii=False, indent=2) + "\n")
    return json.dumps({"created": True, "incident": incident, "reason": "created"}, ensure_ascii=False)


TOOLS = [list_namespaces, list_unhealthy_pods, describe_pod, get_pod_logs, create_incident]


# --- Челендж A: експеримент «опис інструмента = поведінка агента» ------------
# V2 = описи вище (docstring'и). V1 = наївна перша версія: технічно правдива,
# але без контракту ланцюжка і без семантики found=false.
_V2_DESCRIPTIONS = {t.name: t.description for t in TOOLS}
_V1_DESCRIPTIONS = {
    "list_namespaces": "Повертає список неймспейсів.",
    "create_incident": "Створює інцидент.\n\nArgs:\n    namespace, pod_name, summary, evidence.",
    "list_unhealthy_pods": "Повертає нездорові поди в неймспейсі.\n\nArgs:\n    namespace: назва неймспейсу.",
    "describe_pod": "Повертає деталі пода: контейнери, події, статуси.\n\nArgs:\n    namespace: неймспейс.\n    pod_name: ім'я пода.",
    "get_pod_logs": "Повертає логи пода.\n\nArgs:\n    namespace: неймспейс.\n    pod_name: ім'я пода.\n    previous: логи попереднього запуску.",
}


_CHAIN_HINT = "\n\nІмена подів беруться ТІЛЬКИ з list_unhealthy_pods. Вигадане ім'я поверне помилку."
# V1+chain: наївний опис ПЛЮС одне речення про ланцюжок — щоб виміряти, що саме вирішує.
_V1_CHAIN_DESCRIPTIONS = {n: d + (_CHAIN_HINT if n.startswith(("describe", "get_")) else "")
                          for n, d in _V1_DESCRIPTIONS.items()}
_VARIANTS = {"v1": _V1_DESCRIPTIONS, "v1_chain": _V1_CHAIN_DESCRIPTIONS, "v2": _V2_DESCRIPTIONS}


def set_descriptions(variant: str) -> None:
    """Перемикає описи інструментів між версіями: v1 (наївна), v1_chain, v2 (робоча)."""
    for t in TOOLS:
        t.description = _VARIANTS[variant][t.name]
