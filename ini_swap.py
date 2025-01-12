import time
import os
import requests
from web3 import Web3
from web3.exceptions import TimeExhausted
from typing import Callable, Any
import random
#############################################################################
# 1. Global config
#############################################################################
INI_CHAIN_RPC = "http://rpc-testnet.inichain.com"  # Genesis testnet
WALLET_ADDRESS_RAW = "REPLACE_WITH_YOUR_EVM_WALLET"
PRIVATE_KEY = "REPLACE_WITH_YOUR_PRIVATE_KEY"

if not PRIVATE_KEY:
    raise Exception("No private key found. Set PRIVATE_KEY in your environment.")

# We'll default to a 20-second HTTP request timeout for connection-based errors
# and use an extended wait_for_transaction_receipt() timeout for mining delays.
def make_web3_provider() -> Web3:
    return Web3(Web3.HTTPProvider(INI_CHAIN_RPC, request_kwargs={"timeout": 20}))

web3 = make_web3_provider()
if not web3.is_connected():
    raise Exception("Cannot connect to IniChain testnet RPC.")

#############################################################################
# 2. Addresses & ABIs
#############################################################################
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
            {"internalType": "uint256","name":"amountOutMin","type":"uint256"},
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
        "inputs": [{"internalType": "address","name": "account","type":"address"}],
        "outputs": [{"internalType":"uint256","name":"","type":"uint256"}]
    }
]

#############################################################################
# 3. Wait for transaction receipt (with bigger timeout)
#############################################################################
def wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5, max_tries=2):
    for attempt in range(1, max_tries + 1):
        try:
            receipt = web3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=timeout, poll_latency=poll_latency
            )
            return receipt
        except TimeExhausted:
            print(f"[wait_for_tx_receipt_with_retry] Attempt {attempt} timed out after {timeout}s.")
            if attempt < max_tries:
                print("[wait_for_tx_receipt_with_retry] Retrying wait...")
            else:
                print("[wait_for_tx_receipt_with_retry] Max attempts reached, giving up.")
                raise

#############################################################################
# 4. Contracts
#############################################################################
def get_router_contract():
    return web3.eth.contract(address=ROUTER_ADDRESS, abi=ROUTER_ABI)

def get_usdt_contract():
    return web3.eth.contract(address=USDT_ADDRESS, abi=USDT_ABI)

#############################################################################
# 5. Balance helpers
#############################################################################
def get_ini_balance() -> float:
    bal_wei = web3.eth.get_balance(WALLET_ADDRESS)
    return float(web3.from_wei(bal_wei, 'ether'))

def get_usdt_balance() -> float:
    usdt_c = get_usdt_contract()
    bal_wei = usdt_c.functions.balanceOf(WALLET_ADDRESS).call()
    return float(web3.from_wei(bal_wei, 'ether'))

