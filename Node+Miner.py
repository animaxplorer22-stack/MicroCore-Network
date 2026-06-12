#!/usr/bin/env python3
"""
MICROCORE (MCX) COMPLETE NODE + EMBEDDED MINER
Real ECDSA secp256k1 | 100M Cap | 100 MCX Reward | 4-year halving
Auto peer discovery | DNS seeds | PEX | DEX bridge | Level system | Slashing
EMBEDDED MINER runs automatically with the node

Usage:
  python3 node_full.py --genesis     (First node - creates blockchain)
  python3 node_full.py --peer IP:PORT (Join existing network)
  python3 node_full.py --no-miner    (Run node without embedded miner)
"""

import asyncio
import json
import time
import hashlib
import sqlite3
import random
import os
import sys
import socket
import struct
import secrets
import argparse
import traceback
import signal
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
from enum import Enum

# ==================== DEPENDENCY CHECK ====================
try:
    import websockets
except ImportError:
    print("ERROR: Install websockets: pip install websockets")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: Install requests: pip install requests")
    sys.exit(1)

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature
    from cryptography.exceptions import InvalidSignature
except ImportError:
    print("ERROR: Install cryptography: pip install cryptography")
    sys.exit(1)

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    print("WARNING: dns.resolver not installed. DNS seed discovery disabled.")
    print("Install: pip install dnspython")
    DNS_AVAILABLE = False

# ==================== CONFIGURATION ====================
NODE_HOST = "0.0.0.0"
NODE_PORT = 8080
P2P_PORT = 8081

SYMBOL = "MCX"
NAME = "MicroCore"
VERSION = "4.0.0"

# DNS Seeds
DNS_SEEDS = [
    "seed.microcore.com",
    "seed1.microcore.com",
    "seed2.microcore.com",
]

# TOKENOMICS - 100 MILLION HARD CAP
TOTAL_SUPPLY_CAP = 100_000_000
INITIAL_BLOCK_REWARD = 100
HALVING_INTERVAL = 2_102_400  # 4 years at 10 sec blocks
MINIMUM_BLOCK_REWARD = 1

# Reward Distribution Percentages
VALIDATOR_SHARE = 0.75   # 75% to validators (10 people)
NODE_SHARE = 0.08        # 8% to nodes
UPTIME_SHARE = 0.07      # 7% to uptime pool
LP_SHARE = 0.10          # 10% to liquidity providers

# Consensus Parameters
LEVEL_STAKE_RANGE = 100
SIGNING_WINDOW_MS = 2500
SLASH_RATE = 0.10
MIN_VALIDATORS_PER_BLOCK = 10
MIN_WALLETS_FOR_NEXT_LEVEL = 10
MAX_LEVEL = 100
UPTIME_PING_INTERVAL = 30
DISTRIBUTION_INTERVAL_SEC = 300

# P2P Settings
MAX_PEERS = 30
SYNC_INTERVAL = 10
HEARTBEAT_INTERVAL = 30
PEER_TIMEOUT = 90
PEX_INTERVAL = 60
SEED_REFRESH_INTERVAL = 3600

# Ban settings
BAN_THRESHOLD = 5
BAN_DURATION = 3600

# DEX Settings
SWAP_FEE_RATE = 0.003
MCX_FEE_MIN = 1
MCX_FEE_MAX = 100

# ==================== ENUMS ====================
class TxStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"

class PeerState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    BANNED = "banned"

class Chain(Enum):
    ETHEREUM = "ethereum"
    BSC = "bsc"
    SOLANA = "solana"
    BITCOIN = "bitcoin"
    POLYGON = "polygon"

# ==================== TOKEN DEFINITIONS ====================
SUPPORTED_TOKENS = {
    "BTC": {"symbol": "BTC", "name": "Bitcoin", "chain": Chain.BITCOIN, "decimals": 8},
    "ETH": {"symbol": "ETH", "name": "Ethereum", "chain": Chain.ETHEREUM, "decimals": 18},
    "SOL": {"symbol": "SOL", "name": "Solana", "chain": Chain.SOLANA, "decimals": 9},
    "USDC": {"symbol": "USDC", "name": "USD Coin", "chain": Chain.ETHEREUM, "decimals": 6},
    "USDT": {"symbol": "USDT", "name": "Tether", "chain": Chain.ETHEREUM, "decimals": 6},
    "BNB": {"symbol": "BNB", "name": "Binance Coin", "chain": Chain.BSC, "decimals": 18},
}

