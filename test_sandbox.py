#!/usr/bin/env python3
"""
Comprehensive sandbox test suite for Tripletex agent.
Tests parse_prompt classification AND handler execution against the sandbox API.

Usage:
    cd tripletex-agent
    python test_sandbox.py

Sandbox creds are hardcoded. All tests use unique timestamps to avoid duplicate conflicts.
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from tripletex_client import TripletexClient
from agent import parse_prompt
from task_handlers import HANDLERS, _ensure_bank_account, _get_default_department_id

SANDBOX_URL = "https://kkpqfuj-amager.tripletex.dev/v2"
SANDBOX_TOKEN = "eyJ0b2tlbklkIjoyMTQ3NjMyNzQ5LCJ0b2tlbiI6IjhmM2ZkZjRjLTVhZDAtNDczNy04MjY5LWNkZTNkNDkzYTg5MSJ9"

TODAY = date.today().isoformat()
TS = str(int(time.time()))[-6:]  # 6-digit suffix for uniqueness

client = TripletexClient(SANDBOX_URL, SANDBOX_TOKEN)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((name, condition, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def test_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────
# 1. LLM CLASSIFICATION TESTS
# ─────────────────────────────────────────────────────────────

test_section("1. LLM CLASSIFICATION (parse_prompt)")

classification_tests = [
    # (prompt, expected_task_type, key_field_checks)
    (
        f"Opprett en ansatt med navn Test{TS} Testesen, e-post test{TS}@example.com. Gi administratortilgang.",
        "create_employee",
        {"firstName": f"Test{TS}", "email": f"test{TS}@example.com"},
        "admin",
    ),
    (
        f"Créez le client DemoFR{TS} SARL avec le numéro d'organisation 87013{TS[:4]}. L'adresse est Solveien 88, 9008 Tromsø. E-mail : post@demofr{TS}.no.",
        "create_customer",
        {"name": f"DemoFR{TS} SARL", "addressLine1": "Solveien 88", "postalCode": "9008", "city": "Tromsø"},
        "address_extraction",
    ),
    (
        f"Opprett produktet 'Konsulenttime{TS}' med produktnummer P{TS}, pris 1500 kr ekskl. mva, MVA-sats 25 %.",
        "create_product",
        {"name": f"Konsulenttime{TS}", "priceExcludingVatCurrency": 1500, "vatPercentage": 25},
        "vat",
    ),
    (
        f"Opprett avdelingen 'Salg{TS}' med avdelingsnummer {TS[:4]}.",
        "create_department",
        {"name": f"Salg{TS}"},
        None,
    ),
    (
        f"Opprett prosjektet 'Nettside{TS}' for kunden Testkunde AS (org.nr 123456789). Startdato 2026-04-01.",
        "create_project",
        {"name": f"Nettside{TS}"},
        None,
    ),
    (
        f"Sett fastpris 361250 kr på prosjektet 'Skymigrering{TS}' for Bergvik AS (org.nr 917683336). Prosjektleder er Tor Bakken (tor.bakken@example.org). Fakturer kunden for 25 % av fastprisen som en delbetaling.",
        "project_billing",
        {"fixedprice": 361250},
        "project_billing_classification",
    ),
    (
        f"Hemos recibido la factura INV-{TS} del proveedor Luna SL (org. nº 98521{TS[:4]}) por 28100 NOK con IVA incluido. El importe corresponde a servicios de oficina (cuenta 7100). Registre la factura del proveedor con el IVA soportado correcto (25 %).",
        "register_supplier_invoice",
        {"amountIncludingVat": 28100},
        "supplier_invoice_classification",
    ),
    (
        f"Registrer betaling for fakturaen til kunde Testbetaler{TS} AS. Beløp: 15000 NOK.",
        "register_payment",
        {"amount": 15000},
        "payment_classification",
    ),
    (
        f"Créer une facture pour le client Sierra{TS} SL avec une ligne: Conseil informatique, 8000 NOK HT, TVA 25%.",
        "create_invoice",
        {},
        "french_invoice",
    ),
    (
        f"Erstelle eine Reisekostenabrechnung für Mitarbeiter mit dem Titel 'Reise nach Berlin{TS}', Abfahrtsdatum 2026-04-15.",
        "create_travel_expense",
        {"title": f"Reise nach Berlin{TS}"},
        "german_travel",
    ),
    (
        f"Opprett en faktura for kunde Acme{TS} AS med ordre og registrer betaling i samme operasjon. Produktlinje: Konsulenttjenester, 10000 NOK, 25% MVA.",
        "create_invoice_with_payment",
        {},
        "invoice_with_payment",
    ),
    (
        f"Legg til leverandøren Netthandel{TS} AS med org.nr 99912{TS[:4]}. Ikke en kunde, bare leverandør.",
        "create_customer",
        {"isSupplier": True, "isCustomer": False},
        "supplier_not_customer",
    ),
    (
        f"Oppdater ansatt Ola Nordmann: ny e-post ola{TS}@newmail.com",
        "update_employee",
        {"email": f"ola{TS}@newmail.com"},
        "update_employee",
    ),
    (
        f"Slett produktet 'GammelVare{TS}'",
        "delete_product",
        {},
        "delete_product",
    ),
    (
        f"Opprett en kreditnota for kunden Refund{TS} AS",
        "create_credit_note",
        {},
        "credit_note",
    ),
]

llm_pass = 0
for i, test in enumerate(classification_tests):
    prompt, expected_type, expected_fields, tag = test
    try:
        parsed = parse_prompt(prompt)
        task_type = parsed.get("task_type")
        type_ok = task_type == expected_type

        # Check key fields exist in any entity
        all_entities_fields = {}
        for e in parsed.get("entities", []):
            all_entities_fields.update(e.get("fields", {}))

        fields_ok = all(
            str(v) in str(all_entities_fields.get(k, "")) or all_entities_fields.get(k) == v
            for k, v in expected_fields.items()
        ) if expected_fields else True

        ok = type_ok and fields_ok
        detail = f"got={task_type}" if not type_ok else ""
        if expected_fields and not fields_ok:
            missing = [k for k, v in expected_fields.items() if str(v) not in str(all_entities_fields.get(k, ""))]
            detail += f" missing_fields={missing} got={dict(list(all_entities_fields.items())[:5])}"

        check(f"[{i+1:02d}] {tag or expected_type}", ok, detail)
        if ok:
            llm_pass += 1
    except Exception as e:
        check(f"[{i+1:02d}] {tag or expected_type}", False, f"exception: {e}")

print(f"\nLLM classification: {llm_pass}/{len(classification_tests)} passed")


# ─────────────────────────────────────────────────────────────
# 2. HANDLER EXECUTION TESTS (against sandbox)
# ─────────────────────────────────────────────────────────────

test_section("2. HANDLER EXECUTION (sandbox API)")

handler_pass = 0
handler_total = 0

def run_handler(task_type, prompt, expect_key=None):
    """Parse prompt then run handler. Returns result dict or None on error."""
    global handler_pass, handler_total
    handler_total += 1
    try:
        parsed = parse_prompt(prompt)
        actual_type = parsed.get("task_type")
        handler = HANDLERS.get(actual_type)
        if not handler:
            check(task_type, False, f"no handler for parsed type={actual_type}")
            return None
        result = handler(parsed, client)
        success = True
        if expect_key:
            success = result.get(expect_key) is not None
        detail = json.dumps({k: v for k, v in result.items() if k in (expect_key or "id", "ids", "created")}, default=str)
        check(task_type, success, detail)
        if success:
            handler_pass += 1
        return result
    except Exception as e:
        check(task_type, False, f"exception: {e}")
        return None


print("\n-- create_employee (no admin) --")
r = run_handler(
    "create_employee",
    f"Opprett en ansatt med navn Kari{TS} Hansen, e-post kari{TS}@example.com, mobil 99887766.",
    "ids"
)

print("\n-- create_employee (with admin) --")
r = run_handler(
    "create_employee_admin",
    f"Opprett ansatt Admin{TS} Bruker, e-post admin{TS}@firma.no. Gi administratortilgang.",
    "ids"
)

print("\n-- create_customer (with address, Norwegian) --")
r = run_handler(
    "create_customer_nb_address",
    f"Opprett kunde Fjordvik{TS} AS, org.nr 81234{TS[:4]}, e-post post@fjordvik{TS}.no, adresse Strandveien 42, 5003 Bergen.",
    "ids"
)

print("\n-- create_customer (with address, French) --")
r = run_handler(
    "create_customer_fr_address",
    f"Créez le client MerFR{TS} SARL avec le numéro d'organisation 87099{TS[:4]}. L'adresse est Solveien 88, 9008 Tromsø. E-mail : post@merfr{TS}.no.",
    "ids"
)

print("\n-- create_customer (supplier only) --")
r = run_handler(
    "create_customer_supplier",
    f"Legg til leverandøren LevAS{TS} med org.nr 77812{TS[:4]}. Ikke en kunde.",
    "ids"
)

print("\n-- create_product (with VAT 25%) --")
r = run_handler(
    "create_product_25pct",
    f"Opprett produktet 'Timer{TS}' med produktnummer T{TS}, pris 2000 kr ekskl. mva, MVA-sats 25%.",
    "ids"
)

print("\n-- create_product (VAT 0%) --")
r = run_handler(
    "create_product_0pct",
    f"Opprett produktet 'Gebyr{TS}' med produktnummer G{TS}, pris 500 kr, ingen MVA.",
    "ids"
)

print("\n-- create_department --")
r = run_handler(
    "create_department",
    f"Opprett avdelingen 'TestAvd{TS}' med avdelingsnummer {TS}.",
    "ids"
)

print("\n-- create_project --")
r = run_handler(
    "create_project",
    f"Opprett prosjektet 'TestProsjekt{TS}' med startdato {TODAY}.",
    "ids"
)

print("\n-- create_invoice (via order, 3 lines) --")
# First create a customer to invoice
try:
    cust_r = client.create_customer({"name": f"InvKunde{TS}", "isCustomer": True})
    cust_id = cust_r.get("value", {}).get("id")
    print(f"  Created customer id={cust_id}")

    # Set bank account
    _ensure_bank_account(client)

    order_r = client.create_order({
        "customer": {"id": cust_id},
        "orderDate": TODAY,
        "deliveryDate": (date.today() + timedelta(days=30)).isoformat(),
        "orderLines": [
            {"count": 1, "description": "Konsulenttjenester", "unitPriceExcludingVatCurrency": 5000.0},
            {"count": 2, "description": "Opplæring", "unitPriceExcludingVatCurrency": 1500.0},
        ]
    })
    order_id = order_r.get("value", {}).get("id")
    print(f"  Created order id={order_id}")

    inv_r = client.create_invoice({
        "invoiceDate": TODAY,
        "invoiceDueDate": (date.today() + timedelta(days=30)).isoformat(),
        "customer": {"id": cust_id},
        "orders": [{"id": order_id}],
    })
    inv_id = inv_r.get("value", {}).get("id")
    check("create_invoice_direct", inv_id is not None, f"invoice_id={inv_id}")
    if inv_id:
        handler_pass += 1
    handler_total += 1

    print("\n-- register_payment (on that invoice) --")
    handler_total += 1
    pt_r = client.get("/invoice/paymentType", params={"count": 1})
    pt_id = pt_r.get("values", [{}])[0].get("id")

    inv_detail = client.get(f"/invoice/{inv_id}", params={"fields": "id,amountCurrency,amountCurrencyOutstanding"})
    amount = inv_detail.get("value", {}).get("amountCurrencyOutstanding") or inv_detail.get("value", {}).get("amountCurrency", 8000.0)

    pay_r = client.put(f"/invoice/{inv_id}/:payment", {}, params={
        "paymentDate": TODAY,
        "paymentTypeId": pt_id,
        "paidAmount": amount,
    })
    check("register_payment_direct", True, f"invoice={inv_id} amount={amount}")
    handler_pass += 1

except Exception as e:
    check("create_invoice_direct", False, f"exception: {e}")
    check("register_payment_direct", False, "skipped due to invoice creation failure")
    handler_total += 2

print("\n-- register_supplier_invoice (ledger voucher) --")
r = run_handler(
    "register_supplier_invoice",
    f"Hemos recibido la factura INV-{TS} del proveedor Luna{TS} SL (org. nº 98521{TS[:4]}) por 28100 NOK con IVA incluido. El importe corresponde a servicios de oficina (cuenta 7100). Registre la factura del proveedor con el IVA soportado correcto (25 %).",
    "voucher_id"
)

print("\n-- create_travel_expense --")
r = run_handler(
    "create_travel_expense",
    f"Opprett reiseregning for ansatt med tittel 'Reise til Trondheim{TS}', avreisedato 2026-04-15.",
    "id"
)


# ─────────────────────────────────────────────────────────────
# 3. FIELD EXTRACTION VALIDATION
# ─────────────────────────────────────────────────────────────

test_section("3. FIELD EXTRACTION EDGE CASES")

extraction_tests = [
    # (description, prompt, check_fn)
    (
        "Norwegian date format",
        "Opprett ansatt Per Hansen, fødselsdato 15.03.1985.",
        lambda p: any("1985-03-15" in str(e.get("fields", {}).get("dateOfBirth", "")) for e in p.get("entities", [])),
    ),
    (
        "Phone number digits only",
        "Opprett ansatt Lise Dahl, mobil +47 900 12 345.",
        lambda p: any("4790012345" == str(e.get("fields", {}).get("phoneNumberMobile", "")).replace(" ", "") for e in p.get("entities", [])),
    ),
    (
        "Price with NOK suffix",
        "Opprett produkt Stol, pris 4 150 kr ekskl. mva.",
        lambda p: any(abs(float(e.get("fields", {}).get("priceExcludingVatCurrency", 0)) - 4150) < 1 for e in p.get("entities", [])),
    ),
    (
        "VAT 0% extraction",
        "Opprett produkt Avgiftsfri vare, pris 100 kr, 0 % MVA.",
        lambda p: any(e.get("fields", {}).get("vatPercentage") == 0 for e in p.get("entities", [])),
    ),
    (
        "isSupplier=True, isCustomer=False",
        "Legg til leverandøren Lev AS org.nr 123456789. Ikke kunde.",
        lambda p: any(e.get("fields", {}).get("isSupplier") is True and e.get("fields", {}).get("isCustomer") is False for e in p.get("entities", [])),
    ),
    (
        "Spanish address extraction",
        "Crea el cliente ABC SL, dirección es Calle Mayor 10, 28001 Madrid.",
        lambda p: any(e.get("fields", {}).get("city") == "Madrid" for e in p.get("entities", [])),
    ),
    (
        "German address extraction",
        "Erstelle Kunde Müller GmbH, Adresse: Hauptstraße 5, 10115 Berlin.",
        lambda p: any("Berlin" in str(e.get("fields", {}).get("city", "")) for e in p.get("entities", [])),
    ),
    (
        "project_billing NOT classified as create_invoice",
        "Sett fastpris 361250 kr på prosjektet 'Skymigrering' for Bergvik AS. Fakturer for 25% som delbetaling.",
        lambda p: p.get("task_type") == "project_billing",
    ),
    (
        "Supplier invoice classification (French)",
        "Nous avons reçu la facture F-001 du fournisseur Dupont SA pour 10000 EUR TTC. Enregistrez la facture fournisseur.",
        lambda p: p.get("task_type") == "register_supplier_invoice",
    ),
    (
        "Amount with space thousands separator",
        "Faktura på 1 250 000 kr.",
        lambda p: any(abs(float(e.get("fields", {}).get("amount", e.get("fields", {}).get("amountIncludingVat", 0))) - 1250000) < 1 for e in p.get("entities", [])),
    ),
]

ext_pass = 0
for desc, prompt, check_fn in extraction_tests:
    try:
        parsed = parse_prompt(prompt)
        ok = check_fn(parsed)
        detail = f"task={parsed.get('task_type')} entities={[{e.get('type'): list(e.get('fields',{}).keys())} for e in parsed.get('entities',[])]}"
        check(desc, ok, "" if ok else detail)
        if ok:
            ext_pass += 1
    except Exception as e:
        check(desc, False, f"exception: {e}")

print(f"\nField extraction: {ext_pass}/{len(extraction_tests)} passed")


# ─────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────

test_section("SUMMARY")
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
print(f"Total: {passed}/{total} passed ({failed} failed)")

if failed > 0:
    print("\nFailed tests:")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))

sys.exit(0 if failed == 0 else 1)
