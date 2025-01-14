import time
import os
import random
import requests
from typing import Callable, Any
from web3 import Web3
from web3.exceptions import TimeExhausted

INI_CHAIN_RPC = "http://rpc-testnet.inichain.com"
WALLET_ADDRESS_RAW = "REPLACE_WITH_YOUR_EVM_WALLET"
PRIVATE_KEY = "REPLACE_WITH_YOUR_PRIVATE_KEY"

if not PRIVATE_KEY:
    raise Exception("No PRIVATE_KEY found. Set PRIVATE_KEY in your code or environment.")

# We might keep a global reference to web3, but re-init on errors
web3 = None

def make_web3_provider() -> Web3:
    """Creates a fresh Web3 instance with ~20s HTTP request timeout."""
    return Web3(Web3.HTTPProvider(INI_CHAIN_RPC, request_kwargs={"timeout": 20}))

#############################################################################
# 2. Re-init Web3 + Basic Checks
#############################################################################
def init_web3():
    global web3
    web3 = make_web3_provider()
    if not web3.is_connected():
        raise Exception("Cannot connect to the IniChain testnet RPC.")

# Initialize once at the start
init_web3()

WALLET_ADDRESS = web3.to_checksum_address(WALLET_ADDRESS_RAW)
ROUTER_ADDRESS = web3.to_checksum_address("0x4ccB784744969D9B63C15cF07E622DDA65A88Ee7")
USDT_ADDRESS   = web3.to_checksum_address("0xcF259Bca0315C6D32e877793B6a10e97e7647FdE")
WINI_ADDRESS   = web3.to_checksum_address("0xfbECae21C91446f9c7b87E4e5869926998f99ffe")

ROUTER_ABI = [
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"internalType": "uint256", "name":"amountOutMin","type":"uint256"},
            {"internalType": "address[]","name":"path","type":"address[]"},
            {"internalType": "address","name":"to","type":"address"},
            {"internalType": "uint256","name":"deadline","type":"uint256"}
        ],
        "outputs": [{"internalType":"uint256[]","name":"","type":"uint256[]"}]
    },
    {
        "name": "swapExactTokensForETH",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
            {"internalType":"address","name":"to","type":"address"},
            {"internalType":"uint256","name":"deadline","type":"uint256"}
        ],
        "outputs": [{"internalType":"uint256[]","name":"","type":"uint256[]"}]
    }
]

USDT_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"internalType": "address","name": "spender","type": "address"},
            {"internalType": "uint256","name": "value","type": "uint256"}
        ],
        "outputs": [{"internalType":"bool","name":"","type":"bool"}]
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"internalType": "address","name": "account","type":"address"}
        ],
        "outputs": [{"internalType":"uint256","name":"","type":"uint256"}]
    }
]

#############################################################################
# 3. Universal call_with_retries
#############################################################################
def call_with_retries(
    func: Callable[..., Any],
    max_tries: int = 3,
    sleep_seconds: float = 5.0,
    reinit_on_error: bool = True,
    **kwargs
) -> Any:
    """
    Calls func(**kwargs) with up to max_tries retries if we see:
      - requests.exceptions.ConnectionError
      - "replacement transaction underpriced"
      - code': -32000
    We also handle random ValueErrors and re-init web3 if desired.
    """
    global web3
    for attempt in range(1, max_tries + 1):
        try:
            return func(**kwargs)

        except requests.exceptions.ConnectionError as e:
            print(f"[call_with_retries] Attempt {attempt} - ConnectionError: {e}")
            if reinit_on_error:
                print("[call_with_retries] Re-initializing web3 provider due to connection error.")
                init_web3()
            if attempt < max_tries:
                time.sleep(sleep_seconds)
            else:
                raise

        except ValueError as e:
            msg = str(e)
            if "replacement transaction underpriced" in msg or "code': -32000" in msg:
                print("[call_with_retries] Caught 'replacement transaction underpriced'.")
                if attempt < max_tries:
                    print("[call_with_retries] Retry after sleeping 5s...")
                    time.sleep(5)
                else:
                    raise
            else:
                print(f"[call_with_retries] Unhandled ValueError: {msg}")
                if attempt < max_tries:
                    time.sleep(sleep_seconds)
                else:
                    raise

        except Exception as e:
            print(f"[call_with_retries] Unhandled error on attempt {attempt}: {e}")
            if reinit_on_error:
                init_web3()
            if attempt < max_tries:
                time.sleep(sleep_seconds)
            else:
                raise

