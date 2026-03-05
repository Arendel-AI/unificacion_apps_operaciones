import os
import json
import time as time_mod
from datetime import datetime, date, timezone
from typing import Optional, List, Dict, Any

from dateutil import tz
import requests
import streamlit as st
import pandas as pd
from pyairtable import Table, Api
from requests.exceptions import HTTPError

# Cloudinary (solo funciona si configuras CLOUDINARY_*)
import cloudinary
import cloudinary.uploader

import base64
import hashlib
import traceback


# =========================
# CONFIG GLOBAL
# =========================
st.set_page_config(page_title="Panel Repartidores", page_icon="📋", layout="wide")


def get_secret(key: str, default: str = "") -> str:
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    try:
        return st.secrets[key]
    except Exception:
        return default


# =========================
# GITHUB ERROR LOGGER
# =========================

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_str(x, max_len=2000) -> str:
    s = str(x) if x is not None else ""
    s = s.replace("\x00", "")
    return s[:max_len]


def _redact_text(s: str) -> str:
    if not s:
        return s
    # redacción básica
    s = s.replace("Bearer ", "Bearer [REDACTED] ")
    return s


def _hash_pii(value: str) -> str:
    """
    Hashea valores sensibles (DNI, nombre) para NO guardarlos en claro.
    Usa un SALT desde secrets para que no sea “predecible”.
    """
    if not value:
        return ""
    salt = get_secret("ERROR_LOG_SALT", "")
    raw = f"{salt}::{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _make_error_id(exc: Exception, where: str) -> str:
    raw = f"{type(exc).__name__}|{_safe_str(exc)}|{where}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def github_create_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content_bytes: bytes,
    message: str,
    token: str,
):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch,
    }
    r = requests.put(url, headers=_gh_headers(token), json=payload, timeout=30)

    # Si hay conflicto típico (409) por “file exists where dir should be”
    # hacemos un fallback a otra ruta para no perder el log.
    if r.status_code == 409:
        fallback_path = f"{path}.fallback.json"
        url2 = f"https://api.github.com/repos/{owner}/{repo}/contents/{fallback_path}"
        r2 = requests.put(url2, headers=_gh_headers(token), json=payload, timeout=30)
        if r2.status_code in (200, 201):
            return

    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub log upload failed {r.status_code}: {_safe_str(r.text)}")


def log_exception_to_github(
    exc: Exception,
    where: str,
    extra: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Best-effort: si GitHub falla, NO rompe la app.
    Devuelve error_id si se pudo generar.
    """
    try:
        token = get_secret("GITHUB_TOKEN")
        owner = get_secret("GITHUB_OWNER")
        repo = get_secret("GITHUB_REPO")
        branch = get_secret("GITHUB_BRANCH", "main")
        base_dir = get_secret("ERROR_LOG_DIR", "logs/errors")
        app_instance = get_secret("APP_INSTANCE", "unknown")

        if not (token and owner and repo):
            return None

        now = datetime.now(timezone.utc)
        month_dir = now.strftime("%Y-%m")
        error_id = _make_error_id(exc, where)

        payload = {
            "ts_utc": now.isoformat(),
            "app_instance": app_instance,
            "where": _safe_str(where, 200),
            "error_id": error_id,
            "exc_type": type(exc).__name__,
            "exc_message": _redact_text(_safe_str(exc, 2000)),
            "traceback": _redact_text(traceback.format_exc()),
            "extra": extra or {},
        }

        fname = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{error_id}.json"
        path = f"{base_dir}/{month_dir}/{fname}"
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        github_create_file(
            owner=owner,
            repo=repo,
            branch=branch,
            path=path,
            content_bytes=content,
            message=f"chore(log): error {error_id} at {where}",
            token=token,
        )
        return error_id
    except Exception:
        return None


def run_with_error_logging(where: str, fn, extra: Optional[Dict[str, Any]] = None):
    """
    Ejecuta fn() y si falla:
      - log a GitHub
      - muestra error en Streamlit con un ID
      - st.stop() para cortar el flujo actual de forma limpia
    """
    try:
        return fn()
    except Exception as e:
        err_id = log_exception_to_github(e, where=where, extra=extra)
        if err_id:
            st.error(f"❌ Ocurrió un error. ID: {err_id}")
        else:
            st.error("❌ Ocurrió un error (no se pudo enviar log a GitHub).")
        st.stop()


# =========================
# SECRETS APP 1 — LLAMADOS DE ATENCIÓN
# =========================

API_KEY = get_secret("AIRTABLE_API_KEY")
BASE_ID = get_secret("AIRTABLE_BASE_ID")
TBL_LLAMADOS = get_secret("AIRTABLE_TABLE_LLAMADOS", "llamados")

EXT_API_KEY = get_secret("EXT_AIRTABLE_API_KEY")
EXT_BASE_ID = get_secret("EXT_AIRTABLE_BASE_ID", BASE_ID or "")
EXT_TABLE = get_secret("EXT_AIRTABLE_TABLE", "cuentas")
EXT_DNI_FIELD = get_secret("EXT_DNI_FIELD", "documentoDniONie")
EXT_NAME_FIELD = get_secret("EXT_NAME_FIELD", "nombre")
EXT_VIEW = get_secret("EXT_VIEW", "")

# =========================
# SECRETS APP 2 — QUITAR HORAS
# =========================

HORAS_API_KEY = get_secret("HORAS_AIRTABLE_API_KEY")
HORAS_BASE_ID = get_secret("HORAS_AIRTABLE_BASE_ID")
HORAS_QUITAR_HORAS_TABLE_NAME = get_secret("HORAS_QUITAR_HORAS_TABLE_NAME", "quitar_horas_trabajadores")

HORAS_EXT_API_KEY = get_secret("HORAS_EXT_AIRTABLE_API_KEY")
HORAS_EXT_BASE_ID = get_secret("HORAS_EXT_AIRTABLE_BASE_ID", HORAS_BASE_ID or "")
HORAS_TRABAJADORES_TABLE_NAME = get_secret("HORAS_TRABAJADORES_TABLE_NAME", "cuentas")

HORAS_EXT_VIEW = get_secret("HORAS_EXT_VIEW", "")
HORAS_FIELD_FECHA_NO_TRABAJADA = get_secret("HORAS_FIELD_FECHA_NO_TRABAJADA", "Fecha_No_Trabajada")

# =========================
# SECRETS APP 3 — REASIGNACIONES
# =========================

REASIG_SRC_API_KEY = get_secret("REASIG_SRC_API_KEY")
REASIG_SRC_BASE_ID = get_secret("REASIG_SRC_BASE_ID")
REASIG_SRC_TABLE = get_secret("REASIG_SRC_TABLE", "cuentas")
REASIG_SRC_VIEW = get_secret("REASIG_SRC_VIEW", "")

PLANTILLA_RIDER_ID_FIELD = get_secret("PLANTILLA_RIDER_ID_FIELD", "riderId")
PLANTILLA_NAME_FIELD = get_secret("PLANTILLA_NAME_FIELD", "nombre")
PLANTILLA_DNI_FIELD = get_secret("PLANTILLA_DNI_FIELD", "documentoDniONie")

REASIG_AIRTABLE_API_KEY = get_secret("REASIG_AIRTABLE_API_KEY")
REASIG_AIRTABLE_BASE_ID = get_secret("REASIG_AIRTABLE_BASE_ID")
REASIG_TABLE_NAME = get_secret("REASIG_TABLE_NAME", "Reasignaciones")

REASIG_FIELD_RIDER_ID = get_secret("REASIG_FIELD_RIDER_ID", "Rider_ID")
REASIG_FIELD_NOMBRE = get_secret("REASIG_FIELD_NOMBRE", "Nombre")
REASIG_FIELD_DNI = get_secret("REASIG_FIELD_DNI", "DNI")
REASIG_FIELD_FECHA = get_secret("REASIG_FIELD_FECHA", "Fecha_Reasignacion")
REASIG_FIELD_MOTIVO = get_secret("REASIG_FIELD_MOTIVO", "Motivo")
REASIG_FIELD_RESP = get_secret("REASIG_FIELD_RESP", "Responsable")
REASIG_FIELD_VEHICULO = get_secret("REASIG_FIELD_VEHICULO", "Vehiculo")
REASIG_FIELD_IMAGEN = get_secret("REASIG_FIELD_IMAGEN", "Imagen")


# =========================
# VALIDACIÓN DE SECRETS + LOG
# =========================

def _stop_with_log(msg: str, where: str, extra: Optional[Dict[str, Any]] = None):
    e = RuntimeError(msg)
    _ = log_exception_to_github(e, where=where, extra=extra or {})
    st.error(msg)
    st.stop()


if not (API_KEY and BASE_ID and TBL_LLAMADOS):
    _stop_with_log(
        "Faltan variables para APP1: AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_LLAMADOS.",
        where="BOOT:missing_secrets_app1",
        extra={"has_api_key": bool(API_KEY), "has_base_id": bool(BASE_ID), "tbl": TBL_LLAMADOS or ""},
    )

if not (HORAS_API_KEY and HORAS_BASE_ID and HORAS_QUITAR_HORAS_TABLE_NAME):
    _stop_with_log(
        "Faltan variables para APP2: HORAS_AIRTABLE_API_KEY, HORAS_AIRTABLE_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME.",
        where="BOOT:missing_secrets_app2",
        extra={"has_horas_api_key": bool(HORAS_API_KEY), "has_horas_base_id": bool(HORAS_BASE_ID)},
    )

HEADERS_MAIN = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
HEADERS_EXT = {"Authorization": f"Bearer {EXT_API_KEY or API_KEY}", "Content-Type": "application/json"}


# =========================
# UTILIDADES COMUNES
# =========================

def normalize_dni(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw).strip().upper()
    s = " ".join(s.split())
    return s.replace(" ", "")


def airtable_request(method: str, url: str, headers: dict, params=None, data=None, max_retries=3):
    """
    Wrapper con:
    - retries por 429
    - y LOG AUTOMÁTICO a GitHub de cualquier status != 200/201 (401/403/etc.)
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=json.dumps(data) if data else None,
                timeout=30,
            )

            if resp.status_code in (200, 201):
                return resp.json()

            if resp.status_code == 429:
                time_mod.sleep(min(2**attempt, 10))
                continue

            try:
                err = resp.json()
            except Exception:
                err = {"error": resp.text}

            # ✅ LOG AUTOMÁTICO
            log_exception_to_github(
                RuntimeError(f"Airtable HTTP {resp.status_code}: {err}"),
                where="airtable_request:http_error",
                extra={
                    "status": resp.status_code,
                    "method": method,
                    "url": _safe_str(url, 300),
                    "params": params or {},
                    "attempt": attempt,
                },
            )

            raise RuntimeError(f"Error Airtable {resp.status_code}: {err}")

        except Exception as e:
            last_exc = e
            if attempt == max_retries:
                # último intento: re-lanzar
                raise
            time_mod.sleep(1.2 * attempt)

    # debería no llegar aquí
    if last_exc:
        raise last_exc
    raise RuntimeError("airtable_request: error desconocido")