#############################################################################
# 6. The core "send transaction with retries" logic
#############################################################################
def send_tx_with_retries(tx, private_key, max_tries=3):
    """
    Signs and sends the given transaction, waiting 5s if it fails due to:
      - 'replacement transaction underpriced'
      - connection issues
    On each retry, we optionally bump the gas price by 20%.

    Returns: tx_hash on success.
    Raises an exception if all tries fail.
    """
    current_tx = tx.copy()  # don't mutate the original

    for attempt in range(1, max_tries + 1):
        try:
            signed_tx = web3.eth.account.sign_transaction(current_tx, private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            return tx_hash  # success
        except requests.exceptions.ConnectionError as e:
            print(f"[send_tx_with_retries] Connection error on attempt {attempt}: {e}")
            if attempt < max_tries:
                time.sleep(5)
                continue
            else:
                raise
        except ValueError as e:
            # This usually means a JSON-RPC error. We can check for 'replacement transaction underpriced'
            msg = str(e)
            if "replacement transaction underpriced" in msg or "code': -32000" in msg:
                # Bump gas price by +20%
                old_gas_price = current_tx['gasPrice']
                new_gas_price = int(old_gas_price * 1.2)
                current_tx['gasPrice'] = new_gas_price
                print(f"[send_tx_with_retries] 'replacement tx underpriced', bumping gas price from {old_gas_price} -> {new_gas_price}")
                if attempt < max_tries:
                    time.sleep(5)
                    continue
                else:
                    raise
            else:
                # other errors, e.g. insufficient funds
                print(f"[send_tx_with_retries] Unhandled ValueError: {msg}")
                if attempt < max_tries:
                    time.sleep(5)
                    continue
                else:
                    raise

    # if we exhausted all tries
    raise Exception("[send_tx_with_retries] All attempts to send tx have failed.")

#############################################################################
# 7. Approve USDT example
#############################################################################
def approve_usdt(spend_amount_wei: int):
    usdt_c = get_usdt_contract()
    nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
    tx_data = usdt_c.functions.approve(ROUTER_ADDRESS, spend_amount_wei).build_transaction({
        'from': WALLET_ADDRESS,
        'gas': 100_000,
        'gasPrice': web3.to_wei('10', 'gwei'),
        'nonce': nonce
    })

    print("[approve_usdt] Sending transaction with retries...")
    tx_hash = send_tx_with_retries(tx_data, PRIVATE_KEY, max_tries=3)
    print("[approve_usdt] TX hash:", tx_hash.hex())

    print("[approve_usdt] waiting for receipt with extended timeout...")
    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    print(f"approve_usdt -> Confirmed in block: {receipt.blockNumber}\n")

#############################################################################
# 8. Swap: INI -> USDT
#############################################################################
def swap_ini_to_usdt(ini_amount_in_ether: float, min_out_wei=0):
    print("Swap ini to usdt in progress...")
    router_c = get_router_contract()
    path = [WINI_ADDRESS, USDT_ADDRESS]
    amount_in_wei = web3.to_wei(ini_amount_in_ether, 'ether')

    deadline = int(time.time()) + 300
    nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
    tx_data = router_c.functions.swapExactETHForTokens(
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

    # 1) Sign + send with retry logic
    tx_hash = send_tx_with_retries(tx_data, PRIVATE_KEY)
    print(f"swap_ini_to_usdt({ini_amount_in_ether} INI) -> TX hash: {tx_hash.hex()}")

    # 2) Wait for receipt with bigger timeout
    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    block = receipt.blockNumber
    print(f"swap_ini_to_usdt -> Confirmed in block: {block}")

    # 3) Calculate fee
    gas_used = receipt.gasUsed
    gas_price_wei = tx_data['gasPrice']
    fee_wei = gas_used * gas_price_wei
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"INI->USDT Tx Fee: {fee_ini} INI\n")

#############################################################################
# 9. Swap: USDT -> INI
#############################################################################
def swap_usdt_to_ini(usdt_amount_in_ether: float, min_out_wei=0):
    print("Swap usdt to INI in progress...")
    router_c = get_router_contract()
    path = [USDT_ADDRESS, WINI_ADDRESS]
    amount_in_wei = web3.to_wei(usdt_amount_in_ether, 'ether')

    deadline = int(time.time()) + 300
    nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
    tx_data = router_c.functions.swapExactTokensForETH(
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

    # 1) Sign + send with retry logic
    tx_hash = send_tx_with_retries(tx_data, PRIVATE_KEY)
    print(f"swap_usdt_to_ini({usdt_amount_in_ether} USDT) -> TX hash: {tx_hash.hex()}")

    # 2) Wait for receipt
    receipt = wait_for_tx_receipt_with_retry(tx_hash, timeout=300, poll_latency=5)
    block = receipt.blockNumber
    print(f"swap_usdt_to_ini -> Confirmed in block: {block}")

    # 3) Calculate fee
    gas_used = receipt.gasUsed
    gas_price_wei = tx_data['gasPrice']
    fee_wei = gas_used * gas_price_wei
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"USDT->INI Tx Fee: {fee_ini} INI\n")

#############################################################################
# 10. Main Loop
#############################################################################
def main():
    print("\n--------------- Bot for IniChain by LazyNode ---------------")
    print("\n--------------- https://lazynode.xyz ---------------")
    print("\n--------------- Bot for IniChain by LazyNode ---------------")

    while True:
        print("\n--------------- Starting a new cycle ---------------")
        ttw = random.randint(2,5)

        # 1) Check balances
        ini_balance = get_ini_balance()
        usdt_balance = get_usdt_balance()
        print(f"Balance BEFORE any swaps: {ini_balance:.4f} INI, {usdt_balance:.4f} USDT")

        # 2) If INI < 1.0, skip
        if ini_balance < 1.0:
            print("INI < 1.0, skipping cycle.\n")
            time.sleep(600)
            continue

        # 3) Swap 0.2 INI -> USDT
        try:
            amount_to_swap = round(random.uniform(0.2, 0.99), 2)
            swap_ini_to_usdt(amount_to_swap)
        except Exception as e:
            # Could be "replacement tx underpriced" after 3 tries, or something else
            print(f"[main] swap_ini_to_usdt(0.2) failed: {e}")
            time.sleep(5)
            continue

        # Re-check balances
        ini_after = get_ini_balance()
        usdt_after = get_usdt_balance()
        print(f"Balance AFTER INI->USDT: {ini_after:.4f} INI, {usdt_after:.4f} USDT")

        print(f"Sleep for {ttw} seconds")
        time.sleep(ttw)
        ttw = random.randint(200,500)

        # 4) USDT_to_swap = usdt_after - 0.1, skip if < 0.2
        usdt_to_swap = usdt_after - 0.1
        if usdt_to_swap < 0.2:
            print(f"usdt_to_swap = {usdt_to_swap:.4f}, less than 0.2, skipping second swap.\n")
            continue

        # 5) Swap USDT->INI
        try:
            swap_usdt_to_ini(usdt_to_swap)
        except Exception as e:
            print(f"[main] swap_usdt_to_ini({usdt_to_swap}) failed: {e}")

        # Show final balances
        ini_final = get_ini_balance()
        usdt_final = get_usdt_balance()
        print(f"Balance AFTER USDT->INI: {ini_final:.4f} INI, {usdt_final:.4f} USDT")

        # Sleep 10 min
        print(f"Sleep for {ttw} seconds")
        time.sleep(ttw)


if __name__ == "__main__":
    main()
