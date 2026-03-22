"""Task-specific handlers — one per task type."""

import logging
import re
from datetime import date
from tripletex_client import TripletexClient

logger = logging.getLogger(__name__)


def handle_create_employee(parsed: dict, client: TripletexClient) -> dict:
    needs_admin = "admin" in parsed.get("notes", "").lower()
    dept_id = _get_default_department_id(client)
    created = []

    for entity in _get_entities(parsed, "employee"):
        fields = entity.get("fields", {})
        has_email = bool(fields.get("email"))
        # Tripletex requires email for STANDARD/EXTENDED users — use NO_ACCESS if no email
        user_type = "EXTENDED" if needs_admin else ("STANDARD" if has_email else "NO_ACCESS")
        payload = {"userType": user_type}
        for key in ("firstName", "lastName", "email", "phoneNumberMobile",
                    "employeeNumber", "dateOfBirth", "nationalIdentityNumber"):
            if key in fields:
                payload[key] = fields[key]

        # Resolve department: explicit id, named department entity, or default
        dept_entity = _get_entity(parsed, "department")
        resolved_dept_id = None
        if "departmentId" in fields:
            resolved_dept_id = int(fields["departmentId"])
        elif dept_entity and dept_entity.get("fields", {}).get("name"):
            dept_name = dept_entity["fields"]["name"]
            depts = client.get("/department", params={"name": dept_name, "fields": "id", "count": 1}).get("values", [])
            if depts:
                resolved_dept_id = depts[0]["id"]
        if not resolved_dept_id:
            resolved_dept_id = dept_id
        if resolved_dept_id:
            payload["department"] = {"id": resolved_dept_id}

        result = client.create_employee(payload)
        employee_id = result.get("value", {}).get("id")

        if employee_id:
            start_date = fields.get("startDate", fields.get("dateOfEmployment", date.today().isoformat()))
            try:
                # Tripletex requires dateOfBirth before employment can be created
                try:
                    emp_data = client.get(f"/employee/{employee_id}", params={"fields": "*"}).get("value", {})
                    if not emp_data.get("dateOfBirth"):
                        emp_data["dateOfBirth"] = "1980-01-01"
                        client.put(f"/employee/{employee_id}", emp_data)
                except Exception as e_dob:
                    logger.warning(f"Could not set dateOfBirth: {e_dob}")

                # Get division ID (virksomhet) for employment link — creates one if none exists
                division_id = _ensure_division(client)
                emp_payload = {
                    "employee": {"id": employee_id},
                    "startDate": start_date,
                }
                if division_id:
                    emp_payload["division"] = {"id": division_id}
                result_emp = client.post("/employee/employment", emp_payload)
                employment_id = result_emp.get("value", {}).get("id")

                # Set employment details (percentage, salary) via separate endpoint
                emp_entity = _get_entity(parsed, "employment")
                if emp_entity and employment_id:
                    ef = emp_entity.get("fields", {})
                    details_payload = {
                        "employment": {"id": employment_id},
                        "date": start_date,
                        "remunerationType": "MONTHLY_WAGE",
                    }
                    if ef.get("percentage"):
                        details_payload["percentageOfFullTimeEquivalent"] = _to_float(ef["percentage"])
                    if ef.get("annualSalary"):
                        details_payload["annualSalary"] = _to_float(ef["annualSalary"])
                    if ef.get("hoursPerDay"):
                        details_payload["shiftDurationHours"] = _to_float(ef["hoursPerDay"])
                    if ef.get("occupationCode"):
                        try:
                            oc_code = str(ef["occupationCode"])
                            oc_r = client.get("/employee/employment/occupationCode", params={"code": oc_code, "fields": "id,code", "count": 1})
                            oc_id = (oc_r.get("values") or [{}])[0].get("id")
                            if oc_id:
                                details_payload["occupationCode"] = {"id": oc_id}
                        except Exception as oc_e:
                            logger.warning(f"Could not resolve occupationCode: {oc_e}")
                    try:
                        client.post("/employee/employment/details", details_payload)
                    except Exception as e2:
                        logger.warning(f"Could not set employment details: {e2}")
            except Exception as e:
                logger.warning(f"Could not create employment: {e}")

        if needs_admin and employee_id:
            _assign_admin_role(client, employee_id)

        created.append(employee_id)

    return {"created": "employees", "ids": created}


def handle_create_customer(parsed: dict, client: TripletexClient) -> dict:
    created = []
    for entity in _get_entities(parsed, "customer"):
        fields = entity.get("fields", {})
        # If explicitly only a supplier (isSupplier=True, isCustomer not set), don't force isCustomer=True
        is_supplier_only = fields.get("isSupplier", False) and "isCustomer" not in fields
        payload = {"isCustomer": fields.get("isCustomer", not is_supplier_only)}
        for key in ("name", "email", "phoneNumber", "organizationNumber", "isSupplier"):
            if key in fields:
                payload[key] = fields[key]
        if fields.get("addressLine1") or fields.get("postalCode") or fields.get("city"):
            payload["physicalAddress"] = {
                "addressLine1": fields.get("addressLine1", ""),
                "postalCode": fields.get("postalCode", ""),
                "city": fields.get("city", ""),
                "country": {"id": 161},
            }
        result = client.create_customer(payload)
        created.append(result.get("value", {}).get("id"))
    return {"created": "customers", "ids": created}


def handle_create_product(parsed: dict, client: TripletexClient) -> dict:
    created = []
    for entity in _get_entities(parsed, "product"):
        fields = entity.get("fields", {})
        payload = {}
        for key in ("name", "unit"):
            if key in fields:
                payload[key] = fields[key]
        # number must be string
        if "number" in fields:
            payload["number"] = str(fields["number"])
        # numeric price fields — strip currency suffix and convert to float
        for key in ("costExcludingVatCurrency", "priceExcludingVatCurrency", "priceIncludingVatCurrency"):
            if key in fields:
                payload[key] = _to_float(fields[key])

        # VAT type — look up by percentage if specified
        vat_pct = fields.get("vatPercentage")
        vat_id = fields.get("vatTypeId")
        if vat_id:
            payload["vatType"] = {"id": int(vat_id)}
        elif vat_pct is not None:
            resolved = _find_vat_type_id(client, float(vat_pct))
            if resolved:
                payload["vatType"] = {"id": resolved}

        result = client.create_product(payload)
        created.append(result.get("value", {}).get("id"))
    return {"created": "products", "ids": created}


def handle_create_department(parsed: dict, client: TripletexClient) -> dict:
    created = []
    for entity in _get_entities(parsed, "department"):
        fields = entity.get("fields", {})
        payload = {}
        for key in ("name", "departmentNumber"):
            if key in fields:
                payload[key] = fields[key]
        result = client.create_department(payload)
        created.append(result.get("value", {}).get("id"))
    return {"created": "departments", "ids": created}


def handle_create_project(parsed: dict, client: TripletexClient) -> dict:
    today = date.today().isoformat()
    created = []
    default_manager_id = None
    default_customer_id = None

    # Look for project manager in employee entities
    emp_entity = _get_entities(parsed, "employee")
    emp_fields = emp_entity[0].get("fields", {}) if emp_entity and emp_entity[0].get("type", "").lower() == "employee" else {}
    if emp_fields:
        emp_name = f"{emp_fields.get('firstName', '')} {emp_fields.get('lastName', '')}".strip()
        emp_email = emp_fields.get("email", "")
        # Try email first (single precise GET), fall back to name search
        if emp_email:
            default_manager_id = _find_employee_by_email(client, emp_email)
        if not default_manager_id and emp_name:
            default_manager_id = _find_employee_id(client, emp_name)

    # Look for customer in customer entities
    cust_entity = _get_entities(parsed, "customer")
    cust_fields = cust_entity[0].get("fields", {}) if cust_entity and cust_entity[0].get("type", "").lower() == "customer" else {}
    if cust_fields:
        default_customer_id = (
            cust_fields.get("id")
            or _find_customer_by_org(client, cust_fields.get("organizationNumber", ""))
            or _find_customer_id(client, cust_fields.get("name", ""))
        )
        # Create customer if not found
        if not default_customer_id and cust_fields.get("name"):
            cpayload = {"isCustomer": True, "name": cust_fields["name"]}
            for k in ("email", "phoneNumber", "organizationNumber"):
                if k in cust_fields:
                    cpayload[k] = cust_fields[k]
            r = client.create_customer(cpayload)
            default_customer_id = r.get("value", {}).get("id")

    for entity in _get_entities(parsed, "project"):
        fields = entity.get("fields", {})
        payload = {}
        for key in ("name", "number", "startDate", "endDate"):
            if key in fields:
                payload[key] = fields[key]
        # startDate is required — default to today
        if "startDate" not in payload:
            payload["startDate"] = today

        # projectManager — prefer explicit field, then employee entity, then first employee
        if "projectManagerId" in fields:
            payload["projectManager"] = {"id": fields["projectManagerId"]}
        elif "projectManager" in fields and isinstance(fields["projectManager"], dict):
            payload["projectManager"] = fields["projectManager"]
        else:
            mgr_id = default_manager_id or _get_default_employee_id(client)
            if mgr_id:
                payload["projectManager"] = {"id": mgr_id}

        # customer — prefer explicit field, then customer entity
        if "customerId" in fields:
            payload["customer"] = {"id": fields["customerId"]}
        elif default_customer_id:
            payload["customer"] = {"id": default_customer_id}

        result = client.create_project(payload)
        created.append(result.get("value", {}).get("id"))
    return {"created": "projects", "ids": created}


def handle_update_employee(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "employee")
    fields = entity.get("fields", {})
    employee_id = fields.pop("id", None)

    if not employee_id:
        name = f"{fields.get('firstName', '')} {fields.get('lastName', '')}".strip()
        employee_id = _find_employee_id(client, name)

    if not employee_id:
        raise ValueError("Could not determine employee ID for update")

    current = client.get_employee(employee_id, fields="*")
    current.update({k: v for k, v in fields.items() if v is not None})
    client.update_employee(employee_id, current)
    return {"updated": "employee", "id": employee_id}


def handle_update_customer(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "customer")
    fields = entity.get("fields", {})
    customer_id = fields.pop("id", None)

    if not customer_id:
        customer_id = _find_customer_id(client, fields.get("name", ""))

    if not customer_id:
        raise ValueError("Could not determine customer ID for update")

    result = client.get(f"/customer/{customer_id}", params={"fields": "*"})
    current = result.get("value", {})
    current.update({k: v for k, v in fields.items() if v is not None})
    client.put(f"/customer/{customer_id}", current)
    return {"updated": "customer", "id": customer_id}


def handle_update_product(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "product")
    fields = entity.get("fields", {})
    product_id = fields.pop("id", None)

    if not product_id:
        product_id = _find_product_id(client, fields.get("name", ""))

    if not product_id:
        raise ValueError("Could not determine product ID for update")

    result = client.get(f"/product/{product_id}", params={"fields": "*"})
    current = result.get("value", {})
    current.update({k: v for k, v in fields.items() if v is not None})
    client.put(f"/product/{product_id}", current)
    return {"updated": "product", "id": product_id}


def handle_delete_employee(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "employee")
    fields = entity.get("fields", {})
    name = f"{fields.get('firstName', '')} {fields.get('lastName', '')}".strip()
    employee_id = fields.get("id") or _find_employee_id(client, name)
    if employee_id:
        client.delete(f"/employee/{employee_id}")
    return {"deleted": "employee", "id": employee_id}


def handle_delete_customer(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "customer")
    fields = entity.get("fields", {})
    customer_id = fields.get("id") or _find_customer_id(client, fields.get("name", ""))
    if customer_id:
        client.delete(f"/customer/{customer_id}")
    return {"deleted": "customer", "id": customer_id}


def handle_delete_product(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "product")
    fields = entity.get("fields", {})
    product_id = fields.get("id") or _find_product_id(client, fields.get("name", ""))
    if product_id:
        client.delete(f"/product/{product_id}")
    return {"deleted": "product", "id": product_id}


def handle_assign_admin_role(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "employee")
    fields = entity.get("fields", {})
    name = f"{fields.get('firstName', '')} {fields.get('lastName', '')}".strip()
    employee_id = _find_employee_id(client, name)
    if not employee_id:
        raise ValueError(f"Employee not found: {name}")
    _assign_admin_role(client, employee_id)
    return {"assigned": "admin_role", "employee_id": employee_id}


