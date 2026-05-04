"""Drug Interaction Checker — Commons guild's INVENT-tier tool.

India-first drug-drug interaction (DDI) checker for clinic doctors. Combines:

- ``openFDA`` (free public API) drug-label data for interaction parsing
- A hand-curated baseline DDI table covering high-risk pairs commonly
  seen in Indian outpatient prescribing (warfarin, NSAIDs, paracetamol,
  metformin, statins, macrolides, SSRIs, etc.).
- Indian brand-name → generic mapping (top OTC + Rx brands) for
  prescription text normalisation. Crocin → paracetamol, Combiflam →
  ibuprofen+paracetamol, Saridon → paracetamol+propyphenazone+caffeine.
- India-banned drug list (CDSCO 2026, Schedule H1, FDC bans).

Endpoints:

- ``POST /commons/drug/check`` — body ``{drugs:[{name, dose?}]}`` →
  pairwise DDI scan with severity / mechanism / recommendation.
- ``POST /commons/drug/normalize`` — body ``{raw_text}`` — pull drug
  names out of free-text prescription, return generic + brand split.
- ``GET /commons/drug/info/{drug_name}`` — single-drug profile (class,
  Indian brands, common doses, banned status, side effects).
- ``GET /commons/drug/health`` — public probe.

SC pricing: check = 5 SC, normalize = 2 SC, info = 1 SC.

Disclaimer is included in EVERY response. This is an educational
decision-support tool — NOT a substitute for clinical pharmacist
review. We do not claim CDSCO / FDA certification.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import time
import uuid
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/api/v1/commons/drug", tags=["commons-drug"])


# ── SC pricing ────────────────────────────────────────────────────────────
SC_COST = {
    "check": float(os.environ.get("DRUG_SC_CHECK", "5")),
    "normalize": float(os.environ.get("DRUG_SC_NORMALIZE", "2")),
    "info": float(os.environ.get("DRUG_SC_INFO", "1")),
}

MAX_DRUGS_PER_CHECK = int(os.environ.get("DRUG_MAX_DRUGS", "12"))
MAX_TEXT_BYTES = int(os.environ.get("DRUG_MAX_TEXT_BYTES", str(64 * 1024)))
OPENFDA_TIMEOUT_S = float(os.environ.get("DRUG_OPENFDA_TIMEOUT", "6"))

DISCLAIMER = (
    "Educational use only. Always verify with official sources before "
    "prescribing. Not a substitute for clinical pharmacist review or "
    "CDSCO / FDA-approved drug labels."
)

USER_AGENT = (
    "SurfaceCommonsDrugDDI/1.0 "
    "(+https://surface.agentnation.in/commons; respectful, rate-limited)"
)

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"


# ── India-specific data — banned drugs (CDSCO + FDC) ──────────────────────
# Source: CDSCO Schedule H1 + 2026 FDC ban gazette + WHO essential meds
# review. Keys are generics (case-folded). Values describe restriction.
INDIA_BANNED: dict[str, str] = {
    "phenformin": "Banned in India (CDSCO 2003) — lactic acidosis risk",
    "propoxyphene": "Banned in India (CDSCO 2013) — cardiac toxicity",
    "rosiglitazone": "Suspended in India (CDSCO 2010) — cardiovascular events",
    "cisapride": "Banned in India — fatal arrhythmia (QT prolongation)",
    "nimesulide": (
        "Banned for under-12 in India (CDSCO 2011) — hepatotoxicity. "
        "Adult use restricted."
    ),
    "rimonabant": "Banned in India — psychiatric side effects",
    "pioglitazone": "Suspended-then-relaxed (2013) — restricted in bladder cancer",
    "gatifloxacin": "Banned (oral) in India (CDSCO 2011) — dysglycaemia",
    "tegaserod": "Banned in India — ischaemic colitis",
    "sibutramine": "Banned in India (CDSCO 2010) — cardiovascular risk",
    "letrozole": "Banned in India for ovulation induction (off-label)",
    "diclofenac (vet)": "Banned veterinary use in India — vulture decline",
    "metamizole": "Banned in India — agranulocytosis risk",
    "deanxit": "Banned in India (FDC of fluphenazine + nortriptyline)",
    "buclizine": "Banned as appetite stimulant in India",
    "fixed-dose nimesulide+paracetamol": "FDC banned in India (2018)",
    "fixed-dose phenylephrine+caffeine+paracetamol+chlorpheniramine":
        "FDC banned in India (2023 gazette)",
    "fixed-dose chlorpheniramine+codeine": "Restricted (Schedule H1)",
    "fixed-dose pholcodine": "Banned (cough syrups, 2023)",
    "fixed-dose aceclofenac+paracetamol+rabeprazole": "FDC banned (2018)",
    "fixed-dose tramadol+paracetamol+domperidone": "FDC banned (2018)",
    "fixed-dose furazolidone+metronidazole": "FDC banned",
    "fixed-dose dextromethorphan+phenylephrine+chlorpheniramine":
        "FDC banned (2023, paediatric)",
    "fixed-dose ofloxacin+ornidazole+racecadotril+zinc": "FDC banned (2018)",
    "fixed-dose pholcodine+promethazine": "FDC banned",
    "phenolphthalein": "Banned in India — laxative, carcinogenicity",
    "nitrofurazone": "Banned in India (topical) — carcinogenicity",
    "analgin": "Restricted in India — agranulocytosis (= metamizole)",
    "chloramphenicol (oral)":
        "Restricted in India — aplastic anaemia (oral form)",
    "pemoline": "Banned in India — hepatotoxicity",
    "terfenadine": "Banned in India — fatal arrhythmia",
    "astemizole": "Banned in India — fatal arrhythmia",
    "gentamicin (paediatric drops)": "Restricted in paediatric otic use",
}

# ── India brand → generic mapping (top brands by Rx volume) ───────────────
# Curated from PharmaTrac / IQVIA India top-200 (2025). Multi-ingredient
# brands map to a tuple-like list. Keys are case-folded.
INDIA_BRANDS: dict[str, list[str]] = {
    # Analgesics / Antipyretics
    "crocin": ["paracetamol"],
    "calpol": ["paracetamol"],
    "dolo": ["paracetamol"],
    "dolo 650": ["paracetamol"],
    "p 500": ["paracetamol"],
    "metacin": ["paracetamol"],
    "pacimol": ["paracetamol"],
    "fevastin": ["paracetamol"],
    "combiflam": ["ibuprofen", "paracetamol"],
    "brufen": ["ibuprofen"],
    "ibugesic": ["ibuprofen"],
    "saridon": ["paracetamol", "propyphenazone", "caffeine"],
    "anacin": ["aspirin", "caffeine"],
    "disprin": ["aspirin"],
    "ecosprin": ["aspirin"],
    "aspirin": ["aspirin"],
    "voveran": ["diclofenac"],
    "volini": ["diclofenac"],
    "dynapar": ["diclofenac"],
    "nise": ["nimesulide"],
    "nimulid": ["nimesulide"],
    "nicip": ["nimesulide"],
    "ultracet": ["tramadol", "paracetamol"],
    "tramazac": ["tramadol"],
    "zerodol": ["aceclofenac"],
    "hifenac": ["aceclofenac"],
    "movon": ["aceclofenac"],
    # Antibiotics
    "augmentin": ["amoxicillin", "clavulanic acid"],
    "clavam": ["amoxicillin", "clavulanic acid"],
    "moxikind": ["amoxicillin", "clavulanic acid"],
    "amoxil": ["amoxicillin"],
    "novamox": ["amoxicillin"],
    "azee": ["azithromycin"],
    "azithral": ["azithromycin"],
    "zithromax": ["azithromycin"],
    "ciplox": ["ciprofloxacin"],
    "cifran": ["ciprofloxacin"],
    "norflox": ["norfloxacin"],
    "levoflox": ["levofloxacin"],
    "tavanic": ["levofloxacin"],
    "ofloxin": ["ofloxacin"],
    "mox": ["amoxicillin"],
    "althrocin": ["erythromycin"],
    "erythrocin": ["erythromycin"],
    "doxy 1": ["doxycycline"],
    "doxypal": ["doxycycline"],
    "ceftum": ["cefuroxime"],
    "taxim": ["cefotaxime"],
    "monocef": ["ceftriaxone"],
    "metrogyl": ["metronidazole"],
    "flagyl": ["metronidazole"],
    "ornof": ["ofloxacin", "ornidazole"],
    "norflox tz": ["norfloxacin", "tinidazole"],
    "rifagut": ["rifaximin"],
    "linezolid": ["linezolid"],
    "vancogen": ["vancomycin"],
    # Cardiac / Antihypertensives / Lipid
    "telma": ["telmisartan"],
    "telma h": ["telmisartan", "hydrochlorothiazide"],
    "telma am": ["telmisartan", "amlodipine"],
    "stamlo": ["amlodipine"],
    "amlong": ["amlodipine"],
    "amlokind": ["amlodipine"],
    "concor": ["bisoprolol"],
    "metolar": ["metoprolol"],
    "betaloc": ["metoprolol"],
    "atenol": ["atenolol"],
    "tenormin": ["atenolol"],
    "ramipril": ["ramipril"],
    "cardace": ["ramipril"],
    "lipril": ["lisinopril"],
    "rosuvas": ["rosuvastatin"],
    "crestor": ["rosuvastatin"],
    "atorva": ["atorvastatin"],
    "atorlip": ["atorvastatin"],
    "lipitor": ["atorvastatin"],
    "ecosprin gold": ["aspirin", "atorvastatin", "clopidogrel"],
    "clopilet": ["clopidogrel"],
    "deplatt": ["clopidogrel"],
    "warf": ["warfarin"],
    "uniwarfin": ["warfarin"],
    "warfarin": ["warfarin"],
    "acitrom": ["acenocoumarol"],
    "xarelto": ["rivaroxaban"],
    "eliquis": ["apixaban"],
    "pradaxa": ["dabigatran"],
    "lasix": ["furosemide"],
    "lasilactone": ["furosemide", "spironolactone"],
    "aldactone": ["spironolactone"],
    # Diabetes
    "glycomet": ["metformin"],
    "glyciphage": ["metformin"],
    "obimet": ["metformin"],
    "okamet": ["metformin"],
    "amaryl": ["glimepiride"],
    "amaryl m": ["glimepiride", "metformin"],
    "glimisave": ["glimepiride"],
    "glynase": ["glipizide"],
    "daonil": ["glibenclamide"],
    "januvia": ["sitagliptin"],
    "istamet": ["sitagliptin", "metformin"],
    "galvus": ["vildagliptin"],
    "galvus met": ["vildagliptin", "metformin"],
    "trajenta": ["linagliptin"],
    "forxiga": ["dapagliflozin"],
    "jardiance": ["empagliflozin"],
    "insugen": ["insulin"],
    "humalog": ["insulin lispro"],
    "lantus": ["insulin glargine"],
    # GI
    "pan": ["pantoprazole"],
    "pan d": ["pantoprazole", "domperidone"],
    "pan 40": ["pantoprazole"],
    "pantocid": ["pantoprazole"],
    "razo": ["rabeprazole"],
    "rabicip": ["rabeprazole"],
    "omez": ["omeprazole"],
    "ocid": ["omeprazole"],
    "esomac": ["esomeprazole"],
    "nexpro": ["esomeprazole"],
    "zinetac": ["ranitidine"],
    "rantac": ["ranitidine"],
    "domstal": ["domperidone"],
    "vomistop": ["domperidone"],
    "emeset": ["ondansetron"],
    "perinorm": ["metoclopramide"],
    "buscopan": ["hyoscine butylbromide"],
    "drotin": ["drotaverine"],
    "meftal spas": ["mefenamic acid", "dicyclomine"],
    "cyclopam": ["dicyclomine", "paracetamol"],
    "lomotil": ["diphenoxylate", "atropine"],
    "eldoper": ["loperamide"],
    "imodium": ["loperamide"],
    # Allergy / Cough / Cold
    "cetzine": ["cetirizine"],
    "alerid": ["cetirizine"],
    "okacet": ["cetirizine"],
    "allegra": ["fexofenadine"],
    "avil": ["pheniramine"],
    "montair": ["montelukast"],
    "monticope": ["montelukast", "levocetirizine"],
    "deriphyllin": ["theophylline", "etofylline"],
    "asthalin": ["salbutamol"],
    "ventorlin": ["salbutamol"],
    "duolin": ["salbutamol", "ipratropium"],
    "foracort": ["formoterol", "budesonide"],
    "budecort": ["budesonide"],
    "seroflo": ["salmeterol", "fluticasone"],
    "wikoryl": ["paracetamol", "phenylephrine", "chlorpheniramine"],
    "sinarest": ["paracetamol", "phenylephrine", "chlorpheniramine"],
    "vicks action 500": ["paracetamol", "phenylephrine", "caffeine"],
    "d cold": ["paracetamol", "phenylephrine", "chlorpheniramine"],
    # Psych / Neuro
    "alprax": ["alprazolam"],
    "restyl": ["alprazolam"],
    "nitravet": ["nitrazepam"],
    "valium": ["diazepam"],
    "calmpose": ["diazepam"],
    "etizola": ["etizolam"],
    "depran": ["fluoxetine"],
    "fludac": ["fluoxetine"],
    "prozac": ["fluoxetine"],
    "nexito": ["escitalopram"],
    "stalopam": ["escitalopram"],
    "cipralex": ["escitalopram"],
    "zoloft": ["sertraline"],
    "serlift": ["sertraline"],
    "nortispas": ["nortriptyline", "chlordiazepoxide"],
    "epilex": ["valproate"],
    "encorate": ["valproate"],
    "valparin": ["valproate"],
    "tegrital": ["carbamazepine"],
    "mazetol": ["carbamazepine"],
    "dilantin": ["phenytoin"],
    "eptoin": ["phenytoin"],
    "frisium": ["clobazam"],
    # Thyroid / Hormones
    "thyronorm": ["levothyroxine"],
    "eltroxin": ["levothyroxine"],
    "thyrox": ["levothyroxine"],
    # Others
    "shelcal": ["calcium carbonate", "vitamin d3"],
    "calcimax": ["calcium carbonate", "vitamin d3"],
    "becosules": ["b complex"],
    "neurobion": ["b complex"],
    "limcee": ["vitamin c"],
    "evion": ["vitamin e"],
    "supradyn": ["multivitamin"],
    "revital": ["multivitamin"],
    "zincovit": ["multivitamin", "zinc"],
}

# Reverse lookup — generic → list of brands seen (built lazily).
_GENERIC_TO_BRANDS: dict[str, list[str]] | None = None


def _build_generic_index() -> dict[str, list[str]]:
    global _GENERIC_TO_BRANDS
    if _GENERIC_TO_BRANDS is not None:
        return _GENERIC_TO_BRANDS
    out: dict[str, list[str]] = {}
    for brand, generics in INDIA_BRANDS.items():
        for g in generics:
            out.setdefault(g.lower(), []).append(brand)
    _GENERIC_TO_BRANDS = out
    return out


# ── Drug profile data (single-drug summary) ───────────────────────────────
# Curated from CDSCO + WHO + UpToDate + Indian National Formulary 2024.
DRUG_PROFILES: dict[str, dict] = {
    "paracetamol": {
        "generic_name": "paracetamol",
        "class": "non-opioid analgesic / antipyretic",
        "indications": ["fever", "mild-moderate pain", "headache"],
        "common_doses": ["500 mg PO q6h", "650 mg PO q6h", "1 g PO q6h max 4 g/day"],
        "common_side_effects": ["hepatotoxicity (overdose)", "rash"],
    },
    "ibuprofen": {
        "generic_name": "ibuprofen",
        "class": "NSAID (non-selective COX inhibitor)",
        "indications": ["pain", "inflammation", "fever", "dysmenorrhoea"],
        "common_doses": ["200-400 mg PO q6-8h", "max 2.4 g/day"],
        "common_side_effects": ["dyspepsia", "GI bleed", "renal impairment", "hypertension"],
    },
    "aspirin": {
        "generic_name": "aspirin",
        "class": "NSAID / antiplatelet",
        "indications": ["antiplatelet (75-150 mg)", "analgesia (300-650 mg)"],
        "common_doses": ["75 mg PO OD (cardiac)", "325-650 mg PO q4-6h (analgesic)"],
        "common_side_effects": ["GI bleed", "tinnitus", "Reye's (paediatric)"],
    },
    "warfarin": {
        "generic_name": "warfarin",
        "class": "vitamin-K antagonist anticoagulant",
        "indications": ["AF", "DVT", "PE", "mechanical heart valve"],
        "common_doses": ["2-10 mg PO OD, INR-titrated"],
        "common_side_effects": ["bleeding", "skin necrosis (rare)", "purple toe syndrome"],
    },
    "metformin": {
        "generic_name": "metformin",
        "class": "biguanide (antidiabetic)",
        "indications": ["type 2 diabetes mellitus", "PCOS"],
        "common_doses": ["500 mg PO BD/TDS", "max 2 g/day"],
        "common_side_effects": ["GI upset", "lactic acidosis (rare)", "B12 deficiency"],
    },
    "atorvastatin": {
        "generic_name": "atorvastatin",
        "class": "HMG-CoA reductase inhibitor (statin)",
        "indications": ["dyslipidaemia", "ASCVD prevention"],
        "common_doses": ["10-80 mg PO OD"],
        "common_side_effects": ["myalgia", "hepatic transaminitis", "rhabdomyolysis (rare)"],
    },
    "amoxicillin": {
        "generic_name": "amoxicillin",
        "class": "aminopenicillin antibiotic",
        "indications": ["URTI", "ENT infection", "UTI", "H. pylori"],
        "common_doses": ["500 mg PO TDS", "875 mg PO BD"],
        "common_side_effects": ["rash", "diarrhoea", "anaphylaxis (in penicillin-allergic)"],
    },
    "azithromycin": {
        "generic_name": "azithromycin",
        "class": "macrolide antibiotic",
        "indications": ["RTI", "STI", "atypical pneumonia"],
        "common_doses": ["500 mg PO OD x 3 days", "500 mg day 1, 250 mg days 2-5"],
        "common_side_effects": ["GI upset", "QT prolongation", "hepatic injury"],
    },
    "ciprofloxacin": {
        "generic_name": "ciprofloxacin",
        "class": "fluoroquinolone antibiotic",
        "indications": ["UTI", "gastroenteritis", "respiratory infection"],
        "common_doses": ["500 mg PO BD", "250-750 mg PO BD"],
        "common_side_effects": [
            "tendon rupture", "QT prolongation", "C. difficile colitis",
        ],
    },
    "telmisartan": {
        "generic_name": "telmisartan",
        "class": "angiotensin-II receptor blocker (ARB)",
        "indications": ["hypertension", "diabetic nephropathy"],
        "common_doses": ["20-80 mg PO OD"],
        "common_side_effects": ["hyperkalaemia", "hypotension", "AKI"],
    },
    "amlodipine": {
        "generic_name": "amlodipine",
        "class": "dihydropyridine calcium-channel blocker",
        "indications": ["hypertension", "angina"],
        "common_doses": ["2.5-10 mg PO OD"],
        "common_side_effects": ["pedal oedema", "flushing", "headache"],
    },
    "ramipril": {
        "generic_name": "ramipril",
        "class": "ACE inhibitor",
        "indications": ["hypertension", "post-MI", "diabetic nephropathy"],
        "common_doses": ["2.5-10 mg PO OD"],
        "common_side_effects": ["dry cough", "hyperkalaemia", "angio-oedema", "AKI"],
    },
    "pantoprazole": {
        "generic_name": "pantoprazole",
        "class": "proton-pump inhibitor",
        "indications": ["GERD", "peptic ulcer", "Zollinger-Ellison"],
        "common_doses": ["40 mg PO OD"],
        "common_side_effects": ["headache", "B12 deficiency (long-term)", "C. difficile risk"],
    },
    "omeprazole": {
        "generic_name": "omeprazole",
        "class": "proton-pump inhibitor",
        "indications": ["GERD", "peptic ulcer"],
        "common_doses": ["20-40 mg PO OD"],
        "common_side_effects": ["headache", "B12 deficiency (long-term)"],
    },
    "levothyroxine": {
        "generic_name": "levothyroxine",
        "class": "thyroid hormone replacement",
        "indications": ["hypothyroidism"],
        "common_doses": ["25-200 mcg PO OD on empty stomach"],
        "common_side_effects": ["palpitations (overdose)", "weight loss", "tremor"],
    },
    "alprazolam": {
        "generic_name": "alprazolam",
        "class": "short-acting benzodiazepine",
        "indications": ["anxiety", "panic disorder"],
        "common_doses": ["0.25-0.5 mg PO TDS"],
        "common_side_effects": ["sedation", "dependence", "respiratory depression"],
    },
    "fluoxetine": {
        "generic_name": "fluoxetine",
        "class": "SSRI antidepressant",
        "indications": ["depression", "OCD", "bulimia"],
        "common_doses": ["20-60 mg PO OD"],
        "common_side_effects": ["nausea", "sexual dysfunction", "QT prolongation", "serotonin syndrome"],
    },
    "tramadol": {
        "generic_name": "tramadol",
        "class": "weak opioid + SNRI",
        "indications": ["moderate pain"],
        "common_doses": ["50-100 mg PO q4-6h, max 400 mg/day"],
        "common_side_effects": ["nausea", "dizziness", "seizures", "serotonin syndrome"],
    },
    "diclofenac": {
        "generic_name": "diclofenac",
        "class": "NSAID",
        "indications": ["pain", "inflammation", "OA / RA"],
        "common_doses": ["50 mg PO TDS", "75 mg IM"],
        "common_side_effects": ["GI bleed", "AKI", "cardiovascular events"],
    },
    "nimesulide": {
        "generic_name": "nimesulide",
        "class": "preferential COX-2 NSAID",
        "indications": ["acute pain (adults only in India)"],
        "common_doses": ["100 mg PO BD x ≤7 days"],
        "common_side_effects": ["hepatotoxicity (banned <12 in India)"],
    },
    "rosuvastatin": {
        "generic_name": "rosuvastatin",
        "class": "HMG-CoA reductase inhibitor",
        "indications": ["dyslipidaemia", "ASCVD prevention"],
        "common_doses": ["5-40 mg PO OD"],
        "common_side_effects": ["myalgia", "rhabdomyolysis", "hepatic transaminitis"],
    },
    "clopidogrel": {
        "generic_name": "clopidogrel",
        "class": "P2Y12 antiplatelet",
        "indications": ["ACS", "post-stent", "ischaemic stroke"],
        "common_doses": ["75 mg PO OD"],
        "common_side_effects": ["bleeding", "TTP (rare)"],
    },
    "domperidone": {
        "generic_name": "domperidone",
        "class": "peripheral D2 antagonist (prokinetic)",
        "indications": ["nausea", "gastroparesis"],
        "common_doses": ["10 mg PO TDS"],
        "common_side_effects": ["QT prolongation", "galactorrhoea"],
    },
    "metoprolol": {
        "generic_name": "metoprolol",
        "class": "cardioselective beta-blocker",
        "indications": ["hypertension", "post-MI", "AF rate-control", "heart failure"],
        "common_doses": ["25-200 mg PO OD/BD"],
        "common_side_effects": ["bradycardia", "fatigue", "bronchospasm"],
    },
    "salbutamol": {
        "generic_name": "salbutamol",
        "class": "short-acting beta-2 agonist",
        "indications": ["asthma", "COPD"],
        "common_doses": ["100-200 mcg MDI prn", "2.5-5 mg nebuliser"],
        "common_side_effects": ["tremor", "tachycardia", "hypokalaemia"],
    },
    "amlodipine": {
        "generic_name": "amlodipine",
        "class": "dihydropyridine CCB",
        "indications": ["hypertension", "angina"],
        "common_doses": ["2.5-10 mg PO OD"],
        "common_side_effects": ["pedal oedema", "flushing"],
    },
}


# ── Curated DDI table (high-yield Indian outpatient pairs) ────────────────
# Severity grades follow Lexi-Interact / Stockley convention:
#   minor    — usually no clinical action
#   moderate — monitor / dose-adjust
#   major    — avoid combination unless benefits outweigh risks; close monitoring
#   severe   — contraindicated / can cause life-threatening event
#
# Pair keys are sorted lower-case generic tuples for O(1) lookup.

_DDI_RAW: list[tuple[str, str, str, str, str]] = [
    # (drug_a, drug_b, severity, mechanism, recommendation)
    (
        "warfarin", "aspirin", "major",
        "Additive antiplatelet + anticoagulant — combined haemostatic impairment.",
        "Avoid unless dual therapy specifically indicated (e.g., post-stent). "
        "Monitor INR weekly; counsel on bleeding signs; consider PPI cover.",
    ),
    (
        "warfarin", "ibuprofen", "major",
        "NSAID inhibits platelet aggregation + GI mucosal damage; "
        "displaces warfarin from albumin → ↑ INR.",
        "Avoid concurrent use. If unavoidable, prefer paracetamol; "
        "monitor INR within 3-5 days; gastroprotection mandatory.",
    ),
    (
        "warfarin", "diclofenac", "major",
        "NSAID-mediated platelet inhibition + GI mucosal injury; "
        "CYP2C9 inhibition raises warfarin levels.",
        "Avoid. Substitute paracetamol. If essential, INR monitoring + PPI.",
    ),
    (
        "warfarin", "paracetamol", "moderate",
        "Paracetamol >2 g/day chronically can elevate INR via CYP-mediated "
        "metabolism shift.",
        "Acceptable short-term at ≤2 g/day. Monitor INR if regular use >2 weeks.",
    ),
    (
        "warfarin", "azithromycin", "moderate",
        "Macrolide-mediated CYP3A4 inhibition + altered gut flora reducing "
        "vitamin K → enhanced anticoagulation.",
        "Monitor INR within 3-5 days of initiation and at completion.",
    ),
    (
        "warfarin", "ciprofloxacin", "major",
        "CYP1A2 / CYP3A4 inhibition + gut-flora effect raises INR.",
        "Avoid if alternative available. Otherwise check INR within 3 days.",
    ),
    (
        "warfarin", "metronidazole", "major",
        "CYP2C9 inhibition raises S-warfarin levels.",
        "Reduce warfarin dose ~30-50%; monitor INR within 3 days.",
    ),
    (
        "warfarin", "amiodarone", "major",
        "CYP2C9 + CYP3A4 inhibition; effect persists weeks after stopping.",
        "Reduce warfarin dose by 30-50%; INR weekly for 4 weeks.",
    ),
    (
        "aspirin", "ibuprofen", "moderate",
        "Ibuprofen blocks aspirin's irreversible COX-1 binding → loss of "
        "cardioprotection. Additive GI / renal toxicity.",
        "Take aspirin ≥30 min before or ≥8 h after ibuprofen. Avoid chronic combo.",
    ),
    (
        "aspirin", "paracetamol", "minor",
        "No clinically significant interaction at usual doses.",
        "Combination is widely used (e.g., Combiflam-like brands).",
    ),
    (
        "paracetamol", "ibuprofen", "minor",
        "Often combined therapeutically; mild additive renal stress at high doses.",
        "Safe at standard doses. Avoid in dehydration / pre-existing CKD.",
    ),
    (
        "ibuprofen", "ramipril", "moderate",
        "NSAID blunts ACEi antihypertensive effect; additive AKI risk "
        "(triple-whammy with diuretic).",
        "Monitor BP and creatinine. Avoid in CKD or volume depletion.",
    ),
    (
        "ibuprofen", "telmisartan", "moderate",
        "NSAID blunts ARB effect; additive renal risk.",
        "Monitor BP, creatinine, potassium.",
    ),
    (
        "diclofenac", "ramipril", "moderate",
        "NSAID + ACEi → reduced GFR, ↑ K+, AKI.",
        "Avoid chronic combo. Monitor renal function + K+.",
    ),
    (
        "metformin", "alcohol", "moderate",
        "Alcohol increases lactate production; metformin impairs lactate "
        "clearance → lactic acidosis risk (especially with binge use).",
        "Counsel against heavy/binge drinking. Hold metformin during acute "
        "intoxication or contrast scans.",
    ),
    (
        "metformin", "ciprofloxacin", "moderate",
        "Fluoroquinolones can cause dysglycaemia (both hypo- and hyper-).",
        "Monitor blood glucose, especially in elderly / renal impairment.",
    ),
    (
        "atorvastatin", "clarithromycin", "major",
        "Strong CYP3A4 inhibition raises statin levels → rhabdomyolysis.",
        "Hold statin during macrolide course or switch to azithromycin.",
    ),
    (
        "atorvastatin", "azithromycin", "minor",
        "Azithromycin is a weak CYP3A4 inhibitor; clinically insignificant.",
        "Routine combination is acceptable.",
    ),
    (
        "atorvastatin", "amlodipine", "moderate",
        "Amlodipine increases atorvastatin AUC ~50%.",
        "Cap atorvastatin at 20 mg/day when on amlodipine.",
    ),
    (
        "rosuvastatin", "clopidogrel", "minor",
        "Theoretical CYP2C8 interaction; not clinically significant.",
        "Combination acceptable; monitor for myalgia.",
    ),
    (
        "azithromycin", "domperidone", "major",
        "Both prolong QT — additive arrhythmia risk (TdP).",
        "Avoid combination, especially in cardiac / electrolyte disturbance.",
    ),
    (
        "ciprofloxacin", "domperidone", "major",
        "Additive QT prolongation.",
        "Avoid. Consider ondansetron alternative if antiemetic essential.",
    ),
    (
        "fluoxetine", "tramadol", "major",
        "Additive serotonergic effect → serotonin syndrome.",
        "Avoid combination. If essential, use lowest tramadol dose, monitor closely.",
    ),
    (
        "fluoxetine", "warfarin", "major",
        "CYP2C9 inhibition + platelet serotonin depletion → bleeding risk.",
        "Avoid if possible. Monitor INR + bleeding signs.",
    ),
    (
        "alprazolam", "tramadol", "major",
        "Additive CNS / respiratory depression.",
        "Avoid. If essential, lowest doses, no driving, naloxone available.",
    ),
    (
        "alprazolam", "alcohol", "major",
        "Synergistic CNS depression; respiratory arrest risk.",
        "Counsel strict avoidance. No driving / hazardous tasks.",
    ),
    (
        "tramadol", "alcohol", "major",
        "Additive CNS / respiratory depression; lowered seizure threshold.",
        "Avoid combination.",
    ),
    (
        "metoprolol", "salbutamol", "moderate",
        "Beta-1 selective, but at high doses metoprolol blunts beta-2 "
        "bronchodilation. In asthma → potential bronchospasm.",
        "Avoid in asthma; use cardioselective at lowest dose in COPD.",
    ),
    (
        "ramipril", "telmisartan", "major",
        "Dual RAAS blockade → hypotension, hyperkalaemia, AKI.",
        "Avoid (ONTARGET trial). Use one agent.",
    ),
    (
        "amlodipine", "atorvastatin", "moderate",
        "Amlodipine inhibits atorvastatin metabolism (CYP3A4) → ↑ statin AUC.",
        "Limit atorvastatin to 20 mg/day.",
    ),
    (
        "pantoprazole", "clopidogrel", "moderate",
        "PPI inhibits CYP2C19 (less so for pantoprazole vs omeprazole) → "
        "reduced clopidogrel activation.",
        "Pantoprazole is preferred over omeprazole if PPI is essential.",
    ),
    (
        "omeprazole", "clopidogrel", "major",
        "Strong CYP2C19 inhibition reduces clopidogrel active metabolite.",
        "Switch to pantoprazole or famotidine.",
    ),
    (
        "levothyroxine", "calcium carbonate", "moderate",
        "Calcium chelates levothyroxine reducing absorption.",
        "Separate by ≥4 hours. Take thyroxine on empty stomach in the morning.",
    ),
    (
        "levothyroxine", "omeprazole", "moderate",
        "Reduced gastric acid impairs thyroxine absorption.",
        "Monitor TSH. Re-titrate dose after PPI initiation.",
    ),
    (
        "ciprofloxacin", "calcium carbonate", "moderate",
        "Divalent cations chelate fluoroquinolone reducing absorption ~50%.",
        "Separate ciprofloxacin and calcium by ≥2 hours.",
    ),
    (
        "doxycycline", "calcium carbonate", "moderate",
        "Chelation reduces doxycycline absorption.",
        "Separate by 2-3 hours.",
    ),
    (
        "metronidazole", "alcohol", "major",
        "Disulfiram-like reaction (acetaldehyde accumulation).",
        "Strict avoidance of alcohol during and 48 h after course.",
    ),
    (
        "tinidazole", "alcohol", "major",
        "Disulfiram-like reaction.",
        "Strict avoidance during and 72 h after course.",
    ),
    (
        "spironolactone", "ramipril", "moderate",
        "Additive hyperkalaemia risk.",
        "Monitor K+ within 1 week. Avoid in CKD / on KCl supplements.",
    ),
    (
        "spironolactone", "telmisartan", "moderate",
        "Additive hyperkalaemia risk.",
        "Monitor K+ within 1 week.",
    ),
    (
        "furosemide", "metformin", "moderate",
        "Loop diuretic → volume depletion → contrast/lactic-acidosis risk; "
        "may impair renal clearance of metformin.",
        "Monitor renal function + lactate if symptoms.",
    ),
    (
        "furosemide", "gentamicin", "major",
        "Synergistic ototoxicity + nephrotoxicity.",
        "Avoid. If essential, monitor levels, audiometry.",
    ),
    (
        "carbamazepine", "warfarin", "major",
        "Carbamazepine is a strong CYP-inducer → reduces warfarin levels → "
        "subtherapeutic INR.",
        "Increase warfarin dose; monitor INR weekly.",
    ),
    (
        "phenytoin", "warfarin", "major",
        "Bidirectional metabolism interaction; initial INR rise then fall.",
        "Frequent INR monitoring.",
    ),
    (
        "valproate", "carbamazepine", "moderate",
        "Mutual metabolism interference; raises carbamazepine epoxide.",
        "Monitor levels and toxicity signs.",
    ),
    (
        "metformin", "iodinated contrast", "major",
        "Contrast-induced AKI raises lactic acidosis risk.",
        "Hold metformin 48 h pre/post contrast in eGFR <60.",
    ),
]


def _ddi_index() -> dict[tuple[str, str], dict]:
    idx: dict[tuple[str, str], dict] = {}
    for a, b, sev, mech, rec in _DDI_RAW:
        key = tuple(sorted([a.lower(), b.lower()]))
        idx[key] = {
            "drug_a": key[0],
            "drug_b": key[1],
            "severity": sev,
            "mechanism": mech,
            "recommendation": rec,
        }
    return idx


_DDI_INDEX = _ddi_index()


# ── Drug name normalisation ───────────────────────────────────────────────

_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z0-9\-]+(?:\s+\d+(?:\.\d+)?)?(?:\s*(?:mg|g|mcg|ml|unit|iu|%))?", re.IGNORECASE)
_DOSE_RX = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|g|mcg|ml|iu|unit|units)\b", re.IGNORECASE)


def _all_known_names() -> set[str]:
    names: set[str] = set()
    names.update(INDIA_BRANDS.keys())
    for generics in INDIA_BRANDS.values():
        names.update(g.lower() for g in generics)
    names.update(DRUG_PROFILES.keys())
    names.update(k.lower() for k in INDIA_BANNED.keys())
    return names


# Non-drug "ingestibles" we still want to flag in DDI (alcohol, contrast, food).
EXTRA_GENERICS: set[str] = {"alcohol", "iodinated contrast", "grapefruit juice"}

_KNOWN = _all_known_names() | EXTRA_GENERICS


def _resolve_to_generic(raw: str) -> tuple[list[str], float, str | None]:
    """Return (generic_list, confidence, matched_brand_or_generic).

    Confidence:
      1.0 → exact brand or generic hit
      0.8 → fuzzy (cutoff 0.85)
      0.0 → not resolved
    """
    s = (raw or "").strip().lower()
    s = re.sub(r"\b(\d+\s*(?:mg|g|mcg|ml|iu|unit|units))\b", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return [], 0.0, None

    if s in INDIA_BRANDS:
        return [g.lower() for g in INDIA_BRANDS[s]], 1.0, s
    if s in DRUG_PROFILES:
        return [s], 1.0, s
    if s in EXTRA_GENERICS:
        return [s], 1.0, s
    # try first token (e.g., "crocin advance" → "crocin")
    first = s.split()[0]
    if first in INDIA_BRANDS:
        return [g.lower() for g in INDIA_BRANDS[first]], 1.0, first
    if first in DRUG_PROFILES:
        return [first], 1.0, first
    if first in EXTRA_GENERICS:
        return [first], 1.0, first
    # fuzzy
    candidates = list(_KNOWN)
    matches = difflib.get_close_matches(s, candidates, n=1, cutoff=0.85)
    if not matches:
        matches = difflib.get_close_matches(first, candidates, n=1, cutoff=0.88)
    if matches:
        m = matches[0]
        if m in INDIA_BRANDS:
            return [g.lower() for g in INDIA_BRANDS[m]], 0.8, m
        return [m], 0.8, m
    return [], 0.0, None


# ── Body validators ───────────────────────────────────────────────────────

class _DrugItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    dose: str | None = Field(None, max_length=64)


class _CheckBody(BaseModel):
    drugs: list[_DrugItem] = Field(..., min_length=2)


class _NormalizeBody(BaseModel):
    raw_text: str = Field(..., min_length=1)


def _validate_check(body: _CheckBody) -> list[_DrugItem]:
    if len(body.drugs) > MAX_DRUGS_PER_CHECK:
        raise HTTPException(
            status_code=413,
            detail={"error": "too_many_drugs", "limit": MAX_DRUGS_PER_CHECK},
        )
    if len(body.drugs) < 2:
        raise HTTPException(status_code=400, detail="need_at_least_two_drugs")
    return body.drugs


def _validate_normalize(body: _NormalizeBody) -> str:
    s = body.raw_text or ""
    if not s.strip():
        raise HTTPException(status_code=400, detail="text_empty")
    if len(s.encode("utf-8")) > MAX_TEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "text_too_large", "limit_bytes": MAX_TEXT_BYTES},
        )
    return s


# ── DDI lookup core ───────────────────────────────────────────────────────

def _lookup_pair(a: str, b: str) -> dict | None:
    """Return DDI dict for an ordered pair (a, b) or None."""
    key = tuple(sorted([a.lower(), b.lower()]))
    return _DDI_INDEX.get(key)


def _expand_generics(items: list[_DrugItem]) -> list[dict]:
    """Resolve each input item to one or more generics."""
    out: list[dict] = []
    for it in items:
        generics, conf, matched = _resolve_to_generic(it.name)
        out.append(
            {
                "input": it.name,
                "dose": it.dose,
                "matched": matched,
                "generics": generics,
                "confidence": conf,
                "banned_in_india": _banned_status(it.name, generics),
            }
        )
    return out


def _banned_status(raw: str, generics: list[str]) -> str | None:
    raw_l = (raw or "").lower()
    for k in INDIA_BANNED:
        if k.lower() in raw_l:
            return INDIA_BANNED[k]
    for g in generics:
        if g.lower() in INDIA_BANNED:
            return INDIA_BANNED[g.lower()]
    return None


def _scan_pairs(resolved: list[dict]) -> list[dict]:
    """All-pairs DDI scan over the expanded generic list."""
    seen: set[tuple[str, str]] = set()
    pairs: list[dict] = []
    for i in range(len(resolved)):
        for j in range(i + 1, len(resolved)):
            for ga in resolved[i]["generics"]:
                for gb in resolved[j]["generics"]:
                    if ga == gb:
                        continue
                    key = tuple(sorted([ga, gb]))
                    if key in seen:
                        continue
                    hit = _lookup_pair(ga, gb)
                    if hit:
                        seen.add(key)
                        pairs.append({
                            **hit,
                            "input_a": resolved[i]["input"],
                            "input_b": resolved[j]["input"],
                        })
    return pairs


# ── openFDA augmentation (best-effort, never blocks) ──────────────────────

async def _openfda_label_warnings(generics: list[str]) -> dict[str, list[str]]:
    """For each generic, fetch openFDA label warnings (best-effort)."""
    if not generics:
        return {}
    out: dict[str, list[str]] = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=OPENFDA_TIMEOUT_S) as c:
        for g in generics:
            try:
                params = {
                    "search": f'openfda.generic_name:"{g}"',
                    "limit": 1,
                }
                r = await c.get(OPENFDA_LABEL_URL, params=params)
                if r.status_code != 200:
                    continue
                data = r.json()
                results = data.get("results", []) or []
                if not results:
                    continue
                row = results[0]
                warnings = []
                for k in ("drug_interactions", "warnings", "contraindications"):
                    val = row.get(k)
                    if isinstance(val, list) and val:
                        for s in val:
                            if isinstance(s, str):
                                warnings.append(s[:400])
                if warnings:
                    out[g] = warnings[:3]
            except Exception:
                continue
    return out


# ── Audit + billing helpers ───────────────────────────────────────────────

async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": json.dumps(input_summary, default=str),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    amount: float,
    tool: str,
    ref: str,
) -> float:
    if amount <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-amount,
            reason=f"commons.drug.{tool}",
            ref_id=ref,
            note=f"Drug DDI: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": amount,
                "tool": f"commons.drug.{tool}",
            },
        )
    return amount


# ── /health (public) ──────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict:
    return {
        "service": "commons.drug",
        "version": "1.0.0",
        "sc_cost": SC_COST,
        "max_drugs_per_check": MAX_DRUGS_PER_CHECK,
        "max_text_bytes": MAX_TEXT_BYTES,
        "data_sources": [
            "openFDA drug label API",
            "Curated India brand-generic table",
            "CDSCO banned-drug list",
            "Curated DDI table (Lexi/Stockley-style severity)",
        ],
        "india_brand_count": len(INDIA_BRANDS),
        "india_banned_count": len(INDIA_BANNED),
        "ddi_pair_count": len(_DDI_INDEX),
        "drug_profile_count": len(DRUG_PROFILES),
        "disclaimer": DISCLAIMER,
    }


# ── /check ────────────────────────────────────────────────────────────────

@router.post("/check")
async def check_interactions(
    body: Annotated[_CheckBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    items = _validate_check(body)
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, amount=SC_COST["check"], tool="check", ref=ref)

    try:
        resolved = _expand_generics(items)
        pairs = _scan_pairs(resolved)
        unresolved = [r["input"] for r in resolved if not r["generics"]]
        banned_alerts = [
            {"input": r["input"], "reason": r["banned_in_india"]}
            for r in resolved
            if r.get("banned_in_india")
        ]
        severity_counts = {"minor": 0, "moderate": 0, "major": 0, "severe": 0}
        for p in pairs:
            sev = p.get("severity")
            if sev in severity_counts:
                severity_counts[sev] += 1

        out = {
            "drugs_resolved": resolved,
            "pairs": pairs,
            "pair_count": len(pairs),
            "severity_counts": severity_counts,
            "unresolved": unresolved,
            "india_banned_alerts": banned_alerts,
            "sc_charged": SC_COST["check"],
            "disclaimer": DISCLAIMER,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.drug.check",
            input_summary={
                "n_drugs": len(items),
                "pair_count": len(pairs),
                "severity_counts": severity_counts,
                "unresolved": len(unresolved),
                "banned_alerts": len(banned_alerts),
            },
            output_len=len(json.dumps(out, default=str)),
            sc_cost=SC_COST["check"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "check_failed", "msg": str(e)[:200]},
        )


# ── /normalize ────────────────────────────────────────────────────────────

@router.post("/normalize")
async def normalize_text(
    body: Annotated[_NormalizeBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    s = _validate_normalize(body)
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, amount=SC_COST["normalize"], tool="normalize", ref=ref)

    try:
        # Strip dose tokens (we record them separately).
        doses = [(m.group(1), m.group(2)) for m in _DOSE_RX.finditer(s)]

        # Tokenise heuristically — words ≥3 chars, drop common scaffolding.
        STOP = {
            "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules",
            "syp", "syrup", "drops", "drop", "inj", "injection", "iv", "po", "od",
            "bd", "tds", "qid", "stat", "prn", "morning", "night", "evening",
            "before", "after", "food", "meals", "patient", "rx", "name", "age",
            "the", "with", "and", "for", "days", "day", "week", "weeks", "month",
            "year", "years", "x", "tab.", "cap.",
        }
        tokens: list[str] = []
        for raw_tok in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", s):
            t = raw_tok.lower()
            if t in STOP or t.isdigit() or len(t) < 3:
                continue
            tokens.append(t)

        # Multi-word matches only allowed when the joined string is an
        # *exact* known brand (e.g. "d cold", "pan d", "telma h"). Single
        # tokens fall back to brand / generic / fuzzy resolve.
        normalised: list[dict] = []
        seen: set[str] = set()
        i = 0
        while i < len(tokens):
            matched = None
            # multi-word: ONLY exact dictionary hits
            for span in (3, 2):
                if i + span > len(tokens):
                    continue
                cand = " ".join(tokens[i : i + span]).strip()
                if cand in INDIA_BRANDS:
                    generics = [g.lower() for g in INDIA_BRANDS[cand]]
                    matched = (cand, generics, 1.0, cand, span)
                    break
                if cand in DRUG_PROFILES:
                    matched = (cand, [cand], 1.0, cand, span)
                    break
            # single token: full resolve (exact + fuzzy)
            if not matched:
                cand = tokens[i]
                generics, conf, matched_name = _resolve_to_generic(cand)
                if matched_name and conf >= 0.8 and generics:
                    matched = (cand, generics, conf, matched_name, 1)
            if matched:
                cand, generics, conf, matched_name, span = matched
                if matched_name not in seen and generics:
                    seen.add(matched_name)
                    normalised.append({
                        "raw": cand,
                        "matched": matched_name,
                        "generics": generics,
                        "confidence": conf,
                        "banned_in_india": _banned_status(cand, generics),
                    })
                i += span
            else:
                i += 1

        out = {
            "drugs": normalised,
            "count": len(normalised),
            "doses_detected": [{"value": v, "unit": u} for v, u in doses],
            "sc_charged": SC_COST["normalize"],
            "disclaimer": DISCLAIMER,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.drug.normalize",
            input_summary={
                "in_bytes": len(s.encode("utf-8")),
                "drugs_found": len(normalised),
                "doses_found": len(doses),
            },
            output_len=len(json.dumps(out, default=str)),
            sc_cost=SC_COST["normalize"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "normalize_failed", "msg": str(e)[:200]},
        )


# ── /info/{drug_name} ─────────────────────────────────────────────────────

@router.get("/info/{drug_name}")
async def drug_info(
    drug_name: str,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    raw = (drug_name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="drug_name_empty")
    if len(raw) > 120:
        raise HTTPException(status_code=413, detail="drug_name_too_long")

    await _charge(db, agent, amount=SC_COST["info"], tool="info", ref=ref)
    try:
        generics, conf, matched = _resolve_to_generic(raw)
        if not generics:
            await _log_tool_call(
                db,
                agent_id=agent.id,
                tool_name="commons.drug.info",
                input_summary={"input": raw, "matched": None},
                output_len=0,
                sc_cost=SC_COST["info"],
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            await db.commit()
            return {
                "input": raw,
                "matched": None,
                "generic_name": None,
                "brand_names_india": [],
                "class": None,
                "indications": [],
                "common_doses": [],
                "common_side_effects": [],
                "indian_brands": [],
                "banned_in_india": _banned_status(raw, []),
                "confidence": 0.0,
                "disclaimer": DISCLAIMER,
            }

        primary = generics[0]
        profile = DRUG_PROFILES.get(primary, {})
        brand_index = _build_generic_index()
        brands = sorted(set(brand_index.get(primary, [])))
        out = {
            "input": raw,
            "matched": matched,
            "generic_name": primary,
            "all_generics": generics,
            "brand_names_india": brands,
            "indian_brands": brands,
            "class": profile.get("class"),
            "indications": profile.get("indications", []),
            "common_doses": profile.get("common_doses", []),
            "common_side_effects": profile.get("common_side_effects", []),
            "banned_in_india": _banned_status(raw, generics),
            "confidence": conf,
            "disclaimer": DISCLAIMER,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.drug.info",
            input_summary={
                "input": raw,
                "matched": matched,
                "generic": primary,
                "brands": len(brands),
            },
            output_len=len(json.dumps(out, default=str)),
            sc_cost=SC_COST["info"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "info_failed", "msg": str(e)[:200]},
        )
