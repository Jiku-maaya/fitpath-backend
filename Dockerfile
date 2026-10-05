FROM python:3.13-slim
WORKDIR /app
COPY server.py .
RUN useradd --uid 10001 app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "server.py"]
