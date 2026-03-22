"""Tripletex v2 REST API wrapper."""

import logging
import requests
from typing import Any

logger = logging.getLogger(__name__)


class TripletexClient:
    def __init__(self, base_url: str, session_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = ("0", session_token)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Content-Type": "application/json"})

    def _raise(self, resp: requests.Response) -> None:
        if not resp.ok:
            body = resp.text[:500] if resp.content else ""
            msg = f"Tripletex {resp.status_code} {resp.request.method} {resp.url}: {body}"
            logger.error(msg)
            print(msg, flush=True)
            resp.raise_for_status()

    def get(self, path: str, params: dict = None) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", params=params)
        self._raise(resp)
        return resp.json()

    def post(self, path: str, body: dict, params: dict = None) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=body, params=params)
        self._raise(resp)
        return resp.json() if resp.content else {}

    def put(self, path: str, body: dict, params: dict = None) -> dict:
        resp = self.session.put(f"{self.base_url}{path}", json=body, params=params)
        self._raise(resp)
        return resp.json() if resp.content else {}

    def delete(self, path: str) -> None:
        resp = self.session.delete(f"{self.base_url}{path}")
        self._raise(resp)

    # --- Employee ---
    def create_employee(self, data: dict) -> dict:
        return self.post("/employee", data)

    def get_employees(self, fields: str = "id,firstName,lastName,email") -> list:
        result = self.get("/employee", params={"fields": fields, "count": 100})
        return result.get("values", [])

    def get_employee(self, employee_id: int, fields: str = "*") -> dict:
        result = self.get(f"/employee/{employee_id}", params={"fields": fields})
        return result.get("value", result)

    def update_employee(self, employee_id: int, data: dict) -> dict:
        return self.put(f"/employee/{employee_id}", data)

    def add_employee_to_role(self, employee_id: int, role_id: int) -> dict:
        return self.post(f"/employee/{employee_id}/employmentDetails", {"employeeId": employee_id, "roleId": role_id})

    # --- Customer ---
    def create_customer(self, data: dict) -> dict:
        return self.post("/customer", data)

    def get_customers(self, fields: str = "id,name,email") -> list:
        result = self.get("/customer", params={"fields": fields, "count": 100})
        return result.get("values", [])

    # --- Product ---
    def create_product(self, data: dict) -> dict:
        return self.post("/product", data)

    def get_products(self, fields: str = "id,name,number") -> list:
        result = self.get("/product", params={"fields": fields, "count": 100})
        return result.get("values", [])

    # --- Department ---
    def create_department(self, data: dict) -> dict:
        return self.post("/department", data)

    def get_departments(self, fields: str = "id,name") -> list:
        result = self.get("/department", params={"fields": fields, "count": 100})
        return result.get("values", [])

    # --- Project ---
    def create_project(self, data: dict) -> dict:
        return self.post("/project", data)

    def get_projects(self, fields: str = "id,name,number") -> list:
        result = self.get("/project", params={"fields": fields, "count": 100})
        return result.get("values", [])

    # --- Order / Invoice ---
    def create_order(self, data: dict) -> dict:
        return self.post("/order", data)

    def create_invoice(self, data: dict) -> dict:
        return self.post("/invoice", data)

    # --- Company settings ---
    def get_company_settings(self) -> dict:
        result = self.get("/company", params={"fields": "*"})
        return result.get("value", result)

    def update_company_module(self, module_data: dict) -> dict:
        return self.put("/company/modules", module_data)

    def get_modules(self) -> dict:
        result = self.get("/company/modules", params={"fields": "*"})
        return result.get("value", result)
