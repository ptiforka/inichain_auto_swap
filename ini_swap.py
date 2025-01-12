import time
import os
import requests
from web3 import Web3
from dotenv import load_dotenv
from typing import Callable, Any

load_dotenv()

#############################################################################
# 1. Global config
#############################################################################
INI_CHAIN_RPC = "http://rpc-testnet.inichain.com"  # Genesis testnet
WALLET_ADDRESS_RAW = "EVM_WALLET"
PRIVATE_KEY = "PRIVATE_KEY"

if not PRIVATE_KEY:
    raise Exception("No private key found. Set PRIVATE_KEY in your environment.")


def make_web3_provider() -> Web3:
    """Create a fresh Web3 instance with a short timeout."""
    return Web3(Web3.HTTPProvider(INI_CHAIN_RPC, request_kwargs={"timeout": 20}))


# We instantiate web3 here, but we'll re‑instantiate it if an error occurs
web3 = make_web3_provider()

# Check initial connection
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
# 3. Retriable web3 call helper
#############################################################################
def call_with_retries(
    func: Callable[..., Any],
    max_tries: int = 3,
    wait_seconds: float = 5.0,
    reinit_web3: bool = True,
    **kwargs
) -> Any:
    """
    Calls `func(**kwargs)`, catching requests.exceptions.ConnectionError.
    Retries up to `max_tries`. If reinit_web3 is True, re-creates
    web3 provider if we see a ConnectionError.
    """
    global web3
    for attempt in range(max_tries):
        try:
            return func(**kwargs)
        except requests.exceptions.ConnectionError as e:
            print(f"[call_with_retries] ConnectionError on attempt {attempt+1}: {e}")
            if attempt < max_tries - 1:
                # Possibly the old web3 is borked; re-init if desired
                if reinit_web3:
                    print("[call_with_retries] Re-initializing web3 provider...")
                    web3 = make_web3_provider()
                print(f"[call_with_retries] Sleeping {wait_seconds}s before retry...\n")
                time.sleep(wait_seconds)
            else:
                # Last attempt -> re-raise or return None
                print(f"[call_with_retries] Max attempts reached. Giving up.")
                raise e


#############################################################################
# 4. Contract objects (we'll re-instantiate them after re-creating web3)
#############################################################################
def get_router_contract():
    return web3.eth.contract(address=ROUTER_ADDRESS, abi=ROUTER_ABI)

def get_usdt_contract():
    return web3.eth.contract(address=USDT_ADDRESS, abi=USDT_ABI)


#############################################################################
# 5. Balance Checkers (with retries)
#############################################################################
def get_ini_balance() -> float:
    """Get native INI balance for WALLET_ADDRESS, with retries."""
    def do_balance():
        bal = web3.eth.get_balance(WALLET_ADDRESS)
        return float(web3.from_wei(bal, 'ether'))
    return call_with_retries(func=do_balance)


def get_usdt_balance() -> float:
    """Get USDT balance for WALLET_ADDRESS, with retries."""
    usdt_contract = get_usdt_contract()
    def do_balance():
        bal_wei = usdt_contract.functions.balanceOf(WALLET_ADDRESS).call()
        return float(web3.from_wei(bal_wei, 'ether'))
    return call_with_retries(func=do_balance)


#############################################################################
# 6. Approve USDT (with retries)
#############################################################################
def approve_usdt(spend_amount_wei: int):
    """
    Approve the router to spend 'spend_amount_wei' of your USDT, with retries.
    """
    usdt_c = get_usdt_contract()

    def do_approve():
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        tx = usdt_c.functions.approve(ROUTER_ADDRESS, spend_amount_wei).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 100_000,
            'gasPrice': web3.to_wei('10', 'gwei'),
            'nonce': nonce
        })
        signed = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        return receipt

    receipt = call_with_retries(func=do_approve)
    block = receipt.blockNumber if receipt else None
    print(f"approve_usdt -> Confirmed in block: {block}")


