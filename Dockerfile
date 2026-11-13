FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p /data
ENV PORT=8080 DB_PATH=/data/walletwatch.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "server.py"]
