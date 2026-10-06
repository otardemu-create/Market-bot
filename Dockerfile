FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN useradd --create-home --uid 10001 botuser && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser
CMD ["python","app.py"]