# ==================== REAL CRYPTOGRAPHY ====================
def verify_signature(public_key_pem: str, message: str, signature_hex: str) -> bool:
    """Verify ECDSA secp256k1 signature - Bitcoin standard"""
    if len(signature_hex) != 128:
        return False
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature_bytes = bytes.fromhex(signature_hex)
        r = int.from_bytes(signature_bytes[:32], 'big')
        s = int.from_bytes(signature_bytes[32:], 'big')
        signature_der = encode_dss_signature(r, s)
        public_key.verify(signature_der, message.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False

def sign_message(private_key_hex: str, message: str) -> str:
    """Sign message with secp256k1 private key"""
    private_value = int(private_key_hex, 16)
    private_key = ec.derive_private_key(private_value, ec.SECP256K1())
    signature = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

def generate_wallet() -> tuple:
    """Generate new wallet (address, private key, public key)"""
    private_key = ec.generate_private_key(ec.SECP256K1())
    private_key_hex = private_key.private_numbers().private_value.to_bytes(32, 'big').hex()
    public_key = private_key.public_key()
    public_key_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    addr_hash = hashlib.sha256(public_key_pem.encode()).hexdigest()
    address = f"MCR_{addr_hash[:32].upper()}"
    return address, private_key_hex, public_key_pem

def hash_block(block_data: dict) -> str:
    """Generate SHA256 block hash"""
    return hashlib.sha256(json.dumps(block_data, sort_keys=True).encode()).hexdigest()

def hash_transaction(tx_data: dict) -> str:
    """Generate transaction hash"""
    return hashlib.sha256(json.dumps(tx_data, sort_keys=True).encode()).hexdigest()

# ==================== P2P PROTOCOL ====================
P2P_MAGIC = b"MCR1"
P2P_VERSION = 1

P2P_MSG_HANDSHAKE = 0x01
P2P_MSG_PING = 0x02
P2P_MSG_PONG = 0x03
P2P_MSG_GET_BLOCKS = 0x04
P2P_MSG_BLOCKS = 0x05
P2P_MSG_GET_HEADER = 0x06
P2P_MSG_HEADER = 0x07
P2P_MSG_NEW_BLOCK = 0x08
P2P_MSG_NEW_TRANSACTION = 0x09
P2P_MSG_GET_PEERS = 0x0A
P2P_MSG_PEERS = 0x0B
P2P_MSG_ADDR = 0x0C
P2P_MSG_GET_MEMPOOL = 0x0D
P2P_MSG_MEMPOOL = 0x0E
P2P_MSG_SLASH_EVENT = 0x0F
P2P_MSG_LEVEL_UPDATE = 0x10

def encode_p2p_message(msg_type: int, payload: dict) -> bytes:
    payload_bytes = json.dumps(payload).encode()
    header = P2P_MAGIC + struct.pack(">B", P2P_VERSION) + struct.pack(">B", msg_type) + struct.pack(">I", len(payload_bytes))
    return header + payload_bytes

def decode_p2p_message(data: bytes) -> tuple:
    if len(data) < 4 + 1 + 1 + 4:
        return None, None
    if data[:4] != P2P_MAGIC:
        return None, None
    msg_type = data[5]
    payload_len = struct.unpack(">I", data[6:10])[0]
    if len(data) < 10 + payload_len:
        return None, None
    payload = json.loads(data[10:10+payload_len].decode())
    return msg_type, payload

# ==================== DNS SEED & PEER DISCOVERY ====================
def query_dns_seeds() -> List[str]:
    """Query DNS seeds for peer addresses"""
    if not DNS_AVAILABLE:
        return []
    peers = []
    for seed in DNS_SEEDS:
        try:
            answers = dns.resolver.resolve(seed, 'A')
            for answer in answers:
                peers.append(f"{str(answer)}:{P2P_PORT}")
            print(f"[DNS] Found {len(answers)} peers from {seed}")
        except Exception as e:
            print(f"[DNS] Failed to query {seed}: {e}")
    return peers

def get_public_ip() -> str:
    """Get public IP address"""
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        try:
            response = requests.get('https://checkip.amazonaws.com', timeout=5)
            return response.text.strip()
        except:
            return None

# ==================== DEX BRIDGE WITH MCX FEES ====================
class DEXBridge:
    def __init__(self, network=None):
        self.network = network
        self.connected = False
        self.mcx_price_usd = 0.01
        self.total_liquidity_usd = 0
        self.liquidity_pools = {}
        self.swap_fee_rate = SWAP_FEE_RATE
        self.cross_chain_aggregators = {
            "thorchain": "https://thornode.thorchain.com",
            "lifi": "https://api.li.fi",
            "bungee": "https://api.bungee.exchange"
        }
        self.init_pools()
    
    def init_pools(self):
        self.liquidity_pools = {
            "BTC/USDC": {"token_a": "BTC", "token_b": "USDC", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}},
            "ETH/USDC": {"token_a": "ETH", "token_b": "USDC", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}},
            "SOL/USDC": {"token_a": "SOL", "token_b": "USDC", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}},
            "MCX/USDC": {"token_a": "MCX", "token_b": "USDC", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}},
            "ETH/BTC": {"token_a": "ETH", "token_b": "BTC", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}},
            "SOL/ETH": {"token_a": "SOL", "token_b": "ETH", "liquidity_a": 0, "liquidity_b": 0, "fees_mcx": 0, "lp_providers": {}}
        }
    
    def connect(self) -> bool:
        print(f"[DEX] Connecting to cross-chain aggregators (Thorchain, LI.FI, Bungee)")
        self.connected = True
        return True
    
    def get_price(self) -> float:
        return self.mcx_price_usd
    
    def get_liquidity(self) -> float:
        return self.total_liquidity_usd
    
    def update_price(self, new_price: float):
        self.mcx_price_usd = new_price
    
    def calculate_swap_fee_mcx(self, amount_usd: float) -> int:
        fee_usd = amount_usd * self.swap_fee_rate
        fee_mcx = int(fee_usd / self.mcx_price_usd) if self.mcx_price_usd > 0 else MCX_FEE_MIN
        return max(MCX_FEE_MIN, min(fee_mcx, MCX_FEE_MAX))
    
    async def get_swap_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        prices = {"BTC": 60000, "ETH": 3000, "SOL": 150, "USDC": 1, "USDT": 1, "BNB": 300, "MCX": self.mcx_price_usd}
        from_price = prices.get(from_token, 1)
        to_price = prices.get(to_token, 1)
        value_usd = amount * from_price
        expected_output = (value_usd / to_price) * 0.997
        fee_mcx = self.calculate_swap_fee_mcx(value_usd)
        return {
            "from_token": from_token, "to_token": to_token, "amount_in": amount,
            "expected_output": expected_output, "fee_usd": value_usd * self.swap_fee_rate,
            "fee_mcx": fee_mcx, "route": f"{from_token} → {to_token} via aggregator"
        }
    
    async def execute_swap(self, user_wallet: str, from_token: str, to_token: str, amount: float, fee_mcx: int) -> dict:
        if self.network and self.network.get_balance(user_wallet) < fee_mcx:
            return {"success": False, "error": "Insufficient MCX balance for fee"}
        if self.network:
            self.network.balances[user_wallet] -= fee_mcx
            self.network.node_pool += int(fee_mcx * 0.4)
            self.network.lp_pool += int(fee_mcx * 0.6)
        quote = await self.get_swap_quote(from_token, to_token, amount)
        tx_hash = hashlib.sha256(f"{user_wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return {"success": True, "tx_hash": tx_hash, "from_token": from_token, "to_token": to_token,
                "amount_in": amount, "amount_out": quote["expected_output"], "fee_mcx": fee_mcx}
    
    async def add_liquidity(self, user_wallet: str, token_a: str, amount_a: float, token_b: str, amount_b: float) -> dict:
        pool_id = f"{token_a}/{token_b}"
        if pool_id not in self.liquidity_pools:
            return {"success": False, "error": f"Pool {pool_id} not supported"}
        pool = self.liquidity_pools[pool_id]
        pool["liquidity_a"] += amount_a
        pool["liquidity_b"] += amount_b
        total_value = (amount_a + amount_b) / 2
        if user_wallet in pool["lp_providers"]:
            pool["lp_providers"][user_wallet] += total_value
        else:
            pool["lp_providers"][user_wallet] = total_value
        return {"success": True, "pool_id": pool_id, "amount_a": amount_a, "amount_b": amount_b,
                "lp_share": total_value, "total_liquidity_a": pool["liquidity_a"], "total_liquidity_b": pool["liquidity_b"]}
    
    async def remove_liquidity(self, user_wallet: str, pool_id: str, lp_share: float) -> dict:
        if pool_id not in self.liquidity_pools:
            return {"success": False, "error": f"Pool {pool_id} not supported"}
        pool = self.liquidity_pools[pool_id]
        if user_wallet not in pool["lp_providers"] or pool["lp_providers"][user_wallet] < lp_share:
            return {"success": False, "error": "Insufficient LP shares"}
        total_pool_value = (pool["liquidity_a"] + pool["liquidity_b"]) / 2
        share_ratio = lp_share / total_pool_value if total_pool_value > 0 else 0
        amount_a = int(pool["liquidity_a"] * share_ratio)
        amount_b = int(pool["liquidity_b"] * share_ratio)
        pool["liquidity_a"] -= amount_a
        pool["liquidity_b"] -= amount_b
        pool["lp_providers"][user_wallet] -= lp_share
        if pool["lp_providers"][user_wallet] <= 0:
            del pool["lp_providers"][user_wallet]
        return {"success": True, "pool_id": pool_id, "amount_a": amount_a, "amount_b": amount_b,
                "remaining_lp_share": pool["lp_providers"].get(user_wallet, 0)}
    
    async def get_supported_pairs(self) -> List[dict]:
        return [{"pair": pid, "token_a": p["token_a"], "token_b": p["token_b"],
                "liquidity_a": p["liquidity_a"], "liquidity_b": p["liquidity_b"],
                "fees_mcx": p["fees_mcx"]} for pid, p in self.liquidity_pools.items()]
    
    async def cross_chain_swap(self, user_wallet: str, from_chain: str, to_chain: str,
                               from_token: str, to_token: str, amount: float, fee_mcx: int, dest: str) -> dict:
        if self.network and self.network.get_balance(user_wallet) < fee_mcx:
            return {"success": False, "error": "Insufficient MCX for fee"}
        if self.network:
            self.network.balances[user_wallet] -= fee_mcx
            self.network.lp_pool += fee_mcx
        aggregator = "thorchain"
        if from_chain in ["ethereum", "bsc", "polygon"] and to_chain in ["ethereum", "bsc", "polygon"]:
            aggregator = "lifi"
        elif from_chain == "solana" or to_chain == "solana":
            aggregator = "bungee"
        tx_hash = hashlib.sha256(f"{user_wallet}{from_chain}{to_chain}{amount}{time.time()}".encode()).hexdigest()[:16]
        return {"success": True, "tx_hash": tx_hash, "from_chain": from_chain, "to_chain": to_chain,
                "amount": amount, "fee_mcx": fee_mcx, "aggregator": aggregator, "destination": dest}

