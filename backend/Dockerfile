FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 루트 requirements.txt (ai-pipeline 포함)
COPY requirements.txt /tmp/root_requirements.txt
RUN pip install --no-cache-dir -r /tmp/root_requirements.txt

# 백엔드 requirements.txt
COPY backend/requirements.txt /tmp/backend_requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend_requirements.txt

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
