FROM python:3.12-alpine

WORKDIR /app

RUN pip install --no-cache-dir flask docker

COPY app.py .
COPY templates/ templates/

EXPOSE 8888

CMD ["python", "app.py"]