#############################################################################
# 4. Extended wait_for_transaction_receipt with retry
#############################################################################
def wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5, max_tries=2):
    for attempt in range(1, max_tries + 1):
        try:
            receipt = web3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=timeout, poll_latency=poll_latency
            )
            return receipt
        except TimeExhausted:
            print(f"[wait_for_tx_receipt_with_retry] Attempt {attempt}: timed out after {timeout}s.")
            if attempt < max_tries:
                print("[wait_for_tx_receipt_with_retry] Retrying wait...")
            else:
                print("[wait_for_tx_receipt_with_retry] Max attempts reached, giving up.")
                raise

#############################################################################
# 5. Simple Helpers for Router & USDT Contract
#############################################################################
def get_router_contract():
    return web3.eth.contract(address=ROUTER_ADDRESS, abi=ROUTER_ABI)

def get_usdt_contract():
    return web3.eth.contract(address=USDT_ADDRESS, abi=USDT_ABI)

#############################################################################
# 6. Gas-bumping "send transaction" with call_with_retries
#############################################################################
def send_tx(tx_data, private_key, max_tries=3):
    """
    Signs and sends the transaction with up to `max_tries`.
    If we get 'replacement transaction underpriced', we bump gas price by 20%.
    We also handle connection errors, etc.
    Returns tx_hash on success.
    """
    tx_local = tx_data.copy()

    def do_send():
        signed_tx = web3.eth.account.sign_transaction(tx_local, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        return tx_hash

    for attempt in range(1, max_tries + 1):
        try:
            return call_with_retries(func=do_send, max_tries=1)
        except ValueError as e:
            msg = str(e)
            if "replacement transaction underpriced" in msg or "code': -32000" in msg:
                old_gas = tx_local['gasPrice']
                new_gas = int(old_gas * 1.2)
                tx_local['gasPrice'] = new_gas
                print(f"[send_tx] Bumped gas from {old_gas} -> {new_gas}")
                if attempt < max_tries:
                    time.sleep(5)
                    continue
                else:
                    raise
            else:
                print(f"[send_tx] Unhandled ValueError: {msg}")
                if attempt < max_tries:
                    time.sleep(5)
                    continue
                else:
                    raise
        except requests.exceptions.ConnectionError as ce:
            print(f"[send_tx] ConnectionError attempt {attempt}: {ce}")
            init_web3()
            if attempt < max_tries:
                time.sleep(5)
            else:
                raise

    raise Exception("[send_tx] All attempts to send transaction have failed.")


#############################################################################
# 7. Getting Balances (with call_with_retries)
#############################################################################
def get_ini_balance() -> float:
    def do_get():
        balance_wei = web3.eth.get_balance(WALLET_ADDRESS)
        return float(web3.from_wei(balance_wei, 'ether'))
    return call_with_retries(func=do_get, max_tries=3)

def get_usdt_balance() -> float:
    usdt_c = get_usdt_contract()
    def do_get():
        bal_wei = usdt_c.functions.balanceOf(WALLET_ADDRESS).call()
        return float(web3.from_wei(bal_wei, 'ether'))
    return call_with_retries(func=do_get, max_tries=3)

#############################################################################
# 8. Approve USDT
#############################################################################
def approve_usdt(spend_amount_wei: int):
    usdt_c = get_usdt_contract()
    def do_build():
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        return usdt_c.functions.approve(ROUTER_ADDRESS, spend_amount_wei).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 100_000,
            'gasPrice': web3.to_wei('10', 'gwei'),
            'nonce': nonce
        })

    tx_data = call_with_retries(do_build)
    tx_hash = send_tx(tx_data, PRIVATE_KEY, max_tries=3)
    print("[approve_usdt] TX hash:", tx_hash.hex())

    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    print(f"[approve_usdt] Confirmed in block: {receipt.blockNumber}\n")

