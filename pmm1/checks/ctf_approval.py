"""CTF approval check — verify ERC-1155 setApprovalForAll for selling on Polymarket.

Checks that the bot's wallet has approved all three exchange operator contracts
on the CTF conditional tokens contract. Logs a WARNING if not approved (does not
auto-approve — that costs gas and should be done manually).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# Polygon Mainnet contract addresses
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

EXCHANGE_OPERATORS: list[tuple[str, str]] = [
    ("CTF Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

# Minimal ABI for isApprovedForAll
ERC1155_IS_APPROVED_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]

RPC_URL = "https://polygon-bor-rpc.publicnode.com"


async def check_ctf_approvals(wallet_address: str) -> bool:
    """Check CTF token approvals for all exchange operators.

    Args:
        wallet_address: The bot's wallet address on Polygon.

    Returns:
        True if all approvals are in place, False otherwise.
    """
    try:
        from web3 import Web3
    except ImportError:
        logger.warning(
            "ctf_approval_check_skipped",
            reason="web3 not installed — pip install web3",
        )
        return True  # Don't block startup

    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        if not w3.is_connected():
            logger.warning("ctf_approval_check_skipped", reason="cannot connect to Polygon RPC")
            return True

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT),
            abi=ERC1155_IS_APPROVED_ABI,
        )

        account = Web3.to_checksum_address(wallet_address)
        all_approved = True

        for name, operator_addr in EXCHANGE_OPERATORS:
            operator = Web3.to_checksum_address(operator_addr)
            try:
                approved = ctf.functions.isApprovedForAll(account, operator).call()
                if approved:
                    logger.info(
                        "ctf_approval_ok",
                        operator=name,
                        operator_addr=operator_addr[:10] + "...",
                    )
                else:
                    logger.warning(
                        "ctf_approval_missing",
                        operator=name,
                        operator_addr=operator_addr,
                        wallet=wallet_address,
                        action="Run setApprovalForAll manually — selling will fail without this",
                    )
                    all_approved = False
            except Exception as e:
                logger.warning(
                    "ctf_approval_check_error",
                    operator=name,
                    error=str(e),
                )
                all_approved = False

        return all_approved

    except Exception as e:
        logger.warning("ctf_approval_check_failed", error=str(e))
        return True  # Don't block startup on RPC errors
