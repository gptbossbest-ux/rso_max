# Закрытый Docker-стенд

Правила веток, Pull Request, CI и хранения секретов описаны в
[GitHub-инфраструктуре](docs/GITHUB_INFRASTRUCTURE.md).

Стенд запускает три независимых процесса из одного образа:

- `api` — FastAPI, доступен только внутри Docker-сети;
- `web` — Flask/Gunicorn, опубликован только на `127.0.0.1:5000` сервера;
  число процессов и потоков задаётся `WEB_WORKERS` (по умолчанию 2) и
  `WEB_THREADS` (по умолчанию 4), timeout — `WEB_TIMEOUT` (60 секунд);
- `bot` — polling-процесс MAX, запускается отдельным профилем после добавления токена.

Образ содержит официальный корневой сертификат Минцифры `Russian Trusted Root
CA`, необходимый для проверки TLS-сертификата `platform-api2.max.ru`. Исходный
файл загружен с `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`;
SHA-256 отпечаток сертификата:
`D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`.

## Два изолированных контура

На одном сервере используются независимые Compose-проекты:

- `test`: `.env.test` + `.env.test.runtime`, данные в `runtime/test`, веб-порт `5001`;
- `prod`: `.env` + `.env.prod.runtime`, данные в `runtime/prod`, веб-порт `5000`.

Для каждого контура также используется собственный каталог квитанций:
`runtime/test/kv` или `runtime/prod/kv`. Он монтируется в контейнер как
`/app/KV` только для чтения. PDF-квитанции копируйте в нужный runtime-каталог;
их нельзя добавлять в Git.

Пользовательские `.env` не изменяются скриптами развёртывания. Одноразовые
bootstrap-пароли администраторов можно хранить в соответствующем
`.env.*.runtime`. Все эти файлы исключены из Git. Скрипт принудительно задаёт
`APP_ENV=test` для `test` и `APP_ENV=production` для `prod`, независимо от
локального содержимого `.env`.

## Первый запуск

```bash
cp .env.example .env.test
cp .env.runtime.example .env.test.runtime
cp .env.example .env
cp .env.runtime.example .env.prod.runtime
chmod 600 .env.test .env.test.runtime .env .env.prod.runtime
```

В `.env.test` и `.env` задайте разные `TOKEN`, `SECRET_KEY` и
`INTERNAL_API_TOKEN`. Настройки интеграции с 1С также задаются отдельно для
каждого контура и не удаляются скриптами. При первом запуске с пустой базой в
двух runtime-файлах задайте разные одноразовые значения
`BOOTSTRAP_ADMIN_PASSWORD` длиной не менее 12 символов. После первого входа приложение потребует сменить пароль. Сначала разверните
тестовый контур:

```bash
./scripts/deploy.sh test
```

Выполните health-check, smoke-тесты и приёмку. `prod` запускается только после
успешной приёмки `test` **того же SHA**:

```bash
./scripts/deploy.sh prod
```

Запускайте скрипты от непривилегированного владельца runtime-каталогов. При
сборке UID/GID пользователя контейнера автоматически берутся из `id -u` и
`id -g`, поэтому bind mounts не зависят от фиксированного UID 1000. При
необходимости значения можно явно передать через `APP_UID` и `APP_GID`.

Перед запуском `deploy.sh` проверяет выбранный токен официальным MAX `GET /me`.
Пустой или недействительный токен останавливает развёртывание. Если настроен
второй контур, скрипт также запрещает запуск одного MAX-бота одновременно в
test и prod. Значения токенов в вывод не попадают.

После первого входа и смены bootstrap-пароля очистите
`BOOTSTRAP_ADMIN_PASSWORD` в соответствующем `.env.*.runtime` и повторно
выполните `deploy.sh test` или `deploy.sh prod`. Скрипт пересоздаёт контейнеры,
чтобы одноразовый пароль исчез из их окружения.

Панель намеренно не доступна из интернета. Откройте SSH-туннель с рабочего
компьютера:

```powershell
ssh -L 5000:127.0.0.1:5000 botadmin@SERVER_IP
```

После этого откройте `http://127.0.0.1:5000`.

Для одновременного доступа к обеим панелям:

```powershell
ssh -L 5000:127.0.0.1:5000 -L 5001:127.0.0.1:5001 botadmin@SERVER_IP
```

