FROM python:latest

# Tk for the UI
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-tk tk \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY robot-frontend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY robot-frontend /app
RUN mkdir -p /app/config

CMD ["python", "-u", "main.py"]