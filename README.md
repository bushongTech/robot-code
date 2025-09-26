Robot Code
==========

This project has a frontend and a backend that talk through a message broker
(LavinMQ). Think of the exchanges like API routes:

- ROBOT_CMD_BC = frontend sends commands to the backend
- ROBOT_TLM    = backend sends telemetry to the frontend

How to Run
----------

1. Start the tge lavinMQ container(you know where it is).
2. Update config/message_broker_config.yaml if needed.
3. Start the backend:
   cd robot-backend
   pip install -r requirements.txt
   python main.py
4. Start the frontend:
   cd robot-frontend
   pip install -r requirements.txt
   python main.py

Requirements
------------

Frontend:
  pika
  PyYAML

Backend:
  pika
  PyYAML
  RoboDK

Tkinter comes with Python already.

Quick Summary
-------------

- Frontend: buttons for jog, stop, pause, go-to, and freeform JSON.
- Backend: listens for commands, sends fake telemetry, ready to hook into RoboDK.
- Push buttons, see commands flow, watch telemetry come back.