def ui_datetime_input(label: str, value: datetime, key_prefix: str = "dt") -> datetime:
    dt_input = getattr(st, "datetime_input", None)
    if callable(dt_input):
        return dt_input(label, value=value, key=f"{key_prefix}_dt")

    col1, col2 = st.columns(2)
    with col1:
        d = st.date_input(f"{label} – fecha", value=value.date(), key=f"{key_prefix}_date")
    with col2:
        t = st.time_input(f"{label} – hora", value=value.time(), key=f"{key_prefix}_time")
    return datetime.combine(d, t).replace(tzinfo=value.tzinfo)


# =========================
# REPOSITORIOS AIRTABLE (LLAMADOS)
# =========================

class AirtableTable:
    def __init__(self, base_id: str, table: str, headers: dict):
        self.base_id = base_id
        self.table = table
        self.headers = headers

    @property
    def base_url(self) -> str:
        return f"https://api.airtable.com/v0/{self.base_id}/{self.table}"

    def list(self, params: dict):
        return airtable_request("GET", self.base_url, headers=self.headers, params=params)

    def create(self, fields: dict):
        payload = {"fields": fields}
        return airtable_request("POST", self.base_url, headers=self.headers, data=payload)


class LlamadosRepo:
    def __init__(self, base_id: str, table_name: str):
        self.tbl = AirtableTable(base_id, table_name, HEADERS_MAIN)

    def list_by_dni(self, dni: str, max_records=500):
        formula = f"{{dni}}='{dni}'"
        params = {
            "filterByFormula": formula,
            "pageSize": 100,
            "sort[0][field]": "fecha_hora",
            "sort[0][direction]": "desc",
        }
        resultados, offset = [], None
        while True:
            if offset:
                params["offset"] = offset
            data = self.tbl.list(params)
            resultados.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset or len(resultados) >= max_records:
                break
        return resultados[:max_records]

    def create_llamado(self, dni: str, nombre: str, fecha_iso: str, motivo: str, quien_realiza: str):
        fields = {
            "dni": dni,
            "nombre": nombre,
            "fecha_hora": fecha_iso,
            "motivo": motivo,
            "quien_realiza": quien_realiza,
        }
        return self.tbl.create(fields)


