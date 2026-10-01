FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# 前端构建产物（需先执行 npx ng build）
COPY frontend/dist/frontend/browser ./static

ENV FRONTEND_DIST=/app/static
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
