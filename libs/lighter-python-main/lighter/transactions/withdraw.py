import json


class Withdraw:
    def __init__(self):
        self.from_account_index: int | None = None
        self.collateral_amount: int | None = None
        self.expired_at: int | None = None
        self.nonce: int | None = None
        self.sig: str | None = None

    @classmethod
    def from_json(cls, json_str: str) -> "Withdraw":
        params = json.loads(json_str)
        instance = cls()
        instance.from_account_index = params.get("FromAccountIndex")
        instance.collateral_amount = params.get("CollateralAmount")
        instance.expired_at = params.get("ExpiredAt")
        instance.nonce = params.get("Nonce")
        instance.sig = params.get("Sig")
        return instance

    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str)