#############################################################################
# 7. Swap Functions (with retries)
#############################################################################
def swap_ini_to_usdt(ini_amount_in_ether: float, min_out_wei=0):
    """
    Attempt to swap 'ini_amount_in_ether' of native INI for USDT, with retries.
    """
    print("Swap ini to usdt in progress")
    router_c = get_router_contract()
    path = [WINI_ADDRESS, USDT_ADDRESS]
    amount_in_wei = web3.to_wei(ini_amount_in_ether, 'ether')

    def do_swap():
        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        tx = router_c.functions.swapExactETHForTokens(
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
        signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)

        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        return (tx, receipt)

    tx, receipt = call_with_retries(func=do_swap)
    block = receipt.blockNumber if receipt else None
    print(f"swap_ini_to_usdt({ini_amount_in_ether} INI) confirmed in block: {block}")

    # Calculate fees
    gas_used = receipt.gasUsed
    gas_price_wei = tx['gasPrice']
    fee_wei = gas_used * gas_price_wei
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"INI->USDT Tx Fee: {fee_ini} INI\n")


def swap_usdt_to_ini(usdt_amount_in_ether: float, min_out_wei=0):
    """
    Attempt to swap 'usdt_amount_in_ether' of USDT -> native INI, with retries.
    """
    print("Swap usdt to INI in progress")
    router_c = get_router_contract()
    path = [USDT_ADDRESS, WINI_ADDRESS]
    amount_in_wei = web3.to_wei(usdt_amount_in_ether, 'ether')

    def do_swap():
        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
        tx = router_c.functions.swapExactTokensForETH(
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
        signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        return (tx, receipt)

    tx, receipt = call_with_retries(func=do_swap)
    block = receipt.blockNumber if receipt else None
    print(f"swap_usdt_to_ini({usdt_amount_in_ether} USDT) confirmed in block: {block}")

    # Calculate fees
    gas_used = receipt.gasUsed
    gas_price_wei = tx['gasPrice']
    fee_wei = gas_used * gas_price_wei
    fee_ini = web3.from_wei(fee_wei, 'ether')
    print(f"USDT->INI Tx Fee: {fee_ini} INI\n")


#############################################################################
# 8. Main
#############################################################################
def main():
    print("\n--------------- Bot for IniChain by LazyNode ---------------")
    print("\n--------------- https://lazynode.xyz ---------------")
    print("\n--------------- Bot for IniChain by LazyNode ---------------")
    while True:
        print("\n--------------- Starting a new cycle ---------------")

        # 1) Check balances
        ini_balance = None
        usdt_balance = None
        try:
            ini_balance = get_ini_balance()
            usdt_balance = get_usdt_balance()
        except requests.exceptions.ConnectionError as e:
            print("[main] Could not fetch balances, skipping cycle:", e)
            time.sleep(60)
            continue

        print(f"Balance BEFORE any swaps: {ini_balance:.4f} INI, {usdt_balance:.4f} USDT")

        # 2) If INI < 1.0, skip
        if ini_balance < 1.0:
            print("INI < 1.0, skipping cycle.\n")
            time.sleep(600)
            continue

        # 3) Swap 0.2 INI -> USDT
        try:
            swap_ini_to_usdt(0.2)
        except requests.exceptions.ConnectionError as e:
            print("[main] Connection error on INI->USDT swap, skipping second part:", e)
            time.sleep(600)
            continue

        # Re-check balances
        try:
            ini_after = get_ini_balance()
            usdt_after = get_usdt_balance()
        except requests.exceptions.ConnectionError as e:
            print("[main] Could not fetch post-swap balances, skipping second swap:", e)
            time.sleep(600)
            continue

        print(f"Balance AFTER INI->USDT: {ini_after:.4f} INI, {usdt_after:.4f} USDT")

        # Sleep 10 min
        time.sleep(600)

        # 4) USDT_to_swap = usdt_after - 0.1, skip if < 0.2
        usdt_to_swap = usdt_after - 0.1
        if usdt_to_swap < 0.2:
            print(f"usdt_to_swap = {usdt_to_swap:.4f}, less than 0.2, skipping second swap.\n")
            continue

        # 5) Swap USDT->INI
        try:
            swap_usdt_to_ini(usdt_to_swap)
        except requests.exceptions.ConnectionError as e:
            print("[main] Connection error on USDT->INI swap. Skipping.\n", e)

        # Show final balances
        try:
            ini_final = get_ini_balance()
            usdt_final = get_usdt_balance()
            print(f"Balance AFTER USDT->INI: {ini_final:.4f} INI, {usdt_final:.4f} USDT")
        except requests.exceptions.ConnectionError as e:
            print("[main] Could not fetch final balances.\n", e)

        # Sleep 10 min
        time.sleep(600)


if __name__ == "__main__":
    main()
