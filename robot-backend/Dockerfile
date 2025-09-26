FROM python:latest

WORKDIR /app

COPY robot-backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY robot-backend /app
RUN mkdir -p /app/config

CMD ["python", "-u", "main.py"]