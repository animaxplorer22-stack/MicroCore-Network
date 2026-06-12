#!/usr/bin/env python3
"""
MICROCORE WIFI BRIDGE - For Arduino Uno
Auto node discovery, failover, connection management

Run: python3 wifi_bridge.py
"""

import asyncio
import serial
import serial.tools.list_ports
import json
import websockets
import socket
import time
import dns.resolver
from datetime import datetime

# ==================== CONFIGURATION ====================
NODE_WS_URL = "ws://localhost:8080"  # Will be auto-discovered
BAUD_RATE = 115200
BRIDGE_ID = "arduino_bridge_1"

# DNS Seeds for node discovery
DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]
P2P_PORT = 8080

# ==================== GLOBAL VARIABLES ====================
running = True
ser = None
websocket = None
message_buffer = []
current_node_url = None
node_urls = []
current_node_index = 0
stats = {"messages_sent": 0, "messages_received": 0, "errors": 0, "start_time": time.time(), "node_switches": 0}

# ==================== NODE DISCOVERY ====================
def resolve_dns_seeds():
    """Resolve DNS seeds to node IPs"""
    nodes = []
    for seed in DNS_SEEDS:
        try:
            answers = dns.resolver.resolve(seed, 'A')
            for answer in answers:
                nodes.append(f"ws://{str(answer)}:{P2P_PORT}")
            print(f"[DNS] Found {len(answers)} nodes from {seed}")
        except Exception as e:
            print(f"[DNS] Failed to resolve {seed}: {e}")
    return nodes

def get_public_ip():
    try:
        import requests
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        return None

# ==================== SERIAL PORT ====================
def find_arduino_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "Arduino" in port.description or "USB" in port.description or "ttyACM" in port.device or "ttyUSB" in port.device:
            print(f"[BRIDGE] Found Arduino on {port.device}")
            return port.device
    return None

# ==================== WEBSOCKET CONNECTION ====================
async def connect_to_node():
    global websocket, current_node_url, current_node_index, node_urls
    
    if not node_urls:
        node_urls = resolve_dns_seeds()
        if not node_urls:
            node_urls = [NODE_WS_URL]
            print("[BRIDGE] Using fallback node URL")
    
    while running:
        try:
            current_node_url = node_urls[current_node_index % len(node_urls)]
            print(f"[BRIDGE] Connecting to node: {current_node_url}")
            async with websockets.connect(current_node_url, ping_interval=20, ping_timeout=10) as ws:
                websocket = ws
                print(f"[BRIDGE] Connected to {current_node_url}")
                await ws.send(json.dumps({"type": "bridge_register", "bridge_id": BRIDGE_ID, "timestamp": time.time()}))
                for msg in message_buffer:
                    await ws.send(msg)
                message_buffer.clear()
                try:
                    async for _ in ws:
                        pass
                except:
                    pass
        except Exception as e:
            print(f"[BRIDGE] Node connection failed: {e}")
            current_node_index += 1
            stats["node_switches"] += 1
            await asyncio.sleep(5)
        websocket = None

async def forward_arduino_to_node():
    global ser, websocket, stats
    while running and ser and ser.is_open:
        try:
            if ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    stats["messages_received"] += 1
                    print(f"[→] {line[:100]}")
                    if websocket and websocket.open:
                        try:
                            await websocket.send(line)
                            stats["messages_sent"] += 1
                        except:
                            message_buffer.append(line)
                    else:
                        message_buffer.append(line)
            await asyncio.sleep(0.01)
        except Exception as e:
            print(f"[ERROR] Serial read: {e}")
            await asyncio.sleep(1)

async def forward_node_to_arduino():
    global websocket, ser, stats
    while running:
        try:
            if websocket and websocket.open:
                message = await websocket.recv()
                stats["messages_sent"] += 1
                print(f"[←] {message[:100]}")
                if ser and ser.is_open:
                    ser.write((message + "\n").encode())
            else:
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[ERROR] WebSocket recv: {e}")
            await asyncio.sleep(1)

async def manage_serial():
    global ser
    while running:
        if not ser or not ser.is_open:
            port = find_arduino_port()
            if port:
                try:
                    ser = serial.Serial(port, BAUD_RATE, timeout=1, write_timeout=1)
                    print(f"[BRIDGE] Serial opened: {port} @ {BAUD_RATE} baud")
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"[BRIDGE] Failed to open {port}: {e}")
                    ser = None
                    await asyncio.sleep(5)
            else:
                print("[BRIDGE] No Arduino found. Waiting...")
                await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)

async def status_reporter():
    while running:
        await asyncio.sleep(60)
        uptime = int(time.time() - stats["start_time"])
        hours, minutes = uptime // 3600, (uptime % 3600) // 60
        print(f"\n{'='*50}\nBRIDGE STATUS\n{'='*50}")
        print(f"Uptime: {hours}h {minutes}m")
        print(f"Messages to node: {stats['messages_sent']}")
        print(f"Messages from node: {stats['messages_received']}")
        print(f"Errors: {stats['errors']}")
        print(f"Node switches: {stats['node_switches']}")
        print(f"Current node: {current_node_url}")
        print(f"Serial: {'Open' if ser and ser.is_open else 'Closed'}")
        print(f"WebSocket: {'Connected' if websocket and websocket.open else 'Disconnected'}")
        print(f"{'='*50}\n")

async def main():
    print("\n" + "=" * 60)
    print("MICROCORE WIFI BRIDGE v4.0")
    print("Auto node discovery | Failover | Arduino Uno support")
    print("=" * 60)
    
    global node_urls
    node_urls = resolve_dns_seeds()
    print(f"[BRIDGE] Discovered {len(node_urls)} nodes from DNS seeds")
    
    await asyncio.gather(manage_serial(), connect_to_node(), forward_arduino_to_node(), forward_node_to_arduino(), status_reporter())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[BRIDGE] Stopped")
