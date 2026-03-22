"""Local simulation tests for all task handlers."""
import urllib.request, urllib.error, json

BASE = "http://localhost:8080/solve"
CREDS = {
    "base_url": "https://kkpqfuj-amager.tripletex.dev/v2",
    "session_token": "eyJ0b2tlbklkIjoyMTQ3NjMyNzQ5LCJ0b2tlbiI6IjhmM2ZkZjRjLTVhZDAtNDczNy04MjY5LWNkZTNkNDkzYTg5MSJ9"
}

def solve(prompt, files=None):
    body = {"prompt": prompt, "tripletex_credentials": CREDS}
    if files:
        body["files"] = files
    payload = json.dumps(body).encode()
    req = urllib.request.Request(BASE, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

PASS = "✅"
FAIL = "❌"

def test(name, prompt, expect_completed=True):
    code, body = solve(prompt)
    ok = code == 200 and body.get("status") == "completed"
    print(f"  {PASS if ok else FAIL} {name}: HTTP {code} {body}")
    return ok

print("\n=== create_employee (no admin) ===")
test("basic employee NB", "Opprett ansatt Ingrid Solberg, epost ingrid.solberg@example.com")
test("basic employee EN", "Create employee John Smith, email john.smith@example.com, born 1985-03-15")
test("employee with phone", "Créez un employé: Marie Dupont, email marie.dupont@example.com, téléphone 90123456")

print("\n=== create_employee (with admin) ===")
test("admin employee", "Opprett ansatt Erik Dahl, epost erik.dahl@example.com. Han skal være kontoadministrator.")

print("\n=== create_department ===")
test("single dept", "Create a department called Sales")
test("multiple depts", 'Crie três departamentos no Tripletex: "Logistikk", "Administrasjon" e "Regnskap".')

print("\n=== create_customer ===")
test("basic customer", "Opprett kunde Fjord AS, epost kontakt@fjord.no, org.nr 998877665")
test("multiple customers", "Create two customers: Alpha Corp (alpha@corp.com) and Beta Ltd (beta@ltd.com)")

print("\n=== create_product ===")
test("product no VAT spec", "Opprett produkt Konsulenttime, produktnummer 1001, pris 1500 kr eks mva")
test("product 0% VAT", 'Opprett produktet "Avis" med produktnummer 2061. Prisen er 4150 kr eksklusiv MVA, og MVA-sats på 0 % for aviser skal nyttast.')
test("product 25% VAT", "Create product Widget, number 5001, price 200 NOK excluding 25% VAT")

print("\n=== create_project ===")
test("basic project", "Opprett prosjekt Nettside 2026, prosjektnummer 100, startdato 2026-04-01")

print("\n=== update_employee ===")
test("update phone", "Oppdater telefonnummeret til Ingrid Solberg til 92345678")

print("\n=== update_customer ===")
test("update customer email", "Endre epostadressen til Fjord AS til ny@fjord.no")

print("\n=== delete_employee ===")
test("delete employee", "Slett ansatt John Smith")

print("\n=== delete_customer ===")
test("delete customer", "Delete customer Alpha Corp")

print("\n=== create_travel_expense ===")
test("travel expense", "Registrer reiseutgift: Tjenestereise til Bergen, avreise 2026-03-25")

print("\n=== delete_travel_expense ===")
test("delete travel expense", "Slett reiseutgiftsrapporten for tjenestereisen til Bergen")

print("\n=== create_invoice (sandbox will 422 on invoice step — order creation is what matters) ===")
test("invoice FR", "Créez et envoyez une facture au client Étoile SARL (nº org. 976414284) de 20000 NOK hors TVA. La facture concerne Heures de conseil.")

print("\n=== register_payment (needs pre-populated invoice — will likely fail on sandbox) ===")
test("payment PT", 'O cliente Montanha Lda (org. nº 981506200) tem uma fatura pendente de 44250 NOK sem IVA por "Serviço de rede". Registe o pagamento total desta fatura.')
