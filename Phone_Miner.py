#!/usr/bin/env python3
"""
MICROCORE (MCX) COMPLETE PHONE MINER
Runs on iPhone (a-shell/iSH) and Android (Termux)
Full feature: Real mining, auto node discovery, failover, stats

Run: python3 phone_miner.py
"""

import json
import time
import hashlib
import os
import sys
import random
import socket
import select
import threading
from datetime import datetime

# ==================== CONFIGURATION ====================
# EDIT THESE OR USE FIRST-RUN SETUP
USERNAME = ""  # Leave empty for first-run setup
WALLET_FILE = "microcore_phone_wallet.json"

# Node configuration (change to your node's IP)
NODE_IP = "192.168.1.100"  # ← CHANGE THIS to your node IP
NODE_PORT = 8080

# DNS Seeds for auto node discovery (optional)
DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]

# Mining parameters
INITIAL_STAKE = 100
LEVEL_STAKE_RANGE = 100
SIGNING_WINDOW_MS = 2500
SLASH_RATE = 0.10
UPTIME_PING_INTERVAL = 30
STATUS_INTERVAL = 60
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY = 5

# ==================== CRYPTO FUNCTIONS ====================
def sha256(data):
    """SHA256 hash function"""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()

def generate_private_key():
    """Generate random private key"""
    return sha256(os.urandom(32))

def get_wallet_address(private_key):
    """Derive wallet address from private key"""
    return "MCR_" + sha256(private_key)[:32].upper()

def get_validator_id(username, private_key):
    """Generate validator ID"""
    return sha256(f"{username}{private_key}")[:32]

def sign_message(private_key, message):
    """Sign message (simplified for phone)"""
    return sha256(f"{private_key}{message}{private_key}")[:64]

