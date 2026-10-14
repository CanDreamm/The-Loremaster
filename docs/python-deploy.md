# Запуск Python-бота на существующем VPS Рег.Облака

Существующие n8n и PostgreSQL пока не удаляем. Следующие команды выполняются
на вашем VPS, не в облачной среде разработки. Нужен Ubuntu 24.04 и доступ root по SSH.
Порт для бота открывать не нужно: он сам обращается к Telegram через прокси.

## 1. Получить код через GitHub

В SSH-терминале сервера, под root:

```sh
apt-get update
apt-get install -y git python3-venv
```

Если /opt/publikot уже существует, сначала проверьте содержимое. Не удаляйте
папку и не клонируйте поверх неизвестной установки. Для новой установки:

```sh
git clone --branch master https://github.com/CanDreamm/The-Loremaster.git /opt/publikot
```

Если GitHub запросит авторизацию, остановитесь: для закрытого репозитория отдельно
настроим доступ на чтение через SSH deploy key. Не вводите токен в URL команды.

## 2. Установить зависимости

```sh
adduser --system --group --home /opt/publikot --no-create-home publikot
python3 -m venv /opt/publikot/.venv
/opt/publikot/.venv/bin/python -m pip install -r /opt/publikot/requirements.lock
mkdir -p /opt/publikot/data
chown -R publikot:publikot /opt/publikot
chmod 700 /opt/publikot/data
```

## 3. Настройки

```sh
cd /opt/publikot
sudo -u publikot .venv/bin/python scripts/setup-python.py
```

Ввод токена и адреса прокси скрыт. Формат TELEGRAM_PROXY:
`http://USER:PASSWORD@HOST:PORT` или `socks5://USER:PASSWORD@HOST:PORT`.
Спецсимволы в пароле должны быть URL-кодированы. Данные не передавайте в чат.
Если ID Telegram неизвестен, оставьте его пустым, затем выполните:

```sh
sudo -u publikot .venv/bin/python -m publikot --identify
```

Отправьте /start в личную переписку @publikot_bot. Найдите свой ID в выводе.
Откройте файл локально командой `nano /opt/publikot/.env.python` и заполните
OWNER_TELEGRAM_ID. Для начала оставьте DEMO_MODE=true.

Если бот сообщает о вебхуке, сначала отключите старый Telegram workflow n8n,
затем выполните `sudo -u publikot .venv/bin/python -m publikot --identify --clear-webhook`.
Не делайте этого, если старый workflow ещё нужен для другой работы.

## 4. Пробный запуск

```sh
sudo -u publikot .venv/bin/python -m publikot
```

В Telegram: /start → /new Первый проект → /concept Описание → /post Тема.
Проверьте /revise Сделай короче, /status и отмену. В демо публикация запрещена.
Ctrl+C завершает пробный запуск. Это нужно сделать до запуска постоянной службы.

## 5. Автозапуск

```sh
cp /opt/publikot/deploy/publikot.service /etc/systemd/system/publikot.service
systemctl daemon-reload
systemctl enable --now publikot
systemctl status publikot --no-pager
```

Логи: `journalctl -u publikot -n 50 --no-pager`.
После изменения .env.python: `systemctl restart publikot`.
Секреты нельзя передавать в аргументах команд или выкладывать в переписку.

## 6. Реальные сервисы — после пробного запуска

Подключить Yandex AI Studio и проверить оплату, баланс и генерацию на вашем
аккаунте. Подключить ключ ВК и тестовое сообщество, проверить текстовый пост,
затем фото. Установить DEMO_MODE=false и перезапустить службу. Провести полный
цикл с исправлением и подтверждением, проверить отсутствие повторной отправки.
GenAPI подключается отдельно после выбора модели; не покупайте большой пакет заранее.

Если отправка имеет статус unknown, проверьте стену ВК вручную. Автоматического
повтора нет, чтобы не создавать дубли. Незавершённая генерация после перезапуска
получает failed: создайте новое задание. Версии и другие данные сохраняются.

После успешного переноса можно отдельно решить, останавливать ли n8n и postgres,
чтобы освободить память на сервере с 1 ГБ RAM. Данные сначала сохраняются.

## 7. Обновления через Git

Сначала остановите бота и сделайте резервную копию, если база уже существует.
Для каждой копии нужен новый путь; в примере замените UPDATE_DATE на дату и время:

```sh
systemctl stop publikot
cd /opt/publikot
sudo -u publikot .venv/bin/python scripts/backup.py data/publikot.sqlite3 backups/before-UPDATE_DATE.sqlite3
sudo -u publikot git pull --ff-only origin master
sudo -u publikot .venv/bin/python -m pip install -r requirements.lock
sudo -u publikot .venv/bin/python -m unittest discover -s tests -v
systemctl start publikot
systemctl status publikot --no-pager
```

Выполняйте команды по одной. При ошибке остановитесь и разберите её до запуска
следующих команд. Если Git сообщает о локальных изменениях, не используйте reset
или force: сначала сохраните и проверьте изменения. `.env.python` и `data/` не
отслеживаются Git и обычным обновлением кода не заменяются.
