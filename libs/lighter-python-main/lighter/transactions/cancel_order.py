import json


class CancelOrder:
    def __init__(self):
        self.account_index: int | None = None
        self.order_book_index: int | None = None
        self.order_nonce: int | None = None
        self.expired_at: int | None = None
        self.nonce: int | None = None
        self.sig: str | None = None

    @classmethod
    def from_json(cls, json_str: str) -> "CancelOrder":
        params = json.loads(json_str)
        self = cls()
        self.account_index = params.get("AccountIndex")
        self.order_book_index = params.get("OrderBookIndex")
        self.order_nonce = params.get("OrderNonce")
        self.expired_at = params.get("ExpiredAt")
        self.nonce = params.get("Nonce")
        self.sig = params.get("Sig")
        return self

    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str)