# ==================== SIMPLE WEBSOCKET CLIENT ====================
class SimpleWebSocket:
    """Simple WebSocket client for phone (no external dependencies)"""
    
    def __init__(self):
        self.sock = None
        self.connected = False
        self.handshake_done = False
    
    def connect(self, host, port):
        """Connect to WebSocket server"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((host, port))
            
            # Generate WebSocket key
            key = "dGhlIHNhbXBsZSBub25jZQ=="
            
            # Send handshake
            handshake = f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            self.sock.send(handshake.encode())
            
            # Receive response
            response = self.sock.recv(1024).decode()
            
            if "101" in response:
                self.connected = True
                self.handshake_done = True
                print(f"[WS] Connected to {host}:{port}")
                return True
            else:
                print(f"[WS] Handshake failed")
                return False
                
        except Exception as e:
            print(f"[WS] Connection error: {e}")
            return False
    
    def send(self, data):
        """Send WebSocket message"""
        if not self.connected:
            return False
        try:
            # WebSocket frame format
            frame = b'\x81' + bytes([len(data)]) + data.encode()
            self.sock.send(frame)
            return True
        except Exception as e:
            print(f"[WS] Send error: {e}")
            self.connected = False
            return False
    
    def receive(self):
        """Receive WebSocket message (non-blocking)"""
        if not self.connected:
            return None
        try:
            self.sock.settimeout(0.1)
            data = self.sock.recv(4096)
            if data and len(data) > 2:
                # Skip frame header
                payload = data[2:2+data[1]]
                return payload.decode()
            return None
        except socket.timeout:
            return None
        except Exception as e:
            print(f"[WS] Receive error: {e}")
            self.connected = False
            return None
    
    def close(self):
        """Close connection"""
        if self.sock:
            self.sock.close()
            self.sock = None
        self.connected = False

# ==================== DNS NODE DISCOVERY ====================
def resolve_dns_seed(seed):
    """Resolve DNS seed to IP address"""
    try:
        ip = socket.gethostbyname(seed)
        print(f"[DNS] Resolved {seed} -> {ip}")
        return ip
    except Exception as e:
        print(f"[DNS] Failed to resolve {seed}: {e}")
        return None

def discover_nodes():
    """Discover nodes from DNS seeds"""
    nodes = []
    for seed in DNS_SEEDS:
        ip = resolve_dns_seed(seed)
        if ip:
            nodes.append((ip, NODE_PORT))
    if not nodes:
        # Use configured node as fallback
        nodes.append((NODE_IP, NODE_PORT))
        print(f"[DNS] Using configured node: {NODE_IP}:{NODE_PORT}")
    return nodes

# ==================== WALLET MANAGEMENT ====================
class Wallet:
    """Wallet management for phone miner"""
    
    def __init__(self, username, address, private_key):
        self.username = username
        self.address = address
        self.private_key = private_key
    
    def get_validator_id(self):
        return get_validator_id(self.username, self.private_key)
    
    @classmethod
    def create_new(cls, username):
        private_key = generate_private_key()
        address = get_wallet_address(private_key)
        return cls(username, address, private_key)
    
    @classmethod
    def load(cls, filename):
        if not os.path.exists(filename):
            return None
        with open(filename, 'r') as f:
            data = json.load(f)
        return cls(data['username'], data['address'], data['private_key'])
    
    def save(self, filename):
        with open(filename, 'w') as f:
            json.dump({
                'username': self.username,
                'address': self.address,
                'private_key': self.private_key
            }, f, indent=2)

# ==================== PHONE MINER ====================
class PhoneMiner:
    """Complete phone miner implementation"""
    
    def __init__(self, wallet):
        self.wallet = wallet
        self.validator_id = wallet.get_validator_id()
        self.nodes = discover_nodes()
        self.current_node_index = 0
        self.current_node = self.nodes[0]
        
        # WebSocket connection
        self.ws = None
        self.connected = False
        self.reconnect_attempts = 0
        self.node_switch_count = 0
        
        # Mining state
        self.is_validator = False
        self.current_challenge = ""
        self.current_block_id = 0
        self.last_challenge_time = 0
        self.start_time = time.time()
        self.last_uptime_ping = 0
        self.last_status_report = 0
        
        # Stats
        self.total_rewards = 0
        self.blocks_signed = 0
        self.consecutive_misses = 0
        self.slash_count = 0
        self.current_stake = INITIAL_STAKE
        self.current_level = self.calculate_level()
        
        self.load_stats()
    
    def calculate_level(self):
        level = ((self.current_stake - 1) // LEVEL_STAKE_RANGE) + 1
        return max(1, min(level, 100))
    
    def load_stats(self):
        """Load saved stats from file"""
        stats_file = "phone_miner_stats.json"
        if os.path.exists(stats_file):
            try:
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                    self.total_rewards = data.get('rewards', 0)
                    self.blocks_signed = data.get('blocks', 0)
                    self.slash_count = data.get('slashes', 0)
                    self.current_stake = data.get('stake', INITIAL_STAKE)
                    self.current_level = self.calculate_level()
                    print(f"[STATS] Loaded: {self.blocks_signed} blocks, {self.total_rewards} MCX")
            except:
                pass
    
    def save_stats(self):
        """Save current stats to file"""
        with open("phone_miner_stats.json", 'w') as f:
            json.dump({
                'rewards': self.total_rewards,
                'blocks': self.blocks_signed,
                'slashes': self.slash_count,
                'stake': self.current_stake
            }, f, indent=2)
    
    def switch_to_next_node(self):
        """Failover to next node"""
        self.current_node_index = (self.current_node_index + 1) % len(self.nodes)
        self.current_node = self.nodes[self.current_node_index]
        self.node_switch_count += 1
        print(f"\n[FAILOVER] Switching to node: {self.current_node[0]}:{self.current_node[1]} (#{self.node_switch_count})\n")
    
    def add_reward(self, reward):
        """Add reward to stats"""
        self.total_rewards += reward
        self.current_stake += reward
        self.blocks_signed += 1
        self.consecutive_misses = 0
        self.current_level = self.calculate_level()
        self.save_stats()
        print(f"\n💰 REWARD: +{reward} MCX | Total: {self.total_rewards} | Stake: {self.current_stake} | Level: {self.current_level}")
    
    def handle_slash(self):
        """Handle slashing penalty"""
        slash_amount = max(int(self.current_stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        self.current_stake -= slash_amount
        if self.current_stake < LEVEL_STAKE_RANGE:
            self.current_stake = LEVEL_STAKE_RANGE
        self.consecutive_misses += 1
        self.slash_count += 1
        self.current_level = self.calculate_level()
        self.save_stats()
        print(f"\n⚠️ SLASHED: -{slash_amount} MCX | Stake: {self.current_stake} | Level: {self.current_level} | Misses: {self.consecutive_misses}")
        return self.slash_count < 5
    
    def register(self):
        """Register with the node"""
        timestamp = time.time()
        reg_message = f"{self.validator_id}{self.wallet.username}{self.current_stake}{timestamp}"
        signature = sign_message(self.wallet.private_key, reg_message)
        
        msg = {
            "type": "register",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "public_key": self.wallet.private_key,
            "wallet": self.wallet.address,
            "stake": self.current_stake,
            "level": self.current_level,
            "rewards": self.total_rewards,
            "blocks": self.blocks_signed,
            "uptime": int(time.time() - self.start_time),
            "timestamp": timestamp,
            "signature": signature
        }
        
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
            print(f"📡 Registered with node as '{self.wallet.username}'")
    
    def send_uptime(self):
        """Send uptime ping"""
        uptime = int(time.time() - self.start_time)
        msg = {
            "type": "uptime_ping",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "uptime_seconds": uptime,
            "stake": self.current_stake,
            "level": self.current_level
        }
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
    
    def send_signature(self):
        """Sign the current challenge"""
        message = f"{self.current_challenge}{self.validator_id}{self.current_block_id}"
        signature = sign_message(self.wallet.private_key, message)
        
        msg = {
            "type": "block_signature",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "challenge": self.current_challenge,
            "signature": signature,
            "level": self.current_level,
            "stake": self.current_stake,
            "block_id": self.current_block_id,
            "timestamp": time.time()
        }
        
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
            print(f"✍️ Signed block {self.current_block_id}")
    
    def connect(self):
        """Establish WebSocket connection"""
        self.ws = SimpleWebSocket()
        ip, port = self.current_node
        return self.ws.connect(ip, port)
    
    def run(self):
        """Main mining loop"""
        print(f"\n{'='*60}")
        print(f"📱 MICROCORE (MCX) COMPLETE PHONE MINER")
        print(f"{'='*60}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address}")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print(f"{'-'*40}")
        print(f"Initial Stake: {self.current_stake} MCX")
        print(f"Initial Level: {self.current_level}")
        print(f"Signing Window: {SIGNING_WINDOW_MS} ms")
        print(f"Slash Rate: {SLASH_RATE * 100}%")
        print(f"{'-'*40}")
        print(f"Discovered Nodes: {len(self.nodes)}")
        for i, (ip, port) in enumerate(self.nodes):
            print(f"  {i+1}. {ip}:{port}")
        print(f"{'='*60}\n")
        
        self.reconnect_attempts = 0
        
        while True:
            try:
                # Connect if not connected
                if not self.connected or not self.ws or not self.ws.connected:
                    print(f"[CONN] Connecting to {self.current_node[0]}:{self.current_node[1]}...")
                    if self.connect():
                        self.connected = True
                        self.reconnect_attempts = 0
                        self.register()
                    else:
                        self.reconnect_attempts += 1
                        if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                            self.switch_to_next_node()
                            self.reconnect_attempts = 0
                        delay = RECONNECT_DELAY * min(self.reconnect_attempts + 1, 10)
                        print(f"[CONN] Retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                
                # Receive and process messages
                data = self.ws.receive()
                if data:
                    try:
                        msg = json.loads(data)
                        msg_type = msg.get("type")
                        
                        if msg_type == "registered":
                            print(f"✅ Registration confirmed | Level: {msg.get('level')}")
                            print(f"💰 Current reward: {msg.get('current_reward')} MCX per block")
                        
                        elif msg_type == "challenge":
                            self.current_challenge = msg.get("challenge", "")
                            self.current_block_id = msg.get("block_id", 0)
                            self.last_challenge_time = time.time()
                            self.is_validator = True
                            self.send_signature()
                        
                        elif msg_type == "block_accepted":
                            reward = msg.get("reward", 0)
                            self.add_reward(reward)
                            self.is_validator = False
                            print(f"✅ Block {msg.get('block_id')} ACCEPTED! +{reward} MCX")
                        
                        elif msg_type == "block_rejected":
                            self.is_validator = False
                            print(f"❌ Block {msg.get('block_id')} REJECTED")
                        
                        elif msg_type == "slash":
                            print(f"⚠️ SLASH command received")
                            self.handle_slash()
                            self.is_validator = False
                    
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        print(f"[ERROR] Message handling: {e}")
                
                # Send uptime ping
                if time.time() - self.last_uptime_ping > UPTIME_PING_INTERVAL:
                    self.send_uptime()
                    self.last_uptime_ping = time.time()
                
                # Print status periodically
                if time.time() - self.last_status_report > STATUS_INTERVAL:
                    self.print_status()
                    self.last_status_report = time.time()
                
                # Check for signing timeout
                if self.is_validator and (time.time() - self.last_challenge_time) > (SIGNING_WINDOW_MS / 1000):
                    print(f"⏰ TIMEOUT: Missed block {self.current_block_id}")
                    self.handle_slash()
                    self.is_validator = False
                
                time.sleep(0.05)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Stopping miner...")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                self.connected = False
                time.sleep(2)
        
        # Cleanup
        if self.ws:
            self.ws.close()
        self.save_stats()
        self.print_final_stats()
    
    def print_status(self):
        """Print current status"""
        uptime = int(time.time() - self.start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        success_rate = 0
        total = self.blocks_signed + self.consecutive_misses
        if total > 0:
            success_rate = (self.blocks_signed / total) * 100
        
        print(f"\n{'='*50}")
        print(f"📱 PHONE MINER STATUS")
        print(f"{'='*50}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address[:24]}...")
        print(f"{'-'*40}")
        print(f"Level: {self.current_level} / 100")
        print(f"Stake: {self.current_stake:,} MCX")
        print(f"Rewards: {self.total_rewards:,} MCX")
        print(f"Blocks Signed: {self.blocks_signed}")
        print(f"Missed: {self.consecutive_misses}")
        print(f"Success Rate: {success_rate:.1f}%")
        print(f"Slashes: {self.slash_count} / 5")
        print(f"{'-'*40}")
        print(f"Uptime: {hours}h {minutes}m")
        print(f"Current Node: {self.current_node[0]}:{self.current_node[1]}")
        print(f"Node Switches: {self.node_switch_count}")
        print(f"Status: {'🟢 Mining' if self.is_validator else '🟡 Idle'}")
        print(f"Connected: {'✅ Yes' if self.connected else '❌ No'}")
        print(f"{'='*50}\n")
    
    def print_final_stats(self):
        """Print final statistics"""
        uptime = int(time.time() - self.start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        
        print(f"\n{'='*50}")
        print(f"📊 FINAL STATISTICS")
        print(f"{'='*50}")
        print(f"Total Runtime: {hours}h {minutes}m")
        print(f"Blocks Mined: {self.blocks_signed}")
        print(f"Total Rewards: {self.total_rewards} MCX")
        print(f"Final Stake: {self.current_stake} MCX")
        print(f"Final Level: {self.current_level}")
        print(f"Slash Count: {self.slash_count}")
        print(f"Node Switches: {self.node_switch_count}")
        print(f"{'='*50}")

# ==================== MAIN ====================
def main():
    print("\n" + "=" * 60)
    print("🔷 MICROCORE (MCX) COMPLETE PHONE MINER 🔷")
    print("Mine from your iPhone or Android")
    print("=" * 60)
    
    # Load or create wallet
    wallet = Wallet.load(WALLET_FILE)
    if not wallet:
        print("\n[FIRST RUN] No wallet found.")
        
        # Use configured username or ask
        if USERNAME:
            username = USERNAME
        else:
            username = input("Enter your username: ").strip()
            if not username:
                username = f"phone_miner_{int(time.time())}"
        
        wallet = Wallet.create_new(username)
        wallet.save(WALLET_FILE)
        print(f"\n✅ Wallet created!")
        print(f"   Username: {wallet.username}")
        print(f"   Address: {wallet.address}")
        print(f"\n⚠️ SAVE THESE CREDENTIALS!")
        print(f"   Wallet file: {os.path.abspath(WALLET_FILE)}")
        print(f"   Private key: {wallet.private_key}")
    else:
        print(f"\n✅ Wallet loaded: {wallet.username}")
        print(f"   Address: {wallet.address[:32]}...")
    
    # Create and run miner
    miner = PhoneMiner(wallet)
    
    try:
        miner.run()
    except KeyboardInterrupt:
        print("\n[EXIT] Goodbye!")

if __name__ == "__main__":
    main()