def handle_create_invoice(parsed: dict, client: TripletexClient) -> dict:
    """Create customer + order + invoice."""
    today = date.today().isoformat()
    from datetime import timedelta
    due_default = (date.today() + timedelta(days=30)).isoformat()

    # Invoice entity fields
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}
    invoice_date = inv_fields.get("invoiceDate", today)
    due_date = inv_fields.get("invoiceDueDate", inv_fields.get("dueDate", due_default))

    # Get or create customer
    customer_entities = _get_entities(parsed, "customer")
    customer_id = None
    if customer_entities:
        cf = customer_entities[0].get("fields", {})
        customer_id = cf.get("id")
        if not customer_id and cf.get("organizationNumber"):
            customer_id = _find_customer_by_org(client, cf["organizationNumber"])
        if not customer_id and cf.get("name"):
            customer_id = _find_customer_id(client, cf["name"])
        if not customer_id and cf.get("name"):
            payload = {"isCustomer": True, "name": cf["name"]}
            for k in ("email", "phoneNumber", "organizationNumber"):
                if k in cf:
                    payload[k] = cf[k]
            r = client.create_customer(payload)
            customer_id = r.get("value", {}).get("id")

    if not customer_id:
        raise ValueError("No customer found or created for invoice")

    _ensure_bank_account(client)

    # Build order lines — try product lookup by number, fall back to description
    order_lines = []
    raw_lines = inv_fields.get("orderLines", [])
    if raw_lines:
        for line in raw_lines:
            ol = {"count": _to_float(line.get("quantity", line.get("count", 1)))}
            product_number = str(line.get("productNumber", line.get("number", ""))).strip()
            product_id = None
            if product_number and product_number != "None":
                try:
                    r = client.get("/product", params={"number": product_number, "fields": "id", "count": 1})
                    vals = r.get("values", [])
                    if vals:
                        product_id = vals[0]["id"]
                except Exception:
                    pass
            if product_id:
                ol["product"] = {"id": product_id}
            else:
                desc = line.get("description") or (line.get("product", {}) or {}).get("name", "")
                if desc:
                    ol["description"] = desc
            price = line.get("unitPriceExcludingVatCurrency") or (line.get("product", {}) or {}).get("priceExcludingVatCurrency")
            if price:
                ol["unitPriceExcludingVatCurrency"] = _to_float(price)
            # Set vatType on description-only lines (product lines carry their own vatType)
            if not product_id:
                line_vat = line.get("vatPercentage")
                if line_vat is None:
                    line_vat = inv_fields.get("vatPercentage")
                if line_vat is not None:
                    vat_id = _find_vat_type_id(client, float(line_vat))
                    if vat_id:
                        ol["vatType"] = {"id": vat_id}
            order_lines.append(ol)

    # Create order — deliveryDate is required
    order_payload = {
        "customer": {"id": customer_id},
        "orderDate": invoice_date,
        "deliveryDate": due_date,
    }
    if order_lines:
        order_payload["orderLines"] = order_lines

    order_result = client.create_order(order_payload)
    order_id = order_result.get("value", {}).get("id")
    if not order_id:
        raise ValueError("Failed to create order")

    # Create invoice from order
    invoice_result = client.create_invoice({
        "invoiceDate": invoice_date,
        "invoiceDueDate": due_date,
        "customer": {"id": customer_id},
        "orders": [{"id": order_id}],
    })
    invoice_id = invoice_result.get("value", {}).get("id")
    return {"created": "invoice", "id": invoice_id, "order_id": order_id}


def handle_register_payment(parsed: dict, client: TripletexClient) -> dict:
    """Find an unpaid invoice and register full payment against it."""
    today = date.today().isoformat()

    invoice_id = None

    customer_entities = _get_entities(parsed, "customer")
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}

    # Amount from parsed invoice fields (several field names Gemini may use)
    raw_amount = (inv_fields.get("amount") or inv_fields.get("amountRemainingCurrency")
                  or inv_fields.get("amountExcludingVat") or inv_fields.get("amountIncludingVat"))
    paid_amount = _to_float(raw_amount) if raw_amount else None

    # Detect payment reversal from multiple signals:
    # 1) negative parsed amount, 2) notes mention reversal, 3) api_sequence uses DELETE
    notes_lower = parsed.get("notes", "").lower()
    api_methods = [a.get("method", "").upper() for a in parsed.get("api_sequence", [])]
    reversal_keywords = ("revers", "stornieren", "zurückbuch", "reverta", "tilbakeføre",
                         "annuler", "returned by the bank", "bank returned", "cancel",
                         "devuelto", "revierta", "zurückgesendet", "renvoyé", "retornado",
                         "returnert", "bounce")
    is_reversal = (
        (paid_amount is not None and paid_amount < 0)
        or any(kw in notes_lower for kw in reversal_keywords)
        or "DELETE" in api_methods
    )

    # Find or create customer
    customer_id = None
    if customer_entities:
        cf = customer_entities[0].get("fields", {})
        name = cf.get("name", "")
        org_no = cf.get("organizationNumber", "")
        if org_no:
            customer_id = _find_customer_by_org(client, org_no)
        if not customer_id and name:
            customer_id = _find_customer_id(client, name)
        # Create customer if still not found
        if not customer_id and (name or org_no):
            cpayload = {"isCustomer": True}
            if name:
                cpayload["name"] = name
            if org_no:
                cpayload["organizationNumber"] = org_no
            for k in ("email", "phoneNumber"):
                if k in cf:
                    cpayload[k] = cf[k]
            r = client.create_customer(cpayload)
            customer_id = r.get("value", {}).get("id")

    # Get payment type
    pt_result = client.get("/invoice/paymentType", params={"count": 1})
    payment_types = pt_result.get("values", [])
    if not payment_types:
        raise ValueError("No payment types available")
    payment_type_id = payment_types[0]["id"]

    # Find invoice — date params required by competition accounts
    from datetime import timedelta
    date_from = "2020-01-01"
    date_to = (date.today() + timedelta(days=365)).isoformat()
    base_params = {
        "invoiceDateFrom": date_from,
        "invoiceDateTo": date_to,
        "fields": "id,amountCurrency,amountCurrencyOutstanding",
        "count": 10,
    }
    if customer_id:
        base_params["customerId"] = customer_id

    # Try OPEN first; for payment reversals, fall back to PAID
    invoices = []
    for status in ("OPEN", "PAID"):
        params = {**base_params, "invoiceStatus": status}
        inv_result = client.get("/invoice", params=params)
        invoices = inv_result.get("values", [])
        if invoices:
            break

    if not invoices:
        raise ValueError("No invoice found to pay")

    invoice = invoices[0]
    invoice_id = invoice["id"]
    # Always use the actual outstanding amount from the invoice (includes VAT correctly)
    # Ignore parsed prompt amount — it may be excl. VAT while invoice total includes VAT
    if is_reversal:
        paid_amount = -(invoice.get("amountCurrency", abs(paid_amount or 0)))
    else:
        paid_amount = invoice.get("amountCurrencyOutstanding") or invoice.get("amountCurrency", 0)

    # Register payment
    client.put(
        f"/invoice/{invoice_id}/:payment",
        {},
        params={
            "paymentDate": today,
            "paymentTypeId": payment_type_id,
            "paidAmount": paid_amount,
        },
    )

    # Post FX difference vouchers if Gemini extracted them (agio/disagio)
    fx_voucher_ids = []
    voucher_entities = _get_entities(parsed, "voucher")
    for vou in voucher_entities:
        vf = vou.get("fields", {})
        v_desc = vf.get("description", "Valutadifferanse")
        v_date = vf.get("date", today)
        v_postings = vf.get("postings", [])
        api_posts = []
        for j, vp in enumerate(v_postings):
            acct_no = str(vp.get("account", ""))
            r = client.get("/ledger/account", params={"number": acct_no, "fields": "id", "count": 1})
            v_acct_id = (r.get("values") or [{}])[0].get("id")
            if not v_acct_id:
                continue
            v_amt = _to_float(vp.get("amount") or 0)
            if not v_amt:
                continue
            v_side = vp.get("side", "debit")
            v_gross = v_amt if v_side == "debit" else -v_amt
            v_post = {"row": j + 1, "account": {"id": v_acct_id}, "amountGross": v_gross,
                      "amountGrossCurrency": v_gross, "description": v_desc, "date": v_date}
            # Account 1500 (receivables) requires customer reference
            if str(vp.get("account", "")) == "1500" and customer_id:
                v_post["customer"] = {"id": customer_id}
            api_posts.append(v_post)
        if len(api_posts) >= 2:
            try:
                vr = client.post("/ledger/voucher", {"date": v_date, "description": v_desc, "postings": api_posts})
                fx_voucher_ids.append(vr.get("value", {}).get("id"))
            except Exception as ve:
                logger.warning(f"Could not post FX voucher: {ve}")

    # Fallback: if no voucher entity but notes mention agio/disagio, auto-post FX difference voucher
    if not fx_voucher_ids:
        notes = parsed.get("notes", "")
        # Pattern 1: agio/disagio/currency diff followed by a number
        fx_match = re.search(
            r'(?:agio|disagio|currency diff|valutadiff|exchange rate diff|kursdiff|FX diff|forex diff)[^\d]*([0-9]+[.,][0-9]+)',
            notes, re.IGNORECASE
        )
        # Pattern 2: number followed by NOK/kr when notes mention agio/disagio anywhere
        if not fx_match:
            notes_l_check = notes.lower()
            if any(w in notes_l_check for w in ("agio", "disagio", "valutagevinst", "valutatap", "valutadiff")):
                fx_match = re.search(r'([0-9]+[.,][0-9]+)\s*(?:NOK|kr)\b', notes, re.IGNORECASE)
        logger.info(f"FX fallback: notes_snippet={notes[:80]!r}, fx_match={bool(fx_match)}")
        if fx_match:
            fx_amount = _to_float(fx_match.group(1))
            notes_l = notes.lower()
            is_gain = not any(w in notes_l for w in ("disagio", "loss", "tap", "valutatap")) and \
                      any(w in notes_l for w in ("agio", "gain", "gevinst", "valutagevinst"))
            logger.info(f"FX: amount={fx_amount}, is_gain={is_gain}, customer_id={customer_id}")
            if fx_amount and fx_amount > 0:
                try:
                    def _acct_id(number):
                        r = client.get("/ledger/account", params={"number": str(number), "fields": "id", "count": 1})
                        return (r.get("values") or [{}])[0].get("id")

                    a1500 = _acct_id(1500)
                    a8060 = _acct_id(8060)   # Valutagevinst (agio)
                    a8160 = _acct_id(8160)   # Valutatap (disagio)
                    logger.info(f"FX accounts: 1500={a1500}, 8060={a8060}, 8160={a8160}")
                    posts = []
                    if is_gain and a1500 and a8060:
                        p1 = {"row": 1, "account": {"id": a1500}, "amountGross": fx_amount,
                              "amountGrossCurrency": fx_amount, "description": "Valutadifferanse", "date": today}
                        if customer_id:
                            p1["customer"] = {"id": customer_id}
                        posts = [p1, {"row": 2, "account": {"id": a8060}, "amountGross": -fx_amount,
                                      "amountGrossCurrency": -fx_amount, "description": "Valutadifferanse", "date": today}]
                    elif not is_gain and a8160 and a1500:
                        p2 = {"row": 2, "account": {"id": a1500}, "amountGross": -fx_amount,
                              "amountGrossCurrency": -fx_amount, "description": "Valutadifferanse", "date": today}
                        if customer_id:
                            p2["customer"] = {"id": customer_id}
                        posts = [{"row": 1, "account": {"id": a8160}, "amountGross": fx_amount,
                                  "amountGrossCurrency": fx_amount, "description": "Valutadifferanse", "date": today}, p2]
                    if posts:
                        vr = client.post("/ledger/voucher", {"date": today, "description": "Valutadifferanse", "postings": posts})
                        fx_voucher_ids.append(vr.get("value", {}).get("id"))
                        logger.info(f"Auto-posted FX {'gain' if is_gain else 'loss'} voucher {fx_amount} NOK")
                except Exception as fe:
                    logger.warning(f"Could not auto-post FX voucher: {fe}")

    return {"registered": "payment", "invoice_id": invoice_id, "amount": paid_amount, "fx_voucher_ids": fx_voucher_ids}


def handle_create_travel_expense(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "travelExpense")
    fields = entity.get("fields", {})
    emp_entity = _get_entity(parsed, "employee")
    emp_fields = emp_entity.get("fields", {}) if emp_entity.get("type") else {}
    today = date.today().isoformat()

    payload = {
        "travelDetails": {"departureDate": fields.get("departureDate", today)},
    }
    if "title" in fields:
        payload["title"] = fields["title"]

    # Find employee: prefer email lookup, then name, then first available
    employee_id = fields.get("employeeId")
    if not employee_id and emp_fields.get("email"):
        employee_id = _find_employee_by_email(client, emp_fields["email"])
    if not employee_id:
        name = f"{emp_fields.get('firstName','')} {emp_fields.get('lastName','')}".strip()
        if name:
            employee_id = _find_employee_id(client, name)
    if not employee_id:
        employees = client.get_employees(fields="id")
        if employees:
            employee_id = employees[0]["id"]
    if employee_id:
        payload["employee"] = {"id": employee_id}

    result = client.post("/travelExpense", payload)
    expense_id = result.get("value", {}).get("id")
    return {"created": "travelExpense", "id": expense_id}


def handle_delete_travel_expense(parsed: dict, client: TripletexClient) -> dict:
    entity = _get_entity(parsed, "travelExpense")
    fields = entity.get("fields", {})
    expense_id = fields.get("id")

    if not expense_id:
        # Find by employee name or just grab the first one
        result = client.get("/travelExpense", params={"fields": "id", "count": 1})
        expenses = result.get("values", [])
        if expenses:
            expense_id = expenses[0]["id"]

    if expense_id:
        client.delete(f"/travelExpense/{expense_id}")
    return {"deleted": "travelExpense", "id": expense_id}


