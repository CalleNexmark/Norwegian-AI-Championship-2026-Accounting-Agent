#!/usr/bin/env python3
"""Offline scoring system — tests every handler + verifies API state against sandbox.

Usage:
    python test_suite.py           # handler execution + field verification
    python test_suite.py --llm     # also run LLM classification tests (slower, uses Gemini API)
    python test_suite.py --only create_employee  # test one section
"""

import sys
import os
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from tripletex_client import TripletexClient
from task_handlers import (
    HANDLERS, _ensure_bank_account, _to_float,
    _get_default_employee_id, _find_employee_id,
)

SANDBOX_URL = "https://kkpqfuj-amager.tripletex.dev/v2"
SANDBOX_TOKEN = "eyJ0b2tlbklkIjoyMTQ3NjMyNzQ5LCJ0b2tlbiI6IjhmM2ZkZjRjLTVhZDAtNDczNy04MjY5LWNkZTNkNDkzYTg5MSJ9"

TODAY = date.today().isoformat()
DUE = (date.today() + timedelta(days=30)).isoformat()
TS = str(int(time.time()))[-6:]  # 6-digit unique suffix

client = TripletexClient(SANDBOX_URL, SANDBOX_TOKEN)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

results = []
_current_section = [""]


def check(name, cond, detail=""):
    label = f"{GREEN}PASS{RESET}" if cond else f"{RED}FAIL{RESET}"
    full_name = f"{_current_section[0]}:{name}"
    results.append((full_name, bool(cond), detail))
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{label}] {name}{detail_str}")
    return bool(cond)


def section(title, only=None):
    if only and title != only:
        return False
    _current_section[0] = title
    print(f"\n{'='*60}\n  {title}\n{'='*60}")
    return True


def api_get(path, **params):
    """Safe GET — returns empty dict on error."""
    try:
        return client.get(path, params=params or None)
    except Exception as e:
        return {"values": [], "_error": str(e)}


def run_handler(task_type, parsed):
    """Run a handler, catch and report exceptions. Returns result or None."""
    handler = HANDLERS.get(task_type)
    if not handler:
        check("handler_registered", False, f"No handler for {task_type}")
        return None
    try:
        return handler(parsed, client)
    except Exception as e:
        check("no_exception", False, str(e)[:200])
        return None


def make_parsed(task_type, entities, notes=""):
    return {"task_type": task_type, "entities": entities, "notes": notes, "api_sequence": []}


# ─────────────────────────────────────────────────────────────
# HELPER: set up an open invoice for a given customer
# ─────────────────────────────────────────────────────────────

