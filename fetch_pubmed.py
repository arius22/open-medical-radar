#!/usr/bin/env python3
"""
open-medical-radar — Motor Central de Vigilancia Bibliográfica Médica (PICO + IIC)
===================================================================================
Arquitectura resiliente con CERO dependencias externas (Python 3.11 Standard Library Only).
Diseñado para ejecutarse en GitHub Actions con coste estrictamente 0 € empleando:
- PubMed E-Utilities (esearch, efetch, PMC fulltext XML)
- Google Gemini API Free Tier con cascada determinista:
    1. gemini-3.8-flash (Primario, Timeout 20s)
    2. gemini-3.5-flash-lite (Fallback 1: 15 RPM / 500 RPD, Timeout 20s)
    3. gemini-3.1-flash-lite (Fallback 2: 15 RPM / 500 RPD, Timeout 20s)
- Doble canal de notificación (SMTP HTML/Plaintext + Telegram chunked <= 4000 chars)
"""

import argparse
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# ============================================================================
# CONSTANTES GLOBALES Y CONFIGURACIÓN DE RESILIENCIA
# ============================================================================

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TELEGRAM_API_BASE = "https://api.telegram.org"

MODEL_CASCADE = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

GEMINI_TIMEOUT_SEC = 20
PROPHYLACTIC_SLEEP_SEC = 5
MAX_LANDMARK_CALLS_DEFAULT = 4
MAX_REGULAR_CALLS_DEFAULT = 8
GLOBAL_EXECUTION_BUDGET_SEC = 1200  # 20 minutos máximo de presupuesto global

# Expresiones regulares en memoria para filtrar ruido antes de invocar la API de Gemini
NOISE_TITLE_PATTERNS = re.compile(
    r"\b("
    r"erratum|corrigendum|retraction|retracted|published erratum|"
    r"correction to|author correction|publisher correction|"
    r"reply to|comment on|letter to the editor|in reply|"
    r"narrative review|scoping review|expert opinion|position paper|"
    r"consensus statement|clinical vignette|image in clinical medicine|"
    r"case report|case series|book review|obituary"
    r")\b",
    re.IGNORECASE,
)

HIGH_EVIDENCE_PATTERNS = re.compile(
    r"\b("
    r"randomized|randomised|clinical trial|phase 3|phase iii|phase 2|phase ii|"
    r"meta-analysis|systematic review|multicenter|multicentre|double-blind|"
    r"placebo-controlled|prospective cohort|propensity score|target trial|"
    r"noninferiority|superiority trial|hazard ratio|relative risk"
    r")\b",
    re.IGNORECASE,
)

EXCLUDED_PUB_TYPES = {
    "published erratum",
    "erratum",
    "retraction of publication",
    "retracted publication",
    "comment",
    "letter",
    "editorial",
    "news",
    "biography",
    " patient education handout",
    "address",
    "Congresses",
}


# ============================================================================
# 1. CONSTRUCCIÓN DE CONSULTAS Y EXTRACCIÓN DESDE PUBMED / PMC
# ============================================================================

def build_pubmed_query(config: dict) -> str:
    """
    Ensambla la query booleana de PubMed a partir del array de journals del perfil,
    excluyendo cartas, editoriales, comentarios y erratas directamente en ESearch,
    y exigiendo presencia de abstract.
    """
    journals = config.get("journals", [])
    if not journals:
        journals = [
            "New England Journal of Medicine",
            "Lancet",
            "JAMA",
            "BMJ",
            "Annals of Internal Medicine",
        ]

    journal_clauses = []
    for j in journals:
        clean_j = j.strip().replace('"', "")
        if clean_j:
            journal_clauses.append(f'"{clean_j}"[Journal]')

    journal_block = "(" + " OR ".join(journal_clauses) + ")"
    publication_filter = (
        'NOT ("Letter"[Publication Type] OR "Editorial"[Publication Type] '
        'OR "Comment"[Publication Type] OR "Published Erratum"[Publication Type] '
        'OR "Retracted Publication"[Publication Type])'
    )
    abstract_filter = "AND hasabstract[text]"

    custom_query_filter = config.get("pubmed_extra_query", "").strip()
    if custom_query_filter:
        return f"{journal_block} AND ({custom_query_filter}) {abstract_filter} {publication_filter}"
    return f"{journal_block} {abstract_filter} {publication_filter}"


def _ncbi_rate_limit_sleep(ncbi_api_key: str | None) -> None:
    """Respeta la tasa máxima de E-Utilities (10 req/s con API Key, 3 req/s sin ella)."""
    time.sleep(0.12 if ncbi_api_key else 0.35)


def fetch_pmids_by_date_range(
    query: str,
    days_lookback: int = 7,
    mindate: str | None = None,
    maxdate: str | None = None,
    retmax: int = 120,
    ncbi_api_key: str | None = None,
) -> list[str]:
    """
    Llama a esearch.fcgi de PubMed para recuperar PMIDs publicados dentro del rango
    de fechas especificado, capturando HTTPError, URLError y TimeoutError.
    """
    today = datetime.date.today()
    if not maxdate:
        maxdate = today.strftime("%Y/%m/%d")
    if not mindate:
        start_date = today - datetime.timedelta(days=max(1, int(days_lookback)))
        mindate = start_date.strftime("%Y/%m/%d")

    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(retmax),
        "sort": "pub+date",
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
    }
    if ncbi_api_key:
        params["api_key"] = ncbi_api_key

    url = f"{EUTILS_BASE}/esearch.fcgi?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "open-medical-radar/1.0 (https://github.com/arius22/open-medical-radar)"},
        method="GET",
    )

    _ncbi_rate_limit_sleep(ncbi_api_key)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw_body)
            id_list = data.get("esearchresult", {}).get("idlist", [])
            return [str(pmid).strip() for pmid in id_list if str(pmid).strip()]
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[WARN] Error en fetch_pmids_by_date_range ({mindate} - {maxdate}): {exc}", file=sys.stderr)
        return []


