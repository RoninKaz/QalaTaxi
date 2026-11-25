# Qala Taxi

Сервис заказа такси: клиент, водитель, администратор. Python 3.10+, только стандартная библиотека, база SQLite.

## Структура

```
QalaTaxi/
├── main.py                  точка входа
├── app/
│   ├── web_app.py           HTTP-сервер, страницы, права доступа
│   ├── service.py           заказы, тарифы, расчёты
│   ├── accounts.py          аккаунты и сессии
│   ├── google_auth.py       вход через Google (OAuth)
│   └── static/style.css
├── tests/                   автотесты
├── docs/GOOGLE_SETUP.txt    настройка Google-входа
├── render.yaml              настройки для Render
├── google_oauth.example.json  шаблон настроек Google
└── start.bat                запуск в один клик (Windows)
```

## Запуск у себя

```
python main.py
```
Адрес: http://127.0.0.1:8000. Вход по телефону и паролю работает сразу.
Тесты: `python -m unittest discover -s tests -t .`

## Google-вход

Нужен свой OAuth-клиент в Google Cloud (docs/GOOGLE_SETUP.txt).
Настройки задаются файлом `google_oauth.json` (по образцу example) или переменными окружения:
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `ADMIN_EMAILS` (через запятую).
Вход администратора через Google открыт только для почт из `ADMIN_EMAILS`.
Файл `google_oauth.json` и `taxi.db` никому не передавайте.

## Публикация на Render

1. Загрузите проект в репозиторий GitHub (файлы `google_oauth.json` и `taxi.db` туда попасть не должны, `.gitignore` их исключает).
2. На render.com: New → Web Service → выберите репозиторий. Render подхватит `render.yaml`.
3. В Environment заполните: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ADMIN_EMAILS`,
   `GOOGLE_REDIRECT_URI` = `https://<имя-сервиса>.onrender.com/auth/google/callback`.
4. В Google Cloud → Credentials → ваш OAuth-клиент → Authorized redirect URIs добавьте тот же адрес.

Важно: на бесплатном плане Render файлы сбрасываются при каждом перезапуске, поэтому
аккаунты и заказы будут пропадать. Для постоянного хранения подключите диск
(платный план, см. комментарий в `render.yaml`) и задайте `DATABASE_PATH=/var/data/taxi.db`.
Без Google-настроек сайт тоже работает, просто Google-вход неактивен.