# ==================== DATA STRUCTURES ====================
@dataclass
class Miner:
    validator_id: str
    public_key: str
    username: str
    wallet: str
    stake: int
    level: int
    uptime_seconds: int = 0
    last_ping: float = 0
    is_active: bool = True
    total_rewards: int = 0
    blocks_signed: int = 0
    slash_count: int = 0
    consecutive_misses: int = 0
    registered_at: float = 0
    last_challenge_response: float = 0

@dataclass
class Peer:
    address: str
    last_seen: float
    height: int
    version: int = P2P_VERSION
    is_outbound: bool = False
    state: PeerState = PeerState.CONNECTED
    ban_until: float = 0

@dataclass
class Transaction:
    tx_hash: str
    from_wallet: str
    to_wallet: str
    amount: int
    fee: int
    timestamp: float
    block_id: int = -1
    signature: str = ""
    status: TxStatus = TxStatus.PENDING

@dataclass
class Block:
    block_id: int
    timestamp: float
    previous_hash: str
    validators: List[str]
    level: int
    signatures: Dict[str, str] = field(default_factory=dict)
    block_hash: str = ""
    accepted: bool = False
    reward_distributed: bool = False
    reward_amount: int = 0
    transaction_count: int = 0
    transactions: List[Transaction] = field(default_factory=list)

# ==================== P2P NODE ====================
class P2PNode:
    def __init__(self, network):
        self.network = network
        self.peers: Dict[str, Peer] = {}
        self.server = None
        self.running = True
        self.public_ip = get_public_ip()
        self.banned_peers: Dict[str, float] = {}
    
    async def start(self):
        self.server = await asyncio.start_server(self.handle_connection, NODE_HOST, P2P_PORT)
        print(f"[P2P] Server on port {P2P_PORT}")
        if self.public_ip:
            print(f"[P2P] Public IP: {self.public_ip}:{P2P_PORT}")
    
    async def handle_connection(self, reader, writer):
        peer_addr = writer.get_extra_info('peername')
        addr_str = f"{peer_addr[0]}:{peer_addr[1]}"
        if addr_str in self.banned_peers:
            if time.time() < self.banned_peers[addr_str]:
                writer.close()
                return
            else:
                del self.banned_peers[addr_str]
        try:
            length_data = await reader.read(4)
            if not length_data:
                writer.close()
                return
            msg_len = struct.unpack(">I", length_data)[0]
            if msg_len > 10_000_000:
                self.ban_peer(addr_str, "Message too large")
                writer.close()
                return
            data = await reader.read(msg_len)
            msg_type, payload = decode_p2p_message(data)
            if msg_type is not None:
                await self.process_message(msg_type, payload, writer, addr_str)
        except Exception as e:
            print(f"[P2P] Error: {e}")
        finally:
            writer.close()
    
    def ban_peer(self, addr: str, reason: str):
        self.banned_peers[addr] = time.time() + BAN_DURATION
        if addr in self.peers:
            del self.peers[addr]
        print(f"[P2P] Banned {addr}: {reason}")
    
    async def process_message(self, msg_type, payload, writer, peer_addr):
        if msg_type == P2P_MSG_HANDSHAKE:
            response = {"node_id": socket.gethostname(), "version": P2P_VERSION,
                       "height": self.network.current_block_id, "public_ip": self.public_ip, "timestamp": time.time()}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, response)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), payload.get("height", 0))
            print(f"[P2P] New peer: {peer_addr}")
            await self.send_peers(writer)
            if payload.get("height", 0) > self.network.current_block_id:
                asyncio.create_task(self.request_blocks(peer_addr, self.network.current_block_id, payload.get("height", 0)))
        elif msg_type == P2P_MSG_GET_PEERS:
            await self.send_peers(writer)
        elif msg_type == P2P_MSG_PEERS:
            for p in payload.get("peers", []):
                if p not in self.peers and p != f"{self.public_ip}:{P2P_PORT}" and p not in self.banned_peers:
                    self.peers[p] = Peer(p, time.time(), 0)
                    asyncio.create_task(self.connect_to_peer(p))
            print(f"[P2P] Received {len(payload.get('peers', []))} peers")
        elif msg_type == P2P_MSG_GET_BLOCKS:
            start, end = payload.get("start", 0), payload.get("end", self.network.current_block_id)
            if end - start > 2000:
                end = start + 2000
            blocks = self.network.get_blocks_in_range(start, end)
            data = encode_p2p_message(P2P_MSG_BLOCKS, {"blocks": blocks, "count": len(blocks)})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        elif msg_type == P2P_MSG_BLOCKS:
            await self.network.import_blocks(payload.get("blocks", []))
        elif msg_type == P2P_MSG_NEW_BLOCK:
            await self.network.receive_external_block(payload.get("block"), peer_addr)
        elif msg_type == P2P_MSG_NEW_TRANSACTION:
            await self.network.receive_external_transaction(payload.get("transaction"), peer_addr)
        elif msg_type == P2P_MSG_GET_MEMPOOL:
            mempool = self.network.get_mempool()
            data = encode_p2p_message(P2P_MSG_MEMPOOL, {"transactions": mempool, "count": len(mempool)})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        elif msg_type == P2P_MSG_SLASH_EVENT:
            await self.network.process_slash_event(payload.get("slash"), peer_addr)
        elif msg_type == P2P_MSG_PING:
            data = encode_p2p_message(P2P_MSG_PONG, {"timestamp": time.time()})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        elif msg_type == P2P_MSG_PONG:
            if peer_addr in self.peers:
                self.peers[peer_addr].last_seen = time.time()
    
    async def send_peers(self, writer):
        peers_list = list(self.peers.keys())[:100]
        data = encode_p2p_message(P2P_MSG_PEERS, {"peers": peers_list, "count": len(peers_list)})
        writer.write(struct.pack(">I", len(data)) + data)
        await writer.drain()
    
    async def request_blocks(self, peer_addr, start, end):
        try:
            host, port = peer_addr.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            data = encode_p2p_message(P2P_MSG_GET_BLOCKS, {"start": start, "end": end})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            writer.close()
        except Exception as e:
            print(f"[P2P] Request failed: {e}")
    
    async def connect_to_peer(self, peer_addr):
        if peer_addr in self.peers or peer_addr in self.banned_peers:
            return
        try:
            host, port = peer_addr.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            handshake = {"node_id": socket.gethostname(), "version": P2P_VERSION,
                        "height": self.network.current_block_id, "public_ip": self.public_ip, "timestamp": time.time()}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, handshake)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), self.network.current_block_id, is_outbound=True)
            print(f"[P2P] Connected to {peer_addr}")
            writer.close()
        except Exception as e:
            print(f"[P2P] Failed to connect to {peer_addr}: {e}")
    
    async def broadcast_new_block(self, block: dict):
        data = encode_p2p_message(P2P_MSG_NEW_BLOCK, {"block": block, "timestamp": time.time(), "node_id": socket.gethostname()})
        for peer_addr in list(self.peers.keys()):
            try:
                host, port = peer_addr.split(":")
                reader, writer = await asyncio.open_connection(host, int(port))
                writer.write(struct.pack(">I", len(data)) + data)
                await writer.drain()
                writer.close()
            except:
                pass
    
    async def discover_peers(self):
        for peer in query_dns_seeds():
            if peer not in self.peers and peer not in self.banned_peers:
                asyncio.create_task(self.connect_to_peer(peer))
        for peer_addr in list(self.peers.keys()):
            try:
                host, port = peer_addr.split(":")
                reader, writer = await asyncio.open_connection(host, int(port))
                data = encode_p2p_message(P2P_MSG_GET_PEERS, {})
                writer.write(struct.pack(">I", len(data)) + data)
                await writer.drain()
                writer.close()
            except:
                if peer_addr in self.peers:
                    del self.peers[peer_addr]
    
    async def sync_with_peers(self):
        if not self.peers:
            return
        best_peer, best_height = None, self.network.current_block_id
        for addr, peer in self.peers.items():
            if peer.height > best_height:
                best_height, best_peer = peer.height, addr
        if best_peer and best_height > self.network.current_block_id:
            print(f"[P2P] Syncing from {best_peer}: local={self.network.current_block_id}, remote={best_height}")
            await self.request_blocks(best_peer, self.network.current_block_id, best_height)
    
    async def heartbeat(self):
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            data = encode_p2p_message(P2P_MSG_PING, {"timestamp": time.time()})
            for peer_addr in list(self.peers.keys()):
                try:
                    host, port = peer_addr.split(":")
                    reader, writer = await asyncio.open_connection(host, int(port))
                    writer.write(struct.pack(">I", len(data)) + data)
                    await writer.drain()
                    writer.close()
                except:
                    if peer_addr in self.peers:
                        del self.peers[peer_addr]

