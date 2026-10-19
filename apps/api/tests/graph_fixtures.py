"""A small shop repository whose flows cross files, for graph tests and the flow benchmark."""

ROUTES_PY = b'''from shop.services import orders
from ..billing import refund as send_refund
import importlib


def create_order_endpoint(request):
    """HTTP handler for POST /orders."""
    order = orders.place_order(request.user, request.items)
    return {"id": order["id"]}


def cancel_order_endpoint(request):
    send_refund(request.user, request.amount)
    return {"ok": True}


def load_plugin(name):
    return importlib.import_module(name)
'''

ORDERS_PY = b"""from shop.billing import charge
from shop.db.store import OrderRepository
from shop.services.base import BaseService


def place_order(user, items):
    total = sum(item.price for item in items)
    charge(user, total)
    repo = OrderRepository()
    return repo.store_order(user, items)


class OrderService(BaseService):
    def run(self, user, items):
        self.log("placing")
        return place_order(user, items)
"""

BASE_PY = b"""class BaseService:
    def log(self, message):
        print(f"[service] {message}")
"""

BILLING_PY = b'''def charge(user, amount):
    """Take payment through the card gateway."""
    return gateway_request(user.card, amount)


def refund(user, amount):
    return gateway_request(user.card, -amount)


def gateway_request(card, amount):
    return {"card": card[-4:], "amount": amount}
'''

STORE_PY = b"""class Repository:
    def persist(self, row):
        self.rows.append(row)


class OrderRepository(Repository):
    def store_order(self, user, items):
        row = {"id": len(items), "user": user.id}
        self.persist(row)
        return row
"""

API_TS = b"""import { formatPrice } from './format';
import * as http from './http';

export async function submitOrder(items: { price: number }[]) {
  const total = items.reduce((sum, item) => sum + item.price, 0);
  return http.post('/orders', { total: formatPrice(total) });
}
"""

FORMAT_TS = b"""export function formatPrice(amount: number): string {
  return amount.toFixed(2);
}
"""

HTTP_TS = b"""export async function post(url: string, body: unknown) {
  const response = await fetch(url, { method: 'POST', body: JSON.stringify(body) });
  return response.json();
}
"""

CHECKOUT_TSX = b"""import { submitOrder } from './api';
import './checkout.css';
import { track } from '@/lib/analytics';

export function Checkout() {
  const onPay = () => submitOrder([]);
  return <button onClick={onPay}>Pay</button>;
}
"""

LAZY_TS = b"""export async function loadLocale(name: string) {
  return import(name);
}
"""

GRAPH_FILES: dict[str, bytes] = {
    "README.md": b"# Shop\n\nA fixture shop.\n",
    "shop/__init__.py": b"",
    "shop/api/__init__.py": b"",
    "shop/api/routes.py": ROUTES_PY,
    "shop/services/__init__.py": b"",
    "shop/services/orders.py": ORDERS_PY,
    "shop/services/base.py": BASE_PY,
    "shop/billing.py": BILLING_PY,
    "shop/db/__init__.py": b"",
    "shop/db/store.py": STORE_PY,
    "web/src/api.ts": API_TS,
    "web/src/format.ts": FORMAT_TS,
    "web/src/http.ts": HTTP_TS,
    "web/src/Checkout.tsx": CHECKOUT_TSX,
    "web/src/lazy.ts": LAZY_TS,
}

# Flow questions and the files a good answer needs (blueprint §17: fixed evaluation cases).
FLOW_CASES: list[tuple[str, set[str]]] = [
    (
        "What happens when create_order_endpoint is called?",
        {"shop/api/routes.py", "shop/services/orders.py", "shop/billing.py"},
    ),
    (
        "How does Checkout send the order to the server?",
        {"web/src/Checkout.tsx", "web/src/api.ts", "web/src/http.ts"},
    ),
    (
        "What does OrderService.run do?",
        {"shop/services/orders.py", "shop/services/base.py"},
    ),
    (
        "What does cancel_order_endpoint do with the card?",
        {"shop/api/routes.py", "shop/billing.py"},
    ),
]
