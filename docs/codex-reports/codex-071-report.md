# Задание 071

Добавлен необязательный флаг `--streamlake` в существующий experiment_069.
Он передаёт в OpenRouterProvider настройку `provider.order=["StreamLake"]`;
`allow_fallbacks=false`, require_parameters и ценовые ограничения сохранены.
Без флага поле order отсутствует. OnePostTransport проверяет ожидаемый order,
сохраняет лимит одного POST, retries=0, timeout=120, бюджет $0.10.
Промпт v2, квалификация и CSV-конвейер не изменены.

Проверены локальные provider/experiment, тесты и отчёт 069, метаданные requests
064 (они подтверждают успех модели, но не называют провайдера).
Чужие накопленные изменения рабочего дерева не отменялись.

Проверка официальной документации 2026-10-16:

- https://openrouter.ai/docs/guides/routing/provider-selection — order является
  массивом provider slugs; совместно с allow_fallbacks=false ограничивает маршрут.
- https://openrouter.ai/deepseek/deepseek-chat/api — официальный поисковый индекс
  перечисляет StreamLake и DeepInfra для deepseek/deepseek-chat, а также поддержку
  response_format и max_tokens.
- https://openrouter.ai/provider/streamlake — подтверждает написание StreamLake.

**Маршрут подтверждён частично:** модель обслуживается StreamLake по официальной
странице, но точное routing-значение `"StreamLake"` не удалось независимо
подтвердить. Актуальная документация требует slug; имя страницы и отображаемое
имя сами по себе не доказывают допустимость значения в order. Значение взято
из задания как явный экспериментальный кандидат, варианты и endpoint ID не
придумывались. Успех этого маршрута с require_parameters, JSON и текущими
ограничениями не установлен. Флаг не следует считать проверенной production
настройкой. Недоступный маршрут должен завершиться ошибкой без fallback.

Офлайн: 19 passed (test_streamlake_071, test_experiment_069, test_transport_061);
Ruff для изменённых Python-файлов — passed. Новые тесты используют только
синтетические сообщения/ключ и перехват транспорта, без сети и сохранения данных.

Одна команда для будущей контролируемой проверки из корня репозитория при уже
заданном OPENROUTER_API_KEY в окружении (ключ не помещается в argv/вывод).
Команда **не выполнялась**; каталог результата должен отсутствовать:

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.experiment_069 runs-054/doktstom-improved/dossier runs-071/doktstom-v2-streamlake-001 --packet-approval runs-058/packet-approval.json --streamlake
```

Бюджет трактован как $0.10 (в задании отображено `-bash.10`), уже фиксирован
профилем experiment_069. Это предел локального бюджетного учёта с резервированием,
а не отдельный серверный лимит OpenRouter. Разрешение/SHA пакета проверяются
существующим authorize; один запуск — максимум один POST. Платных AI-запросов,
запросов баланса/usage, commit, push, merge, deploy не было.
