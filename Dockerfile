FROM node:22-alpine AS frontend
WORKDIR /app/geng_agent/web/frontend
COPY geng_agent/web/frontend/package*.json ./
RUN npm install
COPY geng_agent/web/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
COPY . .
COPY --from=frontend /app/geng_agent/web/frontend/dist ./geng_agent/web/frontend/dist
RUN pip install --no-cache-dir -e ".[web,repro]"
EXPOSE 8765
CMD ["uvicorn", "geng_agent.web.app:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "2"]