#############################################################################
# 9. Swap: INI -> USDT
#############################################################################
def swap_ini_to_usdt(ini_amount_in_ether, min_out_wei=0):
    print("[swap_ini_to_usdt] swapping", ini_amount_in_ether, "INI -> USDT")

    router_c = get_router_contract()
    def do_build():
        path = [WINI_ADDRESS, USDT_ADDRESS]
        amount_in_wei = web3.to_wei(ini_amount_in_ether, 'ether')
        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        return router_c.functions.swapExactETHForTokens(
            min_out_wei,
            path,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'value': amount_in_wei,
            'gas': 300_000,
            'gasPrice': web3.to_wei('10', 'gwei'),
            'nonce': nonce
        })

    tx_data = call_with_retries(do_build, max_tries=3)
    tx_hash = send_tx(tx_data, PRIVATE_KEY, max_tries=3)
    print(f"[swap_ini_to_usdt] TX hash: {tx_hash.hex()}")

    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    print(f"[swap_ini_to_usdt] Confirmed in block: {receipt.blockNumber}")

    gas_used = receipt.gasUsed
    final_gas_price = tx_data['gasPrice']
    fee_wei = gas_used * final_gas_price
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"[swap_ini_to_usdt] Tx Fee: {fee_ini} INI\n")

#############################################################################
# 10. Swap: USDT -> INI
#############################################################################
def swap_usdt_to_ini(usdt_amount_in_ether, min_out_wei=0):
    print("[swap_usdt_to_ini] swapping", usdt_amount_in_ether, "USDT -> INI")

    router_c = get_router_contract()
    def do_build():
        path = [USDT_ADDRESS, WINI_ADDRESS]
        amount_in_wei = web3.to_wei(usdt_amount_in_ether, 'ether')
        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        return router_c.functions.swapExactTokensForETH(
            amount_in_wei,
            min_out_wei,
            path,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 300_000,
            'gasPrice': web3.to_wei('10', 'gwei'),
            'nonce': nonce
        })

    tx_data = call_with_retries(do_build, max_tries=3)
    tx_hash = send_tx(tx_data, PRIVATE_KEY, max_tries=3)
    print(f"[swap_usdt_to_ini] TX hash: {tx_hash.hex()}")

    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    print(f"[swap_usdt_to_ini] Confirmed in block: {receipt.blockNumber}")

    gas_used = receipt.gasUsed
    final_gas_price = tx_data['gasPrice']
    fee_wei = gas_used * final_gas_price
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"[swap_usdt_to_ini] Tx Fee: {fee_ini} INI\n")

