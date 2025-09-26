"""
What this does:
- Reads the same YAML as the frontend (single source of truth)
- Connects to AMQP
- Periodically publishes telemetry (pose/process) to ROBOT_TLM
- Consumes commands from ROBOT_CMD_BC and acts on them (hook up RoboDK here)

Notes:
- Pose is faked for now so you can see packets moving; swap read_pose_mm() with RoboDK later.
- Command shapes match the frontend: jog/goto/stop/pause/freeform.
"""

import json
import math
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

import pika
import yaml
import RoboDK  # required here; you’ll wire real robot I/O with it

# Config path (local, no containers)
CONFIG_PATH = "./config/message_broker_config.yaml"
TLM_EXCHANGE = "ROBOT_TLM"
CMD_QUEUE = "robot-commands"  # bound to ROBOT_CMD_BC in your YAML
ROBOT_ID = os.environ.get("ROBOT_ID", "r00")


# Same idea as the frontend: hide pika wiring so logic stays clean.
class MQ:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        b = cfg["brokers"]["lavinmq"]
        self.host = b["host"]
        self.port = int(b.get("port", 5672))
        self.vhost = b.get("virtual_host", "/")
        self.user = b.get("username", "guest")
        self.pw = b.get("password", "guest")
        self.exchanges = b.get("exchanges", [])

        self.conn: Optional[pika.BlockingConnection] = None
        self.ch: Optional[pika.adapters.blocking_connection.BlockingChannel] = None

    # Open channel we’ll reuse for everything
    def connect(self) -> None:
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

    # Declare exchanges/queues per YAML (idempotent)
    def declare_from_config(self) -> None:
        assert self.ch is not None
        for ex in self.exchanges:
            name = ex["name"]
            self.ch.exchange_declare(exchange=name, exchange_type="fanout", durable=True)
            for q in ex.get("queues", []):
                qn = q["name"]
                self.ch.queue_declare(queue=qn, durable=True)
                self.ch.queue_bind(exchange=name, queue=qn)

    # Small helper to publish JSON telemetry
    def publish_json(self, exchange: str, payload: Dict[str, Any]) -> None:
        assert self.ch is not None
        self.ch.basic_publish(
            exchange=exchange,
            routing_key="",
            body=json.dumps(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )

    # Ack/nack wrappers (when we add retries/DLX later this gets handy)
    def ack(self, tag) -> None:
        assert self.ch is not None
        self.ch.basic_ack(tag)

    def nack(self, tag, requeue: bool = False) -> None:
        assert self.ch is not None
        self.ch.basic_nack(tag, requeue=requeue)


# Read YAML once at boot
def load_cfg() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Fake pose for now so you can see live telemetry without a robot attached.
def read_pose_mm() -> Dict[str, Any]:
    t = time.time()
    return {
        "x": 500.0 + 50.0 * math.sin(t / 3.0),
        "y": 200.0 + 25.0 * math.cos(t / 4.0),
        "z": 300.0 + 10.0 * math.sin(t / 5.0),
        "rx": 0.0, "ry": 0.0, "rz": 0.0,
        "frame": "world", "units": "mm", "angles": "deg",
    }


# Push telemetry on a timer so UIs/loggers can just listen.
def telemetry_loop(mq: MQ, period: float = 0.5) -> None:
    while True:
        pose = read_pose_mm()  # TODO: replace with RoboDK pose read
        tlm = {
            "schema": "rfib.robot.tlm/1",
            "robot_id": ROBOT_ID,
            "pose": pose,
            "process": {"state": "idle"},  # TODO: surface real state when you have it
            "ts": time.time_ns(),
        }
        mq.publish_json(TLM_EXCHANGE, tlm)
        time.sleep(period)


# Decode and handle commands here. Replace prints with real RoboDK/robot calls when ready.
def handle_command(cmd: Dict[str, Any]) -> None:
    kind = str(cmd.get("cmd", "")).lower()
    print(f"[BACKEND][CMD] {cmd}", flush=True)

    # TODO: wire these into real logic:
    if kind == "jog":
        axis = cmd.get("axis")
        delta = float(cmd.get("delta", 0.0))
        # RoboDK move: translate along axis by delta (mm). Your call whether tool/world frame.
        # Example pseudo:
        # rdk = RoboDK.Robolink()
        # robot = rdk.Item('YourRobot', RoboDK.ITEM_TYPE_ROBOT)
        # ... compute target pose delta ...
        pass

    elif kind == "goto":
        x = float(cmd.get("x", 0.0))
        y = float(cmd.get("y", 0.0))
        z = float(cmd.get("z", 0.0))
        speed = float(cmd.get("speed", 100.0))
        units = cmd.get("units", "mm")
        # RoboDK move: build a pose at (x,y,z) and execute at 'speed'.
        # robot.setSpeed(speed)  # convert units if needed
        # robot.MoveJ/MoveL(target_pose)
        pass

    elif kind in ("pause", "stop"):
        # Hook up to your controller's pause/stop semantics
        pass

    else:
        # Freeform or unknown: log for now
        pass


# One authoritative consumer for commands from the frontend.
def consume_commands(mq: MQ, queue_name: str = CMD_QUEUE) -> None:
    assert mq.ch is not None
    mq.ch.basic_qos(prefetch_count=10)

    def _cb(ch, method, props, body: bytes):
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


# Wire-up: connect → declare → start telemetry thread → block on command consumer.
def main() -> None:
    cfg = load_cfg()
    mq = MQ(cfg)
    mq.connect()
    mq.declare_from_config()

    threading.Thread(target=telemetry_loop, args=(mq, 0.5), daemon=True).start()

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    consume_commands(mq, CMD_QUEUE)


if __name__ == "__main__":
    main()