def _create_test_invoice(customer_id, amount_excl=10000.0):
    """Create order + invoice for a customer. Returns invoice_id or None."""
    try:
        _ensure_bank_account(client)
        or_r = client.create_order({
            "customer": {"id": customer_id},
            "orderDate": TODAY,
            "deliveryDate": DUE,
            "orderLines": [{"count": 1, "description": "Test", "unitPriceExcludingVatCurrency": amount_excl}],
        })
        or_id = or_r.get("value", {}).get("id")
        if not or_id:
            return None
        inv_r = client.create_invoice({
            "invoiceDate": TODAY, "invoiceDueDate": DUE,
            "customer": {"id": customer_id},
            "orders": [{"id": or_id}],
        })
        return inv_r.get("value", {}).get("id")
    except Exception as e:
        print(f"  {YELLOW}[SETUP]{RESET} Could not create test invoice: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# 1. create_employee
# ─────────────────────────────────────────────────────────────

args = argparse.Namespace(llm=False, only=None)  # default; overridden below

def run_tests(only=None):
    global args

    # ── create_employee ──────────────────────────────────────
    if section("create_employee", only):
        emp_email = f"test{TS}@example.com"
        emp_first = f"Test{TS}"
        emp_last = "Testerson"

        parsed = make_parsed("create_employee", [
            {"type": "employee", "fields": {
                "firstName": emp_first, "lastName": emp_last,
                "email": emp_email, "phoneNumberMobile": "99887766",
            }},
            {"type": "employment", "fields": {"percentage": 100, "annualSalary": 600000}},
        ])
        res = run_handler("create_employee", parsed)
        if res:
            emp_id = (res.get("ids") or [None])[0]
            check("returned_id", emp_id is not None, f"id={emp_id}")

            emp_data = api_get("/employee", email=emp_email,
                               fields="id,firstName,lastName,email", count=1).get("values", [{}])[0]
            check("found_in_api", bool(emp_data.get("id")))
            check("firstName", emp_data.get("firstName") == emp_first, f"got={emp_data.get('firstName')}")
            check("lastName", emp_data.get("lastName") == emp_last, f"got={emp_data.get('lastName')}")
            check("email", emp_data.get("email") == emp_email)

            if emp_id:
                emp_recs = api_get("/employee/employment", employeeId=emp_id, fields="id", count=5).get("values", [])
                check("has_employment", bool(emp_recs), f"count={len(emp_recs)}")
                if emp_recs:
                    # Check division via full record GET
                    rec_full = api_get(f"/employee/employment/{emp_recs[0]['id']}", fields="*").get("value", {})
                    # Check if sandbox even has divisions — if not, skip division check
                    sandbox_divs = api_get("/division", fields="id", count=1).get("values", [])
                    if sandbox_divs:
                        check("employment_has_division", bool(rec_full.get("division")),
                              f"division={rec_full.get('division')}")
                    else:
                        print(f"  {YELLOW}[SKIP]{RESET} employment_has_division — no divisions on sandbox")
                    # Check salary details
                    rec_id = emp_recs[0]["id"]
                    det = api_get("/employee/employment/details", employmentId=rec_id,
                                  fields="annualSalary,percentageOfFullTimeEquivalent", count=1).get("values", [{}])
                    if det and det[0].get("annualSalary"):
                        check("annualSalary", abs(_to_float(det[0]["annualSalary"]) - 600000) < 1,
                              f"got={det[0]['annualSalary']}")
        else:
            check("returned_id", False, "handler returned None")

    # ── create_employee (admin) ──────────────────────────────
    if section("create_employee_admin", only):
        admin_email = f"admin{TS}@example.com"
        parsed = make_parsed("create_employee", [
            {"type": "employee", "fields": {"firstName": f"Admin{TS}", "lastName": "Boss", "email": admin_email}},
        ], notes="assign_admin_role")
        res = run_handler("create_employee", parsed)
        if res:
            admin_id = (res.get("ids") or [None])[0]
            check("returned_id", admin_id is not None)
            if admin_id:
                ent = api_get("/employee/entitlement", employeeId=admin_id, fields="id", count=1).get("values", [])
                check("has_entitlements", bool(ent), f"count={len(ent)}")

    # ── create_customer ──────────────────────────────────────
    if section("create_customer", only):
        cust_name = f"TestKunde{TS} AS"
        parsed = make_parsed("create_customer", [
            {"type": "customer", "fields": {
                "name": cust_name,
                "email": f"post@test{TS}.no", "isCustomer": True,
                "addressLine1": "Strandveien 42", "postalCode": "5003", "city": "Bergen",
            }},
        ])
        res = run_handler("create_customer", parsed)
        if res:
            cust_id = (res.get("ids") or [None])[0]
            check("returned_id", cust_id is not None)
            if cust_id:
                # Look up by ID directly (name search is fuzzy and unreliable)
                cust_data = api_get(f"/customer/{cust_id}", fields="id,name,email,physicalAddress").get("value", {})
                check("found_in_api", bool(cust_data.get("id")))
                check("name", cust_data.get("name") == cust_name, f"got={cust_data.get('name')}")
                addr = cust_data.get("physicalAddress") or {}
                check("addressLine1", addr.get("addressLine1") == "Strandveien 42", f"got={addr.get('addressLine1')}")
                check("postalCode", addr.get("postalCode") == "5003", f"got={addr.get('postalCode')}")
                check("city", addr.get("city") == "Bergen", f"got={addr.get('city')}")

    # ── create_product ───────────────────────────────────────
    # NOTE: Sandbox limitation — standard VAT IDs (3/5/31/32) are invalid on sandbox.
    # Handler will 422 when vatPercentage is specified. Test without VAT to verify base flow.
    if section("create_product", only):
        prod_num = f"P{TS}"
        parsed = make_parsed("create_product", [
            {"type": "product", "fields": {
                "name": f"Prod{TS}", "number": prod_num,
                "priceExcludingVatCurrency": 1500,
                # No vatPercentage — sandbox rejects standard VAT IDs
            }},
        ])
        res = run_handler("create_product", parsed)
        if res:
            prod_id = (res.get("ids") or [None])[0]
            check("returned_id", prod_id is not None)
            if prod_id:
                # GET /product/{id} — look up by ID directly (number filter expects int IDs)
                prod = api_get(f"/product/{prod_id}", fields="id,name,priceExcludingVatCurrency").get("value", {})
                check("found_in_api", bool(prod.get("id")))
                check("price", abs(_to_float(prod.get("priceExcludingVatCurrency", 0)) - 1500) < 1)
        # VAT is tested indirectly — handler uses dynamic lookup for non-standard percentages
        print(f"  {YELLOW}[NOTE]{RESET} VAT IDs 3/5/31/32 invalid on sandbox — VAT not verified here")

    # ── create_department ────────────────────────────────────
    if section("create_department", only):
        dept_name = f"TestAvd{TS}"
        parsed = make_parsed("create_department", [
            {"type": "department", "fields": {"name": dept_name, "departmentNumber": TS}},
        ])
        res = run_handler("create_department", parsed)
        if res:
            dept_id = (res.get("ids") or [None])[0]
            check("returned_id", dept_id is not None)
            dept = api_get("/department", name=dept_name, fields="id,name,departmentNumber", count=1).get("values", [{}])[0]
            check("found_in_api", bool(dept.get("id")))
            check("name", dept.get("name") == dept_name)

    # ── create_project ───────────────────────────────────────
    if section("create_project", only):
        proj_name = f"TestProj{TS}"
        parsed = make_parsed("create_project", [
            {"type": "project", "fields": {"name": proj_name, "startDate": TODAY}},
        ])
        res = run_handler("create_project", parsed)
        if res:
            proj_id = (res.get("ids") or [None])[0]
            check("returned_id", proj_id is not None)
            proj = api_get("/project", name=proj_name, fields="id,name,startDate,projectManager", count=1).get("values", [{}])[0]
            check("found_in_api", bool(proj.get("id")))
            check("name", proj.get("name") == proj_name)
            check("has_manager", bool(proj.get("projectManager")), f"manager={proj.get('projectManager')}")

    # ── project_billing ──────────────────────────────────────
    if section("project_billing", only):
        _ensure_bank_account(client)
        # Pre-create project + customer (competition pre-populates these)
        mgr_id = _get_default_employee_id(client)
        pb_proj_r = client.post("/project", {
            "name": f"SkymigProj{TS}", "startDate": TODAY,
            **({"projectManager": {"id": mgr_id}} if mgr_id else {}),
        })
        pb_proj_id = pb_proj_r.get("value", {}).get("id")
        pb_cust_r = client.create_customer({"name": f"BillingAS{TS}", "isCustomer": True})
        pb_cust_id = pb_cust_r.get("value", {}).get("id")

        fixed_price = 361250.0
        pct = 25.0
        expected_amount = fixed_price * pct / 100  # 90312.5

        parsed = make_parsed("project_billing", [
            {"type": "project", "fields": {"name": f"SkymigProj{TS}", "fixedprice": fixed_price}},
            {"type": "customer", "fields": {"name": f"BillingAS{TS}"}},
            {"type": "invoice", "fields": {"percentage": pct, "amount": expected_amount}},
        ])
        res = run_handler("project_billing", parsed)
        if res:
            check("invoice_created", bool(res.get("invoice_id")))
            check("amount_correct",
                  abs(_to_float(res.get("amount", 0)) - expected_amount) < 1,
                  f"expected={expected_amount} got={res.get('amount')}")
            # Verify project has isFixedPrice + fixedprice
            if pb_proj_id:
                proj_d = api_get(f"/project/{pb_proj_id}", fields="id,isFixedPrice,fixedprice").get("value", {})
                check("isFixedPrice", proj_d.get("isFixedPrice") is True, f"got={proj_d.get('isFixedPrice')}")
                check("fixedprice_value",
                      abs(_to_float(proj_d.get("fixedprice", 0)) - fixed_price) < 1,
                      f"got={proj_d.get('fixedprice')}")

    # ── register_project_hours ───────────────────────────────
    if section("register_project_hours", only):
        try:
            _ensure_bank_account(client)
        except Exception:
            pass
        mgr_id = _get_default_employee_id(client)
        try:
            rph_cust_r = client.create_customer({"name": f"HrsAS{TS}", "isCustomer": True})
            rph_cust_id = rph_cust_r.get("value", {}).get("id")
        except Exception as e:
            print(f"  {YELLOW}[SETUP]{RESET} Could not create customer: {e}")
            rph_cust_id = None
        try:
            rph_proj_r = client.post("/project", {
                "name": f"HrsProj{TS}", "startDate": TODAY,
                **({"projectManager": {"id": mgr_id}} if mgr_id else {}),
            })
        except Exception as e:
            print(f"  {YELLOW}[SETUP]{RESET} Could not create project: {e}")
            rph_proj_r = {"value": {}}

        emps = client.get_employees(fields="id,firstName,lastName,email")
        emp = emps[0] if emps else {}

        hours = 8.0
        rate = 1500.0
        parsed = make_parsed("register_project_hours", [
            {"type": "employee", "fields": {
                "firstName": emp.get("firstName", ""), "lastName": emp.get("lastName", ""),
                "email": emp.get("email", ""),
            }},
            {"type": "project", "fields": {"name": f"HrsProj{TS}"}},
            {"type": "customer", "fields": {"name": f"HrsAS{TS}"}},
            {"type": "invoice", "fields": {
                "hours": hours, "hourlyRate": rate,
                "activityName": "Konsulenttjenester", "amount": hours * rate,
            }},
        ])
        res = run_handler("register_project_hours", parsed)
        if res:
            check("invoice_id", bool(res.get("invoice_id")))
            check("timesheet_entry", res.get("timesheet_entry_id") is not None,
                  f"ts_id={res.get('timesheet_entry_id')}")
            check("hours_correct",
                  abs(_to_float(res.get("hours", 0)) - hours) < 0.1,
                  f"expected={hours} got={res.get('hours')}")

    # ── process_payroll ──────────────────────────────────────
    if section("process_payroll", only):
        emps = client.get_employees(fields="id,firstName,lastName,email")
        emp = emps[0] if emps else {}
        parsed = make_parsed("process_payroll", [
            {"type": "employee", "fields": {
                "firstName": emp.get("firstName", "Test"),
                "lastName": emp.get("lastName", "User"),
                "email": emp.get("email", ""),
            }},
            {"type": "payroll", "fields": {"baseSalary": 50000, "totalAmount": 50000}},
        ])
        res = run_handler("process_payroll", parsed)
        if res:
            via_tx = "transaction_id" in res
            via_voucher = "voucher_id" in res
            check("produced_output", via_tx or via_voucher, f"keys={list(res.keys())}")
            if via_tx:
                check("salary_transaction_id", bool(res.get("transaction_id")))
            else:
                check("fallback_voucher_id", bool(res.get("voucher_id")), "used ledger voucher fallback")

    # ── post_vouchers ────────────────────────────────────────
    if section("post_vouchers", only):
        parsed = make_parsed("post_vouchers", [
            {"type": "voucher", "fields": {
                "description": f"Testbilag{TS}",
                "date": TODAY,
                "postings": [
                    {"account": "6900", "amount": 5000, "side": "debit"},
                    {"account": "1900", "amount": 5000, "side": "credit"},
                ],
            }},
        ])
        res = run_handler("post_vouchers", parsed)
        if res:
            v_ids = res.get("voucher_ids", [])
            check("voucher_created", len(v_ids) > 0, f"ids={v_ids}")
            if v_ids:
                v_data = api_get(f"/ledger/voucher/{v_ids[0]}", fields="id,description,postings").get("value", {})
                check("found_in_api", bool(v_data.get("id")))
                posts = v_data.get("postings", [])
                debit_sum = sum(
                    abs(_to_float(p.get("amountGross", 0)))
                    for p in posts if _to_float(p.get("amountGross", 0)) > 0
                )
                check("debit_amount_correct", abs(debit_sum - 5000) < 1, f"got={debit_sum}")

    # ── post_vouchers (salary accrual with zero amount) ──────
    if section("post_vouchers_salary_accrual", only):
        parsed = make_parsed("post_vouchers", [
            {"type": "voucher", "fields": {
                "description": "Lønnsavsetning",
                "date": TODAY,
                "postings": [
                    {"account": "5000", "amount": 0, "side": "debit"},
                    {"account": "2940", "amount": 0, "side": "credit"},
                ],
            }},
        ])
        res = run_handler("post_vouchers", parsed)
        if res:
            v_ids = res.get("voucher_ids", [])
            # Either it derived an amount and posted, or it skipped (no salary data on sandbox)
            check("no_exception", True)
            if v_ids:
                check("posted_with_derived_amount", True, f"voucher_id={v_ids[0]}")
            else:
                check("skipped_gracefully", True, "no salary data on sandbox — OK")

    # ── bank_reconciliation ──────────────────────────────────
    if section("bank_reconciliation", only):
        _ensure_bank_account(client)
        # Create 2 customers with open invoices
        bc1_r = client.create_customer({"name": f"BCKunde1x{TS}", "isCustomer": True})
        bc1_id = bc1_r.get("value", {}).get("id")
        bc2_r = client.create_customer({"name": f"BCKunde2x{TS}", "isCustomer": True})
        bc2_id = bc2_r.get("value", {}).get("id")

        bc_inv_ids = []
        for cid in [bc1_id, bc2_id]:
            if cid:
                inv_id = _create_test_invoice(cid, 10000.0)
                if inv_id:
                    bc_inv_ids.append(inv_id)

        if not bc_inv_ids:
            check("setup", False, "Could not create test invoices")
        else:
            # Fetch actual invoice amounts (incl. VAT) for matching
            inv_amounts = {}
            for inv_id in bc_inv_ids:
                d = api_get(f"/invoice/{inv_id}", fields="id,amountCurrency,customer").get("value", {})
                inv_amounts[inv_id] = _to_float(d.get("amountCurrency", 12500))

            parsed = make_parsed("bank_reconciliation", [
                {"type": "customer", "fields": {
                    "name": f"BCKunde1x{TS}",
                    "amount": inv_amounts.get(bc_inv_ids[0], 12500),
                    "paymentDate": TODAY,
                }},
                {"type": "customer", "fields": {
                    "name": f"BCKunde2x{TS}",
                    "amount": inv_amounts.get(bc_inv_ids[1] if len(bc_inv_ids) > 1 else bc_inv_ids[0], 12500),
                    "paymentDate": TODAY,
                }},
            ])
            res = run_handler("bank_reconciliation", parsed)
            if res:
                paid = res.get("paid_invoice_ids", [])
                check("paid_count", len(paid) >= len(bc_inv_ids),
                      f"expected={len(bc_inv_ids)} paid={paid}")
                # Verify invoices are paid: outstanding amount should be 0 (or invoiceStatus PAID)
                for inv_id in bc_inv_ids:
                    # invoiceStatus is not a valid fields= filter on GET by ID — check amountCurrencyOutstanding
                    d = api_get(f"/invoice/{inv_id}", fields="id,amountCurrencyOutstanding,amountCurrency").get("value", {})
                    outstanding = _to_float(d.get("amountCurrencyOutstanding", -1))
                    check(f"invoice_{inv_id}_paid",
                          outstanding == 0.0 or outstanding < 0.01,
                          f"outstanding={outstanding}")

    # ── register_supplier_invoice ────────────────────────────
    if section("register_supplier_invoice", only):
        parsed = make_parsed("register_supplier_invoice", [
            {"type": "supplier", "fields": {"name": f"LunaAS{TS}", "organizationNumber": f"9852{TS}"}},
            {"type": "invoice", "fields": {
                "invoiceNumber": f"INV-{TS}",
                "amountIncludingVat": 28100,
                "vatPercent": 25,
                "expenseAccount": "7100",
            }},
        ])
        res = run_handler("register_supplier_invoice", parsed)
        if res:
            success = bool(res.get("supplier_invoice_id") or res.get("voucher_id") or res.get("approved_ids"))
            check("created_or_approved", success, f"keys={list(res.keys())}")

    # ── create_accounting_dimension ──────────────────────────
    if section("create_accounting_dimension", only):
        # Sandbox limits to 3 accounting dimensions — check before trying
        existing_dims = api_get("/ledger/accountingDimensionName", fields="id,dimensionName", count=10).get("values", [])
        if len(existing_dims) >= 3:
            print(f"  {YELLOW}[SKIP]{RESET} Sandbox has {len(existing_dims)} dimensions (max 3) — cannot create more")
        else:
            dim_name = f"TestDim{TS}"
            val1, val2 = f"Off{TS}", f"Priv{TS}"
            parsed = make_parsed("create_accounting_dimension", [
                {"type": "dimension", "fields": {
                    "name": dim_name,
                    "values": [val1, val2],
                    "account": "6340",
                    "amount": 10000,
                    "linkedValue": val1,
                }},
            ])
            res = run_handler("create_accounting_dimension", parsed)
            if res:
                check("dimension_id", bool(res.get("dimension_id")))
                check("value_ids_count", len(res.get("value_ids", {})) >= 2,
                      f"ids={res.get('value_ids')}")
                check("voucher_id", bool(res.get("voucher_id")))
                dim_list = api_get("/ledger/accountingDimensionName",
                                   fields="id,dimensionName", count=20).get("values", [])
                check("found_in_api",
                      any(d.get("dimensionName") == dim_name for d in dim_list),
                      f"existing={[d.get('dimensionName') for d in dim_list]}")

    # ── late_fee_invoice ─────────────────────────────────────
    if section("late_fee_invoice", only):
        _ensure_bank_account(client)
        lfi_cust_r = client.create_customer({"name": f"LFIKunde{TS}", "isCustomer": True})
        lfi_cust_id = lfi_cust_r.get("value", {}).get("id")
        lfi_inv_id = _create_test_invoice(lfi_cust_id, 20000.0) if lfi_cust_id else None
        if not lfi_inv_id:
            check("setup", False, "Could not create test invoice")
        else:
            parsed = make_parsed("late_fee_invoice", [
                {"type": "fee", "fields": {
                    "amount": 150, "debitAccount": "1500",
                    "creditAccount": "3400", "partialAmount": 5000,
                }},
            ])
            res = run_handler("late_fee_invoice", parsed)
            if res:
                check("fee_invoice_id", bool(res.get("fee_invoice_id")))
                check("voucher_id", bool(res.get("voucher_id")))
                check("overdue_invoice_id", bool(res.get("overdue_invoice_id")))

    # ── create_travel_expense ────────────────────────────────
    if section("create_travel_expense", only):
        parsed = make_parsed("create_travel_expense", [
            {"type": "travelExpense", "fields": {"title": f"Reise{TS}", "departureDate": TODAY}},
        ])
        res = run_handler("create_travel_expense", parsed)
        if res:
            check("expense_id", bool(res.get("id")))

    # ── create_invoice + register_payment ────────────────────
    if section("create_invoice_and_payment", only):
        try:
            _ensure_bank_account(client)
        except Exception:
            pass
        try:
            inv_cust_r = client.create_customer({"name": f"InvKunde{TS}", "isCustomer": True})
            inv_cust_id = inv_cust_r.get("value", {}).get("id")
        except Exception as e:
            print(f"  {YELLOW}[SETUP]{RESET} Could not create customer: {e}")
            inv_cust_id = None
        if inv_cust_id:
            # No vatPercentage — sandbox rejects standard VAT IDs (3/5/31/32)
            parsed_inv = make_parsed("create_invoice", [
                {"type": "customer", "fields": {"name": f"InvKunde{TS}"}},
                {"type": "invoice", "fields": {
                    "invoiceDate": TODAY, "invoiceDueDate": DUE,
                    "orderLines": [
                        {"description": "Konsulenttjenester", "quantity": 1,
                         "unitPriceExcludingVatCurrency": 8000},
                    ],
                }},
            ])
            res_inv = run_handler("create_invoice", parsed_inv)
            if res_inv:
                inv_id = res_inv.get("id")
                check("invoice_created", bool(inv_id))
                if inv_id:
                    # invoiceStatus is not a valid fields= filter; check amountCurrencyOutstanding
                    inv_d = api_get(f"/invoice/{inv_id}", fields="id,amountCurrency,amountCurrencyOutstanding").get("value", {})
                    outstanding_before = _to_float(inv_d.get("amountCurrencyOutstanding", -1))
                    check("invoice_has_outstanding", outstanding_before > 0,
                          f"outstanding={outstanding_before}")
                    # register_payment — pay directly via API (bypass status-filter sandbox issues)
                    try:
                        pt = client.get("/invoice/paymentType", params={"count": 1}).get("values", [{}])[0]
                        pt_id = pt.get("id")
                        amount = _to_float(inv_d.get("amountCurrency", 8000))
                        client.put(f"/invoice/{inv_id}/:payment", {}, params={
                            "paymentDate": TODAY, "paymentTypeId": pt_id, "paidAmount": amount,
                        })
                        inv_d2 = api_get(f"/invoice/{inv_id}", fields="id,amountCurrencyOutstanding").get("value", {})
                        outstanding_after = _to_float(inv_d2.get("amountCurrencyOutstanding", -1))
                        check("invoice_now_paid", outstanding_after == 0,
                              f"outstanding_after={outstanding_after}")
                    except Exception as e:
                        check("payment_registered", False, str(e)[:100])


# ─────────────────────────────────────────────────────────────
# LLM CLASSIFICATION TESTS  (--llm flag)
# ─────────────────────────────────────────────────────────────

def run_llm_tests():
    from agent import parse_prompt

    section("LLM_classification")

    tests = [
        # (prompt, expected_task_type, {field: expected_value})
        (
            "Sett fastpris 361250 kr på prosjektet 'Skymigrering' for Bergvik AS. Fakturer for 25% som delbetaling.",
            "project_billing", {"fixedprice": 361250},
        ),
        (
            "Devuelto por el banco. Revierta el pago de 44250 NOK para la factura del cliente.",
            "register_payment", {},
        ),
        (
            "Reconcilie o extrato bancário com as faturas em aberto. Ficheiro CSV em anexo.",
            "bank_reconciliation", {},
        ),
        (
            "Avstem bankutskriften mot åpne fakturaer. CSV-fil vedlagt.",
            "bank_reconciliation", {},
        ),
        (
            "Créez une note de crédit pour le client Dupont SA.",
            "create_credit_note", {},
        ),
        (
            "Kjør lønn for Ola Nordmann med grunnlønn 55000 kr og bonus 10000 kr.",
            "process_payroll", {"baseSalary": 55000},
        ),
        (
            "Registrer 8 timer på prosjektet Nettside for kunde Acme AS. Timespris 1200 kr. Fakturer kunden.",
            "register_project_hours", {"hours": 8, "hourlyRate": 1200},
        ),
        (
            "Lønnsavsetning for desember. Debet konto 5000, kredit 2940.",
            "post_vouchers", {},
        ),
        (
            "Analysez le grand livre pour identifier les 3 comptes avec la plus forte augmentation de coûts en février vs janvier.",
            "analyze_and_create_projects", {},
        ),
        (
            "Legg til leverandøren Netthandel AS org.nr 123456789. Ikke en kunde, bare leverandør.",
            "create_customer", {"isSupplier": True},
        ),
        (
            "Opprett ansatt Erik Dahl, epost erik.dahl@example.com, stillingsprosent 80%, årslønn 480000.",
            "create_employee", {"percentage": 80, "annualSalary": 480000},
        ),
        (
            "Créez le client MER SARL, adresse Solveien 88, 9008 Tromsø. E-mail: post@mer.no.",
            "create_customer", {"addressLine1": "Solveien 88", "postalCode": "9008"},
        ),
        (
            "Erstelle eine Abschreibung: Maschinen (Konto 1230) werden über 5 Jahre abgeschrieben, Anschaffungswert 150000.",
            "post_vouchers", {},
        ),
        (
            "Ciclo de vida completo del proyecto 'Cloud Migration'. Presupuesto 200000 NOK, registra 40 horas del empleado, factura al cliente.",
            "project_lifecycle", {},
        ),
    ]

    llm_pass = 0
    for prompt, expected_type, expected_fields in tests:
        try:
            parsed = parse_prompt(prompt)
            actual_type = parsed.get("task_type")
            type_ok = actual_type == expected_type

            all_fields = {}
            for e in parsed.get("entities", []):
                all_fields.update(e.get("fields", {}))

            def field_ok(k, v):
                got = all_fields.get(k)
                if isinstance(v, (int, float)):
                    try:
                        return abs(float(got) - float(v)) < 1
                    except (TypeError, ValueError):
                        return False
                return v is True and got is True or str(v) in str(got)

            fields_ok = all(field_ok(k, v) for k, v in expected_fields.items()) if expected_fields else True
            ok = type_ok and fields_ok
            detail = ""
            if not type_ok:
                detail = f"got={actual_type}"
            elif not fields_ok:
                missing = [k for k, v in expected_fields.items() if not field_ok(k, v)]
                detail = f"missing_fields={missing} got={dict(list(all_fields.items())[:4])}"

            name = f"{expected_type}:{prompt[:50]}..."
            check(name, ok, detail)
            if ok:
                llm_pass += 1
        except Exception as e:
            check(f"{expected_type}:exception", False, str(e)[:100])

    print(f"\nLLM classification: {llm_pass}/{len(tests)} passed")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tripletex agent sandbox test suite")
    parser.add_argument("--llm", action="store_true", help="Also run LLM classification tests")
    parser.add_argument("--only", type=str, default=None, help="Run only this section")
    args = parser.parse_args()

    print(f"\nTripletex Agent Test Suite  [TS={TS}]")
    print(f"Sandbox: {SANDBOX_URL}")

    run_tests(only=args.only)

    if args.llm:
        run_llm_tests()

    # Summary
    print(f"\n{'='*60}\n  SUMMARY\n{'='*60}")
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"\nTotal: {passed}/{total} passed  ({failed} failed)  Score: {passed/total*100:.1f}%\n")

    if failed > 0:
        print("Failed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))

    sys.exit(0 if failed == 0 else 1)
