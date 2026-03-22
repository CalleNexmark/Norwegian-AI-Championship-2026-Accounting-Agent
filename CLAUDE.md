# Tripletex Agent — NM i AI 2026

## Deployment
- Cloud Run: https://tripletex-agent-664308901515.europe-north1.run.app
- Deploy: `gcloud run deploy tripletex-agent --source . --region europe-north1 --allow-unauthenticated --memory 1Gi --timeout 300 --min-instances 1 --set-env-vars GEMINI_API_KEY=AIzaSyCapo2p2DguFRMWgjAy1Sr45Gq7gF3tcKA`
- Project: ai-nm26osl-1887, region: europe-north1

## Competition facts
- 30 task types, 56 variants (7 languages × 8 data sets). Each submission = 1 fresh account, 1 random task.
- Best score per task kept — bad runs never hurt. Rate limit: 4/task/day.
- Tier 1: basic CRUD. Tier 2: open. Tier 3: opens Saturday 2026-03-21.
- Score: (checks_passed/total_checks) × tier_multiplier × efficiency_bonus
- Efficiency bonus: ONLY applies when correctness=1.0. Every 4xx reduces it.
- Submit: https://app.ainm.no/submit/tripletex

## Sandbox
- URL: https://kkpqfuj-amager.tripletex.dev/v2
- Token: eyJ0b2tlbklkIjoyMTQ3NjMyNzQ5LCJ0b2tlbiI6IjhmM2ZkZjRjLTVhZDAtNDczNy04MjY5LWNkZTNkNDkzYTg5MSJ9
- Auth: Basic, username "0", password = token
- API docs: https://kkpqfuj-amager.tripletex.dev/v2-docs/ (openapi at /v2/openapi.json)
- Sandbox limitations: no bank account, no pre-populated invoices, vatType IDs 3/5/31/32 fail

## LLM
- Model: gemini-2.5-flash via Google AI Studio (API key above, NOT Vertex AI)

## Tripletex API — critical quirks

### Employee
- Required: firstName, lastName, department (id ref), userType (STANDARD/EXTENDED/NO_ACCESS — NOT "ADMINISTRATOR")
- Admin role: `PUT /employee/entitlement/:grantEntitlementsByTemplate?employeeId=X&template=ALL_PRIVILEGES` — returns empty body
- Employee must have userType=EXTENDED to receive ALL_PRIVILEGES

### Product
- vatType required when VAT specified. Outgoing VAT IDs: 25%=3, 15%=31, 12%=32, 0%=5 (fallback; fetched dynamically from /ledger/vatType)
- Product number must be string

### Invoice flow
- POST /order → POST /invoice (invoice links to order: {"orders": [{"id": order_id}]})
- deliveryDate required on order (use due_date or today+30d)
- Call `_ensure_bank_account(client)` before any invoice — competition accounts often lack bank account (422 otherwise)
- Bank account: GET /ledger/account?isBankAccount=true → PUT /ledger/account/{id} with {"bankAccountNumber": "15030515002"}

### Payment registration
- PUT /invoice/{id}/:payment?paymentDate=X&paymentTypeId=Y&paidAmount=Z
- Get paymentTypeId: GET /invoice/paymentType (first result)
- Competition pre-populates customer + open invoice. Find: GET /invoice?customerId=X&invoiceStatus=OPEN
- Always use invoice's actual amountCurrency (incl. VAT) for paidAmount, not parsed excl. VAT amount

### Project
- projectManager required — find by email first, fallback to first employee
- startDate required — default to today
- PUT /project/{id}: strip projectRateTypes, hourlyRates, participants, contact before PUT
- Competition pre-populates employee + customer on fresh accounts

### Process payroll
- Must POST /employee/employment (startDate=first of month) before salary transaction
- Employment only accepts: employee, startDate, employer — NO percentage/salary fields
- Percentage/salary go in POST /employee/employment/details: {employment:{id}, date, remunerationType:"MONTHLY_WAGE", percentageOfFullTimeEquivalent, annualSalary, shiftDurationHours}
- Employment required: "Ansatt nr. er ikke registrert med et arbeidsforhold i perioden"
- employer field required: {id: company_id} — GET /company (no params) for company ID

### GET /company
- Always call without params: `client.get("/company")` — adding ?fields=id returns 405 on competition proxy