#############################################################################
# 11. DAILY CHECK-IN CODE
#############################################################################
CHECKIN_ADDRESS = web3.to_checksum_address("0x73439c32e125B28139823fE9C6C079165E94C6D1")
CHECKIN_ABI = [
    {
        "inputs": [],
        "stateMutability": "nonpayable",
        "type": "constructor"
    }, 
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "internalType": "address", "name": "account", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "time", "type": "uint256"}
        ],
        "name": "CheckInEvent",
        "type": "event"
    }, 
    {
        "inputs": [],
        "name": "checkIn",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }, 
    {
        "inputs": [],
        "name": "listCheckInRec",
        "outputs": [
            {"internalType": "uint256[]", "name": "times_", "type": "uint256[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }, 
    {
        "inputs": [
            {"internalType": "uint256", "name": "_day", "type": "uint256"}
        ],
        "name": "listCheckInStatu",
        "outputs": [
            {"internalType": "bool", "name": "statu_", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    }, 
    {
        "inputs": [
            {"internalType": "uint256", "name": "_start", "type": "uint256"},
            {"internalType": "uint256", "name": "_end", "type": "uint256"}
        ],
        "name": "listCheckInStatus",
        "outputs": [
            {"internalType": "bool[]", "name": "status_", "type": "bool[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# We'll track the last check-in time and the next random wait (in seconds).
last_checkin_time = 0
next_checkin_wait = 0

def daily_sign_in():
    """Calls checkIn() on the CheckIn contract once."""
    global web3
    checkin_contract = web3.eth.contract(address=CHECKIN_ADDRESS, abi=CHECKIN_ABI)

    print("[daily_sign_in] Attempting daily sign-in...")
    try:
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS, "pending")
        tx = checkin_contract.functions.checkIn().build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 120000,
            'gasPrice': web3.to_wei('10', 'gwei'),
            'nonce': nonce
        })
        tx_hash = send_tx(tx, PRIVATE_KEY, max_tries=3)
        print(f"[daily_sign_in] Tx sent: {web3.to_hex(tx_hash)}")

        receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=180, poll_latency=5)
        if receipt.status == 1:
            print(f"[daily_sign_in] Success! Confirmed in block: {receipt.blockNumber}")
        else:
            print("[daily_sign_in] Transaction failed or reverted.")
    except Exception as e:
        print("[daily_sign_in] Error during sign-in:", e)

#############################################################################
# 12. Main loop
#############################################################################
def main():
    print("\n--- Bot for IniChain By Lazynode ---\n")
    print("\n--- https://lazynode.xyz ---\n")

    # ### DAILY CHECK-IN CODE ###
    # On startup, we do an immediate sign in
    global last_checkin_time, next_checkin_wait
    daily_sign_in()
    last_checkin_time = time.time()
    # random wait between 18 hours (64800s) and 22 hours (79200s)
    next_checkin_wait = random.randint(14400, 18000)

    while True:
        try:
            print("=== New Cycle ===")
            current_time = time.time()

            # ### DAILY CHECK-IN CODE ###
            # check if it's time for next daily sign-in
            if (current_time - last_checkin_time) > next_checkin_wait:
                daily_sign_in()
                last_checkin_time = current_time
                next_checkin_wait = random.randint(64800, 79200)

            # --- main logic continues ---
            ini_balance = get_ini_balance()
            usdt_balance = get_usdt_balance()
            print(f"Balances: {ini_balance:.4f} INI, {usdt_balance:.4f} USDT")
            sleep_cycle = random.randint(220,450)

            if ini_balance < 1.0:
                print("INI < 1.0, skip cycle.")
                time.sleep(300)
                continue

            # random swap of 0.2..0.99
            ini_to_swap = round(random.uniform(0.2, 0.99), 2)
            try:
                swap_ini_to_usdt(ini_to_swap)
            except Exception as e:
                print("[main] swap_ini_to_usdt failed:", e)
                time.sleep(10)
                continue

            ini_after = get_ini_balance()
            usdt_after = get_usdt_balance()
            print(f"After swap: {ini_after:.4f} INI, {usdt_after:.4f} USDT")

            sleep_cycle = random.randint(220,450)
            print(f"Sleeping {sleep_cycle} sec before second swap.")
            time.sleep(sleep_cycle)

            # second swap
            usdt_to_swap = usdt_after - 0.1
            if usdt_to_swap < 0.2:
                print("Not enough USDT to swap, skip second swap.")
            else:
                try:
                    swap_usdt_to_ini(usdt_to_swap)
                except Exception as e:
                    print("[main] swap_usdt_to_ini failed:", e)

            final_ini = get_ini_balance()
            final_usdt = get_usdt_balance()
            print(f"Final Balances: {final_ini:.4f} INI, {final_usdt:.4f} USDT")

            sleep_cycle = random.randint(220,450)
            print(f"Sleeping {sleep_cycle}s before next cycle...\n")
            time.sleep(sleep_cycle)

        except Exception as e:
            print("[main] Unexpected error in main loop:", e)
            init_web3()
            time.sleep(10)


if __name__ == "__main__":
    main()