def handle_create_credit_note(parsed: dict, client: TripletexClient) -> dict:
    """Find an invoice and create a credit note for it."""
    customer_entities = _get_entities(parsed, "customer")
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}

    customer_id = None
    if customer_entities:
        cf = customer_entities[0].get("fields", {})
        customer_id = (
            cf.get("id")
            or _find_customer_by_org(client, cf.get("organizationNumber", ""))
            or _find_customer_id(client, cf.get("name", ""))
        )

    # Find invoice
    invoice_id = inv_fields.get("id")
    if not invoice_id:
        from datetime import timedelta
        params = {
            "fields": "id,amount",
            "count": 5,
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": (date.today() + timedelta(days=365)).isoformat(),
        }
        if customer_id:
            params["customerId"] = customer_id
        result = client.get("/invoice", params=params)
        invoices = result.get("values", [])
        if invoices:
            invoice_id = invoices[0]["id"]

    if not invoice_id:
        raise ValueError("No invoice found to credit")

    result = client.put(f"/invoice/{invoice_id}/:createCreditNote", {}, params={"date": __import__("datetime").date.today().isoformat()})
    credit_id = result.get("value", {}).get("id") if result else None
    return {"created": "credit_note", "credit_invoice_id": credit_id, "original_invoice_id": invoice_id}


def handle_create_invoice_with_payment(parsed: dict, client: TripletexClient) -> dict:
    """Create order → invoice → register full payment in one task."""
    today = date.today().isoformat()
    from datetime import timedelta
    due_default = (date.today() + timedelta(days=30)).isoformat()

    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}
    invoice_date = inv_fields.get("invoiceDate", today)
    due_date = inv_fields.get("invoiceDueDate", inv_fields.get("dueDate", due_default))

    # Get or create customer
    customer_entities = _get_entities(parsed, "customer")
    customer_id = None
    if customer_entities:
        cf = customer_entities[0].get("fields", {})
        customer_id = cf.get("id")
        if not customer_id and cf.get("organizationNumber"):
            customer_id = _find_customer_by_org(client, cf["organizationNumber"])
        if not customer_id and cf.get("name"):
            customer_id = _find_customer_id(client, cf["name"])
        if not customer_id and cf.get("name"):
            cpayload = {"isCustomer": True, "name": cf["name"]}
            for k in ("email", "phoneNumber", "organizationNumber"):
                if k in cf:
                    cpayload[k] = cf[k]
            r = client.create_customer(cpayload)
            customer_id = r.get("value", {}).get("id")

    if not customer_id:
        raise ValueError("No customer found or created for invoice_with_payment")

    _ensure_bank_account(client)

    # Build order lines — try to find product by number, fall back to description
    order_lines = []
    raw_lines = inv_fields.get("orderLines", [])
    parsed_total = _to_float(inv_fields.get("paymentAmount", 0))
    for line in raw_lines:
        ol = {"count": _to_float(line.get("quantity", line.get("count", 1)))}
        product_number = str(line.get("productNumber", line.get("number", ""))).strip()
        product_id = None
        if product_number:
            try:
                result = client.get("/product", params={"number": product_number, "fields": "id", "count": 1})
                vals = result.get("values", [])
                if vals:
                    product_id = vals[0]["id"]
            except Exception:
                pass
        if product_id:
            ol["product"] = {"id": product_id}
        else:
            desc = line.get("description") or (line.get("product", {}) or {}).get("name", "")
            if desc:
                ol["description"] = desc
        price = line.get("unitPriceExcludingVatCurrency") or (line.get("product", {}) or {}).get("priceExcludingVatCurrency")
        if price:
            ol["unitPriceExcludingVatCurrency"] = _to_float(price)
        # Set vatType on description-only lines (product lines carry their own vatType)
        if not product_id:
            line_vat = line.get("vatPercentage")
            if line_vat is None:
                line_vat = inv_fields.get("vatPercentage")
            if line_vat is not None:
                vat_id = _find_vat_type_id(client, float(line_vat))
                if vat_id:
                    ol["vatType"] = {"id": vat_id}
        order_lines.append(ol)

    # If no order lines parsed, nothing to invoice
    order_payload = {
        "customer": {"id": customer_id},
        "orderDate": invoice_date,
        "deliveryDate": due_date,
    }
    if order_lines:
        order_payload["orderLines"] = order_lines

    order_result = client.create_order(order_payload)
    order_id = order_result.get("value", {}).get("id")
    if not order_id:
        raise ValueError("Failed to create order")

    invoice_result = client.create_invoice({
        "invoiceDate": invoice_date,
        "invoiceDueDate": due_date,
        "customer": {"id": customer_id},
        "orders": [{"id": order_id}],
    })
    invoice_id = invoice_result.get("value", {}).get("id")
    if not invoice_id:
        raise ValueError("Failed to create invoice")

    # Get payment type and register payment
    pt_result = client.get("/invoice/paymentType", params={"fields": "id", "count": 1})
    payment_type_id = pt_result.get("values", [{}])[0].get("id")

    # Always prefer the actual invoice amount (includes VAT) over the parsed amount
    total_amount = invoice_result.get("value", {}).get("amountCurrency", 0)
    if not total_amount:
        # Fallback: parsed total from prompt (may be excl. VAT)
        total_amount = parsed_total
    if not total_amount:
        # Last resort: compute from order lines
        for line in raw_lines:
            qty = _to_float(line.get("quantity", line.get("count", 1)))
            price = line.get("unitPriceExcludingVatCurrency") or (line.get("product", {}) or {}).get("priceExcludingVatCurrency", 0)
            total_amount += qty * _to_float(price)

    client.put(
        f"/invoice/{invoice_id}/:payment",
        {},
        params={
            "paymentDate": today,
            "paymentTypeId": payment_type_id,
            "paidAmount": total_amount,
        },
    )
    return {"created": "invoice_with_payment", "invoice_id": invoice_id, "order_id": order_id, "amount": total_amount}


def handle_register_supplier_invoice(parsed: dict, client: TripletexClient) -> dict:
    """Register an incoming supplier invoice. Tries /supplierInvoice endpoint, falls back to /ledger/voucher."""
    today = date.today().isoformat()

    supplier_entities = _get_entities(parsed, "supplier") or _get_entities(parsed, "customer")
    supplier_fields = supplier_entities[0].get("fields", {}) if supplier_entities else {}
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}

    amount_incl = _to_float(inv_fields.get("amountIncludingVat", inv_fields.get("amount", 0)))
    amount_excl = _to_float(inv_fields.get("amountExcludingVat", 0))
    vat_pct = _to_float(inv_fields.get("vatPercent", inv_fields.get("vatPercentage", 25)))
    expense_account_no = str(inv_fields.get("expenseAccount") or "7100").strip()
    invoice_number = inv_fields.get("invoiceNumber", inv_fields.get("description", "Leverandørfaktura"))
    description = invoice_number
    invoice_date = inv_fields.get("invoiceDate", today)
    from datetime import timedelta
    due_date = inv_fields.get("invoiceDueDate", (date.today() + timedelta(days=30)).isoformat())

    # Calculate amounts
    if amount_incl and not amount_excl:
        amount_excl = round(amount_incl / (1 + vat_pct / 100), 2)
    elif amount_excl and not amount_incl:
        amount_incl = round(amount_excl * (1 + vat_pct / 100), 2)
    vat_amount = round(amount_incl - amount_excl, 2)

    # Find or create supplier — use /supplier endpoint (separate from /customer)
    supplier_id = None
    if supplier_fields:
        org_no = supplier_fields.get("organizationNumber", "")
        name = supplier_fields.get("name", "")

        # 1. Search /supplier by org number
        if org_no:
            sv = client.get("/supplier", params={"organizationNumber": org_no, "fields": "id", "count": 1}).get("values", [])
            if sv:
                supplier_id = sv[0]["id"]

        # 2. Search /supplier by name
        if not supplier_id and name:
            sv = client.get("/supplier", params={"name": name, "fields": "id", "count": 1}).get("values", [])
            if sv:
                supplier_id = sv[0]["id"]

        # 3. Also search /customer as fallback (some suppliers registered there)
        if not supplier_id and org_no:
            supplier_id = _find_customer_by_org(client, org_no)
        if not supplier_id and name:
            supplier_id = _find_customer_id(client, name)

        # 4. Create supplier via /supplier endpoint
        if not supplier_id and (name or org_no):
            try:
                spayload = {}
                if name:
                    spayload["name"] = name
                if org_no:
                    spayload["organizationNumber"] = org_no
                r = client.post("/supplier", spayload)
                supplier_id = r.get("value", {}).get("id")
            except Exception:
                # Fall back to customer endpoint
                cpayload = {"isSupplier": True, "isCustomer": False}
                if name:
                    cpayload["name"] = name
                if org_no:
                    cpayload["organizationNumber"] = org_no
                r = client.create_customer(cpayload)
                supplier_id = r.get("value", {}).get("id")

    # Check for pre-existing supplier invoices awaiting approval
    # Search broadly — by specific supplier first, then ALL to catch mismatched supplier IDs
    try:
        today_obj = date.today()
        date_from = (today_obj.replace(year=today_obj.year - 2)).isoformat()
        date_to = (today_obj.replace(year=today_obj.year + 2)).isoformat()

        existing_invoices = []
        # 1) Search by supplier_id if we have one
        if supplier_id:
            si_result = client.get("/supplierInvoice", params={
                "supplierId": supplier_id,
                "invoiceDateFrom": date_from,
                "invoiceDateTo": date_to,
                "fields": "id,amountCurrency,invoiceNumber,supplier(id,name)",
                "count": 10,
            })
            existing_invoices = si_result.get("values", [])

        # 2) If none found by supplier, search ALL supplier invoices and match by amount/invoice number
        if not existing_invoices:
            all_si = client.get("/supplierInvoice", params={
                "invoiceDateFrom": date_from,
                "invoiceDateTo": date_to,
                "fields": "id,amountCurrency,invoiceNumber,supplier(id,name)",
                "count": 50,
            }).get("values", [])
            logger.info(f"register_supplier_invoice: found {len(all_si)} total supplier invoices on account")
            if all_si:
                # Match by invoice number first
                inv_num = inv_fields.get("invoiceNumber", "")
                for si in all_si:
                    si_inv_num = si.get("invoiceNumber", "")
                    if inv_num and si_inv_num and inv_num.lower().strip() == si_inv_num.lower().strip():
                        existing_invoices = [si]
                        logger.info(f"Matched supplier invoice by number: {inv_num} → SI#{si.get('id')}")
                        break
                # Match by amount (incl VAT)
                if not existing_invoices and amount_incl:
                    for si in all_si:
                        si_amt = _to_float(si.get("amountCurrency", 0))
                        if abs(si_amt - amount_incl) < 1.0:
                            existing_invoices = [si]
                            logger.info(f"Matched supplier invoice by amount: {amount_incl} → SI#{si.get('id')}")
                            break
                # Last resort: if only 1 supplier invoice exists, use it
                if not existing_invoices and len(all_si) == 1:
                    existing_invoices = all_si
                    logger.info(f"Using sole supplier invoice SI#{all_si[0].get('id')}")

        if existing_invoices:
            # Approve pre-existing supplier invoices
            approved_ids = []
            for si in existing_invoices:
                si_id = si.get("id")
                if si_id:
                    try:
                        client.put(f"/supplierInvoice/{si_id}/:approve", {})
                        approved_ids.append(si_id)
                    except Exception as e:
                        logger.warning(f"Could not approve supplierInvoice {si_id}: {e}")
            if approved_ids:
                return {"created": "supplier_invoice_approved", "approved_ids": approved_ids, "supplier_id": supplier_id}
    except Exception as e:
        logger.warning(f"Could not check pre-existing supplier invoices: {e}")

    # Approach 1: Use /incomingInvoice endpoint (POST /supplierInvoice doesn't exist — only GET)
    try:
        def _find_account_id_local(number: str) -> int | None:
            r = client.get("/ledger/account", params={"number": number, "fields": "id,number", "count": 1})
            vals = r.get("values", [])
            return vals[0]["id"] if vals else None

        expense_id_local = _find_account_id_local(expense_account_no)
        # Incoming deductible VAT type IDs (standard Tripletex): 25%=1, 15%=11, 12%=12
        incoming_vat_ids = {25.0: 1, 15.0: 11, 12.0: 12}
        vat_type_id = incoming_vat_ids.get(float(vat_pct))
        if not vat_type_id:
            # Dynamic fallback for non-standard percentages
            try:
                vat_vals = client.get("/ledger/vatType", params={"fields": "id,name,percentage", "count": 100}).get("values", [])
                for v in vat_vals:
                    name_lower = v.get("name", "").lower()
                    pct = v.get("percentage", -999)
                    if abs(float(pct) - float(vat_pct)) < 0.01 and "fradrag" in name_lower and "inng" in name_lower:
                        vat_type_id = v["id"]
                        break
            except Exception:
                pass

        order_line = {
            "externalId": "line-1",  # required by incomingInvoice API
            "row": 1,
            "description": description,
            "amountInclVat": amount_incl,
            "count": 1,
        }
        if expense_id_local:
            order_line["accountId"] = expense_id_local
        if vat_type_id:
            order_line["vatTypeId"] = vat_type_id

        ii_payload = {
            "invoiceHeader": {
                "invoiceDate": invoice_date,
                "dueDate": due_date,
                "currencyId": 1,
                "invoiceAmount": amount_incl,
                "invoiceNumber": invoice_number,
                "description": description,
            },
            "orderLines": [order_line],
        }
        if supplier_id:
            ii_payload["invoiceHeader"]["vendorId"] = supplier_id

        result = client.post("/incomingInvoice", ii_payload)
        si_id = result.get("value", {}).get("id")
        if si_id:
            logger.info(f"Created incomingInvoice id={si_id}")
            return {"created": "supplier_invoice", "supplier_invoice_id": si_id, "amount_incl": amount_incl}
    except Exception as e:
        logger.warning(f"incomingInvoice endpoint failed: {e}, falling back to ledger/voucher")

    # Approach 2: Fall back to manual ledger voucher
    def _find_account_id(number: str) -> int | None:
        r = client.get("/ledger/account", params={"number": number, "fields": "id,number", "count": 1})
        vals = r.get("values", [])
        if vals:
            return vals[0]["id"]
        # Create the account if it doesn't exist
        try:
            cr = client.post("/ledger/account", {"number": int(number), "name": f"Konto {number}"})
            new_id = cr.get("value", {}).get("id")
            if new_id:
                logger.info(f"Created missing account {number} for supplier invoice")
                return new_id
        except Exception:
            pass
        return None

    # Resolve department if mentioned (for department-linked voucher posting)
    dept_id = None
    dept_entities = _get_entities(parsed, "department")
    if dept_entities:
        dept_name = dept_entities[0].get("fields", {}).get("name", "")
        if dept_name:
            depts = client.get("/department", params={"name": dept_name, "fields": "id", "count": 1}).get("values", [])
            if depts:
                dept_id = depts[0]["id"]

    payables_id = _find_account_id("2400")
    expense_id = _find_account_id(expense_account_no)
    vat_account_no = {25.0: "2710", 15.0: "2711", 12.0: "2712"}.get(float(vat_pct), "2710")
    vat_account_id = _find_account_id(vat_account_no)

    if not payables_id or not expense_id:
        raise ValueError(f"Could not find required accounts (payables={payables_id}, expense={expense_id})")

    if not amount_incl:
        # If amount is unknown (e.g. only a receipt image with no extracted amount),
        # we cannot post a balanced voucher — return partial success (supplier was found/created)
        logger.warning("Supplier invoice amount unknown — cannot post voucher without an amount")
        return {"created": "supplier_invoice_skipped", "reason": "amount_unknown", "supplier_id": supplier_id}

    # Build postings with sequential row numbers (no gaps allowed)
    expense_posting = {"row": 1, "account": {"id": expense_id}, "amountGross": amount_excl, "amountGrossCurrency": amount_excl, "description": description, "date": invoice_date}
    if dept_id:
        expense_posting["department"] = {"id": dept_id}
    postings = [expense_posting]
    next_row = 2
    if vat_account_id and vat_amount:
        postings.append({"row": next_row, "account": {"id": vat_account_id}, "amountGross": vat_amount, "amountGrossCurrency": vat_amount, "description": description, "date": invoice_date})
        next_row += 1
    credit_posting = {"row": next_row, "account": {"id": payables_id}, "amountGross": -amount_incl, "amountGrossCurrency": -amount_incl, "description": description, "date": invoice_date}
    if supplier_id:
        credit_posting["supplier"] = {"id": supplier_id}
    postings.append(credit_posting)

    result = client.post("/ledger/voucher", {
        "date": invoice_date,
        "description": description,
        "postings": postings,
    })
    voucher_id = result.get("value", {}).get("id")
    return {"created": "supplier_invoice_voucher", "voucher_id": voucher_id, "amount_incl": amount_incl}


