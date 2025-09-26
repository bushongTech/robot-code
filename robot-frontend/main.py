"""
What this does:
- Reads the YAML (so broker/exchange/queue names live in config, not code)
- Connects to AMQP
- Starts a background telemetry consumer (so the UI never blocks)
- Tk UI lets you: Jog (X/Y/Z), STOP, PAUSE, GoTo (X/Y/Z + speed + units), and send raw JSON

Important:
- Commands publish to ROBOT_CMD_BC
- Telemetry is read from ROBOT_TLM (we just print it and drain so we don't balloon memory)
"""

import json
import queue
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

import pika
import yaml
import tkinter as tk
from tkinter import ttk

# Config path (local, no containers)
CONFIG_PATH = "./config/message_broker_config.yaml"
TLM_EXCHANGE = "ROBOT_TLM"
CMD_EXCHANGE = "ROBOT_CMD_BC"


# Why this tiny class:
# - Hide pika boilerplate so UI code stays readable.
# - Give us: connect, declare (idempotent), publish JSON, consume with a handler.
class AMQPClient:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        b = cfg["brokers"]["lavinmq"]
        self.host = b["host"]
        self.port = int(b.get("port", 5672))
        self.username = b.get("username", "guest")
        self.password = b.get("password", "guest")
        self.vhost = b.get("virtual_host", "/")
        self.exchanges = b.get("exchanges", [])

        self._conn: Optional[pika.BlockingConnection] = None
        self._chan: Optional[pika.adapters.blocking_connection.BlockingChannel] = None
        self._closing = False

    # Open connection/channel once and reuse it everywhere.
    def connect(self) -> None:
        creds = pika.PlainCredentials(self.username, self.password)
        params = pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            virtual_host=self.vhost,
            credentials=creds,
            heartbeat=30,
            blocked_connection_timeout=300,
        )
        self._conn = pika.BlockingConnection(params)
        self._chan = self._conn.channel()

    # Create exchanges/queues on every boot. Safe + keeps infra drift from biting us.
    def declare_from_config(self) -> None:
        assert self._chan is not None
        for ex in self.exchanges:
            name = ex["name"]
            self._chan.exchange_declare(exchange=name, exchange_type="fanout", durable=True)
            for q in ex.get("queues", []):
                qn = q["name"]
                self._chan.queue_declare(queue=qn, durable=True)
                self._chan.queue_bind(exchange=name, queue=qn)

    # One-liner JSON publish so buttons can stay tiny.
    def publish_json(self, exchange: str, payload: Dict[str, Any]) -> None:
        assert self._chan is not None
        body = json.dumps(payload).encode("utf-8")
        self._chan.basic_publish(
            exchange=exchange,
            routing_key="",
            body=body,
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )

    # Put the consumer on a background thread; if the broker hiccups, reconnect and keep going.
    def consume_forever(self, queue_name: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        def _cb(ch, method, props, body: bytes):
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:
                msg = {"_raw": body.decode("utf-8", errors="replace")}
            handler(msg)
            ch.basic_ack(delivery_tag=method.delivery_tag)

        while not self._closing:
            try:
                assert self._chan is not None
                self._chan.basic_qos(prefetch_count=32)
                self._chan.basic_consume(queue=queue_name, on_message_callback=_cb, auto_ack=False)
                self._chan.start_consuming()
            except Exception:
                if self._closing:
                    break
                time.sleep(1)
                self.connect()
                self.declare_from_config()

    # Clean close so we don’t leave consuming loops angry.
    def close(self) -> None:
        self._closing = True
        try:
            if self._chan and self._chan.is_open:
                self._chan.stop_consuming()
        except Exception:
            pass
        try:
            if self._conn and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass


# Helper: read YAML so broker/exchange names come from config.
def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Helper: grab the first queue bound to an exchange (good enough for this UI).
def first_queue_for_exchange(cfg: Dict[str, Any], ex_name: str) -> Optional[str]:
    for ex in cfg["brokers"]["lavinmq"].get("exchanges", []):
        if ex.get("name") == ex_name:
            qs = ex.get("queues", [])
            return qs[0]["name"] if qs else None
    return None


# All the UI gunk lives here so main() is just wiring.
class RobotUI:
    def __init__(self, amqp: AMQPClient, cmd_exchange: str, tlm_fifo: "queue.Queue[dict]"):
        self.amqp = amqp
        self.cmd_exchange = cmd_exchange
        self.tlm_fifo = tlm_fifo

        self.root = tk.Tk()
        self.root.title("Robot Frontend (Tk)")

        # Layout scaffolding
        container = ttk.Frame(self.root, padding=12)
        container.grid(column=0, row=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Freeform JSON (escape hatch for weird stuff)
        ttk.Label(container, text="Command JSON:").grid(column=0, row=0, sticky="w")
        self.cmd_box = tk.Text(container, width=72, height=6)
        self.cmd_box.grid(column=0, row=1, sticky="nsew", pady=(4, 6))
        self.cmd_box.insert("1.0", json.dumps({"cmd": "ping", "ts": time.time_ns()}, indent=2))
        ttk.Button(container, text="Send JSON", command=self.send_freeform).grid(column=0, row=2, sticky="e")

        # Jog panel (just quick nudges; fixed step for now)
        jog = ttk.LabelFrame(container, text="Jog", padding=8)
        jog.grid(column=0, row=3, sticky="ew", pady=(8, 6))
        for i in range(3):
            jog.columnconfigure(i, weight=1)
        ttk.Button(jog, text="Up (+Z)", command=lambda: self.jog("Z", +1)).grid(column=1, row=0, pady=2)
        ttk.Button(jog, text="Down (-Z)", command=lambda: self.jog("Z", -1)).grid(column=1, row=2, pady=2)
        ttk.Button(jog, text="Left (-X)", command=lambda: self.jog("X", -1)).grid(column=0, row=1, padx=2)
        ttk.Button(jog, text="Right (+X)", command=lambda: self.jog("X", +1)).grid(column=2, row=1, padx=2)
        ttk.Button(jog, text="Forward (+Y)", command=lambda: self.jog("Y", +1)).grid(column=1, row=1, pady=2)
        ttk.Button(jog, text="Back (-Y)", command=lambda: self.jog("Y", -1)).grid(column=1, row=3, pady=2)

        # STOP / PAUSE — simple and loud
        sp = ttk.Frame(container)
        sp.grid(column=0, row=4, sticky="ew", pady=(6, 6))
        ttk.Button(sp, text="STOP", command=self.stop).grid(column=0, row=0, padx=4)
        ttk.Button(sp, text="PAUSE", command=self.pause).grid(column=1, row=0, padx=4)

        # Go To (X/Y/Z + speed + units)
        goto = ttk.LabelFrame(container, text="Go To (Absolute)", padding=8)
        goto.grid(column=0, row=5, sticky="ew", pady=(6, 6))
        self.x_var = tk.DoubleVar(value=0.0)
        self.y_var = tk.DoubleVar(value=0.0)
        self.z_var = tk.DoubleVar(value=0.0)
        self.speed_var = tk.DoubleVar(value=100.0)
        self.units_var = tk.StringVar(value="mm")
        ttk.Label(goto, text="X:").grid(column=0, row=0)
        ttk.Entry(goto, textvariable=self.x_var, width=8).grid(column=1, row=0)
        ttk.Label(goto, text="Y:").grid(column=2, row=0)
        ttk.Entry(goto, textvariable=self.y_var, width=8).grid(column=3, row=0)
        ttk.Label(goto, text="Z:").grid(column=4, row=0)
        ttk.Entry(goto, textvariable=self.z_var, width=8).grid(column=5, row=0)
        ttk.Label(goto, text="Speed:").grid(column=6, row=0)
        ttk.Entry(goto, textvariable=self.speed_var, width=8).grid(column=7, row=0)
        ttk.Label(goto, text="Units:").grid(column=8, row=0)
        ttk.Combobox(goto, textvariable=self.units_var, values=["mm", "in"], width=6, state="readonly").grid(column=9, row=0)
        ttk.Button(goto, text="Go", command=self.go_to).grid(column=10, row=0, padx=6)

        # Status line (because feedback beats guessing)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(container, textvariable=self.status).grid(column=0, row=6, sticky="w", pady=(8, 0))

        # Drain telemetry FIFO on a timer so the buffer never runs away
        self._pump_fifo()

    # Keep button handlers tiny: shove all publish + status setting here.
    def _send(self, payload: Dict[str, Any], ok_msg: str) -> None:
        try:
            self.amqp.publish_json(self.cmd_exchange, payload)
            self.status.set(ok_msg)
        except Exception as e:
            self.status.set(f"Publish failed: {e}")

    # Jog = fixed 1.0 unit for now (mm by default)
    def jog(self, axis: str, sign: int) -> None:
        payload = {"cmd": "jog", "axis": axis, "delta": 1.0 if sign >= 0 else -1.0, "units": "mm", "ts": time.time_ns()}
        self._send(payload, f"Jog {axis} {payload['delta']} mm")

    # Stop = hard stop; Pause = soft hold (backend decides the details)
    def stop(self) -> None:
        self._send({"cmd": "stop", "ts": time.time_ns()}, "STOP sent")

    def pause(self) -> None:
        self._send({"cmd": "pause", "ts": time.time_ns()}, "PAUSE sent")

    # Absolute move: X/Y/Z + speed + units to the backend
    def go_to(self) -> None:
        payload = {
            "cmd": "goto",
            "x": float(self.x_var.get()),
            "y": float(self.y_var.get()),
            "z": float(self.z_var.get()),
            "speed": float(self.speed_var.get()),
            "units": self.units_var.get(),
            "ts": time.time_ns(),
        }
        self._send(payload, f"GoTo {payload['x']},{payload['y']},{payload['z']} {payload['units']} @ {payload['speed']}")

    # Raw JSON sender (nice for quick tests)
    def send_freeform(self) -> None:
        raw = self.cmd_box.get("1.0", "end").strip()
        try:
            payload = json.loads(raw)
            payload.setdefault("ts", time.time_ns())
            self._send(payload, "Freeform sent")
        except Exception:
            self.status.set("Invalid JSON")

    # Don’t let telemetry pile up forever — drain a bit each tick.
    def _pump_fifo(self) -> None:
        drained = 0
        while not self.tlm_fifo.empty() and drained < 50:
            _ = self.tlm_fifo.get_nowait()
            drained += 1
        self.root.after(250, self._pump_fifo)

    # Hand control to Tk
    def run(self) -> None:
        self.root.mainloop()


# Wire-up: load config -> connect -> declare -> start telemetry consumer -> start UI.
def main() -> None:
    cfg = load_config()
    amqp = AMQPClient(cfg)
    amqp.connect()
    amqp.declare_from_config()

    tlm_queue = first_queue_for_exchange(cfg, TLM_EXCHANGE) or "robot-telemetry"
    tlm_fifo: "queue.Queue[dict]" = queue.Queue()

    def on_tlm(msg: Dict[str, Any]) -> None:
        tlm_fifo.put(msg)
        print("[TLM]", json.dumps(msg), flush=True)

    t = threading.Thread(target=amqp.consume_forever, args=(tlm_queue, on_tlm), daemon=True)
    t.start()

    ui = RobotUI(amqp, CMD_EXCHANGE, tlm_fifo)

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    ui.run()


if __name__ == "__main__":
    main()