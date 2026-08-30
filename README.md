# ai-agents-pro — SRE-агент для дебагу Kubernetes

Агент, який розслідує зламані поди в кластері. Цикл tool-calling написаний руками
поверх LangChain (`ChatOpenAI.bind_tools`, без `AgentExecutor`) — щоб керування
кроками, ліміт і обробка помилок були видимі, а не заховані в абстракції.

Джерело даних — **фікстури** (`fixtures/cluster.json`), формат навмисно близький до
`kubectl get/describe/logs`. Жодного реального кластера: прогони відтворювані
будь-ким і не тягнуть за собою корпоративних даних.

## Ланцюжок інструментів

Другий крок неможливий без результату першого — імена подів існують тільки у
відповіді `list_unhealthy_pods`, вигадане ім'я повертає помилку:

```
list_unhealthy_pods(namespace)          → ["checkout-api-7d9f6c4b8-x9k2p", ...]
        ↓ ім'я звідси обов'язкове
describe_pod(namespace, pod_name)       → CrashLoopBackOff, exitCode 1, events
        ↓
get_pod_logs(namespace, pod_name, previous=true)
        → "FATAL bootstrap failed: pq: password authentication failed"
        ↓
ДІАГНОЗ + ДОКАЗ + ФІКС (не виконаний)
```

**Інструмента запису немає навмисно.** Це спроєктований стан «немає потрібного
інструмента»: агент не може зробити `rollout restart`, тому віддає команду людині
і явно позначає, що НЕ виконав її. Агент, який вдає, що полагодив прод, — гірший
за агента, який мовчить.

## П'ять станів циклу

| стан | коли | що робить |
|---|---|---|
| `ok` | фінальна відповідь після ≥1 успішного інструмента | діагноз + trace кроків |
| `tool_error` | модель договорила, але **жоден** інструмент не віддав даних | відмова від діагнозу, показує помилки |
| `turns_exhausted` | вичерпано `SRE_MAX_STEPS` (за замовчуванням 6) | часткові знахідки + чого бракує; **не** мовчазний обрив |
| `api_error` | LLM API недоступний після 3 спроб (backoff 1s, 2s) | чесна відмова, нуль висновків |
| `no_tool_used` | модель відповіла, не торкнувшись кластера | **відповідь відкидається**, чернетка показана як відкинута |

Помилка інструмента не валить процес — вона повертається в модель як `ToolMessage`
зі `status="error"`, щоб та могла виправитись або чесно здатись.

## Проти вигадування

- Кожен tool повертає JSON з явним `"found": bool`. `found=false` — це валідна
  відповідь («нездорових подів немає», «логів фізично немає»), а не помилка.
- Docstring кожного інструмента — контракт для моделі: що подавати, що повернеться,
  що означає `found=false`, чого не робити («не вгадуй імена»).
- System prompt вимагає посилання на namespace/pod для кожного факту.
- Стан `no_tool_used` механічно блокує відповідь «з голови» — навіть якщо модель
  вирішила, що знає відповідь.

