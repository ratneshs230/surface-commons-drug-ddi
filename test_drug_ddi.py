"""Tests for /commons/drug — Drug Interaction Checker.

Two layers, mirroring legal_cite tests:

1. Pure-Python tests for resolution, lookup, banned-list, normalisation —
   no auth, no DB, no network. These are the authoritative coverage of
   the data layer.

2. HTTP integration tests against a running surface-api at
   127.0.0.1:3000, gated on SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID.
"""
from __future__ import annotations

import os

import httpx
import pytest

from app.api.commons.drug_ddi import (
    DRUG_PROFILES,
    INDIA_BANNED,
    INDIA_BRANDS,
    _DDI_INDEX,
    _banned_status,
    _expand_generics,
    _lookup_pair,
    _resolve_to_generic,
    _scan_pairs,
)
from app.api.commons.drug_ddi import _DrugItem


BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=30.0,
    )


# ── Resolution layer ──────────────────────────────────────────────────────

def test_resolve_indian_brand_to_generic_crocin():
    generics, conf, matched = _resolve_to_generic("Crocin 500")
    assert "paracetamol" in generics
    assert conf == 1.0
    assert matched == "crocin"


def test_resolve_combiflam_multi_generic():
    generics, conf, _ = _resolve_to_generic("Combiflam")
    assert "ibuprofen" in generics
    assert "paracetamol" in generics
    assert conf == 1.0


def test_resolve_dolo_with_dose_strip():
    generics, conf, matched = _resolve_to_generic("Dolo 650 mg")
    assert "paracetamol" in generics
    assert matched in {"dolo", "dolo 650"}


def test_resolve_generic_direct():
    generics, conf, matched = _resolve_to_generic("warfarin")
    assert generics == ["warfarin"]
    assert conf == 1.0


def test_resolve_unknown_returns_empty():
    generics, conf, _ = _resolve_to_generic("xyzfaketonin")
    assert generics == []
    assert conf == 0.0


def test_resolve_fuzzy_typo():
    generics, conf, _ = _resolve_to_generic("paracetamoll")
    assert "paracetamol" in generics
    assert conf == 0.8


# ── DDI lookup layer ──────────────────────────────────────────────────────

def test_lookup_warfarin_aspirin_major():
    hit = _lookup_pair("warfarin", "aspirin")
    assert hit is not None
    assert hit["severity"] == "major"
    assert "bleeding" in hit["recommendation"].lower() or "inr" in hit["recommendation"].lower()


def test_lookup_paracetamol_ibuprofen_minor():
    hit = _lookup_pair("paracetamol", "ibuprofen")
    assert hit is not None
    assert hit["severity"] == "minor"


def test_lookup_metformin_alcohol_moderate():
    hit = _lookup_pair("metformin", "alcohol")
    assert hit is not None
    assert hit["severity"] == "moderate"
    assert "lactic" in hit["mechanism"].lower()


def test_lookup_unknown_pair_returns_none():
    assert _lookup_pair("paracetamol", "vitamin c") is None


def test_lookup_is_unordered():
    a = _lookup_pair("warfarin", "aspirin")
    b = _lookup_pair("aspirin", "warfarin")
    assert a == b


# ── Banned-drug layer ─────────────────────────────────────────────────────

def test_banned_phenformin():
    s = _banned_status("phenformin", ["phenformin"])
    assert s is not None
    assert "lactic" in s.lower()


def test_banned_nimesulide_under12():
    s = _banned_status("nimesulide", ["nimesulide"])
    assert s is not None
    assert "12" in s or "hepato" in s.lower()


def test_not_banned_paracetamol():
    assert _banned_status("paracetamol", ["paracetamol"]) is None


def test_india_banned_size_at_least_30():
    assert len(INDIA_BANNED) >= 30


def test_india_brand_index_size_at_least_150():
    assert len(INDIA_BRANDS) >= 150


# ── End-to-end pair scan via _expand_generics + _scan_pairs ──────────────

def test_scan_warfarin_aspirin_brand_form():
    items = [_DrugItem(name="Warf"), _DrugItem(name="Ecosprin 75")]
    resolved = _expand_generics(items)
    pairs = _scan_pairs(resolved)
    sevs = [p["severity"] for p in pairs]
    assert "major" in sevs


