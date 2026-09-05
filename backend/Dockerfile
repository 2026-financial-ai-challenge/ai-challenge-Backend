FROM python:3.13-slim

WORKDIR /app

COPY backend/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY ai/ /packages/ai/

EXPOSE 8000

CMD ["sh", "start.sh"]