## Запуск

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # встав OPENAI_API_KEY (і OPENAI_BASE_URL, якщо шлюз)
.venv/bin/python agent.py "Що зламалось у неймспейсі shop-prod?"
.venv/bin/python demos.py     # усі п'ять прогонів нижче
.venv/bin/python -m pytest -q # 11 тестів, без мережі й без ключа
```

Ключ береться тільки з `.env`, `.env` у `.gitignore` — у git його немає.

## Реальні прогони

Вивід нижче скопійований як є, без редагування.

### 1/5 — `ok`: повний ланцюжок, діагноз з доказом

```
$ .venv/bin/python demos.py ok
--- TRACE ---
  1. крок 1: list_unhealthy_pods({"namespace": "shop-prod"}) -> {"namespace": "shop-prod", "found": true, "unhealthy_count": 2, "pods": [{"name": "checkout-api-7d9f6c4b8-x9k2p", "phase": "Running", "ready": "0/1", "restarts"
  2. крок 2: describe_pod({"namespace": "shop-prod", "pod_name": "checkout-api-7d9f6c4b8-x9k2p"}) -> {"namespace": "shop-prod", "pod": "checkout-api-7d9f6c4b8-x9k2p", "phase": "Running", "containers": [{"name": "checkout-api", "image": "registry.local/checkout-
  3. крок 3: get_pod_logs({"namespace": "shop-prod", "pod_name": "checkout-api-7d9f6c4b8-x9k2p", "previous": true}) -> {"namespace": "shop-prod", "pod": "checkout-api-7d9f6c4b8-x9k2p", "previous": true, "found": true, "logs": "2026-08-30T09:08:51Z INFO  starting checkout-api v1.
--- STATE: ok (tools: 3, errors: 0) ---
ДІАГНОЗ: Проблема з подом `shop-prod/checkout-api-7d9f6c4b8-x9k2p`, причина - CrashLoopBackOff через помилку підключення до бази даних.
ДОКАЗ: "ERROR failed to open db: pq: password authentication failed for user \"checkout_rw\""
ФІКС (НЕ ВИКОНАНО, виконай сам): Перевірити правильність пароля для користувача `checkout_rw` у конфігурації підключення до бази даних.
```

Ланцюжок відпрацював як задумано: ім'я пода з кроку 1 → `describe_pod` → `get_pod_logs`
з `previous=true` (поточні логи в CrashLoopBackOff порожні). Фікс не виконано і це сказано.

### 2/5 — `tool_error`: кластер недоступний

```
$ .venv/bin/python demos.py tool_error
--- TRACE ---
  1. крок 1: list_unhealthy_pods({"namespace": "flaky-ns"}) -> ERROR ERROR: Unable to connect to the server: dial tcp 10.0.4.11:443: i/o timeout
--- STATE: tool_error (tools: 1, errors: 1) ---
Не можу поставити діагноз: жоден інструмент не віддав даних (1 помилок). Останнє, що казала модель:
ДІАГНОЗ: Кластер недоступний.
ДОКАЗ: ERROR: Unable to connect to the server: dial tcp 10.0.4.11:443: i/o timeout
ФІКС (НЕ ВИКОНАНО, виконай сам): Перевірити доступність кластера та мережеві налаштування.
```

Помилка інструмента не вбила процес — вона повернулась у модель, і та здалася чесно,
замість вигадати стан подів. Тут видно свідомий компроміс: модель відповіла коректно,
але оболонка все одно перекрила її вердикт своїм — бо нуль успішних інструментів означає
нуль підстав для діагнозу, і це правило не залежить від того, наскільки переконливо
звучить текст моделі.

### 3/5 — `no_tool_used`: спроба відповісти з голови заблокована

```
$ .venv/bin/python demos.py no_tool_used
--- TRACE ---
  1. крок 1: модель відповіла без жодного інструмента — відповідь відкинуто
--- STATE: no_tool_used (tools: 0, errors: 0) ---
Відмовляюсь відповідати: модель спробувала відповісти без звернення до кластера, а такій відповіді вірити не можна. Уточни неймспейс — і я почну з list_unhealthy_pods.
[відкинутий чернетковий текст моделі: "CrashLoopBackOff зазвичай означає, що контейнер у поді постійно завершується з помилкою і Kubernetes намагається перезапустити його, але не може, оскільки він продовжує падати. Це може бути викликано різними причинами, такими як:\n\n1. Помилки в коді програми.\n2. Неправильні конфігурації або відсутні залежності.\n3. Проблеми з ресурсами (наприклад, недостатньо пам'яті або CPU).\n4. Неправильні параметри запуску.\n...\nДля конкретного випадку потрібно спочатку перевірити список нездорових подів у кластері."]
```

Текст моделі був загалом правильний — і саме тому небезпечний: він звучить як діагноз
цього кластера, не будучи ним. Тому відповідь позначена як відкинута чернетка.

### 4/5 — `turns_exhausted`: ліміт кроків = 2

```
$ .venv/bin/python demos.py turns_exhausted
--- TRACE ---
  1. крок 1: list_unhealthy_pods({"namespace": "shop-prod"}) -> {"namespace": "shop-prod", "found": true, "unhealthy_count": 2, ...}
  2. крок 2: list_unhealthy_pods({"namespace": "billing"}) -> {"namespace": "billing", "found": false, "unhealthy_count": 0, "pods": []}
--- STATE: turns_exhausted (tools: 2, errors: 0) ---
Ліміт кроків вичерпано (2). Діагноз НЕ поставлений — не вигадую його.
Що встиг зібрати:
- list_unhealthy_pods{'namespace': 'shop-prod'} -> {"namespace": "shop-prod", "found": true, "unhealthy_count": 2, "pods": [{"name": "checkout-api-7d9f6c4b8-x9k2p", "phase": "Running", "ready": "0/1", "restarts": 17, "age": "2d", "reason": "CrashLoopBackOff"}, {"name": "image-worker-5c8d94f7-qq1mn", "
- list_unhealthy_pods{'namespace': 'billing'} -> {"namespace": "billing", "found": false, "unhealthy_count": 0, "pods": []}
Бракує: фінального висновку. Підніми SRE_MAX_STEPS або звузь питання до одного пода.
```

Обрив не мовчазний: сказано, що ліміт вичерпано, що встигли зібрати і чого бракує.
Заразом видно чесний `found: false` для здорового неймспейсу `billing`.

### 5/5 — `api_error`: LLM недоступний

```
$ .venv/bin/python demos.py api_error
--- TRACE ---
  1. LLM api_error (спроба 1/3): OpenAIAuthenticationError: Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}
  2. LLM api_error (спроба 2/3): OpenAIAuthenticationError: Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}
  3. LLM api_error (спроба 3/3): OpenAIAuthenticationError: Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}
--- STATE: api_error (tools: 0, errors: 0) ---
Не можу відповісти: LLM API недоступний після 3 спроб (OpenAIAuthenticationError: Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}). Дані з кластера не зібрані — жодних висновків не роблю.
```

Три спроби з backoff 1s/2s, далі відмова. Стектрейсу назовні немає, є причина.

## Тести

Скриптована LLM (`ScriptedLLM` у `test_agent.py`) підміняє модель — усі п'ять станів
і поведінка інструментів перевіряються без мережі та без ключа:

```
$ .venv/bin/python -m pytest -q
...........                                                              [100%]
11 passed in 0.06s
```

## Структура

```
agent.py      цикл, п'ять станів, ретраї, ліміт кроків
tools.py      три read-only інструменти + ToolError
demos.py      п'ять прогонів для README
test_agent.py 11 тестів (стани + інструменти), офлайн
fixtures/     фейковий кластер: CrashLoopBackOff, ImagePullBackOff, недоступний ns
```