def handle_project_billing(parsed: dict, client: TripletexClient) -> dict:
    """Find project, set fixed price, create partial invoice."""
    today = date.today().isoformat()
    from datetime import timedelta
    due_date = (date.today() + timedelta(days=30)).isoformat()

    proj_entity = _get_entity(parsed, "project")
    proj_fields = proj_entity.get("fields", {})
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}

    fixed_price = _to_float(proj_fields.get("fixedprice", proj_fields.get("fixedPrice", 0)))
    percentage = _to_float(inv_fields.get("percentage", 100))
    invoice_amount = _to_float(inv_fields.get("amount", 0)) or (fixed_price * percentage / 100)
    # Derive fixed_price from invoice amount + percentage if not explicitly given
    if not fixed_price and invoice_amount and percentage and percentage < 100:
        fixed_price = round(invoice_amount / (percentage / 100), 2)

    # Find project by name, or fall back to first project on account (competition pre-creates one)
    project_id = proj_fields.get("id")
    if not project_id and proj_fields.get("name"):
        r = client.get("/project", params={"name": proj_fields["name"], "fields": "id", "count": 1})
        vals = r.get("values", [])
        if vals:
            project_id = vals[0]["id"]
    if not project_id:
        # No project found by name — take the first available project on the account
        r = client.get("/project", params={"fields": "id", "count": 1})
        vals = r.get("values", [])
        if vals:
            project_id = vals[0]["id"]

    # Set fixed price on project
    if project_id and fixed_price:
        current = client.get(f"/project/{project_id}", params={"fields": "*"}).get("value", {})
        current["isFixedPrice"] = True
        current["fixedprice"] = fixed_price
        # Strip fields that cannot be changed via PUT /project (must use their own endpoints)
        for key in ("projectRateTypes", "hourlyRates", "projectHourlyRates", "participants", "contact", "projectActivities", "orderLines", "invoicingPlan", "preliminaryInvoice", "accountingDimensionValues", "boligmappaAddress"):
            current.pop(key, None)
        client.put(f"/project/{project_id}", current)

    # Find/create customer
    customer_entities = _get_entities(parsed, "customer")
    customer_id = None
    if customer_entities:
        cf = customer_entities[0].get("fields", {})
        customer_id = (
            cf.get("id")
            or _find_customer_by_org(client, cf.get("organizationNumber", ""))
            or _find_customer_id(client, cf.get("name", ""))
        )
        if not customer_id and cf.get("name"):
            r = client.create_customer({"isCustomer": True, "name": cf["name"]})
            customer_id = r.get("value", {}).get("id")

    if not customer_id:
        raise ValueError("No customer for project billing")

    _ensure_bank_account(client)

    pct_str = f" {int(percentage)}%" if percentage < 100 else ""
    desc = inv_fields.get("description") or f"Delbetaling{pct_str}"
    order_payload = {
        "customer": {"id": customer_id},
        "orderDate": today,
        "deliveryDate": due_date,
        "orderLines": [{"count": 1, "description": desc, "unitPriceExcludingVatCurrency": invoice_amount}],
    }
    if project_id:
        order_payload["project"] = {"id": project_id}

    order_result = client.create_order(order_payload)
    order_id = order_result.get("value", {}).get("id")

    invoice_result = client.create_invoice({
        "invoiceDate": today,
        "invoiceDueDate": due_date,
        "customer": {"id": customer_id},
        "orders": [{"id": order_id}],
    })
    invoice_id = invoice_result.get("value", {}).get("id")
    return {"created": "project_billing_invoice", "invoice_id": invoice_id, "amount": invoice_amount}


# --- Helpers ---

def _ensure_bank_account(client: TripletexClient) -> None:
    """Set a valid Norwegian bank account on ledger account 1920 if not already set."""
    try:
        result = client.get("/ledger/account", params={"isBankAccount": True, "fields": "id,bankAccountNumber", "count": 1})
        accounts = result.get("values", [])
        if accounts and not accounts[0].get("bankAccountNumber"):
            acct_id = accounts[0]["id"]
            current = client.get(f"/ledger/account/{acct_id}", params={"fields": "*"}).get("value", {})
            current["bankAccountNumber"] = "15030515002"
            client.put(f"/ledger/account/{acct_id}", current)
    except Exception as e:
        logger.warning(f"Could not set bank account: {e}")


def _ensure_division(client: TripletexClient) -> "int | None":
    """Return a division (virksomhet) ID. Creates one if none exists — required for salary/transaction."""
    try:
        divs = client.get("/division", params={"fields": "id", "count": 1}).get("values", [])
        if divs:
            return divs[0]["id"]
        # No divisions found — create a minimal one so employment can be linked
        result = client.post("/division", {
            "name": "Virksomhet",
            "organizationNumber": "987654321",
            "startDate": "2020-01-01",
            "municipalityDate": "2020-01-01",
            "municipality": {"id": 1},
        })
        new_id = result.get("value", {}).get("id")
        if new_id:
            logger.info(f"Created division {new_id} for employment link")
        return new_id
    except Exception as e:
        logger.warning(f"Could not get/create division: {e}")
        return None


def _get_entities(parsed: dict, entity_type: str) -> list:
    # Normalize: travelExpense == travel_expense == travelexpense
    normalized = entity_type.lower().replace("_", "")
    matches = [e for e in parsed.get("entities", [])
               if e.get("type", "").lower().replace("_", "") == normalized]
    if matches:
        return matches
    entities = parsed.get("entities", [])
    return entities if entities else [{"type": entity_type, "fields": {}}]


def _get_entity(parsed: dict, entity_type: str) -> dict:
    return _get_entities(parsed, entity_type)[0]


def _assign_admin_role(client: TripletexClient, employee_id: int) -> None:
    try:
        client.put(
            "/employee/entitlement/:grantEntitlementsByTemplate",
            {},
            params={"employeeId": employee_id, "template": "ALL_PRIVILEGES"},
        )
    except Exception as e:
        logger.warning(f"Could not assign admin role: {e}")


def _ensure_department_module(client: TripletexClient) -> None:
    try:
        modules = client.get_modules()
        if not modules.get("moduleDepartmentAccounting", False):
            client.put("/company/modules", {"moduleDepartmentAccounting": True})
    except Exception as e:
        logger.warning(f"Could not enable department module: {e}")


