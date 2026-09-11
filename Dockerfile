# Работает на любом хостинге с Docker (Render, Railway, Fly.io, VPS).
# Запуск: docker run -d --restart unless-stopped -e DISCORD_TOKEN=... -v $PWD/data:/app/data modbot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data

# keepalive поднимется сам, если хост задал $PORT; локально можно -p 8080:8080
EXPOSE 8080
CMD ["python", "bot.py"]
