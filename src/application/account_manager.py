# src/application/services/account_manager.py
# Note: This file has been moved to application/services/ for better organization
import logging
import os
from typing import List, Dict, Optional, Tuple
from itertools import cycle
import config

logger = logging.getLogger(__name__)

class AccountManager:
    """
    Manages multiple accounts for Airdrop farming rotation.
    Round-Robin selection ensures fair volume distribution.
    """
    
    def __init__(self):
        self.x10_accounts: List[Dict] = []
        self.lighter_accounts: List[Dict] = []
        self._x10_cycle = None
        self._lighter_cycle = None
        self.initialized = False

    def load_accounts(self):
        """Load accounts from environment variables or config"""
        # Load X10 Accounts
        # Format: X10_PK_1, X10_PUB_1, X10_API_1, X10_VAULT_1
        # Main account is always index 0 (from standard config)
        self.x10_accounts.append({
            "private_key": config.X10_PRIVATE_KEY,
            "public_key": config.X10_PUBLIC_KEY,
            "api_key": config.X10_API_KEY,
            "vault_id": config.X10_VAULT_ID,
            "label": "Main"
        })

        # Load additional X10 accounts
        i = 1
        while True:
            pk = os.getenv(f"X10_PRIVATE_KEY_{i}")
            if not pk:
                break
            self.x10_accounts.append({
                "private_key": pk,
                "public_key": os.getenv(f"X10_PUBLIC_KEY_{i}"),
                "api_key": os.getenv(f"X10_API_KEY_{i}"),
                "vault_id": os.getenv(f"X10_VAULT_ID_{i}"),
                "label": f"Alt_{i}"
            })
            i += 1

        # Load Lighter Accounts
        # Main account
        self.lighter_accounts.append({
            "private_key": config.LIGHTER_PRIVATE_KEY,
            "api_private_key": config.LIGHTER_API_PRIVATE_KEY,
            "account_index": config.LIGHTER_ACCOUNT_INDEX,
            "api_key_index": config.LIGHTER_API_KEY_INDEX,
            "label": "Main"
        })

        # Load additional Lighter accounts
        i = 1
        while True:
            pk = os.getenv(f"LIGHTER_PRIVATE_KEY_{i}")
            if not pk:
                break
            self.lighter_accounts.append({
                "private_key": pk,
                "api_private_key": os.getenv(f"LIGHTER_API_PRIVATE_KEY_{i}"),
                "account_index": int(os.getenv(f"LIGHTER_ACCOUNT_INDEX_{i}", 0)),
                "api_key_index": int(os.getenv(f"LIGHTER_API_KEY_INDEX_{i}", 0)),
                "label": f"Alt_{i}"
            })
            i += 1

        self._x10_cycle = cycle(self.x10_accounts)
        self._lighter_cycle = cycle(self.lighter_accounts)
        self.initialized = True
        
        logger.info(
            f"Account Manager initialized: "
            f"{len(self.x10_accounts)} X10, {len(self.lighter_accounts)} Lighter accounts"
        )

    def get_next_x10_account(self) -> Dict:
        if not self.initialized:
            self.load_accounts()
        return next(self._x10_cycle)

    def get_next_lighter_account(self) -> Dict:
        if not self.initialized:
            self.load_accounts()
        return next(self._lighter_cycle)

    def get_all_x10_accounts(self) -> List[Dict]:
        if not self.initialized:
            self.load_accounts()
        return self.x10_accounts

    def get_all_lighter_accounts(self) -> List[Dict]:
        if not self.initialized:
            self.load_accounts()
        return self.lighter_accounts

_manager = None

def get_account_manager() -> AccountManager:
    global _manager
    if _manager is None:
        _manager = AccountManager()
        _manager.load_accounts()
    return _manager