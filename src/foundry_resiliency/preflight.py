"""Read-only ARM deployment verification before explicitly approved, billable inference."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .config import Backend, Settings

RESOURCE_ID = re.compile(
    r"^/subscriptions/([a-fA-F0-9-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)"
    r"/providers/Microsoft\.CognitiveServices/accounts/([A-Za-z0-9-]+)$",
    re.IGNORECASE,
)
API_VERSION = "2026-07-01"


class AccountProperties(BaseModel):
    customSubDomainName: str


class AccountDocument(BaseModel):
    location: str
    properties: AccountProperties


class ModelDocument(BaseModel):
    name: str
    version: str


class DeploymentProperties(BaseModel):
    model: ModelDocument
    provisioningState: Literal["Succeeded"]


class SkuDocument(BaseModel):
    name: str
    capacity: int | None = Field(default=None, ge=0)


class DeploymentDocument(BaseModel):
    sku: SkuDocument
    properties: DeploymentProperties


def validate_documents(backend: Backend, account: object, deployment: object) -> None:
    if backend.azure_location is None:
        raise ValueError(f"Set private azure_location in local config for {backend.id}")
    resource = AccountDocument.model_validate(account)
    deployed = DeploymentDocument.model_validate(deployment)
    if resource.location.lower().replace(" ", "") != backend.azure_location.lower():
        raise ValueError(f"ARM resource region mismatch for {backend.id}")
    hostname = urlsplit(backend.base_url).hostname
    if hostname != f"{resource.properties.customSubDomainName}.openai.azure.com":
        raise ValueError(f"ARM endpoint mismatch for {backend.id}; use direct OpenAI endpoint")
    if deployed.sku.name != "GlobalStandard":
        raise ValueError(f"Expected GlobalStandard SKU for {backend.id}")
    if deployed.properties.model.name != backend.model:
        raise ValueError(f"ARM model mismatch for {backend.id}")
    if deployed.properties.model.version != backend.version:
        raise ValueError(f"ARM model version mismatch for {backend.id}")


def _read_resource(az: str, resource_id: str) -> object:
    completed = subprocess.run(
        [
            az,
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{resource_id}?api-version={API_VERSION}",
            "--output",
            "json",
            "--only-show-errors",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def preflight(settings: Settings) -> dict[str, object]:
    az = shutil.which("az")
    if az is None:
        raise ValueError(
            "Azure CLI is required for read-only preflight; install it and run az login"
        )
    accounts: dict[str, object] = {}
    checked = []
    for backend in settings.backends:
        resource_id = backend.resource_id
        if (
            not resource_id
            or not RESOURCE_ID.fullmatch(resource_id)
            or "00000000-0000-0000-0000-000000000000" in resource_id
            or "replace-" in backend.base_url
        ):
            raise ValueError(f"Replace example endpoint and ARM resource_id for {backend.id}")
        if resource_id not in accounts:
            accounts[resource_id] = _read_resource(az, resource_id)
        document = _read_resource(az, f"{resource_id}/deployments/{backend.deployment}")
        validate_documents(backend, accounts[resource_id], document)
        checked.append({"backend_id": backend.id, "sku": "GlobalStandard", "verified": True})
    return {
        "mode": "read_only_arm_preflight",
        "deployments": checked,
        "quota_scope_and_available_capacity": "not_verified_check_foundry_quota_scope",
    }