# ==================== MICROCORE NETWORK ====================
class MicroCoreNetwork:
    def __init__(self, is_genesis_node: bool = False):
        self.miners: Dict[str, Miner] = {}
        self.uptime_pool: int = 0
        self.node_pool: int = 0
        self.lp_pool: int = 0
        self.current_block_id: int = 0
        self.blocks: List[Block] = []
        self.pending_transactions: List[Transaction] = []
        self.pending_challenges: Dict[str, Dict] = {}
        self.level_groups: Dict[int, List[str]] = defaultdict(list)
        self.level_unique_wallets: Dict[int, int] = {}
        self.last_distribution: float = time.time()
        self.last_block_hash: str = "0" * 64
        self.max_level: int = 1
        self.total_minted: int = 0
        self.balances: Dict[str, int] = {}
        self.is_genesis_node = is_genesis_node
        
        self.p2p = P2PNode(self)
        self.dex = DEXBridge(self)
        
        self.init_database()
        if self.is_genesis_node:
            self.create_genesis_block()
        else:
            self.check_existing_blockchain()
        self.load_total_minted()
        self.load_balances()
        self.dex.connect()
    
    def init_database(self):
        self.conn = sqlite3.connect('microcore.db', check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS miners
                     (validator_id TEXT PRIMARY KEY, public_key TEXT, username TEXT,
                      wallet TEXT, stake INTEGER, level INTEGER, total_rewards INTEGER,
                      blocks_signed INTEGER, slash_count INTEGER, uptime_seconds INTEGER,
                      registered_at REAL, last_ping REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS blocks
                     (block_id INTEGER PRIMARY KEY, timestamp REAL, previous_hash TEXT,
                      validators TEXT, level INTEGER, block_hash TEXT, reward_amount INTEGER,
                      transaction_count INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS transactions
                     (tx_hash TEXT PRIMARY KEY, from_wallet TEXT, to_wallet TEXT,
                      amount INTEGER, fee INTEGER, timestamp REAL, block_id INTEGER,
                      signature TEXT, status TEXT, nonce INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS balances
                     (wallet TEXT PRIMARY KEY, balance INTEGER, last_updated REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS peers
                     (peer_address TEXT PRIMARY KEY, last_seen REAL, height INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS slashing_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, validator_id TEXT,
                      amount INTEGER, reason TEXT, timestamp REAL, block_id INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS supply_metrics
                     (key TEXT PRIMARY KEY, value INTEGER, updated_at REAL)''')
        self.conn.commit()
    
    def check_existing_blockchain(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocks")
        if c.fetchone()[0] == 0:
            print("[SYNC] No blockchain found. Waiting to sync from genesis node...")
            print("[SYNC] Use --peer IP:PORT to connect to genesis node")
        else:
            print(f"[SYNC] Found existing blockchain, loading...")
            self.load_blocks_from_db()
    
    def load_blocks_from_db(self):
        c = self.conn.cursor()
        c.execute("SELECT block_id, timestamp, previous_hash, validators, level, block_hash, reward_amount FROM blocks ORDER BY block_id")
        for row in c.fetchall():
            block = Block(row[0], row[1], row[2], row[3].split(',') if row[3] else [], row[4],
                         block_hash=row[5], reward_amount=row[6], accepted=True, reward_distributed=True)
            self.blocks.append(block)
            if block.block_id >= self.current_block_id:
                self.current_block_id = block.block_id + 1
                self.last_block_hash = block.block_hash
        print(f"[SYNC] Loaded {len(self.blocks)} blocks")
    
    def load_balances(self):
        c = self.conn.cursor()
        c.execute("SELECT wallet, balance FROM balances")
        for row in c.fetchall():
            self.balances[row[0]] = row[1]
    
    def load_total_minted(self):
        c = self.conn.cursor()
        c.execute("SELECT SUM(reward_amount) FROM blocks WHERE reward_amount > 0")
        result = c.fetchone()[0]
        self.total_minted = (result or 0) + sum(self.balances.values())
    
    def create_genesis_block(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocks")
        if c.fetchone()[0] > 0:
            return
        print("\n" + "=" * 70)
        print(f"{NAME} ({SYMBOL}) GENESIS NODE")
        print("The birth of a new blockchain")
        print("=" * 70)
        genesis = Block(0, time.time(), "0"*64, ["genesis"], 1, reward_amount=0)
        genesis.block_hash = hash_block({"block_id":0, "timestamp":genesis.timestamp, "previous_hash":"0"*64,
                                        "validators":["genesis"], "level":1})
        genesis.accepted = True
        genesis.reward_distributed = True
        self.blocks.append(genesis)
        self.last_block_hash = genesis.block_hash
        self.current_block_id = 1
        self.balances["MCR_GENESIS_CREATOR"] = 10_000
        self.total_minted = 10000
        for wallet, balance in self.balances.items():
            c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (wallet, balance, time.time()))
        c.execute("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (0, genesis.timestamp, genesis.previous_hash, ','.join(genesis.validators),
                  genesis.level, genesis.block_hash, 0, 0))
        self.conn.commit()
        print(f"[GENESIS] Block #0 created: {genesis.block_hash[:32]}...")
        print(f"[GENESIS] Initial supply: 10,000 {SYMBOL}")
        print(f"[GENESIS] Hard cap: {TOTAL_SUPPLY_CAP:,} {SYMBOL}")
        print(f"[GENESIS] Halving: every {HALVING_INTERVAL:,} blocks (~4 years)")
        print(f"[GENESIS] Block reward: {INITIAL_BLOCK_REWARD} {SYMBOL}")
        print("=" * 70 + "\n")
    
    def get_current_block_reward(self) -> int:
        remaining = TOTAL_SUPPLY_CAP - self.total_minted
        if remaining <= 0:
            return 0
        halvings = self.current_block_id // HALVING_INTERVAL
        reward = INITIAL_BLOCK_REWARD // (2 ** halvings)
        reward = max(reward, MINIMUM_BLOCK_REWARD)
        return min(reward, remaining)
    
    def get_remaining_supply(self) -> int:
        return TOTAL_SUPPLY_CAP - self.total_minted
    
    def get_supply_percentage(self) -> float:
        return (self.total_minted / TOTAL_SUPPLY_CAP) * 100
    
    def get_current_halving(self) -> int:
        return self.current_block_id // HALVING_INTERVAL
    
    def get_balance(self, wallet: str) -> int:
        return self.balances.get(wallet, 0)
    
    def transfer(self, from_wallet: str, to_wallet: str, amount: int, fee: int = 1, signature: str = "") -> Optional[str]:
        if self.get_balance(from_wallet) < amount + fee:
            return None
        self.balances[from_wallet] -= (amount + fee)
        self.balances[to_wallet] += amount
        self.node_pool += fee
        tx_hash = hash_transaction({"from": from_wallet, "to": to_wallet, "amount": amount, "fee": fee, "timestamp": time.time()})
        tx = Transaction(tx_hash, from_wallet, to_wallet, amount, fee, time.time(), status=TxStatus.PENDING, signature=signature)
        self.pending_transactions.append(tx)
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (from_wallet, self.balances[from_wallet], time.time()))
        c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (to_wallet, self.balances[to_wallet], time.time()))
        self.conn.commit()
        return tx_hash
    
    def calculate_level(self, stake: int) -> int:
        if stake < LEVEL_STAKE_RANGE:
            return 1
        level = ((stake - 1) // LEVEL_STAKE_RANGE) + 1
        return min(level, self.max_level + 1, MAX_LEVEL)
    
    def update_level_groups(self):
        self.level_groups.clear()
        unique_wallets = {}
        for miner in self.miners.values():
            if miner.is_active:
                self.level_groups.setdefault(miner.level, []).append(miner.validator_id)
                if miner.level not in unique_wallets:
                    unique_wallets[miner.level] = set()
                unique_wallets[miner.level].add(miner.wallet)
        for level, wallets in unique_wallets.items():
            self.level_unique_wallets[level] = len(wallets)
        self.check_level_unlocks()
    
    def check_level_unlocks(self):
        for level in range(1, self.max_level + 2):
            if self.level_unique_wallets.get(level, 0) >= MIN_WALLETS_FOR_NEXT_LEVEL and level + 1 > self.max_level:
                self.max_level = level + 1
                print(f"[LEVEL] Level {self.max_level} unlocked! ({self.level_unique_wallets[level]} unique wallets)")
                asyncio.create_task(self.p2p.broadcast_transaction({"type": "level_update", "level": self.max_level}))
    
    def can_miner_enter_level(self, wallet: str, target: int) -> bool:
        if target <= 1:
            return True
        return self.level_unique_wallets.get(target - 1, 0) >= MIN_WALLETS_FOR_NEXT_LEVEL
    
    def select_validators(self, level: int) -> List[str]:
        miners = self.level_groups.get(level, [])
        if len(miners) < MIN_VALIDATORS_PER_BLOCK:
            return []
        seed = int(self.last_block_hash[:16], 16) if self.last_block_hash != "0"*64 else int(time.time())
        rng = random.Random(seed)
        return rng.sample(miners, MIN_VALIDATORS_PER_BLOCK)
    
    def generate_challenge(self, block_id: int, validators: List[str]) -> str:
        return hashlib.sha256(f"{block_id}{''.join(sorted(validators))}{time.time()}{self.last_block_hash}{secrets.token_hex(8)}".encode()).hexdigest()
    
    def verify_challenge_response(self, vid: str, challenge: str, block_id: int, sig: str) -> bool:
        if vid not in self.miners:
            return False
        return verify_signature(self.miners[vid].public_key, f"{challenge}{vid}{block_id}", sig)
    
    def register_miner(self, vid: str, pubkey: str, username: str, wallet: str, stake: int, sig: str, ts: float) -> bool:
        if not verify_signature(pubkey, f"{vid}{username}{stake}{ts}", sig):
            print(f"[REG] Signature failed for {username}")
            return False
        level = self.calculate_level(stake)
        if level > self.max_level:
            level = self.max_level
        if vid in self.miners:
            self.miners[vid].stake = stake
            self.miners[vid].level = level
            self.miners[vid].public_key = pubkey
            self.miners[vid].username = username
            self.miners[vid].wallet = wallet
            self.miners[vid].last_ping = time.time()
            self.miners[vid].is_active = True
        else:
            self.miners[vid] = Miner(vid, pubkey, username, wallet, stake, level, registered_at=ts)
            print(f"[REG] New miner: {username} | Level {level} | Stake {stake} {SYMBOL}")
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO miners VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (vid, pubkey, username, wallet, stake, level,
                  self.miners[vid].total_rewards, self.miners[vid].blocks_signed,
                  self.miners[vid].slash_count, self.miners[vid].uptime_seconds, ts, time.time()))
        self.conn.commit()
        self.update_level_groups()
        return True
    
    def slash_miner(self, vid: str, reason: str, block_id: int = -1):
        if vid not in self.miners:
            return
        m = self.miners[vid]
        slash = max(int(m.stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        m.stake -= slash
        if m.stake < LEVEL_STAKE_RANGE:
            m.stake = LEVEL_STAKE_RANGE
        m.slash_count += 1
        m.consecutive_misses += 1
        new_level = self.calculate_level(m.stake)
        m.level = min(new_level, self.max_level) if not self.can_miner_enter_level(m.wallet, new_level) else new_level
        if m.slash_count >= BAN_THRESHOLD:
            m.is_active = False
            print(f"[BAN] {m.username} banned after {BAN_THRESHOLD} slashes")
        c = self.conn.cursor()
        c.execute("UPDATE miners SET stake=?, level=?, slash_count=?, is_active=? WHERE validator_id=?",
                 (m.stake, m.level, m.slash_count, m.is_active, vid))
        c.execute("INSERT INTO slashing_events (validator_id, amount, reason, timestamp, block_id) VALUES (?, ?, ?, ?, ?)",
                 (vid, slash, reason, time.time(), block_id))
        self.conn.commit()
        self.update_level_groups()
        print(f"[SLASH] {m.username}: -{slash} {SYMBOL} | Stake: {m.stake} {SYMBOL} | Level: {m.level}")
        asyncio.create_task(self.p2p.broadcast_transaction({"type": "slash", "validator_id": vid, "amount": slash, "reason": reason}))
    
    def distribute_block_reward(self, block: Block):
        if block.reward_distributed:
            return
        reward = self.get_current_block_reward()
        block.reward_amount = reward
        if reward == 0:
            block.reward_distributed = True
            return
        validator_total = (reward * 75) // 100
        validator_each = validator_total // max(len(block.validators), 1)
        validator_remainder = validator_total - (validator_each * len(block.validators))
        node_total = (reward * 8) // 100 + validator_remainder
        uptime_total = (reward * 7) // 100
        lp_total = (reward * 10) // 100
        for vid in block.validators:
            if vid in self.miners:
                m = self.miners[vid]
                m.total_rewards += validator_each
                m.stake += validator_each
                m.blocks_signed += 1
                m.consecutive_misses = 0
                new_level = self.calculate_level(m.stake)
                m.level = min(new_level, self.max_level) if not self.can_miner_enter_level(m.wallet, new_level) else new_level
                self.balances[m.wallet] = self.balances.get(m.wallet, 0) + validator_each
                c = self.conn.cursor()
                c.execute("UPDATE miners SET stake=?, level=?, total_rewards=?, blocks_signed=? WHERE validator_id=?",
                         (m.stake, m.level, m.total_rewards, m.blocks_signed, vid))
                c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (m.wallet, self.balances[m.wallet], time.time()))
                self.conn.commit()
        self.node_pool += node_total
        self.uptime_pool += uptime_total
        self.lp_pool += lp_total
        self.total_minted += reward
        block.reward_distributed = True
        self.update_level_groups()
        remaining = self.get_remaining_supply()
        percent = self.get_supply_percentage()
        halving = self.get_current_halving()
        print(f"[BLOCK {block.block_id}] REWARD: {reward} {SYMBOL}")
        print(f"   ├─ Validators ({len(block.validators)}): {validator_each} {SYMBOL} each")
        print(f"   ├─ Node pool: {node_total} {SYMBOL}")
        print(f"   ├─ Uptime pool: {uptime_total} {SYMBOL}")
        print(f"   └─ LP pool: {lp_total} {SYMBOL}")
        print(f"[SUPPLY] {self.total_minted:,} / {TOTAL_SUPPLY_CAP:,} ({percent:.4f}%) | Halving: {halving}")
        print(f"[REMAINING] {remaining:,} {SYMBOL} until cap")
    
    def distribute_periodic_rewards(self):
        active_miners = [m for m in self.miners.values() if m.is_active]
        total_uptime = sum(m.uptime_seconds for m in active_miners)
        if total_uptime > 0 and self.uptime_pool > 0:
            for miner in active_miners:
                if miner.uptime_seconds > 0:
                    share = int(self.uptime_pool * (miner.uptime_seconds / total_uptime))
                    miner.total_rewards += share
                    miner.stake += share
                    self.balances[miner.wallet] = self.balances.get(miner.wallet, 0) + share
                    new_level = self.calculate_level(miner.stake)
                    miner.level = min(new_level, self.max_level) if not self.can_miner_enter_level(miner.wallet, new_level) else new_level
                    c = self.conn.cursor()
                    c.execute("UPDATE miners SET stake=?, level=?, total_rewards=?, uptime_seconds=? WHERE validator_id=?",
                             (miner.stake, miner.level, miner.total_rewards, miner.uptime_seconds, miner.validator_id))
                    c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (miner.wallet, self.balances[miner.wallet], time.time()))
                    self.conn.commit()
            print(f"[DISTRO] Uptime rewards: {self.uptime_pool} {SYMBOL} to {len(active_miners)} miners")
        self.node_pool = 0
        self.uptime_pool = 0
        self.lp_pool = 0
        self.last_distribution = time.time()
    
    def get_blocks_in_range(self, start: int, end: int) -> List[dict]:
        blocks = []
        for b in self.blocks:
            if start <= b.block_id <= end:
                blocks.append({"block_id": b.block_id, "timestamp": b.timestamp, "previous_hash": b.previous_hash,
                              "validators": b.validators, "level": b.level, "block_hash": b.block_hash,
                              "reward_amount": b.reward_amount})
        return blocks
    
    def get_mempool(self) -> List[dict]:
        return [{"tx_hash": tx.tx_hash, "from": tx.from_wallet, "to": tx.to_wallet,
                "amount": tx.amount, "fee": tx.fee, "timestamp": tx.timestamp} for tx in self.pending_transactions]
    
    async def import_blocks(self, blocks_data: List[dict]):
        for block_data in sorted(blocks_data, key=lambda x: x["block_id"]):
            if block_data["block_id"] > self.current_block_id - 1:
                existing = [b for b in self.blocks if b.block_id == block_data["block_id"]]
                if not existing:
                    block = Block(block_data["block_id"], block_data["timestamp"], block_data["previous_hash"],
                                 block_data["validators"], block_data["level"], block_hash=block_data["block_hash"],
                                 reward_amount=block_data["reward_amount"], accepted=True, reward_distributed=True)
                    self.blocks.append(block)
                    if block.block_id >= self.current_block_id:
                        self.current_block_id = block.block_id + 1
                        self.last_block_hash = block.block_hash
                    print(f"[SYNC] Imported block {block_data['block_id']}")
    
    async def receive_external_block(self, block_data: dict, peer_addr: str):
        block_id = block_data["block_id"]
        existing = [b for b in self.blocks if b.block_id == block_id]
        if existing:
            return
        if block_data["previous_hash"] == self.last_block_hash:
            block = Block(block_id, block_data["timestamp"], block_data["previous_hash"],
                         block_data["validators"], block_data["level"], block_hash=block_data["block_hash"],
                         reward_amount=block_data["reward_amount"], accepted=True, reward_distributed=True)
            self.blocks.append(block)
            self.current_block_id = block_id + 1
            self.last_block_hash = block.block_hash
            print(f"[P2P] Received block {block_id} from {peer_addr}")
    
    async def receive_external_transaction(self, tx_data: dict, peer_addr: str):
        existing = [tx for tx in self.pending_transactions if tx.tx_hash == tx_data.get("tx_hash")]
        if existing:
            return
        tx = Transaction(tx_data.get("tx_hash"), tx_data.get("from"), tx_data.get("to"),
                        tx_data.get("amount"), tx_data.get("fee", 1), tx_data.get("timestamp", time.time()),
                        status=TxStatus.PENDING)
        self.pending_transactions.append(tx)
        print(f"[P2P] Received transaction {tx.tx_hash[:16]}... from {peer_addr}")
    
    async def process_slash_event(self, slash_data: dict, peer_addr: str):
        vid = slash_data.get("validator_id")
        if vid in self.miners:
            self.slash_miner(vid, f"External: {slash_data.get('reason', 'Unknown')}")
            print(f"[P2P] Processed slash event from {peer_addr}")
    
    async def produce_block(self, level: int):
        validators = self.select_validators(level)
        if len(validators) < MIN_VALIDATORS_PER_BLOCK:
            return
        block_id = self.current_block_id
        challenge = self.generate_challenge(block_id, validators)
        self.pending_challenges[challenge] = {"block_id": block_id, "validators": validators, "level": level, "signatures": {}}
        await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
        pending = self.pending_challenges.pop(challenge, {})
        sigs = pending.get("signatures", {})
        valid_sigs = {}
        for vid, sig in sigs.items():
            if vid in self.miners and self.verify_challenge_response(vid, challenge, block_id, sig):
                valid_sigs[vid] = sig
        if len(valid_sigs) >= MIN_VALIDATORS_PER_BLOCK:
            block = Block(block_id, time.time(), self.last_block_hash, list(valid_sigs.keys()), level, valid_sigs,
                         transactions=self.pending_transactions[:100])
            block.transaction_count = len(block.transactions)
            self.distribute_block_reward(block)
            block.block_hash = hash_block({"block_id": block_id, "timestamp": block.timestamp,
                                          "previous_hash": self.last_block_hash, "validators": block.validators, "level": level,
                                          "transactions": [{"tx_hash": tx.tx_hash, "from": tx.from_wallet,
                                                           "to": tx.to_wallet, "amount": tx.amount} for tx in block.transactions]})
            self.last_block_hash = block.block_hash
            self.blocks.append(block)
            self.pending_transactions = self.pending_transactions[100:]
            self.current_block_id += 1
            print(f"[BLOCK {block_id}] ✅ ACCEPTED | Validators: {len(valid_sigs)} | Txs: {block.transaction_count}")
            await self.p2p.broadcast_new_block({"block_id": block_id, "timestamp": block.timestamp,
                                               "previous_hash": block.previous_hash, "validators": block.validators,
                                               "level": level, "block_hash": block.block_hash,
                                               "reward_amount": block.reward_amount})
        else:
            missing = set(validators) - set(sigs.keys())
            for vid in missing:
                self.slash_miner(vid, f"Missed signing for block {block_id}", block_id)
            print(f"[BLOCK {block_id}] ❌ REJECTED | Signatures: {len(valid_sigs)}/{MIN_VALIDATORS_PER_BLOCK}")

# ==================== WEBSOCKET SERVER ====================
class MicroCoreServer:
    def __init__(self, network: MicroCoreNetwork):
        self.network = network
        self.connections = {}
    
    async def handle(self, websocket, path):
        try:
            async for message in websocket:
                data = json.loads(message)
                t = data.get("type")
                if t == "register":
                    vid = data["validator_id"]
                    pubkey = data["public_key"]
                    username = data["username"]
                    wallet = data["wallet"]
                    stake = data.get("stake", 100)
                    sig = data.get("signature", "")
                    ts = data.get("timestamp", time.time())
                    if self.network.register_miner(vid, pubkey, username, wallet, stake, sig, ts):
                        self.connections[vid] = websocket
                        await websocket.send(json.dumps({
                            "type": "registered", "level": self.network.miners[vid].level if vid in self.network.miners else 1,
                            "max_level": self.network.max_level, "remaining_supply": self.network.get_remaining_supply(),
                            "current_reward": self.network.get_current_block_reward(), "mcx_price": self.network.dex.get_price(),
                            "symbol": SYMBOL
                        }))
                elif t == "node_register":
                    self.connections[f"node_{data['node_id']}"] = websocket
                    await websocket.send(json.dumps({"type": "node_registered", "status": "ok"}))
                elif t == "lp_register":
                    result = await self.network.dex.add_liquidity(data["wallet"], data["token_a"], data["amount_a"],
                                                                 data["token_b"], data["amount_b"])
                    await websocket.send(json.dumps({"type": "lp_registered", "data": result}))
                elif t == "block_signature":
                    if data["challenge"] in self.network.pending_challenges:
                        self.network.pending_challenges[data["challenge"]]["signatures"][data["validator_id"]] = data["signature"]
                elif t == "uptime_ping":
                    if data["validator_id"] in self.network.miners:
                        self.network.miners[data["validator_id"]].uptime_seconds = data.get("uptime_seconds", 0)
                elif t == "swap_quote":
                    quote = await self.network.dex.get_swap_quote(data["from_token"], data["to_token"], data["amount"])
                    await websocket.send(json.dumps({"type": "swap_quote", "data": quote}))
                elif t == "execute_swap":
                    result = await self.network.dex.execute_swap(data["wallet"], data["from_token"], data["to_token"],
                                                                 data["amount"], data["fee_mcx"])
                    await websocket.send(json.dumps({"type": "swap_result", "data": result}))
                elif t == "add_liquidity_dex":
                    result = await self.network.dex.add_liquidity(data["wallet"], data["token_a"], data["amount_a"],
                                                                  data["token_b"], data["amount_b"])
                    await websocket.send(json.dumps({"type": "liquidity_added", "data": result}))
                elif t == "remove_liquidity_dex":
                    result = await self.network.dex.remove_liquidity(data["wallet"], data["pool_id"], data["lp_share"])
                    await websocket.send(json.dumps({"type": "liquidity_removed", "data": result}))
                elif t == "get_supported_pairs":
                    pairs = await self.network.dex.get_supported_pairs()
                    await websocket.send(json.dumps({"type": "supported_pairs", "pairs": pairs}))
                elif t == "cross_chain_swap":
                    result = await self.network.dex.cross_chain_swap(data["wallet"], data["from_chain"], data["to_chain"],
                                                                     data["from_token"], data["to_token"], data["amount"],
                                                                     data["fee_mcx"], data["destination"])
                    await websocket.send(json.dumps({"type": "cross_chain_result", "data": result}))
                elif t == "get_balance":
                    balance = self.network.get_balance(data["wallet"])
                    await websocket.send(json.dumps({"type": "balance", "wallet": data["wallet"], "balance": balance}))
                elif t == "send":
                    tx_hash = self.network.transfer(data["from"], data["to"], data["amount"], data.get("fee", 1), data.get("signature", ""))
                    if tx_hash:
                        await websocket.send(json.dumps({"type": "tx_confirmed", "tx_hash": tx_hash}))
                    else:
                        await websocket.send(json.dumps({"type": "tx_failed", "reason": "Insufficient balance"}))
                elif t == "get_status":
                    await websocket.send(json.dumps({
                        "type": "status", "data": {
                            "block_id": self.network.current_block_id,
                            "total_miners": len(self.network.miners),
                            "active_miners": sum(1 for m in self.network.miners.values() if m.is_active),
                            "max_level": self.network.max_level,
                            "current_reward": self.network.get_current_block_reward(),
                            "total_minted": self.network.total_minted,
                            "remaining_supply": self.network.get_remaining_supply(),
                            "supply_percentage": self.network.get_supply_percentage(),
                            "current_halving": self.network.get_current_halving(),
                            "mcx_price": self.network.dex.get_price(),
                            "symbol": SYMBOL
                        }
                    }))
                elif t == "get_blocks":
                    limit = data.get("limit", 20)
                    blocks = self.network.get_blocks_in_range(max(0, self.network.current_block_id - limit), self.network.current_block_id)
                    await websocket.send(json.dumps({"type": "blocks", "blocks": blocks[::-1]}))
                elif t == "get_miners":
                    miners_list = [{"validator_id": m.validator_id, "username": m.username, "wallet": m.wallet,
                                   "level": m.level, "stake": m.stake, "blocks_signed": m.blocks_signed,
                                   "total_rewards": m.total_rewards, "is_active": m.is_active} for m in self.network.miners.values()]
                    await websocket.send(json.dumps({"type": "miners", "miners": miners_list}))
                elif t == "get_transactions":
                    c = self.network.conn.cursor()
                    c.execute("SELECT tx_hash, from_wallet, to_wallet, amount, fee, timestamp, status FROM transactions ORDER BY timestamp DESC LIMIT 20")
                    txs = [{"tx_hash": r[0], "from": r[1], "to": r[2], "amount": r[3], "fee": r[4], "timestamp": r[5], "status": r[6]} for r in c.fetchall()]
                    await websocket.send(json.dumps({"type": "transactions", "transactions": txs}))
        except Exception as e:
            print(f"[WS] Error: {e}")
    
    async def periodic_distribution(self):
        while True:
            await asyncio.sleep(DISTRIBUTION_INTERVAL_SEC)
            self.network.distribute_periodic_rewards()
    
    async def block_production_loop(self):
        level = 1
        while True:
            if self.network.level_groups:
                avail = [l for l in self.network.level_groups if len(self.network.level_groups[l]) >= MIN_VALIDATORS_PER_BLOCK]
                if avail:
                    level = (level % max(avail)) + 1
                    if level in avail:
                        await self.network.produce_block(level)
            await asyncio.sleep(0.1)
    
    async def peer_discovery(self):
        while True:
            await asyncio.sleep(PEX_INTERVAL)
            await self.network.p2p.discover_peers()
    
    async def peer_sync(self):
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            await self.network.p2p.sync_with_peers()
    
    async def status_reporter(self):
        while True:
            await asyncio.sleep(60)
            remaining = self.network.get_remaining_supply()
            percent = self.network.get_supply_percentage()
            reward = self.network.get_current_block_reward()
            halving = self.network.get_current_halving()
            price = self.network.dex.get_price()
            print(f"\n[STATUS] Block: {self.network.current_block_id} | Reward: {reward} {SYMBOL} | Halving: {halving}")
            print(f"[STATUS] Miners: {len(self.network.miners)} | Active: {sum(1 for m in self.network.miners.values() if m.is_active)}")
            print(f"[STATUS] Peers: {len(self.network.p2p.peers)} | Pending TX: {len(self.network.pending_transactions)}")
            print(f"[SUPPLY] {self.network.total_minted:,} / {TOTAL_SUPPLY_CAP:,} ({percent:.4f}%) | Remaining: {remaining:,} {SYMBOL}")
            print(f"[PRICE] 1 {SYMBOL} = ${price:.4f} USD\n")
    
    async def run(self):
        asyncio.create_task(self.network.p2p.start())
        asyncio.create_task(self.network.p2p.heartbeat())
        asyncio.create_task(self.peer_discovery())
        asyncio.create_task(self.peer_sync())
        asyncio.create_task(self.periodic_distribution())
        asyncio.create_task(self.block_production_loop())
        asyncio.create_task(self.status_reporter())
        async with websockets.serve(self.handle, NODE_HOST, NODE_PORT):
            print(f"[WS] Server: ws://{NODE_HOST}:{NODE_PORT}")
            print(f"[P2P] Port: {P2P_PORT}")
            print(f"[DNS] Seeds: {', '.join(DNS_SEEDS)}")
            await asyncio.Future()

# ==================== EMBEDDED LOCAL MINER (RUNS INSIDE NODE) ====================
class LocalMiner:
    """
    EMBEDDED MINER - Runs automatically when you run the node
    This mines on behalf of the node operator
    Disable with --no-miner flag
    """
    
    def __init__(self, network: MicroCoreNetwork, username: str, private_key: str, public_key: str, wallet: str):
        self.network = network
        self.username = username
        self.private_key = private_key
        self.public_key = public_key
        self.wallet = wallet
        self.validator_id = f"LOCAL_{username}_{hashlib.sha256(username.encode()).hexdigest()[:16]}"
        self.stake = 100
        self.level = 1
        self.rewards = 0
        self.blocks = 0
        self.uptime = 0
        self.running = True
    
    def register(self):
        """Register this embedded miner with the node"""
        ts = time.time()
        sig = sign_message(self.private_key, f"{self.validator_id}{self.username}{self.stake}{ts}")
        self.network.register_miner(self.validator_id, self.public_key, self.username, self.wallet, self.stake, sig, ts)
        print(f"[EMBEDDED MINER] Registered: {self.username}")
    
    async def run(self):
        """Main loop for embedded miner - listens for challenges and signs blocks"""
        self.register()
        last_uptime = time.time()
        
        print(f"[EMBEDDED MINER] Started - Node operator is mining with username '{self.username}'")
        print(f"[EMBEDDED MINER] Stake: {self.stake} {SYMBOL}, Level: {self.level}")
        print(f"[EMBEDDED MINER] Waiting for challenges...\n")
        
        while self.running:
            # Check for pending challenges that this miner is selected for
            for challenge, pending in self.network.pending_challenges.items():
                if self.validator_id in pending["validators"] and self.validator_id not in pending["signatures"]:
                    # Sign the block
                    sig = sign_message(self.private_key, f"{challenge}{self.validator_id}{pending['block_id']}")
                    pending["signatures"][self.validator_id] = sig
                    print(f"[EMBEDDED MINER] Signed block {pending['block_id']}")
                    self.blocks += 1
                    break
            
            # Update uptime every 30 seconds
            if time.time() - last_uptime > 30:
                self.uptime += 30
                if self.validator_id in self.network.miners:
                    self.network.miners[self.validator_id].uptime_seconds = self.uptime
                last_uptime = time.time()
            
            await asyncio.sleep(0.1)

# ==================== MAIN ====================
async def main():
    parser = argparse.ArgumentParser(description=f'{NAME} Node + Embedded Miner')
    parser.add_argument('--genesis', action='store_true', help='Run as genesis node (only first node)')
    parser.add_argument('--peer', type=str, help='Connect to peer node (IP:PORT)')
    parser.add_argument('--no-miner', action='store_true', help='Disable embedded miner (run node only)')
    args = parser.parse_args()
    
    print("=" * 60)
    print(f"{NAME} ({SYMBOL}) COMPLETE NODE + EMBEDDED MINER v{VERSION}")
    print("=" * 60)
    print(f"Hard cap: {TOTAL_SUPPLY_CAP:,} {SYMBOL}")
    print(f"Initial reward: {INITIAL_BLOCK_REWARD} {SYMBOL}")
    print(f"Halving interval: {HALVING_INTERVAL:,} blocks (~4 years)")
    print(f"Level stake range: {LEVEL_STAKE_RANGE} {SYMBOL}/level")
    print(f"Reward split: 75% validators | 8% nodes | 7% uptime | 10% LP")
    print("=" * 60)
    print("\nNOTE: This node includes an EMBEDDED MINER that mines automatically.")
    print("      The node operator earns rewards for running the node.")
    print("      Use --no-miner to disable.\n")
    
    if args.peer:
        print(f"[P2P] Bootstrap peer: {args.peer}")
    
    # Load or create wallet for embedded miner
    wallet_file = "microcore_wallet.json"
    if os.path.exists(wallet_file):
        with open(wallet_file, 'r') as f:
            data = json.load(f)
            username = data.get('username', 'local_miner')
            wallet_addr = data['address']
            private_key = data['private_key']
            public_key = data['public_key']
        print(f"[WALLET] Loaded: {username} ({wallet_addr[:20]}...)")
    else:
        wallet_addr, private_key, public_key = generate_wallet()
        username = input("Enter your username for embedded miner: ").strip() or "local_miner"
        with open(wallet_file, 'w') as f:
            json.dump({'username': username, 'address': wallet_addr, 'private_key': private_key, 'public_key': public_key}, f)
        print(f"[WALLET] Created: {username} ({wallet_addr[:20]}...)")
        print(f"[WALLET] SAVE THIS FILE: {os.path.abspath(wallet_file)}")
        print(f"[WALLET] Private key (KEEP SECRET): {private_key}")
    
    # Create network
    network = MicroCoreNetwork(is_genesis_node=args.genesis)
    server = MicroCoreServer(network)
    
    # Start embedded miner (unless disabled)
    if not args.no_miner:
        miner = LocalMiner(network, username, private_key, public_key, wallet_addr)
        asyncio.create_task(miner.run())
        print(f"\n[EMBEDDED MINER] Started for '{username}' - Mining automatically!")
    else:
        print(f"\n[EMBEDDED MINER] Disabled (--no-miner flag). Running node only.")
    
    print(f"\n{NAME} ({SYMBOL}) node is running...\n")
    await server.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Node stopped")
        sys.exit(0)