class TrabajadoresLookup:
    def __init__(self, base_id: str, table: str, dni_field: str, name_field: str, view: str = ""):
        self.enabled = bool(table)
        self.tbl = AirtableTable(base_id, table, HEADERS_EXT) if self.enabled else None
        self.dni_field = dni_field
        self.name_field = name_field
        self.view = (view or "").strip()

    def get_nombre_by_dni(self, dni: str) -> Optional[str]:
        if not self.enabled:
            return None
        formula = f"{{{self.dni_field}}}='{dni}'"
        params = {"filterByFormula": formula, "pageSize": 1}
        if self.view:
            params["view"] = self.view
        data = self.tbl.list(params)
        records = data.get("records", [])
        if not records:
            return None
        return records[0].get("fields", {}).get(self.name_field)

    def search_suggestions(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        if not self.enabled or not query:
            return []
        q = query.lower().replace("'", "\\'")
        formula = f"SEARCH('{q}', LOWER({{{self.dni_field}}}))"
        params = {
            "filterByFormula": formula,
            "pageSize": max(1, min(limit, 50)),
            "sort[0][field]": self.dni_field,
            "sort[0][direction]": "asc",
        }
        if self.view:
            params["view"] = self.view

        data = self.tbl.list(params)
        out = []
        for r in data.get("records", []):
            f = r.get("fields", {})
            out.append({
                "dni": str(f.get(self.dni_field, "")).strip(),
                "nombre": str(f.get(self.name_field, "")).strip() if self.name_field in f else "",
            })
        return out


llamados_repo = LlamadosRepo(BASE_ID, TBL_LLAMADOS)
lookup_repo = TrabajadoresLookup(EXT_BASE_ID or BASE_ID, EXT_TABLE, EXT_DNI_FIELD, EXT_NAME_FIELD, view=EXT_VIEW)


# =========================
# APP 1: LLAMADOS DE ATENCIÓN
# =========================

def app_llamados_atencion():
    st.title("Control de llamados de atención")
    st.caption("Busca por DNI en la tabla externa (cuentas), trae el nombre y registra llamados en Airtable 'llamados'.")

    st.markdown("### 1) Buscar trabajador por documento de identidad")

    dni_prefill = st.session_state.get("dni_input_value", "")
    col_inp, col_sel = st.columns([2, 2])

    with col_inp:
        dni_input = st.text_input(
            "Documento de identidad (DNI/NIE):",
            value=dni_prefill,
            key="dni_input_llamados",
            placeholder="Ej: 6305769X",
            max_chars=64,
        )
        dni_norm_typing = normalize_dni(dni_input)

    nombre_ext = st.session_state.get("nombre_ext_llamados", None)

    with col_sel:
        if lookup_repo.enabled and len(dni_norm_typing) >= 2:
            try:
                sugs = lookup_repo.search_suggestions(dni_norm_typing, limit=10)
                if sugs:
                    labels = [f"{s['dni']} — {s.get('nombre') or 'sin nombre'}" for s in sugs]
                    map_label_to_dni = {lbl: s["dni"] for lbl, s in zip(labels, sugs)}
                    map_dni_to_name = {s["dni"]: s.get("nombre", "") for s in sugs}

                    selected_label = st.radio("Coincidencias", options=labels, index=0, key="dni_choice_radio_llamados")

                    if st.button("Usar esta coincidencia", key="btn_usar_coinc_llamados"):
                        selected_dni = map_label_to_dni[selected_label]
                        selected_name = map_dni_to_name.get(selected_dni, "")
                        st.session_state["dni_llamados"] = selected_dni
                        st.session_state["dni_input_value"] = selected_dni
                        st.session_state["nombre_ext_llamados"] = selected_name or None
                        st.rerun()
                else:
                    st.caption("Sin coincidencias…")
            except Exception as e:
                log_exception_to_github(
                    e,
                    where="APP1:autocompletado_search_suggestions",
                    extra={"dni_hash": _hash_pii(dni_norm_typing)},
                )
                st.error("Autocompletado: error consultando Airtable. (log enviado)")
        else:
            st.caption("Escribe al menos 2 caracteres para ver coincidencias.")

    if st.button("Buscar", key="btn_buscar_llamados") and dni_norm_typing:
        st.session_state["dni_llamados"] = dni_norm_typing
        st.session_state["dni_input_value"] = dni_norm_typing
        if "nombre_ext_llamados" in st.session_state:
            del st.session_state["nombre_ext_llamados"]
        st.rerun()

    dni = st.session_state.get("dni_llamados")
    if not dni:
        st.info("No has seleccionado ningún DNI.")
        return

    lookup_error: Optional[str] = None
    if lookup_repo.enabled and not nombre_ext:
        try:
            nombre_ext = lookup_repo.get_nombre_by_dni(dni)
            st.session_state["nombre_ext_llamados"] = nombre_ext
        except Exception as e:
            lookup_error = str(e)
            log_exception_to_github(
                e,
                where="APP1:get_nombre_by_dni",
                extra={"dni_hash": _hash_pii(dni)},
            )

    st.markdown("---")
    st.markdown("### 2) Datos del trabajador (desde la tabla externa)")
    cols = st.columns(2)
    with cols[0]:
        st.write(f"**DNI seleccionado:** {dni}")
    with cols[1]:
        if lookup_error:
            st.error("Error consultando la tabla externa. (log enviado)")
        elif nombre_ext:
            st.success(f"Nombre: **{nombre_ext}**")
        else:
            st.warning("No se encontró nombre en la tabla externa. Puedes introducirlo manualmente abajo.")

    st.markdown("### 3) Historial de llamados (por DNI)")
    try:
        llamados = llamados_repo.list_by_dni(dni, max_records=500)
        if not llamados:
            st.info("Sin llamados registrados para este DNI.")
        else:
            rows = []
            for r in llamados:
                f = r.get("fields", {})
                rows.append({
                    "Fecha y hora": f.get("fecha_hora", ""),
                    "Motivo": f.get("motivo", ""),
                    "Quién realizó": f.get("quien_realiza", ""),
                    "Nombre (guardado)": f.get("nombre", ""),
                })
            st.dataframe(rows, use_container_width=True, hide_index=True)
    except Exception as e:
        log_exception_to_github(
            e,
            where="APP1:list_by_dni_historial",
            extra={"dni_hash": _hash_pii(dni)},
        )
        st.error("Error al cargar historial. (log enviado)")

    st.markdown("### 4) Registrar nuevo llamado")
    with st.form("form_nuevo_llamado", clear_on_submit=True):
        if nombre_ext:
            _ = st.text_input("Nombre del trabajador:", value=nombre_ext, disabled=True)
            nombre_guardar = nombre_ext
        else:
            nombre_guardar = st.text_input("Nombre del trabajador:", value="", placeholder="Escribe el nombre")

        now_local = datetime.now(tz.tzlocal())
        fecha_hora = ui_datetime_input("Fecha y hora", value=now_local, key_prefix="llamado_dt")
        motivo = st.text_area("Llamada de atención (motivo):", placeholder="Describe brevemente el motivo...", height=120)
        quien_realiza = st.text_input("Nombre de quien realiza el llamado:", placeholder="Nombre y/o cargo")

        submitted = st.form_submit_button("Guardar llamado")
        if submitted:
            if not (nombre_guardar or "").strip():
                st.warning("Por favor, introduce el nombre del trabajador.")
            elif not (motivo or "").strip() or not (quien_realiza or "").strip():
                st.warning("Por favor, completa *Motivo* y *Nombre de quien realiza*.")
            else:
                if fecha_hora.tzinfo is None:
                    fecha_hora = fecha_hora.replace(tzinfo=tz.tzlocal())
                fecha_iso = fecha_hora.isoformat()
                try:
                    _ = llamados_repo.create_llamado(
                        dni=dni,
                        nombre=(nombre_guardar or "").strip(),
                        fecha_iso=fecha_iso,
                        motivo=(motivo or "").strip(),
                        quien_realiza=(quien_realiza or "").strip(),
                    )
                    st.success("Llamado guardado correctamente.")
                    st.rerun()
                except Exception as e:
                    log_exception_to_github(
                        e,
                        where="APP1:create_llamado",
                        extra={"dni_hash": _hash_pii(dni)},
                    )
                    st.error("No se pudo guardar el llamado. (log enviado)")


# =========================
# APP 2: QUITAR HORAS
# =========================

HORAS_FIELD_TIPO = get_secret("HORAS_FIELD_TIPO", "Tipo")

TIPO_OPTIONS = [
    "Reasignación de pedidos",
    "Horas no trabajadas",
]

CORTE_DIA = int(get_secret("HORAS_CORTE_DIA", "20") or 20)
CORTE_HORA = int(get_secret("HORAS_CORTE_HORA", "12") or 12)
CORTE_MINUTO = int(get_secret("HORAS_CORTE_MINUTO", "0") or 0)


def _period_start_for_dt(dt: datetime) -> datetime:
    if dt is None or pd.isna(dt):
        return None  # type: ignore

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.tzlocal())

    y, m = dt.year, dt.month
    corte_mes = datetime(y, m, CORTE_DIA, CORTE_HORA, CORTE_MINUTO, tzinfo=dt.tzinfo)

    if dt < corte_mes:
        if m == 1:
            y2, m2 = y - 1, 12
        else:
            y2, m2 = y, m - 1
        return datetime(y2, m2, CORTE_DIA, CORTE_HORA, CORTE_MINUTO, tzinfo=dt.tzinfo)

    return corte_mes