def _find_employee_id(client: TripletexClient, name: str) -> int | None:
    if not name:
        return None
    parts = name.strip().split(" ", 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    params = {"fields": "id,firstName,lastName", "count": 50}
    if first:
        params["firstName"] = first
    if last:
        params["lastName"] = last
    result = client.get("/employee", params=params)
    name_lower = name.lower()
    for emp in result.get("values", []):
        full = f"{emp.get('firstName', '')} {emp.get('lastName', '')}".lower().strip()
        if full == name_lower:
            return emp["id"]
    return None


def _find_employee_by_email(client: TripletexClient, email: str) -> int | None:
    if not email:
        return None
    result = client.get("/employee", params={"email": email, "fields": "id", "count": 1})
    vals = result.get("values", [])
    return vals[0]["id"] if vals else None


def _find_customer_by_org(client: TripletexClient, org_no: str) -> int | None:
    if not org_no:
        return None
    result = client.get("/customer", params={"organizationNumber": org_no, "fields": "id", "count": 1})
    vals = result.get("values", [])
    return vals[0]["id"] if vals else None


def _get_default_department_id(client: TripletexClient) -> int | None:
    departments = client.get_departments(fields="id")
    return departments[0]["id"] if departments else None


def _find_customer_id(client: TripletexClient, name: str) -> int | None:
    if not name:
        return None
    # Use API-level name search (more efficient than fetch-all)
    result = client.get("/customer", params={"name": name, "fields": "id,name", "count": 10})
    name_lower = name.lower()
    for c in result.get("values", []):
        if c.get("name", "").lower() == name_lower:
            return c["id"]
    return None


def _find_vat_type_id(client: TripletexClient, percentage: float) -> int | None:
    """Find the outgoing VAT type ID matching the given percentage (e.g. 0, 12, 15, 25)."""
    # Use stable standard Norwegian outgoing VAT type IDs directly (no API call needed)
    standard = {25.0: 3, 15.0: 31, 12.0: 32, 0.0: 5}
    hardcoded = standard.get(float(percentage))
    if hardcoded is not None:
        return hardcoded
    # Dynamic lookup only for non-standard percentages
    try:
        result = client.get("/ledger/vatType", params={"fields": "id,name,percentage", "count": 100})
        exclude = ("direktepostert", "tap ", "uttak", "grunnlag", "fradrag", "justering",
                   "tilbakeføring", "innførsel", "innlands", "utfø", "klimakvoter")
        for v in result.get("values", []):
            name_lower = v.get("name", "").lower()
            pct = v.get("percentage", -999)
            if abs(float(pct) - percentage) < 0.01 and "utgående" in name_lower:
                if not any(ex in name_lower for ex in exclude):
                    return v["id"]
    except Exception:
        pass
    return None


def _get_default_employee_id(client: TripletexClient) -> int | None:
    employees = client.get_employees(fields="id")
    return employees[0]["id"] if employees else None


def _to_float(value) -> float:
    """Convert a value like '200 NOK', '4 150 kr', '1 500,00', '1.500,00', 1500 to a float."""
    if isinstance(value, (int, float)):
        return float(value)
    import re
    s = str(value)
    # Extract only numeric characters and separators
    s = re.sub(r"[^\d\s,.]", "", s).strip()
    s = s.strip()
    if "," in s and "." in s:
        # Both separators present — whichever comes last is the decimal sep
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        if last_comma > last_dot:
            # e.g. "1.500,00" — period=thousands, comma=decimal
            s = s.replace(".", "").replace(",", ".")
        else:
            # e.g. "1,500.00" — comma=thousands, period=decimal
            s = s.replace(",", "")
    elif "," in s:
        # Only comma — could be decimal (Nordic) or thousands (EN)
        # If comma has exactly 3 digits after it, treat as thousands sep
        m = re.search(r",(\d+)$", s)
        if m and len(m.group(1)) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(" ", "").replace(",", ".")
    else:
        s = s.replace(" ", "")
    m = re.search(r"[\d.]+", s)
    return float(m.group()) if m else 0.0


def _find_product_id(client: TripletexClient, name: str) -> int | None:
    if not name:
        return None
    result = client.get("/product", params={"name": name, "fields": "id,name", "count": 10})
    name_lower = name.lower()
    for p in result.get("values", []):
        if p.get("name", "").lower() == name_lower:
            return p["id"]
    return None


def handle_register_project_hours(parsed: dict, client: TripletexClient) -> dict:
    """Register hours in timesheet for a project activity, then create an invoice."""
    today = date.today().isoformat()
    from datetime import timedelta
    due_date = (date.today() + timedelta(days=30)).isoformat()

    emp_entity = _get_entity(parsed, "employee")
    emp_fields = emp_entity.get("fields", {}) if emp_entity.get("type") else {}
    proj_entity = _get_entity(parsed, "project")
    proj_fields = proj_entity.get("fields", {}) if proj_entity.get("type") else {}
    cust_entities = _get_entities(parsed, "customer")
    inv_entities = _get_entities(parsed, "invoice")
    inv_fields = inv_entities[0].get("fields", {}) if inv_entities else {}

    hours = _to_float(inv_fields.get("hours", inv_fields.get("quantity", 0)))
    hourly_rate = _to_float(inv_fields.get("hourlyRate", inv_fields.get("unitPriceExcludingVatCurrency", 0)))
    activity_name = inv_fields.get("activityName", inv_fields.get("activity", ""))
    total_amount = _to_float(inv_fields.get("amount", inv_fields.get("totalAmount", 0))) or (hours * hourly_rate)

    # Find employee
    employee_id = None
    if emp_fields.get("email"):
        employee_id = _find_employee_by_email(client, emp_fields["email"])
    if not employee_id:
        name = f"{emp_fields.get('firstName','')} {emp_fields.get('lastName','')}".strip()
        if name:
            employee_id = _find_employee_id(client, name)
    if not employee_id:
        employee_id = _get_default_employee_id(client)

    # Find project
    project_id = proj_fields.get("id")
    if not project_id and proj_fields.get("name"):
        r = client.get("/project", params={"name": proj_fields["name"], "fields": "id,startDate", "count": 5})
        for v in r.get("values", []):
            project_id = v["id"]
            proj_start = v.get("startDate", today)
            break

    # Find activity — search global list by name, fall back to first project activity
    activity_id = None
    acts = client.get("/activity", params={"count": 50, "fields": "id,name,isProjectActivity"}).get("values", [])
    if activity_name:
        name_lower = activity_name.lower()
        for a in acts:
            if a.get("name", "").lower() == name_lower:
                activity_id = a["id"]
                break
    if not activity_id:
        for a in acts:
            if a.get("isProjectActivity"):
                activity_id = a["id"]
                break

    # Register timesheet entry
    entry_id = None
    if employee_id and project_id and activity_id and hours:
        try:
            entry_result = client.post("/timesheet/entry", {
                "employee": {"id": employee_id},
                "project": {"id": project_id},
                "activity": {"id": activity_id},
                "date": today,
                "hours": hours,
            })
            entry_id = entry_result.get("value", {}).get("id")
        except Exception as e:
            logger.warning(f"Timesheet entry failed: {e}")

    # Find customer
    customer_id = None
    if cust_entities:
        cf = cust_entities[0].get("fields", {})
        customer_id = (
            cf.get("id")
            or _find_customer_by_org(client, cf.get("organizationNumber", ""))
            or _find_customer_id(client, cf.get("name", ""))
        )
        if not customer_id and cf.get("name"):
            r = client.create_customer({"isCustomer": True, "name": cf["name"], **{k: cf[k] for k in ("organizationNumber", "email") if k in cf}})
            customer_id = r.get("value", {}).get("id")

    if not customer_id:
        raise ValueError("No customer found for project hours invoice")

    _ensure_bank_account(client)

    desc = activity_name or "Konsulenttjenester"
    order_result = client.create_order({
        "customer": {"id": customer_id},
        "orderDate": today,
        "deliveryDate": due_date,
        "orderLines": [{"count": hours or 1, "description": desc, "unitPriceExcludingVatCurrency": hourly_rate or total_amount}],
        **({"project": {"id": project_id}} if project_id else {}),
    })
    order_id = order_result.get("value", {}).get("id")

    invoice_result = client.create_invoice({
        "invoiceDate": today,
        "invoiceDueDate": due_date,
        "customer": {"id": customer_id},
        "orders": [{"id": order_id}],
    })
    invoice_id = invoice_result.get("value", {}).get("id")
    return {"created": "project_hours_invoice", "timesheet_entry_id": entry_id, "invoice_id": invoice_id, "hours": hours, "total": total_amount}


def handle_process_payroll(parsed: dict, client: TripletexClient) -> dict:
    """Process payroll via salary transaction API (creates proper payslips with voucher fallback)."""
    today = date.today()
    today_str = today.isoformat()

    # Extract salary amounts
    payroll_entity = _get_entity(parsed, "payroll")
    payroll_fields = payroll_entity.get("fields", {}) if payroll_entity.get("type") else {}
    emp_entity = _get_entity(parsed, "employee")
    emp_fields = emp_entity.get("fields", {}) if emp_entity.get("type") else {}

    base_salary = _to_float(payroll_fields.get("baseSalary", payroll_fields.get("salary", 0)))
    bonus = _to_float(payroll_fields.get("bonus", payroll_fields.get("oneTimeBonus", 0)))
    total = _to_float(payroll_fields.get("totalAmount", payroll_fields.get("amount", 0))) or (base_salary + bonus)

    if not total:
        raise ValueError("Could not determine payroll amount")

    # Find employee
    employee_id = None
    if emp_fields.get("email"):
        r = client.get("/employee", params={"email": emp_fields["email"], "fields": "id", "count": 1})
        vals = r.get("values", [])
        if vals:
            employee_id = vals[0]["id"]
    if not employee_id and (emp_fields.get("firstName") or emp_fields.get("lastName")):
        params = {"fields": "id", "count": 1}
        if emp_fields.get("firstName"):
            params["firstName"] = emp_fields["firstName"]
        if emp_fields.get("lastName"):
            params["lastName"] = emp_fields["lastName"]
        r = client.get("/employee", params=params)
        vals = r.get("values", [])
        if vals:
            employee_id = vals[0]["id"]

    if not employee_id:
        raise ValueError("Employee not found for payroll")

    # Look up salary type IDs dynamically
    salary_types = client.get("/salary/type", params={"fields": "id,name", "count": 100}).get("values", [])
    fastlonn_id = next((t["id"] for t in salary_types if "fastlønn" in t.get("name", "").lower()), None)
    bonus_id = next((t["id"] for t in salary_types if t.get("name", "").lower() == "bonus"), None)
    fallback_id = salary_types[0]["id"] if salary_types else None

    # Build payslip specifications
    specs = []
    if base_salary:
        sid = fastlonn_id or fallback_id
        if sid:
            specs.append({"description": "Grunnlønn", "salaryType": {"id": sid}, "rate": base_salary, "count": 1})
    if bonus and bonus > 0:
        sid = bonus_id or fastlonn_id or fallback_id
        if sid:
            specs.append({"description": "Bonus", "salaryType": {"id": sid}, "rate": bonus, "count": 1})
    if not specs:
        sid = fastlonn_id or fallback_id
        if sid:
            specs = [{"description": "Lønn", "salaryType": {"id": sid}, "rate": total, "count": 1}]
        else:
            raise ValueError("No salary types available")

    # Ensure employee has dateOfBirth (required by Tripletex before employment can be created)
    try:
        emp_data = client.get(f"/employee/{employee_id}", params={"fields": "*"}).get("value", {})
        if not emp_data.get("dateOfBirth"):
            emp_data["dateOfBirth"] = "1980-01-01"
            client.put(f"/employee/{employee_id}", emp_data)
    except Exception as e:
        logger.warning(f"Could not update employee dateOfBirth: {e}")

    # Ensure employee has an employment record linked to a division (virksomhet) — required for salary/transaction
    try:
        division_id = _ensure_division(client)

        emp_records = []
        try:
            emp_records = client.get("/employee/employment", params={"employeeId": employee_id, "fields": "id", "count": 5}).get("values", [])
        except Exception:
            pass

        if not emp_records:
            # No employment — create one with division
            emp_payload = {
                "employee": {"id": employee_id},
                "startDate": today.replace(day=1).isoformat(),
            }
            if division_id:
                emp_payload["division"] = {"id": division_id}
            client.post("/employee/employment", emp_payload)
        elif division_id:
            # Employment exists but may lack division — use minimal PUT to avoid field conflicts
            for emp_rec in emp_records:
                emp_id = emp_rec.get("id")
                if not emp_id:
                    continue
                try:
                    existing = client.get(f"/employee/employment/{emp_id}", params={"fields": "*"}).get("value", {})
                    if not existing.get("division"):
                        minimal_put = {
                            "id": emp_id,
                            "employee": {"id": employee_id},
                            "startDate": existing.get("startDate", today.replace(day=1).isoformat()),
                            "division": {"id": division_id},
                        }
                        if existing.get("endDate"):
                            minimal_put["endDate"] = existing["endDate"]
                        client.put(f"/employee/employment/{emp_id}", minimal_put)
                        logger.info(f"Linked employment {emp_id} to division {division_id}")
                except Exception as e2:
                    logger.warning(f"Could not update employment {emp_id} with division: {e2}")
    except Exception as e:
        logger.info(f"Employment setup (may already exist): {e}")

    tx_payload = {
        "date": today_str,
        "year": today.year,
        "month": today.month,
        "payslips": [{"employee": {"id": employee_id}, "specifications": specs}],
    }

    try:
        result = client.post("/salary/transaction", tx_payload)
        tx_id = result.get("value", {}).get("id")
        payslip_ids = [p.get("id") for p in result.get("value", {}).get("payslips", [])]
        return {"created": "salary_transaction", "transaction_id": tx_id, "payslip_ids": payslip_ids, "total": total}
    except Exception as e:
        # Fallback: ledger voucher if salary module not enabled
        logger.warning(f"Salary transaction failed, falling back to voucher: {e}")
        description = f"Lønn {emp_fields.get('firstName', '')} {emp_fields.get('lastName', '')}".strip()

        def _get_account_id(number: int) -> int:
            r = client.get("/ledger/account", params={"number": number, "fields": "id", "count": 1})
            vals = r.get("values", [])
            if not vals:
                raise ValueError(f"Account {number} not found")
            return vals[0]["id"]

        salary_expense_id = _get_account_id(5000)
        salary_payable_id = _get_account_id(2930)
        voucher_payload = {
            "date": today_str,
            "description": description,
            "postings": [
                {"row": 1, "account": {"id": salary_expense_id}, "amountGross": total, "amountGrossCurrency": total, "description": description, "date": today_str},
                {"row": 2, "account": {"id": salary_payable_id}, "amountGross": -total, "amountGrossCurrency": -total, "description": description, "date": today_str},
            ],
        }
        result = client.post("/ledger/voucher", voucher_payload)
        voucher_id = result.get("value", {}).get("id")
        return {"created": "payroll_voucher", "voucher_id": voucher_id, "total": total}


def handle_create_accounting_dimension(parsed: dict, client: TripletexClient) -> dict:
    """Create a custom accounting dimension with values, then post a voucher linked to a dimension value."""
    today = date.today().isoformat()

    # Extract from entities or notes/api_sequence
    # Gemini usually puts dimension info in notes or as unknown entity
    notes = parsed.get("notes", "")
    api_seq = parsed.get("api_sequence", [])

    # Try to get dimension info from entities first, then fall back to notes parsing
    dim_entity = _get_entity(parsed, "dimension")
    dim_fields = dim_entity.get("fields", {}) if dim_entity.get("type") else {}

    # Dimension name and values — Gemini often puts these in notes or api_sequence descriptions
    dim_name = dim_fields.get("name", "")
    dim_values = dim_fields.get("values", [])
    voucher_account = dim_fields.get("account", "")
    voucher_amount = _to_float(dim_fields.get("amount", 0))
    linked_value = dim_fields.get("linkedValue", dim_fields.get("dimensionValue", ""))

    # Parse from api_sequence descriptions if not in entities
    if not dim_name:
        for step in api_seq:
            desc = step.get("description", "")
            endpoint = step.get("endpoint", "")
            if "dimension" in endpoint.lower() and dim_name == "":
                # Try to extract dimension name from description
                import re
                m = re.search(r"[\"']([^\"']+)[\"']", desc)
                if m:
                    dim_name = m.group(1)

    if not dim_name:
        # Last resort: parse from notes
        import re
        m = re.search(r"[\"']([^\"']+)[\"']", notes)
        if m:
            dim_name = m.group(1)

    # Step 1: Create the dimension name (always use dimensionIndex=1 on fresh accounts)
    dim_index = 1
    dim_result = client.post("/ledger/accountingDimensionName", {
        "dimensionName": dim_name,
        "dimensionIndex": dim_index,
    })
    dim_id = dim_result.get("value", {}).get("id")

    if not dim_id:
        raise ValueError(f"Could not create accounting dimension '{dim_name}'")

    # Step 2: Create dimension values (same dimensionIndex)
    value_ids = {}
    for val in dim_values:
        val_name = val if isinstance(val, str) else val.get("name", "")
        if val_name:
            v_result = client.post("/ledger/accountingDimensionValue", {
                "displayName": val_name,
                "dimensionIndex": dim_index,
            })
            vid = v_result.get("value", {}).get("id")
            if vid:
                value_ids[val_name] = vid

    # Step 3: Post voucher linked to the specified dimension value
    linked_value_id = value_ids.get(linked_value)

    if voucher_account and voucher_amount:
        def _find_account_id(number: str) -> int | None:
            r = client.get("/ledger/account", params={"number": number, "fields": "id,number", "count": 1})
            vals = r.get("values", [])
            if vals:
                return vals[0]["id"]
            try:
                cr = client.post("/ledger/account", {"number": int(number), "name": f"Konto {number}"})
                new_id = cr.get("value", {}).get("id")
                if new_id:
                    logger.info(f"Created missing account {number}")
                    return new_id
            except Exception:
                pass
            return None

        account_id = _find_account_id(str(voucher_account))
        if not account_id:
            logger.warning(f"Account {voucher_account} not found for dimension voucher")
        else:
            posting = {
                "row": 1,
                "account": {"id": account_id},
                "amountGross": voucher_amount,
                "amountGrossCurrency": voucher_amount,
                "description": dim_name,
                "date": today,
            }
            if linked_value_id:
                posting["freeAccountingDimension1"] = {"id": linked_value_id}

            # Balancing credit posting — use bank/cash (1920), avoids supplier requirement on 2400
            payable_id = _find_account_id("1920") or _find_account_id("1900") or _find_account_id("2900")

            postings = [posting]
            if payable_id:
                postings.append({
                    "row": 2,
                    "account": {"id": payable_id},
                    "amountGross": -voucher_amount,
                    "amountGrossCurrency": -voucher_amount,
                    "description": dim_name,
                    "date": today,
                })

            v_result = client.post("/ledger/voucher", {
                "date": today,
                "description": dim_name,
                "postings": postings,
            })
            voucher_id = v_result.get("value", {}).get("id")
            return {
                "created": "accounting_dimension",
                "dimension_id": dim_id,
                "value_ids": value_ids,
                "voucher_id": voucher_id,
            }

    return {"created": "accounting_dimension", "dimension_id": dim_id, "value_ids": value_ids}



def handle_late_fee_invoice(parsed: dict, client: TripletexClient) -> dict:
    """Find overdue invoice, post reminder fee voucher, create fee invoice, register partial payment."""
    from datetime import timedelta
    today = date.today()
    today_str = today.isoformat()
    due_date = (today + timedelta(days=30)).isoformat()

    fee_entity = _get_entity(parsed, "fee")
    fee_fields = fee_entity.get("fields", {}) if fee_entity else {}
    fee_amount = _to_float(fee_fields.get("amount", 50))
    debit_account_no = str(fee_fields.get("debitAccount", "1500"))
    credit_account_no = str(fee_fields.get("creditAccount", "3400"))
    partial_payment = _to_float(fee_fields.get("partialAmount", 0))

    payment_entity = _get_entity(parsed, "payment")
    if payment_entity:
        partial_payment = _to_float(payment_entity.get("fields", {}).get("partialAmount", partial_payment))

    # Find the overdue open invoice (invoiceDateFrom/To required by API)
    from datetime import timedelta as _td
    _date_from = "2020-01-01"
    _date_to = (date.today() + _td(days=365)).isoformat()
    overdue_invoices = client.get("/invoice", params={
        "invoiceStatus": "OPEN",
        "invoiceDateFrom": _date_from,
        "invoiceDateTo": _date_to,
        "fields": "id,customer,amountCurrency",
        "count": 1,
    }).get("values", [])
    if not overdue_invoices:
        raise ValueError("No overdue invoice found")
    overdue_invoice = overdue_invoices[0]
    overdue_invoice_id = overdue_invoice["id"]
    customer_id = overdue_invoice.get("customer", {}).get("id")

    # 1. Post reminder fee voucher
    def _get_acct_id(number: str) -> int | None:
        r = client.get("/ledger/account", params={"number": number, "fields": "id", "count": 1})
        vals = r.get("values", [])
        return vals[0]["id"] if vals else None

    debit_id = _get_acct_id(debit_account_no)
    credit_id = _get_acct_id(credit_account_no)
    voucher_id = None
    if debit_id and credit_id:
        # Account 1500 (receivables) requires customer reference in posting
        debit_posting = {"row": 1, "account": {"id": debit_id}, "amountGross": fee_amount, "amountGrossCurrency": fee_amount, "description": "Purregebyr", "date": today_str}
        credit_posting = {"row": 2, "account": {"id": credit_id}, "amountGross": -fee_amount, "amountGrossCurrency": -fee_amount, "description": "Purregebyr", "date": today_str}
        if debit_account_no == "1500" and customer_id:
            debit_posting["customer"] = {"id": customer_id}
        if credit_account_no == "1500" and customer_id:
            credit_posting["customer"] = {"id": customer_id}
        v = client.post("/ledger/voucher", {
            "date": today_str,
            "description": "Purregebyr",
            "postings": [debit_posting, credit_posting],
        })
        voucher_id = v.get("value", {}).get("id")

    # 2. Create invoice for reminder fee
    _ensure_bank_account(client)
    fee_invoice_id = None
    if customer_id:
        try:
            order_result = client.create_order({
                "customer": {"id": customer_id},
                "orderDate": today_str,
                "deliveryDate": due_date,
                "orderLines": [{"count": 1, "description": "Purregebyr", "unitPriceExcludingVatCurrency": fee_amount}],
            })
            order_id = order_result.get("value", {}).get("id")
            if order_id:
                inv_result = client.create_invoice({
                    "invoiceDate": today_str,
                    "invoiceDueDate": due_date,
                    "customer": {"id": customer_id},
                    "orders": [{"id": order_id}],
                })
                fee_invoice_id = inv_result.get("value", {}).get("id")
        except Exception as e:
            logger.warning(f"Could not create fee invoice: {e}")

    # 3. Register partial payment on overdue invoice
    payment_result_id = None
    if partial_payment:
        try:
            payment_type_result = client.get("/invoice/paymentType", params={"fields": "id", "count": 1})
            payment_type_id = payment_type_result.get("values", [{}])[0].get("id")
            pr = client.put(f"/invoice/{overdue_invoice_id}/:payment", {}, params={
                "paymentDate": today_str,
                "paymentTypeId": payment_type_id,
                "paidAmount": partial_payment,
            })
            payment_result_id = pr.get("value", {}).get("id")
        except Exception as e:
            logger.warning(f"Could not register partial payment: {e}")

    return {
        "created": "late_fee_invoice",
        "voucher_id": voucher_id,
        "fee_invoice_id": fee_invoice_id,
        "overdue_invoice_id": overdue_invoice_id,
        "partial_payment_amount": partial_payment,
    }


def handle_post_vouchers(parsed: dict, client: TripletexClient) -> dict:
    """Post one or more ledger vouchers. Used for depreciation, year-end closing, ledger corrections, etc."""
    today = date.today().isoformat()
    voucher_entities = _get_entities(parsed, "voucher")

    # Common account names for auto-creation when account doesn't exist
    _ACCOUNT_NAMES = {
        "1209": "Akkumulerte avskrivninger",
        "6030": "Avskrivning på maskiner og inventar",
        "6040": "Avskrivning på kontormaskiner",
    }

    def _get_acct_id(number: str) -> int | None:
        r = client.get("/ledger/account", params={"number": str(number), "fields": "id", "count": 1})
        vals = r.get("values", [])
        if vals:
            return vals[0]["id"]
        # Account doesn't exist — try to create it
        try:
            name = _ACCOUNT_NAMES.get(str(number), f"Konto {number}")
            cr = client.post("/ledger/account", {"number": int(number), "name": name})
            new_id = cr.get("value", {}).get("id")
            if new_id:
                logger.info(f"Created missing account {number} ({name})")
                return new_id
        except Exception as e:
            logger.warning(f"Could not create account {number}: {e}")
        # Last resort fallback: try round-number accounts in the same hundred-range
        try:
            num = int(number)
            base = (num // 100) * 100
            for offset in [10, 20, 30, 40, 50, 60, 70, 80, 90, 0]:
                candidate = str(base + offset)
                if candidate == str(number):
                    continue
                r2 = client.get("/ledger/account", params={"number": candidate, "fields": "id", "count": 1})
                v2 = r2.get("values", [])
                if v2:
                    logger.info(f"Account {number} not found, using fallback {candidate}")
                    return v2[0]["id"]
        except Exception:
            pass
        return None

    created_ids = []
    for v_entity in voucher_entities:
        fields = v_entity.get("fields", {})
        description = fields.get("description", "Bilag")
        date_str = fields.get("date", today)
        postings = fields.get("postings", [])

        # If all postings have zero amount, try to derive from salary data (salary accrual case)
        if all(not _to_float(p.get("amount") or 0) for p in postings) and postings:
            acct_nums = [str(p.get("account", "")) for p in postings]
            salary_accts = {"5000", "5010", "5020", "5090", "2900", "2910", "2940", "2950"}
            if any(a in salary_accts for a in acct_nums):
                derived = 0.0
                # 1) Try /salary/transaction without fields filter (fields=... causes 500 on empty accounts)
                try:
                    salary_txs = client.get("/salary/transaction", params={"count": 1}).get("values", [])
                    if salary_txs:
                        derived = _to_float(salary_txs[0].get("totalAmount", salary_txs[0].get("total", 0)))
                except Exception:
                    pass
                # 2) Try to sum monthly salaries from employment details
                if not derived:
                    try:
                        employees = client.get("/employee", params={"fields": "id", "count": 50}).get("values", [])
                        total_annual = 0.0
                        for emp in employees[:20]:
                            emp_id = emp.get("id")
                            if not emp_id:
                                continue
                            emp_recs = client.get("/employee/employment", params={"employeeId": emp_id, "fields": "id", "count": 1}).get("values", [])
                            for emp_rec in emp_recs[:1]:
                                rec_id = emp_rec.get("id")
                                if rec_id:
                                    det_list = client.get("/employee/employment/details", params={"employmentId": rec_id, "fields": "annualSalary,percentageOfFullTimeEquivalent", "count": 1}).get("values", [])
                                    for det in det_list[:1]:
                                        annual = _to_float(det.get("annualSalary", 0))
                                        pct = _to_float(det.get("percentageOfFullTimeEquivalent", 100))
                                        if annual:
                                            total_annual += annual * (pct / 100)
                        if total_annual:
                            derived = round(total_annual / 12)
                    except Exception:
                        pass
                if derived:
                    logger.info(f"Derived salary accrual amount {derived} for salary accounts")
                    for p in postings:
                        p["amount"] = derived

        # If all postings have zero amount, try to derive tax provision from ledger (tax provision case)
        if all(not _to_float(p.get("amount") or 0) for p in postings) and postings:
            acct_nums = [str(p.get("account", "")) for p in postings]
            tax_accts = {"8300", "8700", "8800", "2500", "2920"}
            if any(a in tax_accts for a in acct_nums):
                try:
                    # Use the voucher date's year for ledger lookup (not always year-1)
                    try:
                        voucher_year = int(date_str[:4])
                    except Exception:
                        voucher_year = date.today().year - 1
                    # Build account id→number map
                    acct_list = client.get("/ledger/account", params={"count": 500, "fields": "id,number"}).get("values", [])
                    acct_map = {}
                    for a in acct_list:
                        try:
                            acct_map[a["id"]] = int(a.get("number", 0) or 0)
                        except Exception:
                            pass
                    # Try multiple year ranges to find data
                    profit = 0.0
                    for year in [voucher_year, voucher_year - 1, date.today().year]:
                        ledger_entries = client.get("/ledger", params={
                            "dateFrom": f"{year}-01-01", "dateTo": f"{year}-12-31", "count": 500
                        }).get("values", [])
                        if not ledger_entries:
                            continue
                        profit = 0.0
                        for entry in ledger_entries:
                            acct_id = (entry.get("account") or {}).get("id")
                            num = acct_map.get(acct_id, 0)
                            closing = float(entry.get("closingBalance") or 0)
                            if 3000 <= num <= 3999:  # Revenue — credit balance is positive
                                profit += abs(closing)
                            elif 4000 <= num <= 7999:  # Expenses — debit balance reduces profit
                                profit -= abs(closing)
                        if profit != 0.0:
                            logger.info(f"Tax provision: found ledger data for year {year}")
                            break
                    taxable = max(profit, 0.0)
                    tax_amt = round(taxable * 0.22, 2)
                    if tax_amt:
                        logger.info(f"Derived tax provision: profit={taxable}, tax={tax_amt} for year {year}")
                        for p in postings:
                            p["amount"] = tax_amt
                    else:
                        logger.warning(f"Tax provision derivation returned 0 (profit={taxable}), skipping")
                except Exception as te:
                    logger.warning(f"Could not derive tax provision from ledger: {te}")

        api_postings = []
        any_missing = False
        for i, p in enumerate(postings):
            amount = _to_float(p.get("amount") or 0)
            if not amount:
                logger.warning(f"Skipping posting with null/zero amount on account {p.get('account')}")
                continue
            acct_id = _get_acct_id(str(p.get("account", "")))
            if not acct_id:
                logger.warning(f"Account {p.get('account')} not found — cannot post unbalanced voucher, skipping: {description}")
                any_missing = True
                break
            side = p.get("side", "debit")
            gross = amount if side == "debit" else -amount
            api_postings.append({
                "row": i + 1,
                "account": {"id": acct_id},
                "amountGross": gross,
                "amountGrossCurrency": gross,
                "description": description,
                "date": date_str,
            })

        if any_missing or not api_postings:
            logger.warning(f"Skipping entire voucher due to missing account(s): {description}")
            continue

        def _try_post_voucher(dt: str) -> dict:
            # Update date in all postings
            for p in api_postings:
                p["date"] = dt
            return client.post("/ledger/voucher", {"date": dt, "description": description, "postings": api_postings})

        try:
            result = _try_post_voucher(date_str)
        except Exception as e:
            err_msg = str(e).lower()
            if "låst" in err_msg or "locked" in err_msg or "perioden" in err_msg or "closed period" in err_msg:
                # Period is locked — retry posting to first day of current year
                fallback_date = date.today().replace(month=1, day=1).isoformat()
                logger.warning(f"Period {date_str} appears locked, retrying with {fallback_date}")
                result = _try_post_voucher(fallback_date)
            else:
                raise

        v_id = result.get("value", {}).get("id")
        if v_id:
            created_ids.append(v_id)
            logger.info(f"Posted voucher {v_id}: {description}")

    return {"created": "vouchers", "voucher_ids": created_ids, "count": len(created_ids)}


def handle_analyze_and_create_projects(parsed: dict, client: TripletexClient) -> dict:
    """Read ledger to find top N expense accounts with biggest increase between two months, create projects + activities."""
    today = date.today()

    # Parse period from entities (Gemini extracts which months to compare)
    period_entity = _get_entity(parsed, "period")
    pf = period_entity.get("fields", {}) if period_entity else {}
    period1_from = pf.get("period1From", "2026-01-01")
    period1_to = pf.get("period1To", "2026-01-31")
    period2_from = pf.get("period2From", "2026-02-01")
    period2_to = pf.get("period2To", "2026-02-28")
    top_n = int(pf.get("topN", 3))

    def get_account_totals(date_from: str, date_to: str) -> dict:
        """Returns {account_number: {name, total}} for expense accounts in the period."""
        totals = {}
        # Use ledger/voucher with fields that include postings + account info
        params = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "fields": "postings(account(number,name),amountGross)",
            "count": 1000,
        }
        vouchers = client.get("/ledger/voucher", params=params).get("values", [])
        for voucher in vouchers:
            for posting in voucher.get("postings", []):
                acct = posting.get("account", {})
                num = str(acct.get("number", ""))
                # Expense accounts: 4xxx–9xxx range (Norwegian chart of accounts)
                if num and num[0] in "456789":
                    amount = abs(_to_float(posting.get("amountGross", 0)))
                    if num not in totals:
                        totals[num] = {"name": acct.get("name", num), "total": 0.0}
                    totals[num]["total"] += amount
        return totals

    period1 = get_account_totals(period1_from, period1_to)
    period2 = get_account_totals(period2_from, period2_to)

    # Compute increases (period2 - period1), only positive increases
    increases = {}
    for num in set(period1) | set(period2):
        p1 = period1.get(num, {}).get("total", 0.0)
        p2 = period2.get(num, {}).get("total", 0.0)
        diff = p2 - p1
        if diff > 0:
            name = period2.get(num, period1.get(num, {})).get("name", num)
            increases[num] = {"name": name, "increase": diff}

    top_accounts = sorted(increases.items(), key=lambda x: x[1]["increase"], reverse=True)[:top_n]

    # Get project manager (first employee on account)
    manager_id = _get_default_employee_id(client)
    dept_id = _get_default_department_id(client)

    # Get a project activity to associate (first available project activity)
    acts = client.get("/activity", params={"count": 50, "fields": "id,name,isProjectActivity"}).get("values", [])
    default_activity_id = next((a["id"] for a in acts if a.get("isProjectActivity")), None)

    created = []
    for acct_num, data in top_accounts:
        project_name = data["name"]
        proj_payload = {
            "name": project_name,
            "startDate": today.isoformat(),
            "isInternal": True,
        }
        if manager_id:
            proj_payload["projectManager"] = {"id": manager_id}
        if dept_id:
            proj_payload["department"] = {"id": dept_id}

        proj_result = client.post("/project", proj_payload)
        proj_id = proj_result.get("value", {}).get("id")

        # Create a standalone activity for this project (POST /project/activity gives 405)
        activity_id = None
        if proj_id:
            try:
                act_result = client.post("/activity", {
                    "name": project_name,
                    "isProjectActivity": True,
                    "isGeneral": True,
                    "activityType": "PROJECT_GENERAL_ACTIVITY",
                })
                activity_id = act_result.get("value", {}).get("id")
            except Exception as e:
                # Use default existing project activity if available
                activity_id = default_activity_id
                if not activity_id:
                    logger.warning(f"Could not create activity for project {proj_id}: {e}")

        created.append({
            "account": acct_num,
            "name": project_name,
            "project_id": proj_id,
            "activity_id": activity_id,
            "increase": data["increase"],
        })
        logger.info(f"Created project '{project_name}' (acct {acct_num}, increase {data['increase']:.0f})")

    return {"created": "projects_from_analysis", "projects": created}


def handle_project_lifecycle(parsed: dict, client: TripletexClient) -> dict:
    """Complete project lifecycle: set budget, register hours, register supplier cost, create invoice."""
    from datetime import timedelta
    today = date.today()
    today_str = today.isoformat()
    due_date = (today + timedelta(days=30)).isoformat()

    # Only use proj_entity if it is ACTUALLY a project entity (not a fallback from _get_entities)
    _all_entities = parsed.get("entities", [])
    _proj_entities = [e for e in _all_entities if e.get("type", "").lower() == "project"]
    proj_entity = _proj_entities[0] if _proj_entities else {}
    proj_fields = proj_entity.get("fields", {})
    cust_entities = [e for e in _all_entities if e.get("type", "").lower() == "customer"]
    employee_entities = [e for e in _all_entities if e.get("type", "").lower() == "employee"]
    supplier_entities = [e for e in _all_entities if e.get("type", "").lower() == "supplier"]

    budget = _to_float(proj_fields.get("budget", proj_fields.get("fixedprice", proj_fields.get("amount", 0))))
    # Fallback: scan all entities for any budget/amount field
    if not budget:
        for ent in _all_entities:
            ef = ent.get("fields", {})
            b = _to_float(ef.get("budget", ef.get("fixedprice", ef.get("totalBudget", ef.get("projectBudget", 0)))))
            if b:
                budget = b
                break
    # Also try project name from any entity if project entity absent
    if not proj_fields.get("name"):
        for ent in _all_entities:
            ef = ent.get("fields", {})
            if ef.get("projectName"):
                proj_fields["name"] = ef["projectName"]
                break

    # Find/create project
    project_id = proj_fields.get("id")
    if not project_id and proj_fields.get("name"):
        r = client.get("/project", params={"name": proj_fields["name"], "fields": "id", "count": 5})
        vals = r.get("values", [])
        if vals:
            project_id = vals[0]["id"]
    if not project_id:
        r = client.get("/project", params={"fields": "id", "count": 1})
        vals = r.get("values", [])
        if vals:
            project_id = vals[0]["id"]

    # Set budget/fixed price on project
    if project_id and budget:
        try:
            current = client.get(f"/project/{project_id}", params={"fields": "*"}).get("value", {})
            for key in ("projectRateTypes", "hourlyRates", "projectHourlyRates", "participants",
                        "contact", "projectActivities", "orderLines", "invoicingPlan",
                        "preliminaryInvoice", "accountingDimensionValues", "boligmappaAddress"):
                current.pop(key, None)
            current["isFixedPrice"] = True
            current["fixedprice"] = budget
            client.put(f"/project/{project_id}", current)
        except Exception as e:
            logger.warning(f"Could not set project budget: {e}")

    # Get project activity for timesheet entries
    acts = client.get("/activity", params={"count": 50, "fields": "id,name,isProjectActivity"}).get("values", [])
    activity_id = next((a["id"] for a in acts if a.get("isProjectActivity")), None)

    # Register hours for each employee
    timesheet_ids = []
    for emp_entity in employee_entities:
        ef = emp_entity.get("fields", {})
        hours = _to_float(ef.get("hours", 0))
        if not hours:
            continue
        emp_id = None
        if ef.get("email"):
            emp_id = _find_employee_by_email(client, ef["email"])
        if not emp_id:
            name = f"{ef.get('firstName','')} {ef.get('lastName','')}".strip()
            if name:
                emp_id = _find_employee_id(client, name)
        if not emp_id:
            emp_id = _get_default_employee_id(client)
        if emp_id and project_id and activity_id and hours:
            try:
                r = client.post("/timesheet/entry", {
                    "employee": {"id": emp_id},
                    "project": {"id": project_id},
                    "activity": {"id": activity_id},
                    "date": today_str,
                    "hours": hours,
                })
                timesheet_ids.append(r.get("value", {}).get("id"))
            except Exception as e:
                logger.warning(f"Timesheet for employee {emp_id} failed: {e}")

    # Register supplier cost as ledger voucher
    voucher_id = None
    if supplier_entities:
        se = supplier_entities[0].get("fields", {})
        cost = _to_float(se.get("cost", se.get("amount", 0)))
        supplier_name = se.get("name", "")
        supplier_org = se.get("organizationNumber", "")
        if cost:
            try:
                # Find or create supplier — account 2400 requires supplier.id
                supp_id = None
                if supplier_org:
                    sv = client.get("/supplier", params={"organizationNumber": supplier_org, "fields": "id", "count": 1}).get("values", [])
                    if sv:
                        supp_id = sv[0]["id"]
                if not supp_id and supplier_name:
                    sv = client.get("/supplier", params={"name": supplier_name, "fields": "id", "count": 1}).get("values", [])
                    if sv:
                        supp_id = sv[0]["id"]
                if not supp_id and (supplier_name or supplier_org):
                    spayload = {}
                    if supplier_name:
                        spayload["name"] = supplier_name
                    if supplier_org:
                        spayload["organizationNumber"] = supplier_org
                    try:
                        sr = client.post("/supplier", spayload)
                        supp_id = sr.get("value", {}).get("id")
                    except Exception:
                        pass

                # If still no supplier, create a generic one — account 2400 requires supplier.id
                if not supp_id:
                    try:
                        sr = client.post("/supplier", {"name": supplier_name or "Ekstern leverandør"})
                        supp_id = sr.get("value", {}).get("id")
                    except Exception as e_supp:
                        logger.warning(f"Could not create supplier '{supplier_name}': {e_supp}")
                        # Try to find by name (may already exist from prior run)
                        try:
                            sv2 = client.get("/supplier", params={"name": supplier_name or "Ekstern leverandør", "fields": "id", "count": 1}).get("values", [])
                            if sv2:
                                supp_id = sv2[0]["id"]
                        except Exception:
                            pass
                if not supp_id:
                    try:
                        sv3 = client.get("/supplier", params={"name": "Ekstern leverandør", "fields": "id", "count": 1}).get("values", [])
                        if sv3:
                            supp_id = sv3[0]["id"]
                        else:
                            sr3 = client.post("/supplier", {"name": "Ekstern leverandør"})
                            supp_id = sr3.get("value", {}).get("id")
                    except Exception:
                        pass

                expense_acct_no = se.get("account", "7100")
                expense_acct = client.get("/ledger/account", params={"number": str(expense_acct_no), "fields": "id", "count": 1}).get("values", [{}])[0].get("id")
                # Use 2400 (payables, requires supplier) if we have supp_id, else 2960 (annen kortsiktig gjeld)
                credit_acct_no = "2400" if supp_id else "2960"
                payables_acct = client.get("/ledger/account", params={"number": credit_acct_no, "fields": "id", "count": 1}).get("values", [{}])[0].get("id")
                if expense_acct and payables_acct:
                    credit_posting = {"row": 2, "account": {"id": payables_acct}, "amountGross": -cost, "amountGrossCurrency": -cost, "description": supplier_name, "date": today_str}
                    if supp_id:
                        credit_posting["supplier"] = {"id": supp_id}
                    v = client.post("/ledger/voucher", {
                        "date": today_str,
                        "description": f"Leverandørkostnad: {supplier_name}",
                        "postings": [
                            {"row": 1, "account": {"id": expense_acct}, "amountGross": cost, "amountGrossCurrency": cost, "description": supplier_name, "date": today_str},
                            credit_posting,
                        ],
                    })
                    voucher_id = v.get("value", {}).get("id")
            except Exception as e:
                logger.warning(f"Supplier cost voucher failed: {e}")

    # Find customer for invoice
    customer_id = None
    if cust_entities:
        cf = cust_entities[0].get("fields", {})
        customer_id = (
            _find_customer_by_org(client, cf.get("organizationNumber", ""))
            or _find_customer_id(client, cf.get("name", ""))
        )
        if not customer_id and cf.get("name"):
            r = client.create_customer({"isCustomer": True, "name": cf["name"],
                                        **{k: cf[k] for k in ("organizationNumber",) if k in cf}})
            customer_id = r.get("value", {}).get("id")

    # Create invoice for project
    invoice_id = None
    if customer_id:
        _ensure_bank_account(client)
        try:
            invoice_amount = budget or sum(
                _to_float(e.get("fields", {}).get("hours", 0)) *
                _to_float(e.get("fields", {}).get("hourlyRate", 1000))
                for e in employee_entities
            )
            order_payload = {
                "customer": {"id": customer_id},
                "orderDate": today_str,
                "deliveryDate": due_date,
                "orderLines": [{"count": 1, "description": proj_fields.get("name", "Prosjekt"), "unitPriceExcludingVatCurrency": invoice_amount}],
            }
            if project_id:
                order_payload["project"] = {"id": project_id}
            order_result = client.create_order(order_payload)
            order_id = order_result.get("value", {}).get("id")
            if order_id:
                inv_result = client.create_invoice({"invoiceDate": today_str, "invoiceDueDate": due_date, "orders": [{"id": order_id}]})
                invoice_id = inv_result.get("value", {}).get("id")
        except Exception as e:
            logger.warning(f"Invoice creation failed: {e}")

    return {
        "created": "project_lifecycle",
        "project_id": project_id,
        "budget": budget,
        "timesheet_ids": timesheet_ids,
        "supplier_voucher_id": voucher_id,
        "invoice_id": invoice_id,
    }

def handle_bank_reconciliation(parsed: dict, client: TripletexClient) -> dict:
    """Bank reconciliation: match CSV transactions to open invoices and register payments."""
    from datetime import timedelta
    today = date.today().isoformat()

    payment_types = client.get("/invoice/paymentType", params={"count": 1}).get("values", [])
    if not payment_types:
        raise ValueError("No payment types available")
    payment_type_id = payment_types[0]["id"]

    date_from = "2020-01-01"
    date_to = (date.today() + timedelta(days=365)).isoformat()

    # Fetch all open customer invoices
    open_invoices = client.get("/invoice", params={
        "invoiceDateFrom": date_from, "invoiceDateTo": date_to,
        "invoiceStatus": "OPEN",
        "fields": "id,amountCurrency,amountCurrencyOutstanding,customer(id,name)",
        "count": 100,
    }).get("values", [])
    logger.info(f"bank_recon: found {len(open_invoices)} open customer invoices")

    paid_invoice_ids = []
    approved_supplier_ids = []

    customer_entities = _get_entities(parsed, "customer")
    supplier_entities = _get_entities(parsed, "supplier")

    def _pay_invoice(inv, target_amount, payment_date):
        inv_id = inv["id"]
        outstanding = _to_float(inv.get("amountCurrencyOutstanding") or inv.get("amountCurrency", 0))
        amount = target_amount if (target_amount and abs(target_amount - outstanding) > 0.5) else outstanding
        if not amount:
            return False
        try:
            client.put(f"/invoice/{inv_id}/:payment", {}, params={
                "paymentDate": payment_date,
                "paymentTypeId": payment_type_id,
                "paidAmount": amount,
            })
            paid_invoice_ids.append(inv_id)
            return True
        except Exception as e:
            logger.warning(f"Could not pay invoice {inv_id}: {e}")
            return False

    def _name_score(csv_name: str, api_name: str) -> float:
        """Token-overlap score 0.0–1.0 between two names."""
        a = set(csv_name.lower().split())
        b = set(api_name.lower().split())
        if not a or not b:
            return 0.0
        overlap = a & b
        return len(overlap) / max(len(a), len(b))

    if customer_entities:
        # Match each CSV customer row to an open invoice
        remaining = list(open_invoices)
        for ce in customer_entities:
            cf = ce.get("fields", {})
            cust_name = cf.get("name", "")
            target_amount = _to_float(cf.get("amount", 0))
            payment_date = cf.get("paymentDate", today)

            matched = None
            # First try: best name+amount combo match
            best_score = 0.0
            for inv in remaining:
                inv_cust_name = (inv.get("customer", {}) or {}).get("name", "")
                score = _name_score(cust_name, inv_cust_name)
                outstanding = _to_float(inv.get("amountCurrencyOutstanding") or inv.get("amountCurrency", 0))
                # Boost score if amount also matches
                if target_amount and abs(outstanding - target_amount) < 1.0:
                    score += 0.5
                if score > best_score and score >= 0.4:
                    best_score = score
                    matched = inv
            # Second try: exact amount match (if name match failed)
            if not matched:
                for inv in remaining:
                    outstanding = _to_float(inv.get("amountCurrencyOutstanding") or inv.get("amountCurrency", 0))
                    if target_amount and abs(outstanding - target_amount) < 1.0:
                        matched = inv
                        break

            if matched:
                inv_cust_name = (matched.get("customer", {}) or {}).get("name", "")
                logger.info(f"bank_recon: matched '{cust_name}' ({target_amount}) → invoice #{matched['id']} ({inv_cust_name})")
                if _pay_invoice(matched, target_amount, payment_date):
                    remaining = [x for x in remaining if x["id"] != matched["id"]]
            else:
                logger.warning(f"bank_recon: no match for customer '{cust_name}' amount={target_amount}")
    else:
        # No CSV entities — pay ALL open customer invoices
        for inv in open_invoices:
            inv_id = inv.get("id")
            amount = _to_float(inv.get("amountCurrencyOutstanding") or inv.get("amountCurrency", 0))
            if not inv_id or not amount:
                continue
            try:
                client.put(f"/invoice/{inv_id}/:payment", {}, params={
                    "paymentDate": today,
                    "paymentTypeId": payment_type_id,
                    "paidAmount": amount,
                })
                paid_invoice_ids.append(inv_id)
            except Exception as e:
                logger.warning(f"Could not pay invoice {inv_id}: {e}")

    # Handle supplier invoices from CSV (outgoing payments)
    def _pay_supplier_invoice(si_id: int, amount: float, payment_date: str) -> bool:
        """Approve + register bank payment on a supplier invoice."""
        # Step 1: approve (may already be approved — catch error)
        try:
            client.put(f"/supplierInvoice/{si_id}/:approve", {})
        except Exception:
            pass
        # Step 2: add actual bank payment via :addPayment
        try:
            params = {"useDefaultPaymentType": "true", "paymentDate": payment_date or today}
            if amount:
                params["amount"] = amount
            client.post(f"/supplierInvoice/{si_id}/:addPayment", {}, params=params)
            return True
        except Exception as e:
            logger.warning(f"Could not add payment to supplier invoice {si_id}: {e}")
            return False

    try:
        open_supplier_invoices = client.get("/supplierInvoice", params={
            "invoiceDateFrom": date_from, "invoiceDateTo": date_to,
            "fields": "id,amountCurrency,supplier(id,name)",
            "count": 50,
        }).get("values", [])
        logger.info(f"bank_recon: found {len(open_supplier_invoices)} open supplier invoices")
        for si_dbg in open_supplier_invoices[:5]:
            logger.info(f"  SI#{si_dbg.get('id')}: amount={si_dbg.get('amountCurrency')}, supplier={si_dbg.get('supplier')}")

        if supplier_entities:
            remaining_si = list(open_supplier_invoices)
            matched_count = 0
            for se in supplier_entities:
                sf = se.get("fields", {})
                supp_name = sf.get("name", "")
                # Strip common prefixes like "Supplier " that LLM may add
                clean_name = re.sub(r'^(?:Supplier|Leverandør|Fournisseur|Lieferant|Proveedor)\s+', '', supp_name, flags=re.IGNORECASE)
                target_amount = _to_float(sf.get("amount", 0))
                payment_date = sf.get("paymentDate", today)

                matched_si = None
                best_si_score = 0.0
                for si in remaining_si:
                    vendor_name = (si.get("supplier", {}) or {}).get("name", "")
                    si_amount = _to_float(si.get("amountCurrency", 0))
                    # Try both original and cleaned name
                    score1 = _name_score(supp_name, vendor_name)
                    score2 = _name_score(clean_name, vendor_name) if clean_name != supp_name else 0.0
                    name_score = max(score1, score2)
                    amt_match = target_amount and abs(si_amount - target_amount) < 1.0
                    if name_score >= 0.4 and name_score > best_si_score:
                        best_si_score = name_score
                        matched_si = si
                    elif amt_match and not matched_si:
                        matched_si = si

                if matched_si:
                    si_id = matched_si["id"]
                    logger.info(f"bank_recon: matched supplier '{supp_name}' → SI#{si_id}")
                    if _pay_supplier_invoice(si_id, target_amount, payment_date):
                        approved_supplier_ids.append(si_id)
                        remaining_si = [x for x in remaining_si if x["id"] != si_id]
                        matched_count += 1
                else:
                    logger.warning(f"bank_recon: no match for supplier '{supp_name}' amount={target_amount}")

            # Fallback: if no supplier invoices were matched but there ARE open ones, pay them all
            if matched_count == 0 and remaining_si:
                logger.info(f"bank_recon: no matches found, paying all {len(remaining_si)} remaining supplier invoices")
                for si in remaining_si:
                    si_id = si.get("id")
                    if si_id and _pay_supplier_invoice(si_id, 0, today):
                        approved_supplier_ids.append(si_id)

            # Fallback: if no open supplier invoices exist at all, post payment vouchers
            if not open_supplier_invoices and supplier_entities:
                logger.info(f"bank_recon: no open supplier invoices found, posting payment vouchers for {len(supplier_entities)} outgoing CSV rows")
                bank_acct = client.get("/ledger/account", params={"number": "1920", "fields": "id", "count": 1}).get("values", [{}])[0].get("id")
                payables_acct = client.get("/ledger/account", params={"number": "2400", "fields": "id", "count": 1}).get("values", [{}])[0].get("id")
                if bank_acct and payables_acct:
                    for se in supplier_entities:
                        sf = se.get("fields", {})
                        amount = _to_float(sf.get("amount", 0))
                        payment_date = sf.get("paymentDate", today)
                        supp_name = sf.get("name", "Leverandør")
                        if amount:
                            try:
                                v = client.post("/ledger/voucher", {
                                    "date": payment_date,
                                    "description": f"Betaling: {supp_name}",
                                    "postings": [
                                        {"row": 1, "account": {"id": payables_acct}, "amountGross": amount, "amountGrossCurrency": amount, "description": supp_name, "date": payment_date},
                                        {"row": 2, "account": {"id": bank_acct}, "amountGross": -amount, "amountGrossCurrency": -amount, "description": supp_name, "date": payment_date},
                                    ],
                                })
                                v_id = v.get("value", {}).get("id")
                                if v_id:
                                    approved_supplier_ids.append(v_id)
                            except Exception as e:
                                logger.warning(f"bank_recon: voucher for '{supp_name}' failed: {e}")
        else:
            # No CSV entities — pay ALL open supplier invoices
            for si in open_supplier_invoices:
                si_id = si.get("id")
                if not si_id:
                    continue
                if _pay_supplier_invoice(si_id, 0, today):
                    approved_supplier_ids.append(si_id)
    except Exception as e:
        logger.warning(f"Could not process supplier invoices: {e}")

    return {
        "created": "bank_reconciliation",
        "paid_invoice_ids": paid_invoice_ids,
        "approved_supplier_invoice_ids": approved_supplier_ids,
    }


HANDLERS = {
    "register_payment": handle_register_payment,
    "bank_reconciliation": handle_bank_reconciliation,
    "create_employee": handle_create_employee,
    "create_customer": handle_create_customer,
    "create_product": handle_create_product,
    "create_department": handle_create_department,
    "create_project": handle_create_project,
    "create_invoice": handle_create_invoice,
    "create_invoice_with_payment": handle_create_invoice_with_payment,
    "create_credit_note": handle_create_credit_note,
    "project_billing": handle_project_billing,
    "register_supplier_invoice": handle_register_supplier_invoice,
    "create_travel_expense": handle_create_travel_expense,
    "update_employee": handle_update_employee,
    "update_customer": handle_update_customer,
    "update_product": handle_update_product,
    "delete_employee": handle_delete_employee,
    "delete_customer": handle_delete_customer,
    "delete_product": handle_delete_product,
    "delete_travel_expense": handle_delete_travel_expense,
    "assign_admin_role": handle_assign_admin_role,
    "process_payroll": handle_process_payroll,
    "register_project_hours": handle_register_project_hours,
    "create_accounting_dimension": handle_create_accounting_dimension,
    "late_fee_invoice": handle_late_fee_invoice,
    "post_vouchers": handle_post_vouchers,
    "analyze_and_create_projects": handle_analyze_and_create_projects,
    "project_lifecycle": handle_project_lifecycle,
}
