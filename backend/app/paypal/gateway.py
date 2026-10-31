"""Payment gateway abstraction and PayPal implementations.

Supports:
- VaultGateway: Primary autonomy enabler using pinned sandbox shape payment_source.paypal.vault_id.
- BuyerApprovalGateway: Fallback per-purchase buyer approval.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_HALF_UP
import logging
import httpx

from backend.app.config import PAYPAL_BASE_URL, PAYPAL_WEBHOOK_ID
from backend.app.paypal.auth import get_access_token
from backend.app.security import mask_token

logger = logging.getLogger("mandate.paypal.gateway")


def cents_to_paypal_str(cents: int) -> str:
    """Format integer cents to PayPal USD string like '10.00'."""
    d = Decimal(cents) / Decimal(100)
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PaymentGateway(ABC):
    """Abstract payment gateway interface."""

    @abstractmethod
    def grant_mandate(self, mandate_id: str, return_url: str, cancel_url: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        """Initiate mandate funding / authorization grant."""
        pass

    @abstractmethod
    def capture_grant(self, order_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        """Capture grant order, extract vault token, and refund the $1.00."""
        pass

    @abstractmethod
    def charge(
        self,
        vault_token: str,
        amount_cents: int,
        description: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Execute autonomous server-side charge."""
        pass

    @abstractmethod
    def refund(
        self,
        capture_id: str,
        amount_cents: int,
        note: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Execute capture refund (full or partial)."""
        pass

    @abstractmethod
    def verify_webhook(self, headers: Dict[str, str], body: Dict[str, Any]) -> bool:
        """Verify webhook signature."""
        pass


class VaultGateway(PaymentGateway):
    """
    Primary PayPal gateway using the pinned vault shape.
    - Grant: creates a $1.00 vault-setup order; buyer approves once; captured token is stored; $1.00 refunded.
    - Charge: uses payment_source.paypal.vault_id with NO browser interaction.
    """

    def __init__(self, base_url: str = PAYPAL_BASE_URL):
        self.base_url = base_url

    def grant_mandate(
        self,
        mandate_id: str,
        return_url: str,
        cancel_url: str,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a $1.00 vault setup order for the Grant screen.
        Sends fresh PayPal-Request-Id header.
        On buyer approval, token id is extracted from payment_source.paypal.attributes.vault.id.
        """
        import uuid
        req_id = request_id or f"req-grant-{uuid.uuid4()}"
        token = get_access_token()
        order_body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": f"grant-{mandate_id}",
                    "amount": {"currency_code": "USD", "value": "1.00"},
                    "description": "Mandate Vault Authorization ($1.00 refundable)",
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                        "user_action": "PAY_NOW",
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                    },
                    "attributes": {
                        "vault": {
                            "store_in_vault": "ON_SUCCESS",
                            "usage_type": "MERCHANT",
                            "customer_type": "CONSUMER",
                        }
                    },
                }
            },
        }

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/checkout/orders",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": req_id,
                },
                json=order_body,
            )
            resp.raise_for_status()
            data = resp.json()

            order_id = data["id"]
            approve_url = None
            for link in data.get("links", []):
                if link["rel"] in ("approve", "payer-action"):
                    approve_url = link["href"]
                    break

            logger.info(f"Created grant order_id={order_id} (req_id={req_id})")
            return {
                "order_id": order_id,
                "approve_url": approve_url,
                "status": data.get("status"),
            }

    def capture_grant(self, order_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Capture the $1.00 vault setup order, extract the vault token,
        and immediately refund the $1.00 test authorization.
        """
        import uuid
        req_id = request_id or f"req-cap-grant-{uuid.uuid4()}"
        token = get_access_token()

        with httpx.Client(timeout=25.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/checkout/orders/{order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": req_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status")
            vault_token_id = (
                data.get("payment_source", {})
                .get("paypal", {})
                .get("attributes", {})
                .get("vault", {})
                .get("id")
            )
            customer_id = (
                data.get("payment_source", {})
                .get("paypal", {})
                .get("attributes", {})
                .get("vault", {})
                .get("customer", {})
                .get("id")
            )

            masked_vault = mask_token(vault_token_id)
            masked_customer = mask_token(customer_id)
            logger.info(
                f"Grant order {order_id} captured with status {status}. "
                f"VaultToken={masked_vault}, Customer={masked_customer}"
            )

            # Extract capture ID to issue $1.00 refund
            capture_id = None
            for pu in data.get("purchase_units", []):
                for c in pu.get("payments", {}).get("captures", []):
                    capture_id = c["id"]
                    break

            refund_data = None
            if capture_id:
                try:
                    refund_req = f"req-ref-grant-{uuid.uuid4()}"
                    refund_data = self.refund(
                        capture_id=capture_id,
                        amount_cents=100,
                        note="Mandate authorization setup refund",
                        request_id=refund_req,
                    )
                    logger.info(f"Grant $1.00 refunded successfully (capture_id={capture_id})")
                except Exception as exc:
                    logger.warning(f"Failed to auto-refund $1.00 grant: {exc}")

            return {
                "order_id": order_id,
                "status": status,
                "vault_token_id": vault_token_id,
                "customer_id": customer_id,
                "capture_id": capture_id,
                "refund": refund_data,
            }

    def charge(
        self,
        vault_token: str,
        amount_cents: int,
        description: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """
        Execute server-side autonomous charge using pinned shape:
        payment_source.paypal.vault_id.
        Confirmed working in sandbox with status COMPLETED and capture inside response.
        """
        token = get_access_token()
        amount_str = cents_to_paypal_str(amount_cents)
        masked_tok = mask_token(vault_token)
        logger.info(f"Charging via vault_id={masked_tok}, amount={amount_cents}¢, request_id={request_id}")

        order_body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": request_id,
                    "amount": {"currency_code": "USD", "value": amount_str},
                    "description": description,
                }
            ],
            "payment_source": {
                "paypal": {
                    "vault_id": vault_token,
                }
            },
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/checkout/orders",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": request_id,
                },
                json=order_body,
            )
            resp.raise_for_status()
            data = resp.json()

            order_id = data.get("id")
            status = data.get("status")

            # Extract capture ID from response
            capture_id = None
            for pu in data.get("purchase_units", []):
                for c in pu.get("payments", {}).get("captures", []):
                    capture_id = c["id"]
                    break

            return {
                "order_id": order_id,
                "capture_id": capture_id,
                "status": status,
                "raw": data,
            }

    def refund(
        self,
        capture_id: str,
        amount_cents: int,
        note: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Execute refund against a captured PayPal payment."""
        token = get_access_token()
        amount_str = cents_to_paypal_str(amount_cents)
        masked_tok = mask_token(vault_token)
        logger.info(f"Charging via vault_id={masked_tok}, amount={amount_cents}¢, request_id={request_id}")

        body = {
            "amount": {"currency_code": "USD", "value": amount_str},
            "note_to_payer": note,
        }

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/payments/captures/{capture_id}/refund",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": request_id,
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            return {
                "refund_id": data.get("id"),
                "status": data.get("status"),
                "raw": data,
            }

    def verify_webhook(self, headers: Dict[str, str], body: Dict[str, Any]) -> bool:
        """
        Verify incoming webhook signature.
        Note: Webhook verification is currently unverified with live sandbox deliveries.
        """
        if not PAYPAL_WEBHOOK_ID:
            logger.warning("PAYPAL_WEBHOOK_ID not set; cannot verify signature")
            return False

        try:
            token = get_access_token()
            verify_payload = {
                "auth_algo": headers.get("paypal-auth-algo", ""),
                "cert_url": headers.get("paypal-cert-url", ""),
                "transmission_id": headers.get("paypal-transmission-id", ""),
                "transmission_sig": headers.get("paypal-transmission-sig", ""),
                "transmission_time": headers.get("paypal-transmission-time", ""),
                "webhook_id": PAYPAL_WEBHOOK_ID,
                "webhook_event": body,
            }

            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.base_url}/v1/notifications/verify-webhook-signature",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=verify_payload,
                )
                if resp.status_code == 200:
                    status = resp.json().get("verification_status")
                    return status == "SUCCESS"
        except Exception as exc:
            logger.error(f"Webhook verification error: {exc}")

        return False


