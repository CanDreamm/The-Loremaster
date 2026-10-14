# Запуск Python-бота ВК на VPS Рег.Облака

Ubuntu 24.04, Python 3.12+, доступ root по SSH. Для VK Bots Long Poll достаточно
исходящих HTTPS-соединений. Прокси, домен и открытый порт для бота не нужны.
Существующие n8n и PostgreSQL не удаляйте до отдельного решения.

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

Остановите службу, сделайте резервную копию существующей базы и обновите код.
В пути копии замените UPDATE_DATE на дату/время, чтобы не перезаписать старую.

```sh
systemctl stop publikot
cd /opt/publikot
sudo -u publikot .venv/bin/python scripts/backup.py data/publikot-vk.sqlite3 backups/before-UPDATE_DATE.sqlite3
sudo -u publikot git pull --ff-only origin master
sudo -u publikot .venv/bin/python -m pip install -r requirements.lock
sudo -u publikot .venv/bin/python -m unittest discover -s tests -v
systemctl start publikot
systemctl status publikot --no-pager
```

Если базы ВК ещё нет, этап backup пропускается; старую базу Telegram сохраняйте
по её исходному имени. Фото и .env.python копируйте отдельно. При ошибке Git не
используйте reset или force: сначала сохраните локальные изменения.

## Дальше

Подключить Yandex, проверить оплату и реальную генерацию. Для публикации задать
отдельный ключ проекта, проверить пост и фото в тестовом сообществе. Участникам
назначаются ID ВК. GenAPI и генерация изображений — отдельный следующий этап.
Результат публикации unknown требует ручной проверки стены, не повторного нажатия.
Прерванные события и генерации автоматически не возобновляются: проверьте /status.
Квоты ИИ и фоновая очередь пока не реализованы.