def fetch_article_details(pmids: list[str], ncbi_api_key: str | None = None) -> list[dict]:
    """
    Llama a efetch.fcgi en lotes de hasta 100 PMIDs, parsea el XML con ElementTree,
    descarta erratas/comentarios/artículos sin abstract y extrae:
    PMID, PMCID, DOI, Título, Abstract estructurado, Revista, Tipos de Publicación y Fecha.
    """
    if not pmids:
        return []

    articles: list[dict] = []
    batch_size = 80

    for idx in range(0, len(pmids), batch_size):
        batch = pmids[idx : idx + batch_size]
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
        }
        if ncbi_api_key:
            params["api_key"] = ncbi_api_key

        url = f"{EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "open-medical-radar/1.0 (Hospital-Clinical-Surveillance)"},
            method="GET",
        )

        _ncbi_rate_limit_sleep(ncbi_api_key)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                xml_bytes = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"[WARN] Error en fetch_article_details (lote {idx}): {exc}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            print(f"[WARN] Error parseando XML de PubMed: {exc}", file=sys.stderr)
            continue

        for pubmed_article in root.findall(".//PubmedArticle"):
            medline = pubmed_article.find("MedlineCitation")
            if medline is None:
                continue

            pmid_el = medline.find("PMID")
            pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""
            if not pmid:
                continue

            article_el = medline.find("Article")
            if article_el is None:
                continue

            # Verificar tipos de publicación y descartar erratas / cartas / comentarios
            pub_types = []
            for pt in article_el.findall(".//PublicationTypeList/PublicationType"):
                if pt.text:
                    pub_types.append(pt.text.strip())
            pub_types_lower = {pt.lower() for pt in pub_types}
            if pub_types_lower.intersection(EXCLUDED_PUB_TYPES):
                continue

            # Verificar si tiene CommentsCorrections de tipo ErratumIn / RetractionIn
            has_retraction = False
            for cc in medline.findall(".//CommentsCorrectionsList/CommentsCorrections"):
                ref_type = (cc.attrib.get("RefType") or "").lower()
                if ref_type in ("retractionin", "retractionof", "erratumin", "erratumfor"):
                    has_retraction = True
                    break
            if has_retraction:
                continue

            # Extraer título completo (incluyendo etiquetas hijas como <i> o <sup>)
            title_el = article_el.find("ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            if not title or NOISE_TITLE_PATTERNS.search(title):
                continue

            # Extraer Abstract completo (incluyendo secciones estructuradas)
            abstract_parts = []
            for abs_text in article_el.findall(".//Abstract/AbstractText"):
                label = abs_text.attrib.get("Label", "").strip()
                section_content = "".join(abs_text.itertext()).strip()
                if not section_content:
                    continue
                if label:
                    abstract_parts.append(f"{label}: {section_content}")
                else:
                    abstract_parts.append(section_content)

            abstract = "\n".join(abstract_parts).strip()
            if len(abstract) < 120:
                # Descartar artículos sin resumen clínico sustantivo
                continue

            # Extraer nombre de revista (Title o ISOAbbreviation)
            journal_title_el = article_el.find(".//Journal/Title")
            journal_iso_el = article_el.find(".//Journal/ISOAbbreviation")
            journal_title = (
                (journal_title_el.text or "").strip()
                if journal_title_el is not None and journal_title_el.text
                else ""
            )
            journal_iso = (
                (journal_iso_el.text or "").strip()
                if journal_iso_el is not None and journal_iso_el.text
                else ""
            )
            journal_display = journal_title or journal_iso or "Revista Médica Indexada"

            # Extraer fecha de publicación
            pub_date_el = article_el.find(".//Journal/JournalIssue/PubDate")
            year = ""
            month = ""
            day = ""
            if pub_date_el is not None:
                y_el = pub_date_el.find("Year")
                m_el = pub_date_el.find("Month")
                d_el = pub_date_el.find("Day")
                medline_date_el = pub_date_el.find("MedlineDate")
                year = (y_el.text or "").strip() if y_el is not None else ""
                month = (m_el.text or "").strip() if m_el is not None else ""
                day = (d_el.text or "").strip() if d_el is not None else ""
                if not year and medline_date_el is not None and medline_date_el.text:
                    year = medline_date_el.text.strip()
            pub_date_str = " ".join(part for part in (day, month, year) if part).strip() or "Reciente"

            # Extraer PMCID y DOI desde PubmedData/ArticleIdList
            pmcid = ""
            doi = ""
            for aid in pubmed_article.findall(".//PubmedData/ArticleIdList/ArticleId"):
                id_type = (aid.attrib.get("IdType") or "").lower()
                val = (aid.text or "").strip()
                if id_type == "pmc" and val:
                    pmcid = val if val.upper().startswith("PMC") else f"PMC{val}"
                elif id_type == "doi" and val:
                    doi = val

            # Extraer primer autor / institución si existe
            first_author = ""
            author_el = article_el.find(".//AuthorList/Author[1]")
            if author_el is not None:
                last_n = author_el.find("LastName")
                fore_n = author_el.find("ForeName")
                if last_n is not None and last_n.text:
                    first_author = last_n.text.strip()
                    if fore_n is not None and fore_n.text:
                        first_author += f" {fore_n.text.strip()[0]}."
                    first_author += " et al."

            articles.append(
                {
                    "pmid": pmid,
                    "pmcid": pmcid,
                    "doi": doi,
                    "title": title,
                    "abstract": abstract,
                    "journal": journal_display,
                    "journal_iso": journal_iso,
                    "pub_date": pub_date_str,
                    "pub_types": pub_types,
                    "authors": first_author or "Autores del estudio",
                    "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                }
            )

    return articles


def fetch_pmc_fulltext(pmcid: str, ncbi_api_key: str | None = None) -> str:
    """
    Si el artículo dispone de PMCID abierto en PubMed Central, descarga el XML
    desde efetch (db=pmc) y extrae hasta 2500 caracteres de las secciones
    Methods / Results para enriquecer la auditoría metodológica PICO.
    """
    if not pmcid:
        return ""

    clean_id = pmcid.upper().replace("PMC", "").strip()
    if not clean_id.isdigit():
        return ""

    params = {
        "db": "pmc",
        "id": clean_id,
        "retmode": "xml",
    }
    if ncbi_api_key:
        params["api_key"] = ncbi_api_key

    url = f"{EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "open-medical-radar/1.0 (Hospital-Clinical-Surveillance)"},
        method="GET",
    )

    _ncbi_rate_limit_sleep(ncbi_api_key)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            xml_bytes = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        print(f"[INFO] No se pudo recuperar texto completo PMC ({pmcid}): {exc}", file=sys.stderr)
        return ""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""

    extracted_chunks: list[str] = []
    target_sec_keywords = ("method", "patient", "design", "statistical", "result", "outcome", "finding", "efficacy")

    for sec in root.findall(".//body/sec"):
        title_el = sec.find("title")
        sec_title = "".join(title_el.itertext()).strip().lower() if title_el is not None else ""
        sec_type = (sec.attrib.get("sec-type") or "").lower()

        if any(k in sec_title or k in sec_type for k in target_sec_keywords):
            paragraphs = []
            for p in sec.findall(".//p"):
                p_text = " ".join("".join(p.itertext()).split())
                if p_text:
                    paragraphs.append(p_text)
            if paragraphs:
                header = sec_title.upper() if sec_title else "SECTION"
                extracted_chunks.append(f"[{header}] " + " ".join(paragraphs))

    if not extracted_chunks:
        # Fallback: primeros párrafos del body si no usa etiquetas <sec> estándar
        body_paras = []
        for p in root.findall(".//body//p"):
            p_text = " ".join("".join(p.itertext()).split())
            if len(p_text) > 60:
                body_paras.append(p_text)
            if sum(len(x) for x in body_paras) >= 2500:
                break
        combined = " ".join(body_paras)
    else:
        combined = "\n".join(extracted_chunks)

    return combined[:2500].strip()


# ============================================================================
# 2. FILTRADO PRE-API EN MEMORIA Y RANKING DE CANDIDATOS
# ============================================================================

def filter_and_rank_candidates(
    articles: list[dict],
    config: dict,
    sent_pmids: set[str] | list[str] | dict,
) -> list[dict]:
    """
    Filtra en memoria antes de tocar la API de Gemini:
    - Excluye PMIDs ya enviados en ejecuciones previas.
    - Descarta revisiones narrativas, consensos no metaanalíticos, erratas y casos aislados.
    - Aplica exclusiones específicas del perfil respetando la lista blanca traslacional (ej. PK/PD).
    - Puntúa cada candidato por factor de impacto relativo de la revista (journal_weights),
      bonificación de revistas troncales (ground_journals) y densidad metodológica (ECA / Metaanálisis).
    """
    if isinstance(sent_pmids, dict):
        already_sent = set(str(x) for x in sent_pmids.keys())
    else:
        already_sent = set(str(x) for x in sent_pmids)

    journal_weights = {
        k.lower(): float(v) for k, v in config.get("journal_weights", {}).items()
    }
    ground_journals = {j.lower() for j in config.get("ground_journals", [])}
    focus_keywords = [kw.lower() for kw in config.get("focus_keywords", []) if kw.strip()]
    exclusions = [ex.lower() for ex in config.get("exclusions", []) if ex.strip()]
    translational_allow = [
        tr.lower() for tr in config.get("translational_allowlist", []) if tr.strip()
    ]

    ranked_candidates: list[dict] = []

    for art in articles:
        pmid = str(art.get("pmid", "")).strip()
        if not pmid or pmid in already_sent:
            continue

        title = art.get("title", "")
        abstract = art.get("abstract", "")
        combined_text = f"{title}\n{abstract}"
        combined_lower = combined_text.lower()
        pub_types_lower = {pt.lower() for pt in art.get("pub_types", [])}

        # 1. Filtro regex de ruido editorial / revisiones narrativas / consensos sin metaanálisis
        if NOISE_TITLE_PATTERNS.search(title):
            continue

        is_review = "review" in pub_types_lower or "review" in title.lower()
        is_meta_or_syst = (
            "meta-analysis" in pub_types_lower
            or "systematic review" in pub_types_lower
            or "meta-analysis" in combined_lower
            or "systematic review" in combined_lower
        )
        if is_review and not is_meta_or_syst:
            # Descartar revisiones narrativas para priorizar evidencia primaria y metaanálisis
            continue

        if ("consensus" in title.lower() or "guideline" in title.lower()) and not is_meta_or_syst:
            if "randomized" not in combined_lower and "meta-analysis" not in combined_lower:
                continue

        # 2. Filtro de exclusiones específicas del perfil (con excepción traslacional PK/PD)
        has_translational_rescue = any(allow_kw in combined_lower for allow_kw in translational_allow)
        excluded_hit = False
        for excl in exclusions:
            if excl in combined_lower:
                if has_translational_rescue and ("in vitro" in excl or "microbiology" in excl):
                    continue
                excluded_hit = True
                break
        if excluded_hit:
            continue

        # 3. Cálculo de puntuación pre-API
        journal_name = art.get("journal", "").lower()
        journal_iso = art.get("journal_iso", "").lower()

        base_score = 70.0
        for j_key, weight in journal_weights.items():
            if j_key in journal_name or j_key in journal_iso or journal_name in j_key:
                base_score = max(base_score, weight)
                break

        is_ground = any(
            gj in journal_name or gj in journal_iso or journal_name in gj
            for gj in ground_journals
        )
        if is_ground:
            base_score += 15.0

        # Bonificación por diseño metodológico de alto nivel (ECA, Fase 3, Metaanálisis)
        evidence_matches = HIGH_EVIDENCE_PATTERNS.findall(combined_text)
        base_score += min(20.0, len(set(m.lower() for m in evidence_matches)) * 5.0)

        # Bonificación por alineación con palabras clave clínicas de la especialidad
        kw_hits = sum(1 for kw in focus_keywords if kw in combined_lower)
        base_score += min(18.0, kw_hits * 3.0)

        # Bonificación si reporta datos cuantitativos (IC 95%, HR, OR, RR, valor p)
        if re.search(r"\b(95%\s*ci|confidence interval|hazard ratio|odds ratio|relative risk|p\s*[<=>]\s*0\.\d+)\b", combined_lower):
            base_score += 8.0

        art_copy = dict(art)
        art_copy["pre_score"] = round(base_score, 2)
        art_copy["is_ground_journal"] = is_ground
        ranked_candidates.append(art_copy)

    ranked_candidates.sort(key=lambda x: x.get("pre_score", 0.0), reverse=True)
    return ranked_candidates


# ============================================================================
# 3. MOTOR GEMINI API (CASCADA 3.8 -> 3.5-LITE -> 3.1-LITE + JITTER + FAILOVER)
# ============================================================================

def query_gemini_model(
    model_name: str,
    prompt: str,
    api_key: str,
    timeout_sec: int = GEMINI_TIMEOUT_SEC,
) -> dict:
    """
    Realiza una llamada HTTP POST directa a Google AI Studio (v1beta) solicitando
    `response_mime_type: "application/json"` sin librerías externas.
    """
    endpoint = f"{GEMINI_API_BASE}/{model_name}:generateContent?key={urllib.parse.quote(api_key)}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.15,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }

    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "open-medical-radar/1.0 (Python-urllib)",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_sec) as response:
        raw_resp = response.read().decode("utf-8", errors="replace")
        parsed_resp = json.loads(raw_resp)

    candidates = parsed_resp.get("candidates", [])
    if not candidates:
        raise ValueError(f"Respuesta sin candidates desde {model_name}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts or "text" not in parts[0]:
        raise ValueError(f"Respuesta sin texto JSON desde {model_name}")

    raw_json_text = parts[0]["text"].strip()
    # Limpiar posibles bloques markdown ```json ... ``` si algún modelo los añade
    raw_json_text = re.sub(r"^```(?:json)?\s*", "", raw_json_text, flags=re.IGNORECASE)
    raw_json_text = re.sub(r"\s*```$", "", raw_json_text)
    return json.loads(raw_json_text)


def _build_pico_audit_prompt(
    article: dict,
    profile_name: str,
    is_landmark_candidate: bool,
    categories: list[str] | None = None,
) -> str:
    """Construye el prompt estructurado de auditoría clínica PICO + escala IIC (0-100)."""
    cats_str = ", ".join(categories) if categories else "Clínica General, Terapéutica, Diagnóstico, Pronóstico"
    fulltext_snippet = article.get("pmc_fulltext", "").strip()
    pmc_block = (
        f"\nEXTRACTO TEXTO COMPLETO PMC (MÉTODOS/RESULTADOS):\n{fulltext_snippet}\n"
        if fulltext_snippet
        else ""
    )
    landmark_note = (
        "MODO LANDMARK DE ALTO IMPACTO: Evalúa con máximo rigor si este estudio supone un cambio de paradigma asistencial (IIC >= 85) o alta relevancia clínica."
        if is_landmark_candidate
        else "MODO VIGILANCIA SEMANAL: Evalúa la utilidad asistencial directa para el facultativo hospitalario."
    )

    return f"""Actúa como Metodólogo Clínico Senior y Médico Adjunto Especialista en {profile_name}.
{landmark_note}

Analiza críticamente el siguiente artículo biomédico indexado en PubMed y devuelve EXCLUSIVAMENTE un objeto JSON válido con la siguiente estructura exacta (en español médico riguroso, sin inventar ni extrapolar cifras que no figuren en el texto):

{{
  "clinical_title_es": "Título clínico traducido y sintetizado al español indicando diseño y hallazgo clave",
  "category": "Una categoría exacta elegida entre: [{cats_str}]",
  "study_design": "Diseño exacto (ej. ECA multicéntrico doble ciego fase III, Metaanálisis de ECAs, Cohorte prospectiva multinacional)",
  "pico": {{
    "population": "P (Población): Criterios de inclusión, tamaño muestral (N) y ámbito clínico",
    "intervention": "I (Intervención / Exposición): Fármaco, dosis, técnica quirúrgica o estrategia evaluada",
    "comparison": "C (Comparador): Placebo, tratamiento estándar (SoC) o alternativa activa",
    "outcome": "O (Outcome primario y secundarios clave): Variable principal y tiempo de seguimiento"
  }},
  "quantitative_effect": {{
    "primary_effect_measure": "Medida de efecto principal con IC 95% si consta (ej. HR 0.74; IC 95% 0.62-0.88) o null si no se reporta",
    "rar_or_nnt": "Reducción Absoluta de Riesgo (RAR) y/o NNT/NNH si consta o es calculable con tasas exactas del abstract, o null si faltan datos",
    "p_value": "Valor p exacto si consta en el resumen (ej. p < 0.001) o null si no consta"
  }},
  "methodological_limitations": [
    "Limitación crítica 1 (sesgos, tamaño muestral, desenlace subrogado, open-label, exclusión de pacientes frágiles, etc.)",
    "Limitación crítica 2"
  ],
  "hospital_applicability": "Conclusión práctica de 2 frases sobre qué cambia mañana en el pase de planta, urgencias, quirófano o interconsulta hospitalaria.",
  "iic_methodology_score": 48,
  "iic_hospital_impact_score": 34,
  "iic_total_score": 82,
  "is_paradigm_shift": false
}}

REGLAS ESTRICTAS DE PUNTUACIÓN IIC (Índice de Impacto Clínico, 0 a 100 puntos):
1. `iic_methodology_score` (0 a 60 puntos):
   - 50-60: ECA fase III/IV doble ciego multicéntrico o metaanálisis riguroso con bajo riesgo de sesgo y desenlace duro (mortalidad, eventos mayores).
   - 35-49: ECA open-label, ensayo fase II, estudio pragmático o cohorte prospectiva con ajuste robusto.
   - 0-34: Estudio observacional retrospectivo unicéntrico, desenlace puramente subrogado o alto riesgo de confusión.
2. `iic_hospital_impact_score` (0 a 40 puntos):
   - 32-40: Modifica directamente decisiones terapéuticas/diagnósticas en pacientes hospitalizados reales.
   - 20-31: Confirma práctica clínica actual o aporta matiz relevante en subgrupos específicos.
   - 0-19: Impacto marginal, experimental o alejado de la práctica clínica diaria.
3. `iic_total_score`: Suma exacta de `iic_methodology_score + iic_hospital_impact_score`.
4. `is_paradigm_shift`: true únicamente si `iic_total_score >= 85` y los datos justifican cambiar guías o práctica clínica estándar.
5. Si el abstract no contiene RAR/NNT o p-valor, asigna `null` en esos campos (NUNCA inventes números).

DATOS DEL ARTÍCULO:
- PMID: {article.get("pmid")}
- Revista: {article.get("journal")} ({article.get("pub_date")})
- Autores: {article.get("authors")}
- Tipos de publicación: {", ".join(article.get("pub_types", []))}
- Título original: {article.get("title")}
- Abstract:
{article.get("abstract")}
{pmc_block}
"""


def analyze_article_with_gemini(
    article: dict,
    profile_name: str,
    api_key: str,
    is_landmark_candidate: bool = False,
    config: dict | None = None,
) -> dict | None:
    """
    Audita un artículo médico aplicando el protocolo PICO y escala IIC (0-60 Metodología,
    0-40 Impacto Hospitalario) mediante la cascada obligatoria:
      1. gemini-3.8-flash (Timeout 20s)
      2. gemini-3.5-flash-lite (Timeout 20s)
      3. gemini-3.1-flash-lite (Timeout 20s)
    - Ante HTTP 503 o TimeoutError: reintenta en el mismo modelo con Backoff Exponencial
      y Full Jitter: wait = min(15, 4 * (2 ** attempt)) + random.uniform(1.0, 3.0).
    - Ante HTTP 429 (cuota agotada): conmuta inmediatamente al siguiente modelo Lite.
    - Aplica pausa profiláctica de time.sleep(5) tras una llamada válida.
    """
    categories = config.get("categories", []) if isinstance(config, dict) else None
    prompt = _build_pico_audit_prompt(article, profile_name, is_landmark_candidate, categories)

    max_attempts_per_model = 2

    for model_name in MODEL_CASCADE:
        for attempt in range(max_attempts_per_model):
            try:
                raw_analysis = query_gemini_model(
                    model_name=model_name,
                    prompt=prompt,
                    api_key=api_key,
                    timeout_sec=GEMINI_TIMEOUT_SEC,
                )
                # Pausa profiláctica obligatoria para respetar RPM en Free Tier
                time.sleep(PROPHYLACTIC_SLEEP_SEC)

                # Normalizar y validar campos devueltos
                meth_score = int(raw_analysis.get("iic_methodology_score", 35) or 35)
                meth_score = max(0, min(60, meth_score))
                hosp_score = int(raw_analysis.get("iic_hospital_impact_score", 25) or 25)
                hosp_score = max(0, min(40, hosp_score))
                total_score = meth_score + hosp_score

                pico = raw_analysis.get("pico") if isinstance(raw_analysis.get("pico"), dict) else {}
                quant = (
                    raw_analysis.get("quantitative_effect")
                    if isinstance(raw_analysis.get("quantitative_effect"), dict)
                    else {}
                )
                limitations = raw_analysis.get("methodological_limitations")
                if not isinstance(limitations, list) or not limitations:
                    limitations = ["Interpretación sujeta a validación en población hospitalaria local."]

                result = {
                    "pmid": article.get("pmid"),
                    "pmcid": article.get("pmcid", ""),
                    "doi": article.get("doi", ""),
                    "pubmed_url": article.get("pubmed_url", f"https://pubmed.ncbi.nlm.nih.gov/{article.get('pmid')}/"),
                    "original_title": article.get("title", ""),
                    "clinical_title_es": raw_analysis.get("clinical_title_es") or article.get("title", ""),
                    "journal": article.get("journal", ""),
                    "pub_date": article.get("pub_date", ""),
                    "authors": article.get("authors", ""),
                    "category": raw_analysis.get("category") or "Clínica General",
                    "study_design": raw_analysis.get("study_design") or "Estudio Clínico",
                    "pico": {
                        "population": pico.get("population") or "No especificada en resumen.",
                        "intervention": pico.get("intervention") or "No especificada en resumen.",
                        "comparison": pico.get("comparison") or "Estándar de cuidado / Comparador habitual.",
                        "outcome": pico.get("outcome") or "Desenlace clínico primario.",
                    },
                    "quantitative_effect": {
                        "primary_effect_measure": _clean_nullable_metric(quant.get("primary_effect_measure")),
                        "rar_or_nnt": _clean_nullable_metric(quant.get("rar_or_nnt")),
                        "p_value": _clean_nullable_metric(quant.get("p_value")),
                    },
                    "methodological_limitations": [str(item) for item in limitations[:4]],
                    "hospital_applicability": (
                        raw_analysis.get("hospital_applicability")
                        or "Valorar aplicabilidad individualizada según comorbilidad del paciente."
                    ),
                    "iic_methodology_score": meth_score,
                    "iic_hospital_impact_score": hosp_score,
                    "iic_total_score": total_score,
                    "is_paradigm_shift": bool(raw_analysis.get("is_paradigm_shift")) or (total_score >= 85),
                    "model_used": model_name,
                    "is_landmark_section": is_landmark_candidate,
                }
                return result

            except urllib.error.HTTPError as http_err:
                status_code = http_err.code
                if status_code == 429:
                    # Conmutación inmediata al siguiente modelo Lite con colchón de 500 RPD
                    print(
                        f"[FAILOVER 429] Cuota alcanzada en {model_name} (PMID {article.get('pmid')}). "
                        f"Conmutando inmediatamente al siguiente modelo de la cascada...",
                        file=sys.stderr,
                    )
                    break
                elif status_code in (500, 502, 503, 504):
                    wait = min(15.0, 4.0 * (2 ** attempt)) + random.uniform(1.0, 3.0)
                    print(
                        f"[RETRY {status_code}] Congestión en {model_name} (intento {attempt + 1}/{max_attempts_per_model}). "
                        f"Esperando {wait:.2f}s con Full Jitter...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                elif status_code == 404:
                    # Si el alias del modelo aún no está desplegado en esa región, pasar al siguiente inmediatamente
                    print(
                        f"[FAILOVER 404] Modelo {model_name} no disponible en endpoint actual. "
                        f"Conmutando al siguiente modelo...",
                        file=sys.stderr,
                    )
                    break
                else:
                    print(f"[ERROR HTTP {status_code}] en {model_name}: {http_err}", file=sys.stderr)
                    break

            except (urllib.error.URLError, TimeoutError) as net_err:
                wait = min(15.0, 4.0 * (2 ** attempt)) + random.uniform(1.0, 3.0)
                print(
                    f"[TIMEOUT/NET] {model_name} (intento {attempt + 1}/{max_attempts_per_model}): {net_err}. "
                    f"Esperando {wait:.2f}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)

            except (json.JSONDecodeError, ValueError, KeyError) as parse_err:
                print(f"[PARSE WARN] Respuesta malformada desde {model_name}: {parse_err}", file=sys.stderr)
                break

    return None


def _clean_nullable_metric(val) -> str | None:
    """Normaliza métricas nulas o placeholders vacíos para colapsar tablas si no hay datos."""
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    null_tokens = {
        "null",
        "none",
        "n/a",
        "na",
        "no reportado",
        "no consta",
        "no disponible",
        "no especificado",
        "no calculable",
        "-",
        "—",
    }
    if text.lower() in null_tokens:
        return None
    return text


# ============================================================================
# 4. GENERACIÓN DE INFORMES (HTML RESPONSIVE + PLAINTEXT + TELEGRAM)
# ============================================================================

def _escape_html(text: str) -> str:
    """Escapa caracteres especiales para inserción segura en HTML."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_article_card_html(item: dict, is_landmark: bool = False) -> str:
    """
    Renderiza una tarjeta clínica HTML con:
    - Degradación dinámica del badge Landmark ("CAMBIO DE PARADIGMA" si IIC >= 85 o is_paradigm_shift,
      frente a "RELEVANCIA CLÍNICA" si IIC < 85).
    - Colapso condicional de la tabla de magnitud de efecto si faltan RAR/NNT, medida primaria y p-valor.
    """
    total_iic = int(item.get("iic_total_score", 0))
    meth_iic = int(item.get("iic_methodology_score", 0))
    hosp_iic = int(item.get("iic_hospital_impact_score", 0))
    paradigm = bool(item.get("is_paradigm_shift")) or total_iic >= 85

    if is_landmark:
        if paradigm:
            badge_bg = "#7f1d1d"
            badge_border = "#dc2626"
            badge_label = "🚨 CAMBIO DE PARADIGMA"
        else:
            badge_bg = "#1e3a8a"
            badge_border = "#3b82f6"
            badge_label = "⭐ RELEVANCIA CLÍNICA"
    else:
        if total_iic >= 80:
            badge_bg = "#065f46"
            badge_border = "#10b981"
            badge_label = f"ALTO IMPACTO · IIC {total_iic}/100"
        elif total_iic >= 65:
            badge_bg = "#1e40af"
            badge_border = "#60a5fa"
            badge_label = f"EVIDENCIA SÓLIDA · IIC {total_iic}/100"
        else:
            badge_bg = "#374151"
            badge_border = "#9ca3af"
            badge_label = f"INTERÉS CLÍNICO · IIC {total_iic}/100"

    quant = item.get("quantitative_effect", {}) or {}
    primary_eff = quant.get("primary_effect_measure")
    rar_nnt = quant.get("rar_or_nnt")
    p_val = quant.get("p_value")

    # Colapso condicional de tabla de efecto: solo renderiza columnas con datos reales verificados
    metric_cells = []
    if primary_eff:
        metric_cells.append(
            f'<td style="padding:8px 12px;border-right:1px solid #e2e8f0;">'
            f'<div style="font-size:11px;color:#64748b;text-transform:uppercase;font-weight:700;">Efecto Primario (IC 95%)</div>'
            f'<div style="font-size:13px;color:#0f172a;font-weight:700;margin-top:2px;">{_escape_html(primary_eff)}</div>'
            f"</td>"
        )
    if rar_nnt:
        metric_cells.append(
            f'<td style="padding:8px 12px;border-right:1px solid #e2e8f0;">'
            f'<div style="font-size:11px;color:#64748b;text-transform:uppercase;font-weight:700;">RAR / NNT</div>'
            f'<div style="font-size:13px;color:#0f172a;font-weight:700;margin-top:2px;">{_escape_html(rar_nnt)}</div>'
            f"</td>"
        )
    if p_val:
        metric_cells.append(
            f'<td style="padding:8px 12px;">'
            f'<div style="font-size:11px;color:#64748b;text-transform:uppercase;font-weight:700;">Significación (p)</div>'
            f'<div style="font-size:13px;color:#0f172a;font-weight:700;margin-top:2px;">{_escape_html(p_val)}</div>'
            f"</td>"
        )

    effect_table_html = ""
    if metric_cells:
        effect_table_html = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="margin:12px 0;background-color:#f8fafc;border:1px solid #cbd5e1;border-radius:6px;">'
            "<tr>" + "".join(metric_cells) + "</tr></table>"
        )

    pico = item.get("pico", {}) or {}
    limitations_items = "".join(
        f'<li style="margin-bottom:4px;">{_escape_html(lim)}</li>'
        for lim in (item.get("methodological_limitations") or [])
    )

    border_top_color = "#dc2626" if (is_landmark and paradigm) else ("#2563eb" if is_landmark else "#0f766e")

    return f"""
    <div style="background-color:#ffffff;border:1px solid #e2e8f0;border-top:4px solid {border_top_color};border-radius:8px;padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 3px rgba(15,23,42,0.06);">
      <div style="margin-bottom:8px;">
        <span style="display:inline-block;background-color:{badge_bg};border:1px solid {badge_border};color:#ffffff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:4px;letter-spacing:0.4px;margin-right:6px;">
          {_escape_html(badge_label)}
        </span>
        <span style="display:inline-block;background-color:#f1f5f9;color:#334155;font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;margin-right:6px;">
          {_escape_html(item.get("category", "Clínica General"))}
        </span>
        <span style="display:inline-block;background-color:#ecfeff;color:#0f766e;font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;">
          IIC: {total_iic}/100 (Metodología {meth_iic}/60 · Impacto {hosp_iic}/40)
        </span>
      </div>

      <h3 style="margin:6px 0 4px 0;font-size:17px;line-height:1.35;color:#0f172a;">
        <a href="{_escape_html(item.get("pubmed_url", "#"))}" style="color:#0f172a;text-decoration:none;">
          {_escape_html(item.get("clinical_title_es"))}
        </a>
      </h3>
      <p style="margin:0 0 10px 0;font-size:12px;color:#475569;">
        <strong>{_escape_html(item.get("journal"))}</strong> ({_escape_html(item.get("pub_date"))}) ·
        {_escape_html(item.get("study_design"))} · PMID:
        <a href="{_escape_html(item.get("pubmed_url", "#"))}" style="color:#2563eb;text-decoration:underline;">{_escape_html(item.get("pmid"))}</a>
      </p>

      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="font-size:13px;color:#1e293b;line-height:1.45;margin-bottom:10px;">
        <tr>
          <td style="padding:4px 0;"><strong>P (Población):</strong> {_escape_html(pico.get("population"))}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;"><strong>I (Intervención):</strong> {_escape_html(pico.get("intervention"))}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;"><strong>C (Comparador):</strong> {_escape_html(pico.get("comparison"))}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;"><strong>O (Desenlace):</strong> {_escape_html(pico.get("outcome"))}</td>
        </tr>
      </table>

      {effect_table_html}

      <div style="background-color:#fffbeb;border-left:3px solid #f59e0b;padding:8px 12px;margin:10px 0;font-size:12px;color:#78350f;">
        <strong>Lectura Crítica / Limitaciones:</strong>
        <ul style="margin:4px 0 0 16px;padding:0;">
          {limitations_items}
        </ul>
      </div>

      <div style="background-color:#f0fdf4;border-left:3px solid #16a34a;padding:9px 12px;margin-top:10px;font-size:13px;color:#14532d;">
        <strong>Aplicabilidad Hospitalaria Directa:</strong> {_escape_html(item.get("hospital_applicability"))}
      </div>
    </div>
    """


def build_html_report_master(
    profile_config: dict,
    landmark_results: list[dict],
    regular_results: list[dict],
    execution_stats: dict | None = None,
) -> str:
    """
    Construye el boletín HTML completo con diseño tipo tarjeta médica responsive,
    sección Landmark (con degradación dinámica de badge) y novedades agrupadas por categoría.
    """
    stats = execution_stats or {}
    profile_name = profile_config.get("profile_name", "Especialidad Hospitalaria")
    today_str = datetime.date.today().strftime("%d/%m/%Y")

    landmark_html = ""
    if landmark_results:
        cards = "".join(_render_article_card_html(item, is_landmark=True) for item in landmark_results)
        landmark_html = f"""
        <div style="margin-bottom:26px;">
          <h2 style="font-size:18px;color:#991b1b;border-bottom:2px solid #fecaca;padding-bottom:6px;margin-bottom:14px;">
            🏛️ Radar Landmark (Revistas Troncales · Alta Evidencia)
          </h2>
          {cards}
        </div>
        """

    regular_html = ""
    if regular_results:
        # Agrupar por categoría preservando orden por IIC descendente
        by_cat: dict[str, list[dict]] = {}
        for item in sorted(regular_results, key=lambda x: x.get("iic_total_score", 0), reverse=True):
            cat = item.get("category") or "Clínica General"
            by_cat.setdefault(cat, []).append(item)

        cat_sections = []
        for cat_name, items in by_cat.items():
            cards = "".join(_render_article_card_html(it, is_landmark=False) for it in items)
            cat_sections.append(
                f'<h3 style="font-size:15px;color:#0f766e;margin:18px 0 10px 0;text-transform:uppercase;letter-spacing:0.5px;">'
                f"📂 {_escape_html(cat_name)} ({len(items)})</h3>"
                f"{cards}"
            )

        regular_html = f"""
        <div style="margin-bottom:26px;">
          <h2 style="font-size:18px;color:#0f172a;border-bottom:2px solid #cbd5e1;padding-bottom:6px;margin-bottom:14px;">
            🔬 Novedades Semanales de la Especialidad
          </h2>
          {"".join(cat_sections)}
        </div>
        """

    if not landmark_html and not regular_html:
        empty_block = """
        <div style="background-color:#ffffff;border:1px solid #cbd5e1;border-radius:8px;padding:20px;text-align:center;color:#475569;">
          No se han detectado nuevos ensayos clínicos o metaanálisis no enviados previamente en esta ventana temporal.
        </div>
        """
    else:
        empty_block = ""

    scanned_count = stats.get("total_scanned", 0)
    audited_count = len(landmark_results) + len(regular_results)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Open Medical Radar — {_escape_html(profile_name)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:#f1f5f9;padding:20px 10px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background-color:#f8fafc;border-radius:10px;overflow:hidden;border:1px solid #cbd5e1;">
          <tr>
            <td style="background-color:#0f172a;color:#ffffff;padding:22px 24px;">
              <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:#38bdf8;font-weight:700;">
                OPEN MEDICAL RADAR · VIGILANCIA PICO &amp; ESCALA IIC
              </div>
              <h1 style="margin:6px 0 4px 0;font-size:22px;color:#ffffff;">
                {_escape_html(profile_name)}
              </h1>
              <div style="font-size:13px;color:#cbd5e1;">
                Boletín del {today_str} · {audited_count} estudios auditados de {scanned_count} candidatos indexados en PubMed
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 24px;">
              {landmark_html}
              {regular_html}
              {empty_block}
            </td>
          </tr>
          <tr>
            <td style="background-color:#e2e8f0;color:#475569;padding:14px 24px;font-size:11px;text-align:center;">
              Generado automáticamente por <strong>open-medical-radar</strong> (Python 3.11 Standard Library · Coste 0 €).<br>
              Escala IIC: Metodología (0-60 pts) + Impacto Hospitalario (0-40 pts). Cero datos de pacientes procesados.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def build_plaintext_report(
    profile_config: dict,
    landmark_results: list[dict],
    regular_results: list[dict],
    execution_stats: dict | None = None,
) -> str:
    """
    Construye el informe en texto plano (para fallback multipart de correo electrónico
    y base legible en consola).
    """
    profile_name = profile_config.get("profile_name", "Especialidad Hospitalaria")
    today_str = datetime.date.today().strftime("%d/%m/%Y")
    lines = [
        "=" * 74,
        f"OPEN MEDICAL RADAR — {profile_name.upper()} ({today_str})",
        "=" * 74,
        "",
    ]

    def _format_item_txt(item: dict, is_landmark: bool) -> list[str]:
        total_iic = int(item.get("iic_total_score", 0))
        paradigm = bool(item.get("is_paradigm_shift")) or total_iic >= 85
        if is_landmark:
            tag = "[CAMBIO DE PARADIGMA]" if paradigm else "[RELEVANCIA CLÍNICA]"
        else:
            tag = f"[IIC {total_iic}/100]"

        pico = item.get("pico", {}) or {}
        quant = item.get("quantitative_effect", {}) or {}
        block = [
            f"{tag} {item.get('clinical_title_es')}",
            f"Revista: {item.get('journal')} ({item.get('pub_date')}) | Diseño: {item.get('study_design')}",
            f"Categoría: {item.get('category')} | IIC: {total_iic}/100 (Met: {item.get('iic_methodology_score')}/60, Hosp: {item.get('iic_hospital_impact_score')}/40)",
            f"Enlace PubMed: {item.get('pubmed_url')}",
            f"  - P: {pico.get('population')}",
            f"  - I: {pico.get('intervention')}",
            f"  - C: {pico.get('comparison')}",
            f"  - O: {pico.get('outcome')}",
        ]
        metrics = []
        if quant.get("primary_effect_measure"):
            metrics.append(f"Efecto: {quant['primary_effect_measure']}")
        if quant.get("rar_or_nnt"):
            metrics.append(f"RAR/NNT: {quant['rar_or_nnt']}")
        if quant.get("p_value"):
            metrics.append(f"p: {quant['p_value']}")
        if metrics:
            block.append("  - Magnitud de efecto: " + " | ".join(metrics))

        lims = item.get("methodological_limitations") or []
        if lims:
            block.append("  - Limitaciones: " + "; ".join(lims))
        block.append(f"  - Aplicabilidad Hospitalaria: {item.get('hospital_applicability')}")
        block.append("-" * 74)
        return block

    if landmark_results:
        lines.append(">>> SECCIÓN 1: RADAR LANDMARK (REVISTAS TRONCALES) <<<")
        lines.append("")
        for it in landmark_results:
            lines.extend(_format_item_txt(it, is_landmark=True))
            lines.append("")

    if regular_results:
        lines.append(">>> SECCIÓN 2: NOVEDADES SEMANALES DE LA ESPECIALIDAD <<<")
        lines.append("")
        for it in sorted(regular_results, key=lambda x: x.get("iic_total_score", 0), reverse=True):
            lines.extend(_format_item_txt(it, is_landmark=False))
            lines.append("")

    if not landmark_results and not regular_results:
        lines.append("No se han encontrado nuevos artículos que superen el filtro metodológico en este periodo.")

    return "\n".join(lines)


def _build_telegram_html_digest(
    profile_config: dict,
    landmark_results: list[dict],
    regular_results: list[dict],
) -> str:
    """Construye el mensaje HTML compacto compatible con el parser HTML de Telegram."""
    profile_name = _escape_html(profile_config.get("profile_name", "Especialidad"))
    today_str = datetime.date.today().strftime("%d/%m/%Y")
    chunks = [f"🩺 <b>OPEN MEDICAL RADAR · {profile_name}</b> ({today_str})\n"]

    all_items = [(it, True) for it in landmark_results] + [
        (it, False) for it in sorted(regular_results, key=lambda x: x.get("iic_total_score", 0), reverse=True)
    ]

    if not all_items:
        chunks.append("ℹ️ Sin nuevos ensayos o metaanálisis relevantes en esta ventana semanal.")
        return "\n".join(chunks)

    for idx, (item, is_landmark) in enumerate(all_items, start=1):
        total_iic = int(item.get("iic_total_score", 0))
        paradigm = bool(item.get("is_paradigm_shift")) or total_iic >= 85
        if is_landmark:
            header_icon = "🚨 <b>[CAMBIO DE PARADIGMA]</b>" if paradigm else "⭐ <b>[LANDMARK · RELEVANCIA CLÍNICA]</b>"
        else:
            header_icon = f"🔬 <b>[{_escape_html(item.get('category', 'Clínica'))}]</b>"

        pico = item.get("pico", {}) or {}
        quant = item.get("quantitative_effect", {}) or {}
        effect_parts = [
            x for x in (quant.get("primary_effect_measure"), quant.get("rar_or_nnt"), quant.get("p_value")) if x
        ]
        effect_line = f"\n📊 <b>Efecto:</b> {_escape_html(' | '.join(effect_parts))}" if effect_parts else ""

        entry = (
            f"\n{idx}. {header_icon} <b>IIC {total_iic}/100</b>\n"
            f"<b>{_escape_html(item.get('clinical_title_es'))}</b>\n"
            f"<i>{_escape_html(item.get('journal'))} ({_escape_html(item.get('pub_date'))}) — {_escape_html(item.get('study_design'))}</i>\n"
            f"• <b>P:</b> {_escape_html(pico.get('population'))}\n"
            f"• <b>I/C:</b> {_escape_html(pico.get('intervention'))} vs {_escape_html(pico.get('comparison'))}\n"
            f"• <b>O:</b> {_escape_html(pico.get('outcome'))}"
            f"{effect_line}\n"
            f"🏥 <b>Planta/Consulta:</b> {_escape_html(item.get('hospital_applicability'))}\n"
            f"🔗 <a href=\"{_escape_html(item.get('pubmed_url'))}\">Ver en PubMed (PMID {item.get('pmid')})</a>\n"
        )
        chunks.append(entry)

    return "\n".join(chunks)


# ============================================================================
# 5. CANALES DE NOTIFICACIÓN (EMAIL SMTP MULTIPART + TELEGRAM CHUNKED)
# ============================================================================

def send_email_multipart(
    smtp_server: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    email_from: str,
    email_to: str,
    subject: str,
    plaintext_body: str,
    html_body: str,
) -> bool:
    """
    Envía el reporte semanal por correo electrónico mediante SMTP con STARTTLS
    (o SSL si el puerto es 465), adjuntando versión Plaintext y HTML responsive.
    """
    if not (smtp_server and smtp_user and smtp_password and email_to):
        print("[INFO] Credenciales SMTP incompletas; se omite envío por Email.", file=sys.stderr)
        return False

    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]
    if not recipients:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from or smtp_user
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(plaintext_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        port_int = int(smtp_port or 587)
        if port_int == 465:
            with smtplib.SMTP_SSL(smtp_server, port_int, timeout=25) as server:
                server.login(smtp_user, smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, port_int, timeout=25) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        print(f"[OK] Email enviado correctamente a {', '.join(recipients)}.")
        return True
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        print(f"[ERROR] Fallo en send_email_multipart: {exc}", file=sys.stderr)
        return False


def _split_telegram_message(text: str, max_len: int = 4000) -> list[str]:
    """
    Divide mensajes largos en bloques <= 4000 caracteres respetando saltos de párrafo
    para no romper etiquetas HTML de Telegram.
    """
    if len(text) <= max_len:
        return [text]

    blocks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for paragraph in text.split("\n\n"):
        para_len = len(paragraph) + 2
        if current_len + para_len > max_len and current_lines:
            blocks.append("\n\n".join(current_lines))
            current_lines = [paragraph]
            current_len = len(paragraph)
        elif para_len > max_len:
            # Si un único párrafo supera 4000 caracteres, cortar por líneas
            if current_lines:
                blocks.append("\n\n".join(current_lines))
                current_lines = []
                current_len = 0
            sub_chunk = ""
            for line in paragraph.split("\n"):
                if len(sub_chunk) + len(line) + 1 > max_len:
                    if sub_chunk:
                        blocks.append(sub_chunk)
                    sub_chunk = line[:max_len]
                else:
                    sub_chunk = f"{sub_chunk}\n{line}" if sub_chunk else line
            if sub_chunk:
                blocks.append(sub_chunk)
        else:
            current_lines.append(paragraph)
            current_len += para_len

    if current_lines:
        blocks.append("\n\n".join(current_lines))

    return blocks


def send_telegram_alert(token: str, chat_id: str, text_content: str) -> bool:
    """
    Envía alertas a Telegram mediante HTTP POST directo a
    `https://api.telegram.org/bot<TOKEN>/sendMessage` con soporte HTML y
    división automática en bloques <= 4000 caracteres.
    """
    if not token or not chat_id:
        print("[INFO] Token o Chat ID de Telegram no configurados; se omite envío por Telegram.", file=sys.stderr)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token.strip()}/sendMessage"
    chunks = _split_telegram_message(text_content, max_len=4000)
    all_ok = True

    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": str(chat_id).strip(),
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "open-medical-radar/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status not in (200, 201):
                    all_ok = False
            if len(chunks) > 1 and idx < len(chunks):
                time.sleep(1.0)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"[ERROR] Fallo enviando bloque {idx}/{len(chunks)} a Telegram: {exc}", file=sys.stderr)
            all_ok = False

    if all_ok:
        print(f"[OK] Notificación de Telegram enviada en {len(chunks)} bloque(s).")
    return all_ok


# ============================================================================
# 6. MODO DIAGNÓSTICO (test_connection) Y GESTIÓN DE ARCHIVOS
# ============================================================================

def run_test_connection(summary_file: str | None = None) -> int:
    """
    Ejecuta un ping sintético a PubMed E-Utilities y a Google Gemini API para
    validar las API Keys configuradas en GitHub Secrets e imprime un informe
    claro en español para el facultativo en el GitHub Step Summary.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    ncbi_key = os.environ.get("NCBI_API_KEY", "").strip() or None
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()

    report_lines = [
        "## 🩺 Diagnóstico de Conexión — Open Medical Radar",
        "",
        "| Componente | Estado | Detalle Clínico / Técnico |",
        "| :--- | :--- | :--- |",
    ]

    # 1. Prueba PubMed E-Utilities
    pmids = fetch_pmids_by_date_range(
        query='"New England Journal of Medicine"[Journal] AND hasabstract[text]',
        days_lookback=30,
        retmax=3,
        ncbi_api_key=ncbi_key,
    )
    if pmids:
        pubmed_status = "✅ Operativo"
        pubmed_detail = f"Conectado a NCBI PubMed (recuperados {len(pmids)} PMIDs de prueba: {', '.join(pmids)})."
    else:
        pubmed_status = "⚠️ Revisar"
        pubmed_detail = "Sin respuesta de ESearch o sin artículos recientes en el rango de prueba."
    report_lines.append(f"| **PubMed E-Utilities** | {pubmed_status} | {pubmed_detail} |")

    # 2. Prueba cascada Gemini API
    gemini_ok = False
    gemini_detail = "Secreto `GEMINI_API_KEY` no configurado en GitHub Secrets."
    if gemini_key:
        test_prompt = (
            'Devuelve exclusivamente este JSON: {"status": "ok", "message": "Conexion clinica verificada"}'
        )
        for model in MODEL_CASCADE:
            try:
                res = query_gemini_model(model, test_prompt, gemini_key, timeout_sec=15)
                if isinstance(res, dict):
                    gemini_ok = True
                    gemini_detail = f"Respuesta JSON verificada correctamente con el modelo `{model}` (Coste 0 €)."
                    break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                gemini_detail = f"Fallo en `{model}` ({exc}); probando siguiente en cascada..."
                continue

    gemini_status = "✅ Operativo" if gemini_ok else "❌ Error de API Key"
    report_lines.append(f"| **Google Gemini API** | {gemini_status} | {gemini_detail} |")

    # 3. Verificación de canales de salida
    tg_configured = bool(telegram_token and telegram_chat)
    email_configured = bool(smtp_user and email_to)
    if tg_configured or email_configured:
        canales = []
        if email_configured:
            canales.append(f"Email ({email_to})")
        if tg_configured:
            canales.append(f"Telegram (Chat ID {telegram_chat})")
        channel_status = "✅ Configurado"
        channel_detail = "Canales listos: " + " + ".join(canales)
    else:
        channel_status = "⚠️ Pendiente"
        channel_detail = "Configura `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` o las variables `SMTP_*` en Settings -> Secrets."
    report_lines.append(f"| **Canales de Aviso** | {channel_status} | {channel_detail} |")

    report_lines.append("")
    if pmids and gemini_ok:
        report_lines.append("### 🎉 Todo listo: Tu radar médico está preparado para ejecutarse cada lunes a las 07:00 UTC.")
    else:
        report_lines.append("### ⚠️ Revisa la tabla superior para completar los secretos pendientes antes del próximo lunes.")

    markdown_summary = "\n".join(report_lines) + "\n"
    print(markdown_summary)

    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as sf:
                sf.write(markdown_summary)
        except OSError as exc:
            print(f"[WARN] No se pudo escribir en GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)

    return 0 if (pmids and gemini_ok) else 1


def _load_sent_pmids(filepath: str) -> dict:
    """Carga el historial de PMIDs enviados desde data/sent_pmids.json."""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sent_pmids(filepath: str, data: dict) -> None:
    """Guarda de forma atómica el diccionario actualizado en data/sent_pmids.json."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _discover_profile_configs(target_profile: str, config_dir: str = "config") -> list[tuple[str, dict]]:
    """
    Resuelve los perfiles JSON a ejecutar según el parámetro `--profile`:
    - Si es 'all': carga todos los `.json` personalizados en `config/*.json`;
      si aún no hay perfiles personalizados, ejecuta los presets de `config/presets/*.json`.
    - Si es un nombre concreto: lo busca primero en `config/<nombre>.json` y luego en `config/presets/<nombre>.json`.
    """
    presets_dir = os.path.join(config_dir, "presets")
    loaded: list[tuple[str, dict]] = []

    if target_profile.lower() == "all":
        custom_files = sorted(
            os.path.join(config_dir, fn)
            for fn in os.listdir(config_dir)
            if fn.endswith(".json") and os.path.isfile(os.path.join(config_dir, fn))
        ) if os.path.isdir(config_dir) else []

        target_files = custom_files
        if not target_files and os.path.isdir(presets_dir):
            target_files = sorted(
                os.path.join(presets_dir, fn)
                for fn in os.listdir(presets_dir)
                if fn.endswith(".json") and os.path.isfile(os.path.join(presets_dir, fn))
            )

        for path in target_files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                pid = cfg.get("profile_id") or os.path.splitext(os.path.basename(path))[0]
                loaded.append((pid, cfg))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[WARN] Error leyendo perfil {path}: {exc}", file=sys.stderr)
        return loaded

    clean_name = target_profile.strip().replace(".json", "")
    candidates = [
        os.path.join(config_dir, f"{clean_name}.json"),
        os.path.join(presets_dir, f"{clean_name}.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            pid = cfg.get("profile_id") or clean_name
            return [(pid, cfg)]

    raise FileNotFoundError(f"No se encontró el perfil '{target_profile}' en config/ ni en config/presets/.")


# ============================================================================
# 7. ORQUESTADOR PRINCIPAL (main)
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="open-medical-radar: Vigilancia bibliográfica médica semanal (PICO + IIC) sin dependencias externas."
    )
    parser.add_argument(
        "--profile",
        default="all",
        help="Perfil a ejecutar ('all', 'test_connection', o ID del perfil como 'medicina_interna').",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Ventana de días hacia atrás en PubMed (por defecto: 7).",
    )
    parser.add_argument(
        "--sent-db",
        default=os.path.join("data", "sent_pmids.json"),
        help="Ruta al archivo JSON de historial de PMIDs enviados.",
    )
    parser.add_argument(
        "--config-dir",
        default="config",
        help="Directorio raíz de perfiles y presets JSON.",
    )
    parser.add_argument(
        "--summary-file",
        default=os.environ.get("GITHUB_STEP_SUMMARY", ""),
        help="Ruta opcional al archivo GITHUB_STEP_SUMMARY para reporte visual en GitHub Actions.",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Ejecuta el modo diagnóstico de validación de claves (PubMed + Gemini).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ejecuta la canalización sin enviar emails ni mensajes reales.",
    )

    args = parser.parse_args(argv)
    start_monotonic = time.monotonic()

    if args.test_connection or args.profile.strip().lower() == "test_connection":
        return run_test_connection(summary_file=args.summary_file or None)

    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip() or None

    if not gemini_api_key and not args.dry_run:
        print(
            "[ERROR] GEMINI_API_KEY no está configurada. Configúrala en Settings -> Secrets -> Actions.",
            file=sys.stderr,
        )
        return 1

    try:
        profiles = _discover_profile_configs(args.profile, config_dir=args.config_dir)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    sent_registry = _load_sent_pmids(args.sent_db)
    summary_markdown_lines = [
        "## 📡 Resumen de Ejecución — Open Medical Radar",
        "",
        "| Perfil Clínico | Candidatos PubMed | Landmark Auditados | Novedades Auditadas | Canal Notificado |",
        "| :--- | :---: | :---: | :---: | :--- |",
    ]

    for profile_id, profile_cfg in profiles:
        elapsed = time.monotonic() - start_monotonic
        if elapsed > GLOBAL_EXECUTION_BUDGET_SEC:
            print("[WARN] Presupuesto de tiempo global alcanzado; deteniendo procesamiento adicional.", file=sys.stderr)
            break

        profile_name = profile_cfg.get("profile_name", profile_id)
        days_lookback = max(1, int(args.days or profile_cfg.get("days_lookback", 7)))
        max_landmark_budget = int(profile_cfg.get("max_landmark_calls", MAX_LANDMARK_CALLS_DEFAULT))
        max_regular_budget = int(profile_cfg.get("max_regular_calls", MAX_REGULAR_CALLS_DEFAULT))

        print(f"\n=== Procesando perfil: {profile_name} ({profile_id}) ===")

        profile_sent_list = sent_registry.get(profile_id, [])
        if not isinstance(profile_sent_list, list):
            profile_sent_list = []
        profile_sent_set = set(str(x) for x in profile_sent_list)

        # 1. Consultar PubMed y extraer detalles XML
        query_str = build_pubmed_query(profile_cfg)
        pmids = fetch_pmids_by_date_range(
            query=query_str,
            days_lookback=days_lookback,
            retmax=120,
            ncbi_api_key=ncbi_api_key,
        )
        raw_articles = fetch_article_details(pmids, ncbi_api_key=ncbi_api_key)
        ranked_candidates = filter_and_rank_candidates(raw_articles, profile_cfg, profile_sent_set)

        print(
            f"[INFO] PubMed devolvió {len(pmids)} PMIDs -> {len(raw_articles)} con abstract válido -> "
            f"{len(ranked_candidates)} candidatos tras filtrado metodológico en memoria."
        )

        # Separar candidatos en Landmark (revistas troncales top) y Novedades regulares
        landmark_candidates = [c for c in ranked_candidates if c.get("is_ground_journal")][:max_landmark_budget]
        landmark_pmids = {c["pmid"] for c in landmark_candidates}
        regular_candidates = [c for c in ranked_candidates if c["pmid"] not in landmark_pmids][:max_regular_budget]

        landmark_results: list[dict] = []
        regular_results: list[dict] = []

        # 2. Auditar candidatos Landmark (presupuesto: hasta 4 llamadas)
        for cand in landmark_candidates:
            if time.monotonic() - start_monotonic > GLOBAL_EXECUTION_BUDGET_SEC:
                break
            if cand.get("pmcid"):
                cand["pmc_fulltext"] = fetch_pmc_fulltext(cand["pmcid"], ncbi_api_key=ncbi_api_key)
            if args.dry_run and not gemini_api_key:
                continue
            audited = analyze_article_with_gemini(
                article=cand,
                profile_name=profile_name,
                api_key=gemini_api_key,
                is_landmark_candidate=True,
                config=profile_cfg,
            )
            if audited:
                landmark_results.append(audited)
                profile_sent_set.add(str(cand["pmid"]))

        # 3. Auditar candidatos de Novedades Semanales (presupuesto: hasta 8 llamadas)
        for cand in regular_candidates:
            if time.monotonic() - start_monotonic > GLOBAL_EXECUTION_BUDGET_SEC:
                break
            if cand.get("pmcid"):
                cand["pmc_fulltext"] = fetch_pmc_fulltext(cand["pmcid"], ncbi_api_key=ncbi_api_key)
            if args.dry_run and not gemini_api_key:
                continue
            audited = analyze_article_with_gemini(
                article=cand,
                profile_name=profile_name,
                api_key=gemini_api_key,
                is_landmark_candidate=False,
                config=profile_cfg,
            )
            if audited:
                regular_results.append(audited)
                profile_sent_set.add(str(cand["pmid"]))

        # Ordenar resultados por IIC de mayor a menor
        landmark_results.sort(key=lambda x: x.get("iic_total_score", 0), reverse=True)
        regular_results.sort(key=lambda x: x.get("iic_total_score", 0), reverse=True)

        stats = {
            "total_scanned": len(raw_articles),
            "candidates_filtered": len(ranked_candidates),
            "landmark_count": len(landmark_results),
            "regular_count": len(regular_results),
        }

        html_report = build_html_report_master(profile_cfg, landmark_results, regular_results, stats)
        txt_report = build_plaintext_report(profile_cfg, landmark_results, regular_results, stats)
        tg_digest = _build_telegram_html_digest(profile_cfg, landmark_results, regular_results)

        # 4. Envío por los canales configurados
        channel_pref = (profile_cfg.get("notification_channel") or "both").lower()
        channels_notified = []

        if not args.dry_run and (landmark_results or regular_results):
            if channel_pref in ("both", "email"):
                target_email = profile_cfg.get("recipient_email") or os.environ.get("EMAIL_TO", "")
                subject = f"🩺 Radar Bibliográfico: {profile_name} ({datetime.date.today().strftime('%d/%m/%Y')})"
                email_ok = send_email_multipart(
                    smtp_server=os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
                    smtp_port=int(os.environ.get("SMTP_PORT", "587") or 587),
                    smtp_user=os.environ.get("SMTP_USER", ""),
                    smtp_password=os.environ.get("SMTP_PASSWORD", ""),
                    email_from=os.environ.get("EMAIL_FROM", "") or os.environ.get("SMTP_USER", ""),
                    email_to=target_email,
                    subject=subject,
                    plaintext_body=txt_report,
                    html_body=html_report,
                )
                if email_ok:
                    channels_notified.append("Email")

            if channel_pref in ("both", "telegram"):
                target_chat = profile_cfg.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
                tg_ok = send_telegram_alert(
                    token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                    chat_id=target_chat,
                    text_content=tg_digest,
                )
                if tg_ok:
                    channels_notified.append("Telegram")

        # Actualizar historial de PMIDs enviados (conservando los últimos 2000 por perfil)
        sent_registry[profile_id] = sorted(profile_sent_set)[-2000:]
        _save_sent_pmids(args.sent_db, sent_registry)

        notif_str = ", ".join(channels_notified) if channels_notified else ("Dry-Run / Sin envíos" if args.dry_run else "Local / Sin credenciales")
        summary_markdown_lines.append(
            f"| **{profile_name}** | {len(raw_articles)} | {len(landmark_results)} | {len(regular_results)} | {notif_str} |"
        )

    summary_markdown = "\n".join(summary_markdown_lines) + "\n"
    print("\n" + summary_markdown)

    if args.summary_file:
        try:
            with open(args.summary_file, "a", encoding="utf-8") as sf:
                sf.write(summary_markdown)
        except OSError as exc:
            print(f"[WARN] No se pudo escribir en GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())