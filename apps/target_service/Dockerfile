FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.141.1" "uvicorn[standard]==0.52.4" \
    && useradd --create-home --uid 10002 target
COPY --chown=target:target main.py ./
USER target
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