def test_scan_paracetamol_ibuprofen_minor_via_brands():
    items = [_DrugItem(name="Crocin"), _DrugItem(name="Brufen")]
    resolved = _expand_generics(items)
    pairs = _scan_pairs(resolved)
    sevs = [p["severity"] for p in pairs]
    assert "minor" in sevs


def test_scan_combiflam_self_no_pair():
    # Combiflam = ibuprofen + paracetamol. Single drug → no DDI.
    items = [_DrugItem(name="Combiflam")]
    resolved = _expand_generics(items)
    pairs = _scan_pairs(resolved)
    # Same compound — internal pair (paracetamol+ibuprofen) is "minor", we
    # do allow surfacing it because it's an FDC where some clinicians worry
    # about renal stress.
    # The implementation only scans across distinct input items, so a
    # single-input combo won't surface a self-pair.
    assert pairs == []


def test_scan_three_drugs_warfarin_aspirin_metformin():
    items = [
        _DrugItem(name="Warfarin"),
        _DrugItem(name="Aspirin"),
        _DrugItem(name="Metformin"),
    ]
    resolved = _expand_generics(items)
    pairs = _scan_pairs(resolved)
    sevs = [p["severity"] for p in pairs]
    assert "major" in sevs


def test_unresolved_drug_in_check_flow():
    items = [_DrugItem(name="warfarin"), _DrugItem(name="xyzfaketonin")]
    resolved = _expand_generics(items)
    pairs = _scan_pairs(resolved)
    # No interaction surfaces because second drug is unresolved.
    assert pairs == []
    assert resolved[1]["generics"] == []


# ── Drug profile shape ────────────────────────────────────────────────────

def test_drug_profile_paracetamol_has_required_fields():
    p = DRUG_PROFILES["paracetamol"]
    for k in ("class", "indications", "common_doses", "common_side_effects"):
        assert p.get(k), f"missing or empty: {k}"


def test_ddi_index_has_at_least_30_pairs():
    assert len(_DDI_INDEX) >= 30


# ── HTTP integration (gated) ──────────────────────────────────────────────

def test_http_health_unauthed_open():
    # Health endpoint should be open per pattern.
    r = httpx.get(f"{BASE}/api/v1/commons/drug/health", timeout=10.0)
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200
    j = r.json()
    assert j["service"] == "commons.drug"
    assert j["india_banned_count"] >= 30
    assert j["india_brand_count"] >= 150
    assert j["ddi_pair_count"] >= 30
    assert "Educational use only" in j["disclaimer"]


def test_http_check_warfarin_aspirin_major():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/drug/check",
            json={"drugs": [{"name": "warfarin"}, {"name": "aspirin"}]},
        )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200, r.text
    j = r.json()
    assert any(p["severity"] == "major" for p in j["pairs"])
    assert j["disclaimer"].startswith("Educational use only")


def test_http_check_paracetamol_ibuprofen_minor():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/drug/check",
            json={"drugs": [{"name": "paracetamol"}, {"name": "ibuprofen"}]},
        )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200, r.text
    j = r.json()
    assert any(p["severity"] == "minor" for p in j["pairs"])


def test_http_check_metformin_alcohol_moderate():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/drug/check",
            json={"drugs": [{"name": "metformin"}, {"name": "alcohol"}]},
        )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200, r.text
    j = r.json()
    assert any(p["severity"] == "moderate" for p in j["pairs"])


def test_http_normalize_indian_prescription():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/drug/normalize",
            json={"raw_text": (
                "Rx: Tab Crocin 650 mg PO BD x 5 days, Cap Augmentin 625 mg "
                "TDS x 5 days, Tab Pan 40 mg OD before food."
            )},
        )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200, r.text
    j = r.json()
    matched = {d["matched"] for d in j["drugs"]}
    assert "crocin" in matched
    assert any(m in matched for m in ("augmentin",))
    assert any(m in matched for m in ("pan", "pan 40"))


def test_http_info_dolo_returns_paracetamol_with_brands():
    with _client() as c:
        r = c.get("/api/v1/commons/drug/info/Dolo")
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["generic_name"] == "paracetamol"
    assert "crocin" in j["indian_brands"] or "calpol" in j["indian_brands"]