Тестовая панель откроется по адресу `http://127.0.0.1:5001`.

## Запуск MAX-бота

Скрипт `deploy.sh` запускает бот выбранного контура вместе с API и веб-панелью.

## Проверки

Для уже развёрнутых контуров проверьте health endpoints:

```bash
curl --fail http://127.0.0.1:5001/healthz
curl --fail http://127.0.0.1:5000/healthz
```

Обязательный порядок нового релиза — `test`, приёмка и только затем `prod` того
же SHA — описан в разделе [«Обновление только из `main`»](#обновление-только-из-main-сначала-test-затем-prod) ниже.

### Read-only smoke test

`scripts/smoke.sh test` только читает состояние test-контура. Другие аргументы
отклоняются. Скрипт не отправляет сообщения в MAX, не вызывает 1С и не выводит
окружение. Он проверяет:

- принадлежность контейнеров Compose-проекту, running/healthy и restart count;
- loopback `/healthz` с ограниченным timeout и свежесть heartbeat бота;
- SQLite `PRAGMA quick_check` через URI `mode=ro`;
- не менее 15% свободного места;
- commit и безопасный сокращённый image ID без конфигурации и секретов.

Ручной запуск из активного release:

```bash
./scripts/smoke.sh test
```

Для test-контура предусмотрен systemd timer раз в пять минут. Installer должен
запускаться от root и принимает абсолютный путь существующего release строго
внутри `/srv/bot-sandbox/releases`. Например, для текущего release:

```bash
sudo ./scripts/install-smoke-systemd.sh \
  /srv/bot-sandbox/releases/rso_max-2973d64
```

Installer проверяет `compose.yaml`, атомарно обновляет стабильную ссылку
`/srv/bot-sandbox/current/test`, копирует smoke в root-owned
`/usr/local/libexec/rso-max-smoke` и устанавливает только test unit/timer.
Smoke запускается от `botadmin` с `timeout`, `flock` и systemd hardening.
Результаты доступны без вывода секретов:

```bash
systemctl status rso-max-smoke-test.timer
journalctl -u rso-max-smoke-test.service --since today
```

Prod-контур намеренно не входит в область этого smoke-monitoring.

## Резервная копия SQLite

```bash
./scripts/backup.sh test
./scripts/backup.sh prod
```

Скрипт использует SQLite Backup API, поэтому копия согласована даже при работающих
процессах. Сначала создаётся временный файл, затем выполняется
`PRAGMA integrity_check` и только после успешной проверки файл атомарно получает
итоговое имя. Копии старше 30 дней удаляются. Для автоматического запуска
добавьте отдельные задания cron для `backup.sh test` и `backup.sh prod`.

## Обновление только из `main`: сначала `test`, затем `prod`

Получите актуальный `main` и зафиксируйте полный SHA кандидата. Не используйте
плавающую ссылку `latest`: один и тот же принятый SHA должен пройти оба контура.

```bash
git fetch origin main
RELEASE_SHA="$(git rev-parse --verify origin/main^{commit})"
printf 'Release candidate: %s\n' "$RELEASE_SHA"

./scripts/backup.sh test
git switch --detach "$RELEASE_SHA"
./scripts/deploy.sh test
curl --fail http://127.0.0.1:5001/healthz
```

После health-check выполните smoke-тесты и приёмку в `test`. Если кандидат не
принят, не продвигайте его в `prod`: исправление оформляется новым commit в
`main`, после чего процесс начинается заново с новым SHA.

Только после успешной приёмки сделайте резервную копию production непосредственно
перед его обновлением и разверните **тот же** SHA:

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
./scripts/backup.sh prod
./scripts/deploy.sh prod
curl --fail http://127.0.0.1:5000/healthz
```

`git switch --detach` переводит сервер на уже полученный точный commit без
перезаписи локальных файлов через `git reset --hard`. Рабочее дерево перед
началом должно быть чистым; локальные изменения стенда храните вне Git.

Для отката выберите полный SHA последнего успешно проверенного релиза, создайте
актуальную резервную копию затронутого контура, переключитесь на этот SHA через
`git switch --detach <SHA>` и повторите `deploy.sh`. Если неуспешный релиз
изменил данные несовместимым образом, остановите сервисы и восстановите
проверенную копию SQLite согласно плану восстановления данных.