def _period_key(start_dt: datetime) -> str:
    if start_dt is None:
        return ""
    return start_dt.strftime("%Y-%m-%d %H:%M")


def _period_label(start_dt: datetime) -> str:
    if start_dt is None:
        return "(sin fecha)"
    y, m = start_dt.year, start_dt.month
    if m == 12:
        end_dt = datetime(y + 1, 1, CORTE_DIA, CORTE_HORA, CORTE_MINUTO, tzinfo=start_dt.tzinfo)
    else:
        end_dt = datetime(y, m + 1, CORTE_DIA, CORTE_HORA, CORTE_MINUTO, tzinfo=start_dt.tzinfo)
    return f"Del {start_dt.strftime('%d/%m/%Y %H:%M')} al {end_dt.strftime('%d/%m/%Y %H:%M')}"


@st.cache_data(show_spinner=False)
def get_trabajadores_horas():
    try:
        table = Table(
            HORAS_EXT_API_KEY or HORAS_API_KEY,
            HORAS_EXT_BASE_ID or HORAS_BASE_ID,
            HORAS_TRABAJADORES_TABLE_NAME
        )
        records = table.all(view=HORAS_EXT_VIEW) if (HORAS_EXT_VIEW or "").strip() else table.all()
    except HTTPError as e:
        log_exception_to_github(
            e,
            where="APP2:get_trabajadores_horas_http",
            extra={"view": HORAS_EXT_VIEW or "", "table": HORAS_TRABAJADORES_TABLE_NAME},
        )
        st.error("Error leyendo tabla TRABAJADORES (cuentas). (log enviado)")
        try:
            st.code(f"{e.response.status_code}\n{e.response.text}")
        except Exception:
            pass
        return []
    except Exception as e:
        log_exception_to_github(
            e,
            where="APP2:get_trabajadores_horas",
            extra={"view": HORAS_EXT_VIEW or "", "table": HORAS_TRABAJADORES_TABLE_NAME},
        )
        st.error("Error leyendo tabla TRABAJADORES (cuentas). (log enviado)")
        return []

    trabajadores = []
    for r in records:
        f = r.get("fields", {})
        trabajadores.append({
            "record_id": r.get("id"),
            "Nombre": f.get("nombre", ""),
            "DNI": f.get("documentoDeIdentidad", ""),
        })
    return trabajadores


def buscar_trabajadores_por_dni_horas(dni: str):
    dni = (dni or "").strip().upper()
    if not dni:
        return []
    return [t for t in get_trabajadores_horas() if dni in str(t["DNI"]).upper()]