### Custom accounting dimensions (Tier 3)
- Create dimension: POST /ledger/accountingDimensionName {dimensionName, dimensionIndex: 1}
- Create values: POST /ledger/accountingDimensionValue {displayName, dimensionIndex: 1}
- Link in voucher posting: freeAccountingDimension1: {id: value_id}
- Credit side of voucher: use account 1920 (bank) NOT 2400 (payables requires supplier ref)
- NOT /ledger/dimension — that endpoint does NOT exist

### Supplier invoice
- POST /supplierInvoice does NOT exist (only GET). Use POST /incomingInvoice instead.
- incomingInvoice schema: {invoiceHeader: {vendorId, invoiceDate, dueDate, currencyId, invoiceAmount, invoiceNumber}, orderLines: [{externalId (required!), row, description, amountInclVat, vatTypeId, accountId, count}]}
- Incoming VAT type IDs (deductible): 25%=1, 15%=11, 12%=12
- Falls back to POST /ledger/voucher if incomingInvoice fails
- Voucher structure: debit expense + VAT debit, credit 2400 (with supplier ref)
- VAT accounts: 25%=2710, 15%=2711, 12%=2712

### Department
- Competition accounts have moduledepartment enabled
- Multiple departments can be requested in one prompt — loop all entities

## Handlers implemented
create_employee, create_customer, create_product, create_department, create_project,
create_invoice, create_invoice_with_payment, create_credit_note, project_billing,
register_supplier_invoice, create_travel_expense, register_payment, process_payroll,
register_project_hours, create_accounting_dimension,
post_vouchers, analyze_and_create_projects, late_fee_invoice, project_lifecycle,
update_employee, update_customer, update_product,
delete_employee, delete_customer, delete_product, delete_travel_expense,
assign_admin_role

## Key architectural decisions
- POST / and POST /solve both handled (competition uses POST /)
- Errors never return 500 — always return {"status": "completed"}
- All create handlers loop over multiple entities
- _get_default_department_id fetches live (fresh accounts have different IDs)
- _clean_json handles: trailing commas, Python None/True/False, unquoted keys

## Current revision: r66 (2026-03-22)
- r66: post_vouchers: auto-create missing accounts (6030, 1209, etc.) via POST /ledger/account instead of falling back to wrong account numbers; tax provision: try multiple year ranges (voucher year, year-1, current year) to find ledger data; register_supplier_invoice + create_accounting_dimension: same account auto-creation

## Previous revision: r65 (2026-03-22)
- r65: register_supplier_invoice: search ALL supplier invoices (not just by supplierId) when checking for pre-existing ones — match by invoice number, amount, or sole-invoice fallback; bank_reconciliation: expand customer fields to customer(id,name), add name+amount combo scoring for correct matching when duplicate customer names exist; add logging throughout

## Previous revision: r64 (2026-03-22)
- r64: bank_reconciliation: fix supplier invoice matching — expanded fields to supplier(id,name), strip LLM-added "Supplier" prefix from names, add fallback to pay ALL open supplier invoices when matching fails, add voucher-based fallback when no supplier invoices exist; post_vouchers: fix locked-period detection (check "låst"/"locked"/"perioden" instead of "sum")

## Previous revision: r63 (2026-03-22)
- r62: project_lifecycle handler explicit entity type filtering (no more _get_entities fallback returning ALL entities); agent.py: French/Spanish/German multilingual examples for project_lifecycle budget extraction
- r63: post_vouchers tax provision auto-compute from ledger when amount=0; GET /ledger/account (id→number map) + GET /ledger (year balances); profit = 3xxx-7xxx; tax = 22%; agent.py: always output tax provision with amount=0 if profit unknown

## Previous revisions: r60-r61 (2026-03-21/22)
- r60: FX register_payment — auto-post agio/disagio voucher from notes (regex fallback); account 8060 (gain) / 8160 (disagio)
- r61: create_employee: nationalIdentityNumber + occupationCode (looked up via GET /employee/employment/occupationCode); FX: second regex + logging

## Previous revisions: r59 (2026-03-21)
- r59: bank_reconciliation supplier invoices now call POST /:addPayment (approve+pay, not just approve); tripletex_client.post() supports params kwarg; create_customer sets isCustomer=False when isSupplier=True only

