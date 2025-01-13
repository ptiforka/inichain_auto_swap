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
                # Possibly bump gas or re-init. We'll let the caller handle it or raise it
                print("[call_with_retries] Caught 'replacement transaction underpriced'.")
                # raise again or re-init?
                if attempt < max_tries:
                    print("[call_with_retries] Retry after sleeping 5s...")
                    time.sleep(5)
                else:
                    raise
            else:
                # other ValueError
                print(f"[call_with_retries] Unhandled ValueError: {msg}")
                if attempt < max_tries:
                    time.sleep(sleep_seconds)
                else:
                    raise

        except Exception as e:
            # catch-all for anything else
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
            return call_with_retries(func=do_send, max_tries=1)  # single attempt here
        except ValueError as e:
            msg = str(e)
            if "replacement transaction underpriced" in msg or "code': -32000" in msg:
                # Bump gas price
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
            init_web3()  # re-init
            if attempt < max_tries:
                time.sleep(5)
            else:
                raise

    # if we get here, all attempts failed
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

    # fee calc
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

    # fee calc
    gas_used = receipt.gasUsed
    final_gas_price = tx_data['gasPrice']
    fee_wei = gas_used * final_gas_price
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"[swap_usdt_to_ini] Tx Fee: {fee_ini} INI\n")

#############################################################################
# 11. Main loop
#############################################################################
def main():
    print("\n--- Bot for IniChain By Lazynode ---\n")
    print("\n--- https://lazynode.xyz ---\n")

    while True:
        try:
            print("=== New Cycle ===")
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

            # decide how long to wait
            sleep_cycle = random.randint(120,350)
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

            # Sleep random 2-5 minutes
            sleep_cycle = random.randint(150,400)
            print(f"Sleeping {sleep_cycle}s before next cycle...\n")
            time.sleep(sleep_cycle)

        except Exception as e:
            print("[main] Unexpected error in main loop:", e)
            # re-init web3, sleep, and keep going
            init_web3()
            time.sleep(10)


if __name__ == "__main__":
    main()
