# Официальный образ Playwright уже содержит Python, Chromium и все системные
# зависимости браузера — не нужно вручную ставить кучу apt-пакетов.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium уже установлен образом, но на всякий случай подтверждаем (кэшируется, если уже есть)
RUN playwright install chromium

COPY bot.py seed_db.py .

CMD ["python", "bot.py"]
