# Запуск Python-бота ВК на VPS Рег.Облака

Ubuntu 24.04, Python 3.12+, доступ root по SSH. Для VK Bots Long Poll достаточно
исходящих HTTPS-соединений. Прокси, домен и открытый порт для бота не нужны.
Существующие n8n и PostgreSQL не удаляйте до отдельного решения.

## Если проект уже установлен

Начните с [определения способа установки и инструкции обновления](server-update.md).
Там отдельно описаны установка от root без службы, установка с пользователем
`publikot`, создание службы после ручного запуска и типовые ошибки.
Наличие `/opt/publikot` само по себе не означает, что пользователь и служба созданы.
Команды ниже для первой установки не выполняйте поверх существующего проекта.

## Первая установка

Команды выполняются на сервере, по одной. При ошибке остановитесь.
Если /opt/publikot или пользователь publikot уже существуют, сначала проверьте
их состояние: не клонируйте поверх существующей установки и не удаляйте данные.

```sh
apt-get update
apt-get install -y git python3-venv
git clone --branch master https://github.com/CanDreamm/The-Loremaster.git /opt/publikot
adduser --system --group --home /opt/publikot --no-create-home publikot
python3 -m venv /opt/publikot/.venv
/opt/publikot/.venv/bin/python -m pip install -r /opt/publikot/requirements.lock
mkdir -p /opt/publikot/data
chown -R publikot:publikot /opt/publikot
chmod 700 /opt/publikot/data
```

При запросе авторизации GitHub остановитесь — отдельно настроим доступ на чтение.
Не вводите токен в URL клонирования.

## Настройка ВК и пробный запуск

Включите сообщения и Bots Long Poll: [docs/vk-setup.md](vk-setup.md).

```sh
cd /opt/publikot
sudo -u publikot .venv/bin/python scripts/setup-python.py
sudo -u publikot .venv/bin/python -m publikot --check
sudo -u publikot .venv/bin/python -m publikot --identify
```

Отправьте /id в сообщения сообщества, укажите свой ID ВК повторным запуском
setup-python.py, оставив ключ пустым для сохранения текущего значения.

```sh
sudo -u publikot .venv/bin/python -m publikot
```

Проверьте /start, создание проекта, демо-генерацию, кнопки исправления и отмены.
Ctrl+C остановит пробный процесс. При переходе с Telegram используется отдельная
база publikot-vk.sqlite3. Старую базу и файлы никто не удаляет; ID автоматически
не переносятся между платформами.

## Постоянная служба

Этот раздел предполагает, что пользователь `publikot` создан, владеет проектом
и имеет доступ к данным, как описано при первой установке. Если вы запускали
бота от root и пользователя ещё нет, используйте
[переход на постоянную службу](server-update.md#создание-службы-после-установки-от-root).
При нестандартном `DATA_DIR` скорректируйте `ReadWritePaths` в unit-файле.

```sh
cp /opt/publikot/deploy/publikot.service /etc/systemd/system/publikot.service
systemctl daemon-reload
systemctl enable --now publikot
systemctl status publikot --no-pager
```

Логи: `journalctl -u publikot -n 50 --no-pager`. После правок .env.python:
`systemctl restart publikot`. Тестовый процесс сначала остановите: должен
работать один экземпляр бота.

## Обновления через Git

Следуйте [инструкции обновления](server-update.md): сначала диагностика, затем
подходящий вариант команд, резервная копия, обновление, проверки и запуск.
Вариант А рассчитан на root без службы, вариант Б — на уже созданную службу
с пользователем `publikot`. При ошибке остановитесь; не переключайтесь между
вариантами наугад.

## Дальше

Подключить Yandex, проверить оплату и реальную генерацию. Для публикации задать
отдельный ключ проекта, проверить пост и фото в тестовом сообществе. Участникам
назначаются ID ВК. GenAPI и генерация изображений — отдельный следующий этап.
Результат публикации unknown требует ручной проверки стены, не повторного нажатия.
Прерванные события и генерации автоматически не возобновляются: проверьте /status.
Квоты ИИ и фоновая очередь пока не реализованы.