class BuyerApprovalGateway(PaymentGateway):
    """Fallback gateway for per-purchase buyer approval when vault is unavailable."""

    def __init__(self, base_url: str = PAYPAL_BASE_URL):
        self.base_url = base_url

    def grant_mandate(self, mandate_id: str, return_url: str, cancel_url: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        return {"status": "MOCK_APPROVED", "mandate_id": mandate_id}

    def capture_grant(self, order_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        return {"status": "COMPLETED", "order_id": order_id, "vault_token_id": "mock-vault-token"}

    def charge(
        self,
        vault_token: str,
        amount_cents: int,
        description: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Creates standard order requiring browser payer approval."""
        token = get_access_token()
        amount_str = cents_to_paypal_str(amount_cents)
        masked_tok = mask_token(vault_token)
        logger.info(f"Charging via vault_id={masked_tok}, amount={amount_cents}¢, request_id={request_id}")

        order_body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": request_id,
                    "amount": {"currency_code": "USD", "value": amount_str},
                    "description": description,
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "user_action": "PAY_NOW",
                    }
                }
            },
        }

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/checkout/orders",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": request_id,
                },
                json=order_body,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"order_id": data.get("id"), "status": data.get("status")}

    def refund(
        self,
        capture_id: str,
        amount_cents: int,
        note: str,
        request_id: str,
    ) -> Dict[str, Any]:
        token = get_access_token()
        amount_str = cents_to_paypal_str(amount_cents)
        masked_tok = mask_token(vault_token)
        logger.info(f"Charging via vault_id={masked_tok}, amount={amount_cents}¢, request_id={request_id}")

        body = {
            "amount": {"currency_code": "USD", "value": amount_str},
            "note_to_payer": note,
        }

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.base_url}/v2/payments/captures/{capture_id}/refund",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": request_id,
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"refund_id": data.get("id"), "status": data.get("status")}

    def verify_webhook(self, headers: Dict[str, str], body: Dict[str, Any]) -> bool:
        return False

