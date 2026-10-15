# Публикации ВК: исследование на 15 октября 2026

## Границы проверки

dev.vk.ru/dev.vk.com недоступны из рабочей среды: сетевой прокси возвращает 403.
Приложение 54824333, выдача ему прав и реальные публикации пока не проверены.
Ниже разделены официальные исходники и отчёты сторонних разработчиков.

## Официальные исходники

- [User.php](https://github.com/VKCOM/vk-php-sdk/blob/master/src/VK/OAuth/User/User.php):
  актуальный user OAuth — VK ID, Code Flow + PKCE; authorize и POST oauth2/auth,
  code, device_id и state. Наш адаптер vkid использует этот механизм.
- [Scopes.php](https://github.com/VKCOM/vk-php-sdk/blob/master/src/VK/OAuth/User/DTO/Scopes.php):
  есть WALL и PHOTOS. VK ID нельзя считать системой только телефона/email.
  Константы SDK не гарантируют выдачу рабочего write-доступа приложению.
- [wall.post](https://github.com/VKCOM/vk-api-schema/blob/master/wall/methods.json)
  в официальной схеме имеет access_token_type=user.
- [Методы фото](https://github.com/VKCOM/vk-api-schema/blob/master/photos/methods.json):
  photos.getWallUploadServer и photos.saveWallPhoto также имеют тип user.
- [Group.php](https://github.com/VKCOM/vk-php-sdk/blob/master/src/VK/OAuth/Group/Group.php):
  ключи сообщества используют отдельный group OAuth.

Старый README SDK всё ещё описывает прежний VKOAuth. Его примеры нельзя
автоматически переносить на новые приложения без сверки с актуальным кодом/API.

## Практические отчёты, не гарантии ВК

- [Issue #242 в официальном репозитории схемы](https://github.com/VKCOM/vk-api-schema/issues/242):
  разработчик сообщает, что текстовый wall.post работает с ключом сообщества,
  а photos.getWallUploadServer возвращает 27, несмотря на photos. Комментарии
  продолжаются в 2026. Площадка ВК не делает сообщение автора официальной гарантией.
- [vk-mcp-server, live-отчёт за октябрь 2026](https://github.com/bulatko/vk-mcp-server/blob/master/docs/SETUP.md):
  новый VK ID-токен может иметь scopes и получать 1051 на write-методах.
  Ключ сообщества может публиковать текст, но не загружать фото стены.
  Это сторонний отчёт, не доказанная нами универсальная политика для всех приложений.
- [PostFlow #35](https://github.com/hirdle/postflow/issues/35): описаны ограничения
  wall в code flow конкретного приложения и подтверждённый автором implicit/helper
  flow. Это не основание использовать чужой App ID или обещать его новому приложению.

## Следующий шаг без публикаций

Не покупать домен ради предположительного write-доступа. Сначала проверить
реальные права ключа и доступ к адресу загрузки фотографии:

```sh
cd /opt/publikot
.venv/bin/python -m publikot.vk_probe
```

ID сообщества публикаций и ключ вводятся локально. Ключ скрыт, не сохраняется,
в вывод не попадают токен и upload URL. Вызываются только:

groups.getTokenPermissions, account.getAppPermissions, photos.getWallUploadServer.

wall.post, отправки сообщений, загрузки и сохранения фотографий не выполняются.
Маска прав и получение upload URL не подтверждают реальную публикацию.

После диагностики отдельно проверяются подтверждённый тестовый текстовый пост
и фото. Если фото недоступно, не подменять его незаметно ссылкой/превью.
OAuth-кнопка и callback готовы программно, но не считаются готовой живой
интеграцией, пока собственное приложение не получит publishing-capable токен.
Тесты callback не доказывают выдачу прав со стороны ВК.
