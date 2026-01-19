import json


class CreateGroupedOrders:
    def __init__(self):
        self.account_index: int | None = None
        self.order_book_index: int | None = None
        self.grouping_type: int | None = None
        self.orders: list | None = None
        self.nonce: int | None = None
        self.sig: str | None = None

    @classmethod
    def from_json(cls, json_str: str) -> "CreateGroupedOrders":
        params = json.loads(json_str)
        self = cls()
        self.account_index = params.get("AccountIndex")
        self.order_book_index = params.get("OrderBookIndex")
        self.grouping_type = params.get("GroupingType")
        self.orders = params.get("Orders")
        self.nonce = params.get("Nonce")
        self.sig = params.get("Sig")
        return self

    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str)
