"""Order specifications for Delta Exchange.

Kept platform-agnostic; the executor translates these into Delta REST calls.
The result type is the shared contract re-exported for existing callers.
"""

from pydantic import BaseModel, Field

from app.contracts import OrderResult, OrderType

__all__ = ["OrderResult", "OrderSpec", "build_market_order"]


class OrderSpec(BaseModel):
    product_id: int
    side: str = Field(pattern="^(buy|sell)$")
    size: float = Field(gt=0)
    order_type: str = OrderType.MARKET.value
    limit_price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    close_on_trigger: bool = False
    time_in_force: str = "gtc"
    stop_loss_price: float | None = None
    client_order_id: str | None = None
    expected_price: float | None = None


def build_market_order(
    product_id: int,
    side: str,
    size: float,
    stop_loss_price: float | None = None,
    expected_price: float | None = None,
    client_order_id: str | None = None,
) -> OrderSpec:
    return OrderSpec(
        product_id=product_id,
        side=side,
        size=size,
        order_type=OrderType.MARKET.value,
        stop_loss_price=stop_loss_price,
        expected_price=expected_price,
        client_order_id=client_order_id,
    )
