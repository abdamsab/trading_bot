"""Quick test: MT5 initialize with credentials.
Run on Windows after git pull.

Usage:
    cd trading_bot
    python scripts/test_mt5_init.py
"""

import sys
import os

# Add project root to path so we can use the venv MetaTrader5
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5

# === EDIT THESE to match your .env values ===
LOGIN = 436768244
PASSWORD = "Sn@@kies-,2#4"
SERVER = "Exness-MT5Trial9"
PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

print(f"MT5 version: {mt5.__version__}")
print(f"Connecting to {SERVER} as account {LOGIN} ...")

# Try with ALL params (path + credentials) — same as what the Gateway now does
result = mt5.initialize(
    path=PATH,
    login=LOGIN,
    password=PASSWORD,
    server=SERVER,
)

print(f"result: {result}")

if result:
    info = mt5.account_info()
    if info:
        print(f"Connected!")
        print(f"  Balance: {info.balance} {info.currency}")
        print(f"  Equity:  {info.equity}")
        print(f"  Margin:  {info.margin}")
    else:
        print("Connected but account_info() returned None")
    mt5.shutdown()
    print("SUCCESS")
    sys.exit(0)
else:
    code, desc = mt5.last_error()
    print(f"FAILED  error: [{code}] {desc}")
    mt5.shutdown()
    sys.exit(1)
