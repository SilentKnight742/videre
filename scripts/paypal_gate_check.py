#!/usr/bin/env python3
"""Go/no-go check for the agent-wallet idea (PayPal SANDBOX only).

Flow:  create order (intent=AUTHORIZE, $100)  ->  you approve it as the sandbox buyer
       ->  authorize  ->  partial capture $30  ->  partial capture $20  ->  void the rest.

This is exactly the "fund the agent, let it spend, refund the remainder" mechanic.

Usage (credentials stay in your shell, never in chat or in git):
    export PAYPAL_CLIENT_ID=...        # sandbox REST app client id
    export PAYPAL_CLIENT_SECRET=...    # sandbox REST app secret
    python paypal_gate_check.py

Standard library only; Python 3.9+.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://api-m.sandbox.paypal.com"
CURRENCY = "USD"
FAILED = []


def call(method, path, token=None, body=None, basic=None, form=None):
    """Tiny HTTP helper. Returns (status_code, parsed_json_or_{})."""
    headers = {}
    data = None
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation"
        if method == "POST":
            headers["PayPal-Request-Id"] = str(uuid.uuid4())  # idempotency key
            data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"raw": raw}


def step(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if not ok:
        FAILED.append(name)
    return ok


def main():
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        sys.exit("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET first (sandbox REST app).")

    # 1. access token
    code, tok = call("POST", "/v1/oauth2/token", basic=f"{client_id}:{secret}",
                     form={"grant_type": "client_credentials"})
    if not step("get access token", code == 200, f"(HTTP {code})"):
        print(json.dumps(tok, indent=2))
        sys.exit(1)
    token = tok["access_token"]

    # 2. create an AUTHORIZE order for $100
    order_body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "reference_id": "agent-wallet-demo",
            "amount": {"currency_code": CURRENCY, "value": "100.00"},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": "https://example.com/return",
            "cancel_url": "https://example.com/cancel",
            "user_action": "PAY_NOW",
        }}},
    }
    code, order = call("POST", "/v2/checkout/orders", token=token, body=order_body)
    if not step("create AUTHORIZE order ($100)", code in (200, 201), f"(HTTP {code})"):
        print(json.dumps(order, indent=2))
        sys.exit(1)
    order_id = order["id"]
    approve = next((l["href"] for l in order.get("links", [])
                    if l.get("rel") in ("payer-action", "approve")), None)
    print(f"      order id: {order_id}, status: {order.get('status')}")
    if not approve:
        print(json.dumps(order, indent=2))
        sys.exit("No approval link returned.")

    # 3. human approval (this is the "you fund the agent" moment)
    print("\nOpen this link, log in as your SANDBOX PERSONAL (buyer) account and approve:")
    print(f"  {approve}")
    print("(You will land on example.com afterwards - that is expected.)")
    input("\nPress Enter once you have approved the payment... ")

    # 4. authorize
    code, auth = call("POST", f"/v2/checkout/orders/{order_id}/authorize", token=token)
    ok = code in (200, 201)
    step("authorize order", ok, f"(HTTP {code})")
    if not ok:
        print(json.dumps(auth, indent=2))
        sys.exit(1)
    try:
        authorization = auth["purchase_units"][0]["payments"]["authorizations"][0]
        auth_id = authorization["id"]
    except (KeyError, IndexError):
        print(json.dumps(auth, indent=2))
        sys.exit("Could not find authorization id in response.")
    print(f"      authorization id: {auth_id}, status: {authorization.get('status')}")

    # 5. two partial captures (final_capture=false keeps the rest available)
    for label, amount in (("agent spend #1", "30.00"), ("agent spend #2", "20.00")):
        body = {
            "amount": {"currency_code": CURRENCY, "value": amount},
            "final_capture": False,
            "invoice_id": f"agent-wallet-{uuid.uuid4().hex[:12]}",  # unique per capture
            "note_to_payer": label,
        }
        code, cap = call("POST", f"/v2/payments/authorizations/{auth_id}/capture",
                         token=token, body=body)
        step(f"partial capture ${amount} ({label})", code in (200, 201),
             f"(HTTP {code}, status {cap.get('status')})")
        if code not in (200, 201):
            print(json.dumps(cap, indent=2))

    # 6. void the remainder ("refund what is left")
    code, void = call("POST", f"/v2/payments/authorizations/{auth_id}/void", token=token)
    step("void remaining $50", code in (200, 204), f"(HTTP {code})")
    if code not in (200, 204):
        print(json.dumps(void, indent=2))

    print("\n" + ("ALL STEPS PASSED - the wallet mechanic works on your sandbox."
                  if not FAILED else f"FAILED STEPS: {FAILED} - paste the output above to me."))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()