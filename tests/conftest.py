from __future__ import annotations

from datetime import date

import pytest

from frontdesk.config import Settings
from frontdesk.data import ClinicData
from frontdesk.tools.service import ClinicService


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(mode="rules", transport="inproc")


@pytest.fixture(scope="session")
def data(settings: Settings) -> ClinicData:
    return ClinicData.load(settings.data_dir)


@pytest.fixture()
def service(data: ClinicData) -> ClinicService:
    return ClinicService(data)


@pytest.fixture()
def today(data: ClinicData) -> date:
    return data.clinic.today


def open_ctx(service: ClinicService, contact: str | None, body: str = "hi", name: str | None = None,
             dob: str | None = None, at: str = "2026-11-09T08:00:00-06:00", mid: str = "T1") -> str:
    return service.find_patient(mid, contact, name, dob, body, at)["context_id"]
