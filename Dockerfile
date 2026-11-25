FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 1000 agent && mkdir -p recordings && chown -R agent /app
USER agent
EXPOSE 8080
CMD ["python", "app.py"]