@st.cache_data(show_spinner=False)
def get_quitas_de_horas():
    try:
        table = Table(HORAS_API_KEY, HORAS_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME)
        records = table.all()
    except HTTPError as e:
        log_exception_to_github(
            e,
            where="APP2:get_quitas_de_horas_http",
            extra={"table": HORAS_QUITAR_HORAS_TABLE_NAME},
        )
        st.error("Error leyendo tabla QUITAR_HORAS_TRABAJADORES. (log enviado)")
        try:
            st.code(f"{e.response.status_code}\n{e.response.text}")
        except Exception:
            pass
        return pd.DataFrame()
    except Exception as e:
        log_exception_to_github(
            e,
            where="APP2:get_quitas_de_horas",
            extra={"table": HORAS_QUITAR_HORAS_TABLE_NAME},
        )
        st.error("Error leyendo tabla QUITAR_HORAS_TRABAJADORES. (log enviado)")
        return pd.DataFrame()

    rows = []
    for r in records:
        f = r.get("fields", {})
        rows.append({
            "record_id": r["id"],
            "Fecha_Registro": f.get("Fecha_Registro", ""),
            "Fecha_No_Trabajada": f.get(HORAS_FIELD_FECHA_NO_TRABAJADA, ""),
            "Tipo": f.get(HORAS_FIELD_TIPO, ""),
            "Trabajador_Nombre": f.get("Trabajador_Nombre", ""),
            "Trabajador_DNI": f.get("Trabajador_DNI", ""),
            "Horas_Quitadas": f.get("Horas_Quitadas", None),
            "Responsable": f.get("Responsable", ""),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["Fecha_Registro_dt"] = pd.to_datetime(df["Fecha_Registro"], errors="coerce")

        def _ensure_tz(x):
            if pd.isna(x) or x is None:
                return pd.NaT
            if getattr(x, "tzinfo", None) is None:
                return x.replace(tzinfo=tz.tzlocal())
            return x

        df["Fecha_Registro_dt"] = df["Fecha_Registro_dt"].apply(_ensure_tz)
        df["Period_Start_dt"] = df["Fecha_Registro_dt"].apply(lambda x: _period_start_for_dt(x) if not pd.isna(x) else pd.NaT)

        df["Period_Key"] = df["Period_Start_dt"].apply(lambda x: _period_key(x) if not pd.isna(x) else "")
        df["Period_Label"] = df["Period_Start_dt"].apply(lambda x: _period_label(x) if not pd.isna(x) else "")
    return df


def registrar_quita_horas(trabajador: Dict, horas: int, responsable: str, fecha_no_trabajada: date, tipo: str):
    if not fecha_no_trabajada:
        raise ValueError("La fecha no trabajada es obligatoria.")

    tipo = (tipo or "").strip()
    if tipo not in TIPO_OPTIONS:
        raise ValueError("Tipo inválido. Debe ser una de las opciones del desplegable.")

    table = Table(HORAS_API_KEY, HORAS_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME)

    fields = {
        "Trabajador_Nombre": trabajador["Nombre"],
        "Trabajador_DNI": trabajador["DNI"],
        "Horas_Quitadas": int(horas),
        "Responsable": (responsable or "").strip(),
        "Fecha_Registro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        HORAS_FIELD_FECHA_NO_TRABAJADA: fecha_no_trabajada.strftime("%Y-%m-%d"),
        HORAS_FIELD_TIPO: tipo,
    }
    table.create(fields)


def _render_resumen_y_detalle(df_in: pd.DataFrame, titulo: str):
    if df_in.empty:
        st.info("No hay registros para los filtros seleccionados.")
        return

    df_grouped = (
        df_in.groupby(["Tipo", "Trabajador_DNI", "Trabajador_Nombre"], as_index=False)
        .agg({"Horas_Quitadas": "sum", "Fecha_Registro_dt": "max"})
        .sort_values(["Horas_Quitadas", "Fecha_Registro_dt"], ascending=[False, False])
        .rename(columns={
            "Horas_Quitadas": "Horas_Totales_Quitadas",
            "Fecha_Registro_dt": "Ultima_Fecha_Registro",
        })
    )

    st.markdown(titulo)
    st.dataframe(df_grouped, use_container_width=True, hide_index=True)

    with st.expander("Ver registros individuales (detalle) — SOLO LECTURA", expanded=False):
        cols = [
            "Fecha_Registro_dt",
            "Fecha_No_Trabajada",
            "Tipo",
            "Trabajador_Nombre",
            "Trabajador_DNI",
            "Horas_Quitadas",
            "Responsable",
        ]
        cols = [c for c in cols if c in df_in.columns]
        df_det = df_in[cols].sort_values("Fecha_Registro_dt", ascending=False)
        st.dataframe(df_det, use_container_width=True, hide_index=True)


def app_quitar_horas():
    st.title("⏱️ Gestión de Horas Quitadas a Repartidores")

    if "trabajador_horas" not in st.session_state:
        st.session_state.trabajador_horas = None
    if "selection_locked_horas" not in st.session_state:
        st.session_state.selection_locked_horas = False

    st.subheader("👤 Responsable de la acción")
    responsable = st.text_input(
        "Nombre del responsable",
        placeholder="Ej: Juan José / RRHH / Supervisor",
        key="responsable_horas"
    ).strip()

    st.divider()
    st.subheader("1️⃣ Buscar trabajador")
    col1, col2 = st.columns([1.5, 2])

    with col1:
        dni_input = st.text_input("Escribe DNI", placeholder="Ej: 54398765A", key="dni_horas")

    with col2:
        try:
            resultados = buscar_trabajadores_por_dni_horas(dni_input) if dni_input else []
        except Exception as e:
            log_exception_to_github(
                e,
                where="APP2:buscar_trabajadores_por_dni_horas",
                extra={"dni_hash": _hash_pii(dni_input or "")},
            )
            resultados = []
            st.error("Error buscando trabajadores. (log enviado)")

        if resultados:
            opciones = [f"{t['Nombre']} — {t['DNI']}" for t in resultados]
            idx = st.radio(
                "Coincidencias encontradas",
                options=range(len(opciones)),
                format_func=lambda i: opciones[i],
                index=0,
                disabled=st.session_state.selection_locked_horas,
                key="radio_trabajador_horas"
            )

            cbtn1, cbtn2 = st.columns([1, 1])
            with cbtn1:
                if st.button("✅ Seleccionar trabajador", disabled=st.session_state.selection_locked_horas, key="btn_sel_trab_horas"):
                    st.session_state.trabajador_horas = resultados[idx]
                    st.session_state.selection_locked_horas = True
                    st.success(f"Seleccionado: **{resultados[idx]['Nombre']} — {resultados[idx]['DNI']}**")
            with cbtn2:
                if st.session_state.selection_locked_horas:
                    if st.button("🔄 Cambiar trabajador", key="btn_cambiar_trab_horas"):
                        st.session_state.trabajador_horas = None
                        st.session_state.selection_locked_horas = False
                        st.rerun()
        else:
            if dni_input:
                st.info("No hay coincidencias.")

    st.divider()
    st.subheader("2️⃣ Registrar horas quitadas")

    if not st.session_state.trabajador_horas:
        st.info("Selecciona un trabajador primero.")
        return

    st.caption("📅 La *Fecha no trabajada* es obligatoria y se guarda como **solo fecha** (sin hora).")

    with st.form("form_horas_quitar"):
        colh1, colh2, colh3, colh4 = st.columns([1, 1, 1.4, 1])
        with colh1:
            horas = st.selectbox("Horas a quitar", list(range(1, 10)), key="horas_a_quitar")
        with colh2:
            fecha_no_trabajada = st.date_input(
                "Fecha no trabajada (obligatoria)",
                value=date.today(),
                key="horas_fecha_no_trabajada_quitar"
            )
        with colh3:
            tipo = st.selectbox("Tipo", TIPO_OPTIONS, index=1, key="horas_tipo_quitar")
        with colh4:
            guardar = st.form_submit_button("💾 Guardar")

        if guardar:
            if not responsable:
                st.warning("Debes indicar el responsable antes de guardar.")
            elif not fecha_no_trabajada:
                st.warning("Debes indicar la fecha no trabajada.")
            else:
                try:
                    registrar_quita_horas(
                        st.session_state.trabajador_horas,
                        horas,
                        responsable,
                        fecha_no_trabajada,
                        tipo
                    )
                    st.success(f"Se registraron **{horas} horas** quitadas por **{responsable}**. Tipo: **{tipo}**")
                    get_quitas_de_horas.clear()
                    st.rerun()
                except Exception as e:
                    log_exception_to_github(
                        e,
                        where="APP2:registrar_quita_horas",
                        extra={
                            "dni_hash": _hash_pii(st.session_state.trabajador_horas.get("DNI", "")),
                            "horas": int(horas),
                            "tipo": tipo,
                        },
                    )
                    st.error("No se pudo guardar. (log enviado)")

    st.markdown("### 🔄 Corrección de horas (devolver)")

    known_tipo = st.session_state.get("horas_tipo_quitar", TIPO_OPTIONS[1])

    with st.form("form_devolver_horas"):
        colc1, colc2, colc3, colc4 = st.columns([1, 1, 1.4, 1])
        with colc1:
            horas_devolver = st.selectbox("Horas a devolver", list(range(1, 10)), key="select_horas_devolver")
        with colc2:
            fecha_no_trabajada_dev = st.date_input(
                "Fecha no trabajada (obligatoria)",
                value=date.today(),
                key="horas_fecha_no_trabajada_devolver"
            )
        with colc3:
            tipo_dev = st.selectbox(
                "Tipo",
                TIPO_OPTIONS,
                index=TIPO_OPTIONS.index(known_tipo) if known_tipo in TIPO_OPTIONS else 1,
                key="horas_tipo_devolver"
            )
        with colc4:
            corregir = st.form_submit_button("↩️ Devolver horas")

        if corregir:
            if not responsable:
                st.warning("Debes indicar el responsable antes de corregir.")
            elif not fecha_no_trabajada_dev:
                st.warning("Debes indicar la fecha no trabajada.")
            else:
                try:
                    registrar_quita_horas(
                        st.session_state.trabajador_horas,
                        -horas_devolver,
                        responsable,
                        fecha_no_trabajada_dev,
                        tipo_dev
                    )
                    st.success(f"Se han DEVUELTO **{horas_devolver} horas** por **{responsable}** (guardado como *-{horas_devolver}*). Tipo: **{tipo_dev}**")
                    get_quitas_de_horas.clear()
                    st.rerun()
                except Exception as e:
                    log_exception_to_github(
                        e,
                        where="APP2:devolver_horas",
                        extra={
                            "dni_hash": _hash_pii(st.session_state.trabajador_horas.get("DNI", "")),
                            "horas": int(horas_devolver),
                            "tipo": tipo_dev,
                        },
                    )
                    st.error("No se pudo devolver. (log enviado)")

    st.divider()
    st.subheader(f"3️⃣ Horas en curso (periodo {CORTE_DIA} → {CORTE_DIA})")

    df = get_quitas_de_horas()
    if df.empty:
        st.info("No hay registros aún.")
        return

    now_local = datetime.now(tz.tzlocal())
    periodo_start = _period_start_for_dt(now_local)
    periodo_key_actual = _period_key(periodo_start)
    periodo_label_actual = _period_label(periodo_start)

    f0, f1, f2 = st.columns([1.2, 2, 2])
    with f0:
        fil_tipo = st.selectbox("Filtrar por tipo", ["(todos)"] + TIPO_OPTIONS, index=0, key="filtro_tipo_horas_periodo_actual")
    with f1:
        fil_dni = st.text_input("Filtrar por DNI", key="filtro_dni_horas_periodo_actual")
    with f2:
        fil_nombre = st.text_input("Filtrar por nombre", key="filtro_nombre_horas_periodo_actual")

    df_act = df[df["Period_Key"] == periodo_key_actual].copy()
    if fil_tipo and fil_tipo != "(todos)":
        df_act["Tipo"] = df_act["Tipo"].fillna("").astype(str).str.strip()
        df_act = df_act[df_act["Tipo"] == fil_tipo]
    if fil_dni:
        df_act = df_act[df_act["Trabajador_DNI"].astype(str).str.contains(fil_dni, case=False, na=False)]
    if fil_nombre:
        df_act = df_act[df_act["Trabajador_Nombre"].astype(str).str.contains(fil_nombre, case=False, na=False)]

    if df_act.empty:
        st.info(f"No hay registros en el periodo actual ({periodo_label_actual}) con esos filtros.")
    else:
        _render_resumen_y_detalle(df_act, f"#### 📊 Resumen periodo actual — {periodo_label_actual}")

    st.divider()
    st.subheader("📚 Histórico de horas (periodos anteriores)")
    st.caption("🔒 Solo lectura: el histórico no se puede modificar desde el dashboard.")

    periodos = (
        df[["Period_Key", "Period_Label"]]
        .dropna()
        .drop_duplicates()
    )
    periodos = periodos[periodos["Period_Key"].astype(str).str.strip() != ""]
    periodos = periodos.sort_values("Period_Key", ascending=False)

    periodos_hist = periodos[periodos["Period_Key"] != periodo_key_actual].copy()

    if periodos_hist.empty:
        st.info("Todavía no hay periodos anteriores con registros.")
        return

    options = periodos_hist.to_dict("records")
    labels = [x["Period_Label"] for x in options]
    label_to_key = {x["Period_Label"]: x["Period_Key"] for x in options}

    label_sel = st.selectbox(
        "Selecciona periodo",
        options=labels,
        index=0,
        key="hist_periodo_selector_unico_horas"
    )
    periodo_sel_key = label_to_key.get(label_sel, "")

    st.caption(f"Periodo seleccionado: **{label_sel}**")

    h0, h1, h2 = st.columns([1.2, 2, 2])
    with h0:
        fil_tipo_h = st.selectbox("Filtrar por tipo (histórico)", ["(todos)"] + TIPO_OPTIONS, index=0, key="filtro_tipo_horas_hist")
    with h1:
        fil_dni_h = st.text_input("Filtrar por DNI (histórico)", key="filtro_dni_horas_hist")
    with h2:
        fil_nombre_h = st.text_input("Filtrar por nombre (histórico)", key="filtro_nombre_horas_hist")

    df_hist = df[df["Period_Key"] == periodo_sel_key].copy()
    if fil_tipo_h and fil_tipo_h != "(todos)":
        df_hist["Tipo"] = df_hist["Tipo"].fillna("").astype(str).str.strip()
        df_hist = df_hist[df_hist["Tipo"] == fil_tipo_h]
    if fil_dni_h:
        df_hist = df_hist[df_hist["Trabajador_DNI"].astype(str).str.contains(fil_dni_h, case=False, na=False)]
    if fil_nombre_h:
        df_hist = df_hist[df_hist["Trabajador_Nombre"].astype(str).str.contains(fil_nombre_h, case=False, na=False)]

    if df_hist.empty:
        st.info("No hay registros para ese periodo / filtros.")
    else:
        _render_resumen_y_detalle(df_hist, f"#### 📊 Resumen histórico — {label_sel}")


# =========================
# APP 3: REASIGNACIONES
# =========================

def _app3_ready() -> bool:
    return all([
        REASIG_SRC_API_KEY, REASIG_SRC_BASE_ID, REASIG_SRC_TABLE,
        REASIG_AIRTABLE_API_KEY, REASIG_AIRTABLE_BASE_ID, REASIG_TABLE_NAME,
    ])


def _normalize_view_value(v: str) -> str:
    return (v or "").strip()


def _cloudinary_ready() -> bool:
    return all([
        get_secret("CLOUDINARY_CLOUD_NAME"),
        get_secret("CLOUDINARY_API_KEY"),
        get_secret("CLOUDINARY_API_SECRET"),
    ])


def _cloudinary_config():
    cloudinary.config(
        cloud_name=get_secret("CLOUDINARY_CLOUD_NAME"),
        api_key=get_secret("CLOUDINARY_API_KEY"),
        api_secret=get_secret("CLOUDINARY_API_SECRET"),
        secure=True,
    )


def upload_to_cloudinary(file_bytes: bytes, filename: str, folder: str = "reasignaciones") -> str:
    _cloudinary_config()
    res = cloudinary.uploader.upload(
        file_bytes,
        folder=folder,
        resource_type="auto",
        use_filename=True,
        unique_filename=True,
    )
    return res["secure_url"]


@st.cache_data(show_spinner=False)
def get_riders_by_view_src() -> List[Dict[str, Any]]:
    api = Api(REASIG_SRC_API_KEY)
    table = api.base(REASIG_SRC_BASE_ID).table(REASIG_SRC_TABLE)

    view_val = _normalize_view_value(REASIG_SRC_VIEW)
    records = table.all(view=view_val) if view_val else table.all()

    out: List[Dict[str, Any]] = []
    for r in records:
        f = r.get("fields", {}) or {}
        rider_id = f.get(PLANTILLA_RIDER_ID_FIELD, "")
        nombre = f.get(PLANTILLA_NAME_FIELD, "")
        dni = f.get(PLANTILLA_DNI_FIELD, "")

        out.append({
            "record_id": r.get("id"),
            "Rider_ID": str(rider_id).strip(),
            "Nombre": str(nombre).strip(),
            "DNI": str(dni).strip(),
        })

    return [x for x in out if x.get("Rider_ID")]


def buscar_riders_por_rider_id(query: str) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    riders = get_riders_by_view_src()
    return [r for r in riders if q in str(r.get("Rider_ID", ""))]


def crear_reasignacion_record(fields: Dict[str, Any]) -> Dict[str, Any]:
    api = Api(REASIG_AIRTABLE_API_KEY)
    table = api.base(REASIG_AIRTABLE_BASE_ID).table(REASIG_TABLE_NAME)
    return table.create(fields)


def actualizar_record_imagen_url(record_id: str, image_url: str) -> Dict[str, Any]:
    api = Api(REASIG_AIRTABLE_API_KEY)
    table = api.base(REASIG_AIRTABLE_BASE_ID).table(REASIG_TABLE_NAME)
    return table.update(record_id, {REASIG_FIELD_IMAGEN: image_url})


@st.cache_data(show_spinner=False)
def get_reasignaciones_destino() -> pd.DataFrame:
    api = Api(REASIG_AIRTABLE_API_KEY)
    table = api.base(REASIG_AIRTABLE_BASE_ID).table(REASIG_TABLE_NAME)
    records = table.all()

    rows = []
    for r in records:
        f = r.get("fields", {}) or {}
        rows.append({
            "record_id": r.get("id"),
            "Rider_ID": f.get(REASIG_FIELD_RIDER_ID, ""),
            "Nombre": f.get(REASIG_FIELD_NOMBRE, ""),
            "DNI": f.get(REASIG_FIELD_DNI, ""),
            "Fecha_Reasignacion": f.get(REASIG_FIELD_FECHA, ""),
            "Motivo": f.get(REASIG_FIELD_MOTIVO, ""),
            "Responsable": f.get(REASIG_FIELD_RESP, ""),
            "Vehiculo": f.get(REASIG_FIELD_VEHICULO, ""),
            "Imagen": f.get(REASIG_FIELD_IMAGEN, ""),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["Fecha_dt"] = pd.to_datetime(df["Fecha_Reasignacion"], errors="coerce")
        df["Month_Key"] = df["Fecha_dt"].dt.strftime("%Y-%m")
    return df


def app_reasignaciones():
    st.title("🧩 Reasignaciones")
    st.caption("Origen: cuentas (vista) • Destino: tabla Reasignaciones • Imagen: se guarda como URL (Cloudinary si está configurado)")

    if not _app3_ready():
        _stop_with_log(
            "Faltan secrets de APP3. Revisa REASIG_SRC_* y REASIG_*.",
            where="APP3:missing_secrets",
            extra={
                "has_src_api_key": bool(REASIG_SRC_API_KEY),
                "has_src_base": bool(REASIG_SRC_BASE_ID),
                "src_table": REASIG_SRC_TABLE or "",
                "has_dst_api_key": bool(REASIG_AIRTABLE_API_KEY),
                "has_dst_base": bool(REASIG_AIRTABLE_BASE_ID),
                "dst_table": REASIG_TABLE_NAME or "",
            },
        )

    if "rider_sel" not in st.session_state:
        st.session_state.rider_sel = None
    if "rider_sel_locked" not in st.session_state:
        st.session_state.rider_sel_locked = False

    st.subheader("1️⃣ Buscar rider por Rider ID")
    c1, c2 = st.columns([1.3, 2])

    with c1:
        rider_id_input = st.text_input("Rider ID", placeholder="Ej: 4128301", key="reasig_rider_id_input").strip()

    with c2:
        try:
            resultados = buscar_riders_por_rider_id(rider_id_input) if rider_id_input else []
        except Exception as e:
            log_exception_to_github(
                e,
                where="APP3:buscar_riders_por_rider_id",
                extra={"rider_id_query": _safe_str(rider_id_input, 64)},
            )
            resultados = []
            st.error("Error buscando riders. (log enviado)")

        if resultados:
            opciones = [f"{r['Rider_ID']} — {r['Nombre']} — {r['DNI']}" for r in resultados]
            idx = st.radio(
                "Coincidencias",
                options=range(len(opciones)),
                format_func=lambda i: opciones[i],
                index=0,
                disabled=st.session_state.rider_sel_locked,
                key="reasig_radio_match",
            )

            b1, b2 = st.columns([1, 1])
            with b1:
                if st.button("✅ Seleccionar rider", disabled=st.session_state.rider_sel_locked, key="reasig_btn_select"):
                    st.session_state.rider_sel = resultados[idx]
                    st.session_state.rider_sel_locked = True
                    st.success(f"Seleccionado: **{resultados[idx]['Rider_ID']} — {resultados[idx]['Nombre']}**")
            with b2:
                if st.session_state.rider_sel_locked:
                    if st.button("🔄 Cambiar rider", key="reasig_btn_change"):
                        st.session_state.rider_sel = None
                        st.session_state.rider_sel_locked = False
                        st.rerun()
        else:
            if rider_id_input:
                st.info("No hay coincidencias con ese Rider ID.")

    if st.session_state.rider_sel:
        r = st.session_state.rider_sel
        st.markdown(
            f"""
**Rider seleccionado**
- **Rider ID:** {r['Rider_ID']}
- **Nombre:** {r['Nombre']}
- **DNI:** {r['DNI']}
"""
        )

    st.divider()
    st.subheader("2️⃣ Registrar reasignación")

    if not st.session_state.rider_sel:
        st.info("Selecciona un rider primero.")
    else:
        with st.form("form_reasig_create", clear_on_submit=True):
            colf1, colf2, colf3 = st.columns([1, 1, 1])
            with colf1:
                fecha_val = datetime.now(tz.tzlocal())
                fecha_dt = ui_datetime_input("Fecha de reasignación", fecha_val, key_prefix="reasig_dt")
            with colf2:
                responsable = st.text_input("Responsable", placeholder="Ej: Supervisor / RRHH", key="reasig_resp").strip()
            with colf3:
                vehiculo = st.selectbox("Vehículo", options=["moto", "bici", "patinete"], index=0, key="reasig_vehiculo")

            motivo_options = [
                "Reasignación por límite de km",
                "Otro (escribir motivo...)",
            ]
            motivo_sel = st.selectbox("Motivo", options=motivo_options, index=0, key="reasig_motivo_sel")

            motivo_custom = ""
            if motivo_sel == "Otro (escribir motivo...)":
                motivo_custom = st.text_area(
                    "Escribe el motivo",
                    placeholder="Describe el motivo…",
                    height=120,
                    key="reasig_motivo_custom",
                ).strip()

            motivo_final = motivo_sel if motivo_sel != "Otro (escribir motivo...)" else motivo_custom

            img = st.file_uploader(
                "Imagen (opcional) — si Cloudinary está configurado se sube y se guarda la URL en Airtable",
                type=["png", "jpg", "jpeg", "webp"],
                key="reasig_img",
            )

            guardar = st.form_submit_button("💾 Guardar reasignación")

            if guardar:
                if not responsable:
                    st.warning("Debes indicar el responsable.")
                elif not (motivo_final or "").strip():
                    st.warning("Debes indicar un motivo.")
                elif img is not None and not _cloudinary_ready():
                    st.error("Cloudinary NO está configurado (faltan CLOUDINARY_*). O quita la imagen o configura Cloudinary.")
                else:
                    rider = st.session_state.rider_sel
                    fecha_str = fecha_dt.strftime("%Y-%m-%d %H:%M:%S")

                    fields = {
                        REASIG_FIELD_RIDER_ID: str(rider.get("Rider_ID", "")).strip(),
                        REASIG_FIELD_NOMBRE: str(rider.get("Nombre", "")).strip(),
                        REASIG_FIELD_DNI: str(rider.get("DNI", "")).strip(),
                        REASIG_FIELD_FECHA: fecha_str,
                        REASIG_FIELD_MOTIVO: (motivo_final or "").strip(),
                        REASIG_FIELD_RESP: responsable,
                        REASIG_FIELD_VEHICULO: str(vehiculo).strip(),
                    }

                    try:
                        created = crear_reasignacion_record(fields)
                        rec_id = created.get("id")
                        if not rec_id:
                            raise RuntimeError(f"No se obtuvo record_id al crear registro: {created}")

                        if img is not None:
                            file_bytes = img.getvalue()
                            file_name = getattr(img, "name", "archivo")
                            url = upload_to_cloudinary(file_bytes, file_name)
                            _ = actualizar_record_imagen_url(rec_id, url)

                        st.success("✅ Reasignación guardada correctamente.")
                        get_reasignaciones_destino.clear()
                        st.rerun()
                    except Exception as e:
                        log_exception_to_github(
                            e,
                            where="APP3:guardar_reasignacion",
                            extra={
                                "rider_id": _safe_str(rider.get("Rider_ID", ""), 32),
                                "vehiculo": _safe_str(str(vehiculo), 32),
                                "has_img": bool(img is not None),
                            },
                        )
                        st.error("No se pudo guardar la reasignación. (log enviado)")

    st.divider()
    st.subheader("📊 Resumen por vehículo (conteo total)")

    try:
        df = get_reasignaciones_destino()
    except Exception as e:
        log_exception_to_github(e, where="APP3:get_reasignaciones_destino", extra={})
        st.error("Error cargando reasignaciones. (log enviado)")
        return

    if df.empty:
        st.info("Aún no hay registros en Reasignaciones.")
        return

    df["Vehiculo"] = df["Vehiculo"].fillna("").astype(str).str.strip().str.lower()
    conteo_total = df.groupby("Vehiculo", dropna=False).size().reset_index(name="Total")
    conteo_total = conteo_total[conteo_total["Vehiculo"] != ""].sort_values("Total", ascending=False)

    if conteo_total.empty:
        st.info("Aún no hay vehículos guardados.")
    else:
        for _, row in conteo_total.iterrows():
            st.write(f"**{row['Vehiculo']}:** {int(row['Total'])}")

    st.divider()
    st.subheader("📚 Histórico (detalle)")

    meses = sorted([m for m in df["Month_Key"].dropna().unique().tolist() if isinstance(m, str)], reverse=True)
    if not meses:
        st.info("No hay meses detectados aún.")
        return

    mes_sel = st.selectbox("Selecciona mes (YYYY-MM)", options=meses, index=0, key="reasig_mes_sel")
    st.caption(f"Mes seleccionado: **{mes_sel}**")

    f1, f2, f3, f4 = st.columns([1, 1, 1, 1])
    with f1:
        fil_rider = st.text_input("Filtrar por Rider ID", key="reasig_fil_rider")
    with f2:
        fil_dni = st.text_input("Filtrar por DNI", key="reasig_fil_dni")
    with f3:
        fil_resp = st.text_input("Filtrar por Responsable", key="reasig_fil_resp")
    with f4:
        fil_veh = st.selectbox("Filtrar por Vehículo", options=["(todos)", "moto", "bici", "patinete"], index=0, key="reasig_fil_vehiculo")

    dfm = df[df["Month_Key"] == mes_sel].copy()

    if fil_rider:
        dfm = dfm[dfm["Rider_ID"].astype(str).str.contains(fil_rider, case=False, na=False)]
    if fil_dni:
        dfm = dfm[dfm["DNI"].astype(str).str.contains(fil_dni, case=False, na=False)]
    if fil_resp:
        dfm = dfm[dfm["Responsable"].astype(str).str.contains(fil_resp, case=False, na=False)]
    if fil_veh and fil_veh != "(todos)":
        dfm["Vehiculo"] = dfm["Vehiculo"].fillna("").astype(str).str.lower().str.strip()
        dfm = dfm[dfm["Vehiculo"] == fil_veh]

    st.markdown("#### Detalle")
    if dfm.empty:
        st.info("No hay registros para ese mes / filtros.")
    else:
        cols_show = ["Fecha_Reasignacion", "Rider_ID", "Nombre", "DNI", "Vehiculo", "Motivo", "Responsable", "Imagen"]
        cols_show = [c for c in cols_show if c in dfm.columns]
        dfm = dfm.sort_values("Fecha_dt", ascending=False)

        df_show = dfm[cols_show].copy()
        if "Imagen" in df_show.columns:
            df_show["Imagen"] = df_show["Imagen"].fillna("").astype(str).str.strip()

        st.dataframe(
            df_show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Imagen": st.column_config.LinkColumn(
                    "Imagen",
                    help="Abrir",
                    display_text="ver imagen",
                    validate="^https?://.*",
                )
            } if "Imagen" in df_show.columns else None,
        )


# =========================
# MENÚ SUPERIOR (TABS)
# =========================

tab1, tab2, tab3 = st.tabs(["📣 Llamados de atención", "⏱️ Quitar horas", "🧩 Reasignaciones"])

with tab1:
    run_with_error_logging(
        where="TAB1:app_llamados_atencion",
        fn=app_llamados_atencion,
        extra={"tab": "llamados"},
    )

with tab2:
    run_with_error_logging(
        where="TAB2:app_quitar_horas",
        fn=app_quitar_horas,
        extra={"tab": "quitar_horas"},
    )

with tab3:
    run_with_error_logging(
        where="TAB3:app_reasignaciones",
        fn=app_reasignaciones,
        extra={"tab": "reasignaciones"},
    )
