"""
robot-backend/main.py

Why this file exists:
- Backend service: the single authority that listens to ROBOT_CMD_BC and drives the robot.
- Publishes telemetry (pose/process) to ROBOT_TLM on a steady cadence.
- This is where RoboDK (and later: real controller I/O) lives.

Flow:
1) Read YAML and connect to LavinMQ.
2) Start a periodic telemetry publisher.
3) Start a command consumer that handles frontend requests (jog/goto/pause/stop).
"""

import json
import os
import time
import threading
import signal
import sys
import math

import pika
import yaml
import RoboDK  # always imported here (backend needs it)

CONFIG_PATH = "/app/config/message_broker_config.yaml"
TLM_EXCHANGE = "ROBOT_TLM"
CMD_QUEUE = "robot-commands"
ROBOT_ID = os.environ.get("ROBOT_ID", "r00")


# --- MQ wrapper: declare infra, publish JSON, ack/nack helpers ---
class MQ:
    def __init__(self, cfg):
        b = cfg["brokers"]["lavinmq"]
        self.host = b["host"]
        self.port = int(b.get("port", 5672))
        self.vhost = b.get("virtual_host", "/")
        self.user = b.get("username", "guest")
        self.pw = b.get("password", "guest")
        self.exchanges = b.get("exchanges", [])

        self.conn = None
        self.ch = None

    def connect(self):
        params = pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            virtual_host=self.vhost,
            credentials=pika.PlainCredentials(self.user, self.pw),
            heartbeat=30,
            blocked_connection_timeout=300,
        )
        self.conn = pika.BlockingConnection(params)
        self.ch = self.conn.channel()

    def declare_from_config(self):
        for ex in self.exchanges:
            ex_name = ex["name"]
            self.ch.exchange_declare(exchange=ex_name, exchange_type="fanout", durable=True)
            for q in ex.get("queues", []):
                q_name = q["name"]
                self.ch.queue_declare(queue=q_name, durable=True)
                self.ch.queue_bind(exchange=ex_name, queue=q_name)

    def publish_json(self, exchange, payload):
        self.ch.basic_publish(
            exchange=exchange,
            routing_key="",
            body=json.dumps(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )

    def ack(self, tag):
        self.ch.basic_ack(tag)

    def nack(self, tag, requeue=False):
        self.ch.basic_nack(tag, requeue=requeue)


# --- helpers for config/pose ---
def load_cfg():
    """Read YAML so we share a single source of truth across services."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def read_pose_mm():
    """
    Read the current pose.
    For now: fake a smooth motion. Swap with RoboDK API calls when wired.
    """
    t = time.time()
    return {
        "x": 500.0 + 50.0 * math.sin(t / 3.0),
        "y": 200.0 + 25.0 * math.cos(t / 4.0),
        "z": 300.0 + 10.0 * math.sin(t / 5.0),
        "rx": 0.0, "ry": 0.0, "rz": 0.0,
        "frame": "world", "units": "mm", "angles": "deg",
    }


# --- telemetry publisher loop ---
def telemetry_loop(mq: MQ, period=0.5):
    while True:
        pose = read_pose_mm()
        tlm = {
            "schema": "rfib.robot.tlm/1",
            "robot_id": ROBOT_ID,
            "pose": pose,
            "process": {"state": "idle"},
            "ts": time.time_ns(),
        }
        mq.publish_json(TLM_EXCHANGE, tlm)
        time.sleep(period)


# --- command handler ---
def handle_command(cmd: dict):
    """
    Commands we expect:
    - {"cmd":"jog","axis":"X","delta":1.0,"units":"mm","ts":...}
    - {"cmd":"goto","x":100,"y":50,"z":20,"speed":200,"units":"mm","ts":...}
    - {"cmd":"pause"} | {"cmd":"stop"}
    Wire these to RoboDK / hardware here.
    """
    print("[BACKEND][CMD]", cmd, flush=True)
    # TODO: integrate RoboDK API calls here


# --- consume frontend commands ---
def consume_commands(mq: MQ, queue_name=CMD_QUEUE):
    mq.ch.basic_qos(prefetch_count=10)

    def _cb(ch, method, props, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {"_raw": body.decode("utf-8", errors="replace")}
        try:
            handle_command(payload)
            mq.ack(method.delivery_tag)
        except Exception as e:
            print("[BACKEND][ERR]", e, flush=True)
            mq.nack(method.delivery_tag, requeue=False)

    mq.ch.basic_consume(queue=queue_name, on_message_callback=_cb, auto_ack=False)
    mq.ch.start_consuming()


def main():
    """Wire-up: connect → declare infra → start telemetry thread → consume commands from frontend."""
    cfg = load_cfg()
    mq = MQ(cfg)
    mq.connect()
    mq.declare_from_config()

    threading.Thread(target=telemetry_loop, args=(mq, 0.5), daemon=True).start()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    consume_commands(mq, CMD_QUEUE)


if __name__ == "__main__":
    main()