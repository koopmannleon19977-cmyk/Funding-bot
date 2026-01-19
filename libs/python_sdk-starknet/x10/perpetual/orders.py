from decimal import Decimal

from strenum import StrEnum

from x10.utils.model import HexValue, SettlementSignatureModel, X10BaseModel


class TimeInForce(StrEnum):
    GTT = "GTT"
    IOC = "IOC"
    FOK = "FOK"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    CONDITIONAL = "CONDITIONAL"
    MARKET = "MARKET"
    TPSL = "TPSL"


class OrderTpslType(StrEnum):
    ORDER = "ORDER"
    POSITION = "POSITION"


class OrderStatus(StrEnum):
    # Technical status
    UNKNOWN = "UNKNOWN"

    NEW = "NEW"
    UNTRIGGERED = "UNTRIGGERED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class OrderStatusReason(StrEnum):
    # Technical status
    UNKNOWN = "UNKNOWN"

    NONE = "NONE"
    UNKNOWN_MARKET = "UNKNOWN_MARKET"
    DISABLED_MARKET = "DISABLED_MARKET"
    NOT_ENOUGH_FUNDS = "NOT_ENOUGH_FUNDS"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    INVALID_FEE = "INVALID_FEE"
    INVALID_QTY = "INVALID_QTY"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_VALUE = "INVALID_VALUE"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    SELF_TRADE_PROTECTION = "SELF_TRADE_PROTECTION"
    POST_ONLY_FAILED = "POST_ONLY_FAILED"
    REDUCE_ONLY_FAILED = "REDUCE_ONLY_FAILED"
    INVALID_EXPIRE_TIME = "INVALID_EXPIRE_TIME"
    POSITION_TPSL_CONFLICT = "POSITION_TPSL_CONFLICT"
    INVALID_LEVERAGE = "INVALID_LEVERAGE"
    PREV_ORDER_NOT_FOUND = "PREV_ORDER_NOT_FOUND"
    PREV_ORDER_TRIGGERED = "PREV_ORDER_TRIGGERED"
    TPSL_OTHER_SIDE_FILLED = "TPSL_OTHER_SIDE_FILLED"
    PREV_ORDER_CONFLICT = "PREV_ORDER_CONFLICT"
    ORDER_REPLACED = "ORDER_REPLACED"
    POST_ONLY_MODE = "POST_ONLY_MODE"
    REDUCE_ONLY_MODE = "REDUCE_ONLY_MODE"
    TRADING_OFF_MODE = "TRADING_OFF_MODE"


class OrderTriggerPriceType(StrEnum):
    # Technical status
    UNKNOWN = "UNKNOWN"

    MARK = "MARK"
    INDEX = "INDEX"
    LAST = "LAST"


class OrderTriggerDirection(StrEnum):
    # Technical status
    UNKNOWN = "UNKNOWN"

    UP = "UP"
    DOWN = "DOWN"


class OrderPriceType(StrEnum):
    # Technical status
    UNKNOWN = "UNKNOWN"

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class SelfTradeProtectionLevel(StrEnum):
    DISABLED = "DISABLED"
    ACCOUNT = "ACCOUNT"
    CLIENT = "CLIENT"


class StarkSettlementModel(X10BaseModel):
    signature: SettlementSignatureModel
    stark_key: HexValue
    collateral_position: Decimal


class StarkDebuggingOrderAmountsModel(X10BaseModel):
    collateral_amount: Decimal
    fee_amount: Decimal
    synthetic_amount: Decimal


class CreateOrderConditionalTriggerModel(X10BaseModel):
    trigger_price: Decimal
    trigger_price_type: OrderTriggerPriceType
    direction: OrderTriggerDirection
    execution_price_type: OrderPriceType


class CreateOrderTpslTriggerModel(X10BaseModel):
    trigger_price: Decimal
    trigger_price_type: OrderTriggerPriceType
    price: Decimal
    price_type: OrderPriceType
    settlement: StarkSettlementModel
    debugging_amounts: StarkDebuggingOrderAmountsModel | None = None


class NewOrderModel(X10BaseModel):
    id: str
    market: str
    type: OrderType
    side: OrderSide
    qty: Decimal
    price: Decimal
    reduce_only: bool = False
    post_only: bool = False
    time_in_force: TimeInForce
    expiry_epoch_millis: int
    fee: Decimal
    nonce: Decimal
    self_trade_protection_level: SelfTradeProtectionLevel
    cancel_id: str | None = None
    settlement: StarkSettlementModel | None = None
    trigger: CreateOrderConditionalTriggerModel | None = None
    tp_sl_type: OrderTpslType | None = None
    take_profit: CreateOrderTpslTriggerModel | None = None
    stop_loss: CreateOrderTpslTriggerModel | None = None
    debugging_amounts: StarkDebuggingOrderAmountsModel | None = None
    builderFee: Decimal | None = None
    builderId: int | None = None


class PlacedOrderModel(X10BaseModel):
    id: int
    external_id: str


class OpenOrderTpslTriggerModel(X10BaseModel):
    trigger_price: Decimal
    trigger_price_type: OrderTriggerPriceType
    price: Decimal
    price_type: OrderPriceType
    status: OrderStatus | None = None


class OpenOrderModel(X10BaseModel):
    id: int
    account_id: int
    external_id: str
    market: str
    type: OrderType
    side: OrderSide
    status: OrderStatus
    status_reason: OrderStatusReason | None = None
    price: Decimal
    average_price: Decimal | None = None
    qty: Decimal
    filled_qty: Decimal | None = None
    reduce_only: bool
    post_only: bool
    payed_fee: Decimal | None = None
    created_time: int
    updated_time: int
    expiry_time: int | None = None
    time_in_force: TimeInForce
    tp_sl_type: OrderTpslType | None = None
    take_profit: OpenOrderTpslTriggerModel | None = None
    stop_loss: OpenOrderTpslTriggerModel | None = None