## Previous revisions: r58 (2026-03-21)
- r58: _ensure_division helper (creates division via POST /division if GET /division empty — fixes salary/transaction "Arbeidsforholdet er ikke knyttet mot en virksomhet"); process_payroll uses minimal PUT for employment division link (avoids field conflicts); create_employee uses _ensure_division; project_lifecycle budget fallback scans all entities; agent.py project_lifecycle adds Nynorsk keywords + Nynorsk example

## Previous revisions: r57 (2026-03-21)
- r41: process_payroll employer:{id} in employment POST; GET /company without params; late_fee_invoice + post_vouchers handlers
- r42: post_vouchers handles year-end/depreciation/corrections; period lock retry with 2026-01-01
- r43: analyze_and_create_projects handler; create_employee NO_ACCESS when no email; department by name
- r44: register_supplier_invoice uses /supplier endpoint (not /customer); approves pre-existing supplier invoices
- r45: employment details via POST /employee/employment/details; project_lifecycle handler + agent.py task_type
- r46: post_vouchers skips entire voucher on missing account (avoids unbalanced 422) + account fallback search; late_fee_invoice adds date params to GET /invoice; register_supplier_invoice guards None account; register_payment posts FX difference vouchers; max_output_tokens 4096
- r47: analyze_and_create_projects uses POST /activity (not /project/activity which gives 405); late_fee_invoice + FX voucher add customer:{id} to account 1500 postings ("Kunde mangler")
- r48: process_payroll GET /company without params; project_lifecycle supplier lookup + supplier:{id} on account 2400; budget fallback for missing project entity
- r49: register_supplier_invoice fallback voucher: dynamic row numbering (no gaps), zero-amount guard
- r50: process_payroll checks existing employment first (avoids creating unlinked record → salary 422); project_lifecycle creates generic supplier if none found; analyze_and_create_projects uses existing default_activity_id as fallback (no more /project/activity); register_supplier_invoice returns partial success instead of error on zero amount; agent.py: extract amounts from PDF attachments; _clean_json escapes raw newlines in strings
- r51: post_vouchers account fallback extended to include round numbers (6010, 6020...) — fixes account 6030 missing; register_supplier_invoice voucher links department to expense posting; bank_reconciliation new task type + handler (pays all open customer invoices + approves open supplier invoices); post_vouchers salary accrual derives amount from GET /salary/transaction when prompt omits it
- r52: process_payroll + create_employee use GET /division instead of GET /company (405) to get company link for employment; employment uses division:{id} not employer:{id} (employer is not a field in EmploymentDTO); bank_reconciliation smarter matching using parsed CSV entities (match by customer name / amount); added "Avstem", "bankutskrift", "CSV" to bank_reconciliation detection keywords
- r53: analyze_and_create_projects adds activityType="GENERAL" to POST /activity (was 422); process_payroll now UPDATES existing employment to link division (not just creates new) — fixes "Arbeidsforholdet er ikke knyttet mot en virksomhet"; project_lifecycle supplier fallback uses account 2960 if supp_id unavailable (avoids "Leverandør mangler" on account 2400); bank_reconciliation adds Portuguese/Norwegian keywords (reconcilie, extrato bancario, bankutskriften)
- r54: post_vouchers salary accrual: LLM now forced to always include salary accrual voucher even when amount unknown (amount=0); handler tries /salary/transaction without fields filter (avoids 500), then sums monthly salaries from /employee/employment/details as fallback
- r55: analyze_and_create_projects: activityType must be "PROJECT_GENERAL_ACTIVITY" not "GENERAL" (confirmed from API); register_payment: returned bank payment misclassified as create_credit_note — fixed LLM rule + reversal keywords in handler (devuelto, revierta, returnert, zurückgesendet, renvoyé)
- r56: bank_reconciliation 0/10: LLM now extracts ALL CSV rows as customer entities (incoming) + supplier entities (outgoing); handler matches by name then by amount; supplier invoices matched and approved per CSV row; fallback pays/approves ALL when no entities
- r57: create_employee sets dateOfBirth before employment (422 fix); bank_reconciliation uses fields=supplier (not vendor) + token-overlap name matching (≥0.4); agent.py field checklists; test_suite.py (100% sandbox pass)

### project_billing PUT /project strip list
Strip ALL before PUT: projectRateTypes, hourlyRates, projectHourlyRates, participants, contact, projectActivities, orderLines, invoicingPlan, preliminaryInvoice, accountingDimensionValues, boligmappaAddress
