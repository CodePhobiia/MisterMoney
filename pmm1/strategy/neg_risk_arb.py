"""Negative-risk conversion arbitrage from §4B.

For negative-risk events, No on outcome k converts into Yes on all other outcomes.

Two canonical checks:
    ask(No_k) + conversion_cost + exit_cost < Σ_{j≠k} bid(Yes_j)
    Σ_{j≠k} ask(Yes_j) + entry_cost < bid(No_k) − conversion_cost

If either holds after safety buffer → execute.

Filters applied to avoid phantom liquidity:
- Minimum price filter: skip outcomes where prices are < 0.05 (phantom)
- Minimum book depth: require at least $10 at best level
- Staleness check: skip books older than 10 seconds
- Edge reported as percentage of capital deployed
"""

from __future__ import annotations

import time

import structlog
from pydantic import BaseModel, Field

from pmm1.state.books import OrderBook

logger = structlog.get_logger(__name__)

# Minimum price to consider — below this, liquidity is phantom
MIN_PRICE_FILTER = 0.05

# Minimum book depth in dollars at best level
MIN_BOOK_DEPTH_USD = 10.0

# Maximum book age in seconds before considering stale
MAX_BOOK_AGE_S = 10.0


class NegRiskSignal(BaseModel):
    """Result of a negative-risk conversion check."""

    event_id: str
    outcome_index: int  # Which outcome k we're checking
    signal_type: str  # "buy_no_sell_yes" or "buy_yes_sell_no"
    edge: float = 0.0  # Net profit per unit after all costs
    edge_pct: float = 0.0  # Edge as percentage of capital deployed
    max_size: float = 0.0
    is_actionable: bool = False
    details: dict = Field(default_factory=dict)


class NegRiskOrderIntent(BaseModel):
    """An order intent for neg-risk conversion arb."""

    token_id: str
    side: str  # BUY or SELL
    price: str
    size: str
    strategy: str = "neg_risk_arb"
    order_type: str = "FOK"
    neg_risk: bool = True
    condition_id: str = ""
    event_id: str = ""
    intent_tag: str = ""


class NegRiskOutcome(BaseModel):
    """Metadata for one outcome in a neg-risk event."""

    condition_id: str
    token_id_yes: str
    token_id_no: str
    index: int  # Position in the event's outcome list


class NegRiskArbDetector:
    """Detects conversion arb in negative-risk multi-outcome events.

    In neg-risk events:
    - No on outcome k = Yes on all other outcomes
    - Conversion is an on-chain operation with gas cost

    Two canonical arbs:
    1. Buy No_k, convert to Yes_others, sell each Yes_j
    2. Buy all Yes_j (j≠k), convert to No_k, sell No_k
    """

    def __init__(
        self,
        conversion_cost: float = 0.001,  # On-chain conversion gas estimate
        fee_rate: float = 0.002,  # 20bps per trade
        epsilon: float = 0.01,  # Min edge to execute (1 cent, was 0.005)
        safety_buffer: float = 0.002,  # Extra safety margin
        min_size: float = 10.0,
        max_size: float = 300.0,
        min_price: float = MIN_PRICE_FILTER,
        min_book_depth_usd: float = MIN_BOOK_DEPTH_USD,
        max_book_age_s: float = MAX_BOOK_AGE_S,
    ) -> None:
        self.conversion_cost = conversion_cost
        self.fee_rate = fee_rate
        self.epsilon = epsilon
        self.safety_buffer = safety_buffer
        self.min_size = min_size
        self.max_size = max_size
        self.min_price = min_price
        self.min_book_depth_usd = min_book_depth_usd
        self.max_book_age_s = max_book_age_s

    def _estimate_fee(self, price: float) -> float:
        """Estimate fee per share for a trade."""
        return price * self.fee_rate

    def _check_book_fresh(self, book: OrderBook, label: str = "") -> bool:
        """Check if a book is fresh enough (not stale)."""
        if hasattr(book, 'age_seconds') and book.age_seconds > self.max_book_age_s:
            logger.debug(
                "neg_risk_stale_book",
                label=label,
                age_s=f"{book.age_seconds:.1f}",
                max_age_s=self.max_book_age_s,
            )
            return False
        return True

    def _check_depth_usd(self, price: float, size: float, label: str = "") -> bool:
        """Check if there's sufficient depth at the best level."""
        depth_usd = price * size
        if depth_usd < self.min_book_depth_usd:
            logger.debug(
                "neg_risk_thin_book",
                label=label,
                depth_usd=f"{depth_usd:.2f}",
                min_required=self.min_book_depth_usd,
            )
            return False
        return True

    def check_buy_no_sell_yes(
        self,
        outcome_k: NegRiskOutcome,
        other_outcomes: list[NegRiskOutcome],
        books: dict[str, OrderBook],
        event_id: str = "",
    ) -> NegRiskSignal | None:
        """Check canonical arb #1: Buy No_k → convert → sell all Yes_j (j≠k).

        Condition: ask(No_k) + conversion_cost + exit_cost < Σ_{j≠k} bid(Yes_j)
        """
        # Get ask for No_k
        no_book = books.get(outcome_k.token_id_no)
        if no_book is None:
            return None

        # Staleness check
        if not self._check_book_fresh(no_book, f"no_k_{outcome_k.index}"):
            return None

        no_ask = no_book.get_best_ask()
        if no_ask is None:
            return None

        ask_no_k = no_ask.price_float

        # Minimum price filter — skip phantom liquidity
        if ask_no_k < self.min_price:
            logger.debug(
                "neg_risk_skip_low_price",
                outcome_k=outcome_k.index,
                ask_no_k=ask_no_k,
                min_price=self.min_price,
            )
            return None

        # Depth check
        if not self._check_depth_usd(ask_no_k, no_ask.size_float, f"no_ask_{outcome_k.index}"):
            return None

        entry_fee = self._estimate_fee(ask_no_k)

        # Sum bids for all Yes_j (j≠k)
        total_bid_yes = 0.0
        total_exit_fees = 0.0
        min_bid_size = float("inf")

        for outcome in other_outcomes:
            yes_book = books.get(outcome.token_id_yes)
            if yes_book is None:
                return None

            # Staleness check
            if not self._check_book_fresh(yes_book, f"yes_{outcome.index}"):
                return None

            yes_bid = yes_book.get_best_bid()
            if yes_bid is None:
                return None

            bid_price = yes_bid.price_float

            # Minimum price filter on each exit leg
            if bid_price < self.min_price:
                return None

            # Depth check on each exit leg
            if not self._check_depth_usd(bid_price, yes_bid.size_float, f"yes_bid_{outcome.index}"):
                return None

            total_bid_yes += bid_price
            total_exit_fees += self._estimate_fee(bid_price)
            min_bid_size = min(min_bid_size, yes_bid.size_float)

        max_size = min(no_ask.size_float, min_bid_size, self.max_size)

        total_entry_cost = ask_no_k + entry_fee + self.conversion_cost
        total_exit_proceeds = total_bid_yes - total_exit_fees
        edge = total_exit_proceeds - total_entry_cost - self.safety_buffer

        # Compute edge as percentage of capital deployed
        edge_pct = (edge / total_entry_cost * 100) if total_entry_cost > 0 else 0.0

        signal = NegRiskSignal(
            event_id=event_id,
            outcome_index=outcome_k.index,
            signal_type="buy_no_sell_yes",
            edge=edge,
            edge_pct=edge_pct,
            max_size=max_size,
            is_actionable=edge > self.epsilon and max_size >= self.min_size,
            details={
                "ask_no_k": ask_no_k,
                "entry_fee": entry_fee,
                "conversion_cost": self.conversion_cost,
                "total_bid_yes": total_bid_yes,
                "exit_fees": total_exit_fees,
                "total_entry_cost": total_entry_cost,
                "total_exit_proceeds": total_exit_proceeds,
                "edge_pct": round(edge_pct, 4),
            },
        )

        if signal.is_actionable:
            logger.info(
                "neg_risk_buy_no_detected",
                event_id=event_id[:16],
                outcome_k=outcome_k.index,
                edge=f"{edge:.4f}",
                edge_pct=f"{edge_pct:.2f}%",
                max_size=max_size,
                ask_no_k=f"{ask_no_k:.4f}",
                total_bid_yes=f"{total_bid_yes:.4f}",
            )

        return signal

    def check_buy_yes_sell_no(
        self,
        outcome_k: NegRiskOutcome,
        other_outcomes: list[NegRiskOutcome],
        books: dict[str, OrderBook],
        event_id: str = "",
    ) -> NegRiskSignal | None:
        """Check canonical arb #2: Buy all Yes_j (j≠k) → convert → sell No_k.

        Condition: Σ_{j≠k} ask(Yes_j) + entry_cost < bid(No_k) − conversion_cost
        """
        # Get bid for No_k
        no_book = books.get(outcome_k.token_id_no)
        if no_book is None:
            return None

        # Staleness check
        if not self._check_book_fresh(no_book, f"no_k_{outcome_k.index}"):
            return None

        no_bid = no_book.get_best_bid()
        if no_bid is None:
            return None

        bid_no_k = no_bid.price_float

        # Minimum price filter — skip phantom liquidity
        if bid_no_k < self.min_price:
            logger.debug(
                "neg_risk_skip_low_price",
                outcome_k=outcome_k.index,
                bid_no_k=bid_no_k,
                min_price=self.min_price,
            )
            return None

        # Depth check
        if not self._check_depth_usd(bid_no_k, no_bid.size_float, f"no_bid_{outcome_k.index}"):
            return None

        exit_fee = self._estimate_fee(bid_no_k)

        # Sum asks for all Yes_j (j≠k)
        total_ask_yes = 0.0
        total_entry_fees = 0.0
        min_ask_size = float("inf")

        for outcome in other_outcomes:
            yes_book = books.get(outcome.token_id_yes)
            if yes_book is None:
                return None

            # Staleness check
            if not self._check_book_fresh(yes_book, f"yes_{outcome.index}"):
                return None

            yes_ask = yes_book.get_best_ask()
            if yes_ask is None:
                return None

            ask_price = yes_ask.price_float

            # Minimum price filter on each entry leg
            if ask_price < self.min_price:
                return None

            # Depth check on each entry leg
            if not self._check_depth_usd(ask_price, yes_ask.size_float, f"yes_ask_{outcome.index}"):
                return None

            total_ask_yes += ask_price
            total_entry_fees += self._estimate_fee(ask_price)
            min_ask_size = min(min_ask_size, yes_ask.size_float)

        max_size = min(no_bid.size_float, min_ask_size, self.max_size)

        total_entry_cost = total_ask_yes + total_entry_fees
        total_exit_proceeds = bid_no_k - exit_fee - self.conversion_cost
        edge = total_exit_proceeds - total_entry_cost - self.safety_buffer

        # Compute edge as percentage of capital deployed
        edge_pct = (edge / total_entry_cost * 100) if total_entry_cost > 0 else 0.0

        signal = NegRiskSignal(
            event_id=event_id,
            outcome_index=outcome_k.index,
            signal_type="buy_yes_sell_no",
            edge=edge,
            edge_pct=edge_pct,
            max_size=max_size,
            is_actionable=edge > self.epsilon and max_size >= self.min_size,
            details={
                "total_ask_yes": total_ask_yes,
                "entry_fees": total_entry_fees,
                "bid_no_k": bid_no_k,
                "exit_fee": exit_fee,
                "conversion_cost": self.conversion_cost,
                "total_entry_cost": total_entry_cost,
                "total_exit_proceeds": total_exit_proceeds,
                "edge_pct": round(edge_pct, 4),
            },
        )

        if signal.is_actionable:
            logger.info(
                "neg_risk_buy_yes_detected",
                event_id=event_id[:16],
                outcome_k=outcome_k.index,
                edge=f"{edge:.4f}",
                edge_pct=f"{edge_pct:.2f}%",
                max_size=max_size,
                bid_no_k=f"{bid_no_k:.4f}",
                total_ask_yes=f"{total_ask_yes:.4f}",
            )

        return signal

    def generate_orders(
        self,
        signal: NegRiskSignal,
        outcome_k: NegRiskOutcome,
        other_outcomes: list[NegRiskOutcome],
        books: dict[str, OrderBook],
        size_fraction: float = 0.5,
    ) -> list[NegRiskOrderIntent]:
        """Generate order intents from a neg-risk signal."""
        if not signal.is_actionable:
            return []

        size = min(signal.max_size * size_fraction, self.max_size)
        if size < self.min_size:
            return []

        size_str = f"{size:.2f}"
        orders: list[NegRiskOrderIntent] = []

        if signal.signal_type == "buy_no_sell_yes":
            # Buy No_k
            no_book = books.get(outcome_k.token_id_no)
            if no_book:
                ask = no_book.get_best_ask()
                if ask:
                    orders.append(NegRiskOrderIntent(
                        token_id=outcome_k.token_id_no,
                        side="BUY",
                        price=f"{ask.price_float:.4f}",
                        size=size_str,
                        neg_risk=True,
                        condition_id=outcome_k.condition_id,
                        event_id=signal.event_id,
                        intent_tag=f"negr_buy_no_{outcome_k.index}",
                    ))

            # Sell all Yes_j (after conversion)
            for outcome in other_outcomes:
                yes_book = books.get(outcome.token_id_yes)
                if yes_book:
                    bid = yes_book.get_best_bid()
                    if bid:
                        orders.append(NegRiskOrderIntent(
                            token_id=outcome.token_id_yes,
                            side="SELL",
                            price=f"{bid.price_float:.4f}",
                            size=size_str,
                            neg_risk=True,
                            condition_id=outcome.condition_id,
                            event_id=signal.event_id,
                            intent_tag=f"negr_sell_yes_{outcome.index}",
                        ))

        elif signal.signal_type == "buy_yes_sell_no":
            # Buy all Yes_j (j≠k)
            for outcome in other_outcomes:
                yes_book = books.get(outcome.token_id_yes)
                if yes_book:
                    ask = yes_book.get_best_ask()
                    if ask:
                        orders.append(NegRiskOrderIntent(
                            token_id=outcome.token_id_yes,
                            side="BUY",
                            price=f"{ask.price_float:.4f}",
                            size=size_str,
                            neg_risk=True,
                            condition_id=outcome.condition_id,
                            event_id=signal.event_id,
                            intent_tag=f"negr_buy_yes_{outcome.index}",
                        ))

            # Sell No_k (after conversion)
            no_book = books.get(outcome_k.token_id_no)
            if no_book:
                bid = no_book.get_best_bid()
                if bid:
                    orders.append(NegRiskOrderIntent(
                        token_id=outcome_k.token_id_no,
                        side="SELL",
                        price=f"{bid.price_float:.4f}",
                        size=size_str,
                        neg_risk=True,
                        condition_id=outcome_k.condition_id,
                        event_id=signal.event_id,
                        intent_tag=f"negr_sell_no_{outcome_k.index}",
                    ))

        return orders

    def scan_event(
        self,
        outcomes: list[NegRiskOutcome],
        books: dict[str, OrderBook],
        event_id: str = "",
    ) -> list[NegRiskOrderIntent]:
        """Scan all outcomes in a neg-risk event for conversion arbs."""
        all_orders: list[NegRiskOrderIntent] = []

        for i, outcome_k in enumerate(outcomes):
            other_outcomes = [o for j, o in enumerate(outcomes) if j != i]

            # Check arb #1: Buy No_k → sell Yes_others
            signal1 = self.check_buy_no_sell_yes(
                outcome_k, other_outcomes, books, event_id
            )
            if signal1 and signal1.is_actionable:
                orders = self.generate_orders(
                    signal1, outcome_k, other_outcomes, books
                )
                all_orders.extend(orders)
                break  # Only take the best arb per event per cycle

            # Check arb #2: Buy Yes_others → sell No_k
            signal2 = self.check_buy_yes_sell_no(
                outcome_k, other_outcomes, books, event_id
            )
            if signal2 and signal2.is_actionable:
                orders = self.generate_orders(
                    signal2, outcome_k, other_outcomes, books
                )
                all_orders.extend(orders)
                break

        return all_orders
