import os
import json
import time as time_mod
from datetime import datetime, date, time as dtime
from dateutil import tz
from dateutil.relativedelta import relativedelta  # (no imprescindible ahora, pero ok)
from typing import Optional, List, Dict

import requests
import streamlit as st
import pandas as pd
from pyairtable import Table
from requests.exceptions import HTTPError

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


# ============================================================
# SECRETS APP 1 — LLAMADOS DE ATENCIÓN
# ============================================================

# API PRINCIPAL (llamados)
API_KEY = get_secret("AIRTABLE_API_KEY")
BASE_ID = get_secret("AIRTABLE_BASE_ID")
TBL_LLAMADOS = get_secret("AIRTABLE_TABLE_LLAMADOS", "llamados")

# API SECUNDARIA (plantilla app1)
EXT_API_KEY = get_secret("EXT_AIRTABLE_API_KEY")
EXT_BASE_ID = get_secret("EXT_AIRTABLE_BASE_ID", BASE_ID or "")
EXT_TABLE = get_secret("EXT_AIRTABLE_TABLE", "plantilla")
EXT_DNI_FIELD = get_secret("EXT_DNI_FIELD", "documentoDniONie")
EXT_NAME_FIELD = get_secret("EXT_NAME_FIELD", "nombre")

# ============================================================
# SECRETS APP 2 — QUITAR HORAS
# ============================================================

# API PRINCIPAL (quitar_horas_trabajadores)
HORAS_API_KEY = get_secret("HORAS_AIRTABLE_API_KEY")
HORAS_BASE_ID = get_secret("HORAS_AIRTABLE_BASE_ID")
HORAS_QUITAR_HORAS_TABLE_NAME = get_secret(
    "HORAS_QUITAR_HORAS_TABLE_NAME", "quitar_horas_trabajadores"
)

# API SECUNDARIA (plantilla app2)
HORAS_EXT_API_KEY = get_secret("HORAS_EXT_AIRTABLE_API_KEY")
HORAS_EXT_BASE_ID = get_secret("HORAS_EXT_AIRTABLE_BASE_ID", HORAS_BASE_ID or "")
HORAS_TRABAJADORES_TABLE_NAME = get_secret(
    "HORAS_TRABAJADORES_TABLE_NAME", "plantilla"
)

# ============================================================

if not (API_KEY and BASE_ID and TBL_LLAMADOS):
    st.error(
        "Faltan variables para APP1: AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_LLAMADOS."
    )
    st.stop()

if not (HORAS_API_KEY and HORAS_BASE_ID and HORAS_QUITAR_HORAS_TABLE_NAME):
    st.error(
        "Faltan variables para APP2: HORAS_AIRTABLE_API_KEY, HORAS_AIRTABLE_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME."
    )
    st.stop()

HEADERS_MAIN = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}
HEADERS_EXT = {
    "Authorization": f"Bearer {EXT_API_KEY or API_KEY}",
    "Content-Type": "application/json",
}

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
            raise RuntimeError(f"Error Airtable {resp.status_code}: {err}")
        except Exception:
            if attempt == max_retries:
                raise
            time_mod.sleep(1.2 * attempt)


def parse_iso(dt_str: str) -> Optional[datetime]:
    """Convierte ISO (con o sin Z) a datetime con tz local."""
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(dt_str)
        return dt.astimezone(tz.tzlocal())
    except Exception:
        return None


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
        return airtable_request(
            "GET", self.base_url, headers=self.headers, params=params
        )

    def create(self, fields: dict):
        payload = {"fields": fields}
        return airtable_request(
            "POST", self.base_url, headers=self.headers, data=payload
        )


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

    def list_all(self, max_records=5000):
        params = {"pageSize": 100}
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

    def create_llamado(
        self, dni: str, nombre: str, fecha_iso: str, motivo: str, quien_realiza: str
    ):
        fields = {
            "dni": dni,
            "nombre": nombre,
            "fecha_hora": fecha_iso,
            "motivo": motivo,
            "quien_realiza": quien_realiza,
        }
        return self.tbl.create(fields)


class TrabajadoresLookup:
    """Consulta la tabla externa por DNI y devuelve nombre y sugerencias (usa API secundaria de APP1)."""

    def __init__(self, base_id: str, table: str, dni_field: str, name_field: str):
        self.enabled = bool(table)
        self.tbl = AirtableTable(base_id, table, HEADERS_EXT) if self.enabled else None
        self.dni_field = dni_field
        self.name_field = name_field

    def get_nombre_by_dni(self, dni: str) -> Optional[str]:
        if not self.enabled:
            return None
        formula = f"{{{self.dni_field}}}='{dni}'"
        params = {"filterByFormula": formula, "pageSize": 1}
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
        data = self.tbl.list(params)
        out = []
        for r in data.get("records", []):
            f = r.get("fields", {})
            out.append(
                {
                    "dni": str(f.get(self.dni_field, "")).strip(),
                    "nombre": str(f.get(self.name_field, "")).strip()
                    if self.name_field in f
                    else "",
                }
            )
        return out


llamados_repo = LlamadosRepo(BASE_ID, TBL_LLAMADOS)
lookup_repo = TrabajadoresLookup(EXT_BASE_ID or BASE_ID, EXT_TABLE, EXT_DNI_FIELD, EXT_NAME_FIELD)


# =========================
# APP 1: LLAMADOS DE ATENCIÓN
# =========================


def app_llamados_atencion():
    st.title("Control de llamados de atención")
    st.caption(
        "Busca por DNI en la tabla externa, trae el nombre y registra llamados en Airtable 'llamados'."
    )

    with st.expander("Config (solo lectura)", expanded=False):
        def mask_value(value: str, visible: int = 4) -> str:
            if not value:
                return "—"
            if len(value) <= visible * 2:
                return "*" * len(value)
            return f"{value[:visible]}{'*' * (len(value) - visible * 2)}{value[-visible:]}"

        st.write("🔒 Configuración APP1:")
        st.write("Base Llamados:", mask_value(BASE_ID))
        st.write("Tabla Llamados:", TBL_LLAMADOS)
        st.write("API principal (llamados):", mask_value(API_KEY))
        st.write("API secundaria (plantilla):", mask_value(EXT_API_KEY or API_KEY))
        st.write("Lookup externo activo:", bool(lookup_repo.enabled))
        if lookup_repo.enabled:
            st.write("Base externa:", mask_value(EXT_BASE_ID))
            st.write("Tabla externa:", EXT_TABLE)
            st.write("Campo DNI externo:", EXT_DNI_FIELD)
            st.write("Campo nombre externo:", EXT_NAME_FIELD)

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
        selected_label = selected_dni = selected_name = None

        if lookup_repo.enabled and len(dni_norm_typing) >= 2:
            try:
                sugs = lookup_repo.search_suggestions(dni_norm_typing, limit=10)
                if sugs:
                    labels = [
                        f"{s['dni']} — {s.get('nombre') or 'sin nombre'}" for s in sugs
                    ]
                    map_label_to_dni = {lbl: s["dni"] for lbl, s in zip(labels, sugs)}
                    map_dni_to_name = {s["dni"]: s.get("nombre", "") for s in sugs}

                    selected_label = st.radio(
                        "Coincidencias",
                        options=labels,
                        index=0,
                        key="dni_choice_radio_llamados",
                    )

                    if selected_label:
                        selected_dni = map_label_to_dni[selected_label]
                        selected_name = map_dni_to_name.get(selected_dni, "")

                    if st.button(
                        "Usar esta coincidencia", key="btn_usar_coinc_llamados"
                    ):
                        st.session_state["dni_llamados"] = selected_dni
                        st.session_state["dni_input_value"] = selected_dni
                        st.session_state["nombre_ext_llamados"] = selected_name or None
                        st.rerun()
                else:
                    st.caption("Sin coincidencias…")
            except Exception as e:
                st.error(f"Autocompletado: {e}")
        else:
            st.caption("Escribe al menos 2 caracteres para ver coincidencias.")

    if st.button("Buscar", key="btn_buscar_llamados") and dni_norm_typing:
        st.session_state["dni_llamados"] = dni_norm_typing
        st.session_state["dni_input_value"] = dni_norm_typing
        if "nombre_ext_llamados" in st.session_state:
            del st.session_state["nombre_ext_llamados"]
        st.rerun()

    dni = st.session_state.get("dni_llamados")

    if dni:
        lookup_error: Optional[str] = None
        if lookup_repo.enabled and not nombre_ext:
            try:
                nombre_ext = lookup_repo.get_nombre_by_dni(dni)
                st.session_state["nombre_ext_llamados"] = nombre_ext
            except Exception as e:
                lookup_error = str(e)

        st.markdown("---")
        st.markdown("### 2) Datos del trabajador (desde la tabla externa)")
        cols = st.columns(2)
        with cols[0]:
            st.write(f"**DNI seleccionado:** {dni}")
        with cols[1]:
            if lookup_error:
                st.error(f"Error consultando la tabla externa: {lookup_error}")
            elif nombre_ext:
                st.success(f"Nombre: **{nombre_ext}**")
            else:
                st.warning(
                    "No se encontró nombre en la tabla externa. Puedes introducirlo manualmente abajo."
                )

        st.markdown("### 3) Historial de llamados (por DNI)")
        try:
            llamados = llamados_repo.list_by_dni(dni, max_records=500)
            if not llamados:
                st.info("Sin llamados registrados para este DNI.")
            else:
                rows = []
                for r in llamados:
                    f = r.get("fields", {})
                    rows.append(
                        {
                            "Fecha y hora": f.get("fecha_hora", ""),
                            "Motivo": f.get("motivo", ""),
                            "Quién realizó": f.get("quien_realiza", ""),
                            "Nombre (guardado)": f.get("nombre", ""),
                        }
                    )
                st.dataframe(rows, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Error al cargar historial: {e}")

        st.markdown("### 4) Registrar nuevo llamado")
        with st.form("form_nuevo_llamado", clear_on_submit=True):
            if nombre_ext:
                _ = st.text_input(
                    "Nombre del trabajador:", value=nombre_ext, disabled=True
                )
                nombre_guardar = nombre_ext
            else:
                nombre_guardar = st.text_input(
                    "Nombre del trabajador:",
                    value="",
                    placeholder="Escribe el nombre",
                )

            now_local = datetime.now(tz.tzlocal())
            fecha_hora = ui_datetime_input(
                "Fecha y hora", value=now_local, key_prefix="llamado_dt"
            )
            motivo = st.text_area(
                "Llamada de atención (motivo):",
                placeholder="Describe brevemente el motivo...",
                height=120,
            )
            quien_realiza = st.text_input(
                "Nombre de quien realiza el llamado:",
                placeholder="Nombre y/o cargo",
            )

            submitted = st.form_submit_button("Guardar llamado")
            if submitted:
                if not nombre_ext and not (nombre_guardar or "").strip():
                    st.warning("Por favor, introduce el nombre del trabajador.")
                elif not (motivo or "").strip() or not (quien_realiza or "").strip():
                    st.warning(
                        "Por favor, completa *Motivo* y *Nombre de quien realiza*."
                    )
                else:
                    if fecha_hora.tzinfo is None:
                        fecha_hora = fecha_hora.replace(tzinfo=tz.tzlocal())
                    fecha_iso = fecha_hora.isoformat()
                    try:
                        _ = llamados_repo.create_llamado(
                            dni=dni,
                            nombre=(nombre_guardar or "").strip()
                            if not nombre_ext
                            else nombre_ext,
                            fecha_iso=fecha_iso,
                            motivo=(motivo or "").strip(),
                            quien_realiza=(quien_realiza or "").strip(),
                        )
                        st.success("Llamado guardado correctamente.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"No se pudo guardar el llamado: {e}")
    else:
        st.info(
            "No has seleccionado ningún DNI. Puedes ver el ranking general a continuación 👇"
        )

    st.markdown("---")
    st.markdown("###  Ranking de trabajadores por número de llamados")

    colf1, colf2, colf3 = st.columns([1, 1, 1])
    with colf1:
        start_date: Optional[date] = st.date_input(
            "Desde (fecha)", value=None, key="rank_start_date"
        )
    with colf2:
        end_date: Optional[date] = st.date_input(
            "Hasta (fecha)", value=None, key="rank_end_date"
        )
    with colf3:
        top_n = st.number_input(
            "Top N", min_value=1, max_value=1000, value=50, step=1, key="rank_top_n"
        )

    start_dt = end_dt = None
    if start_date:
        start_dt = datetime.combine(start_date, dtime(0, 0)).replace(
            tzinfo=tz.tzlocal()
        )
    if end_date:
        end_dt = datetime.combine(end_date, dtime(23, 59, 59)).replace(
            tzinfo=tz.tzlocal()
        )

    try:
        all_records = llamados_repo.list_all(max_records=5000)
        agg: Dict[str, Dict] = {}

        for r in all_records:
            f = r.get("fields", {})
            dni_r = (f.get("dni") or "").strip()
            if not dni_r:
                continue
            fecha_iso = f.get("fecha_hora")
            dt_local = parse_iso(fecha_iso)

            if start_dt and (not dt_local or dt_local < start_dt):
                continue
            if end_dt and (not dt_local or dt_local > end_dt):
                continue

            item = agg.get(
                dni_r,
                {"dni": dni_r, "nombre": f.get("nombre", ""), "count": 0, "ultimo": None},
            )
            item["count"] += 1
            if not item["nombre"] and f.get("nombre"):
                item["nombre"] = f.get("nombre")
            if dt_local and (item["ultimo"] is None or dt_local > item["ultimo"]):
                item["ultimo"] = dt_local
            agg[dni_r] = item

        ranking = list(agg.values())
        ranking.sort(
            key=lambda x: (
                x["count"],
                x["ultimo"] or datetime.min.replace(tzinfo=tz.tzlocal()),
            ),
            reverse=True,
        )

        rows_rank = []
        for it in ranking[:top_n]:
            rows_rank.append(
                {
                    "DNI": it["dni"],
                    "Nombre": it.get("nombre", ""),
                    "Total llamados": it["count"],
                    "Último llamado": it["ultimo"].strftime("%Y-%m-%d %H:%M")
                    if it["ultimo"]
                    else "",
                }
            )

        if rows_rank:
            st.dataframe(rows_rank, use_container_width=True, hide_index=True)
        else:
            st.info("No hay datos para el rango seleccionado.")
    except Exception as e:
        st.error(f"Error al generar ranking: {e}")


# =========================
# APP 2: QUITAR HORAS
# =========================


@st.cache_data(show_spinner=False)
def get_trabajadores_horas():
    """Lee plantilla usando la API secundaria de APP2."""
    try:
        table = Table(
            HORAS_EXT_API_KEY or HORAS_API_KEY,
            HORAS_EXT_BASE_ID or HORAS_BASE_ID,
            HORAS_TRABAJADORES_TABLE_NAME,
        )
        records = table.all()
    except HTTPError as e:
        st.error("Error leyendo tabla TRABAJADORES (plantilla) para quitar horas.")
        st.code(f"{e.response.status_code}\n{e.response.text}")
        return []

    trabajadores = []
    for r in records:
        f = r.get("fields", {})
        trabajadores.append(
            {
                "record_id": r.get("id"),
                "Nombre": f.get("nombre", ""),
                "DNI": f.get("documentoDniONie", ""),
            }
        )
    return trabajadores


def buscar_trabajadores_por_dni_horas(dni: str):
    dni = (dni or "").strip().upper()
    if not dni:
        return []
    return [t for t in get_trabajadores_horas() if dni in str(t["DNI"]).upper()]


@st.cache_data(show_spinner=False)
def get_quitas_de_horas():
    """Lee quitar_horas_trabajadores usando la API principal de APP2."""
    try:
        table = Table(HORAS_API_KEY, HORAS_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME)
        records = table.all()
    except HTTPError as e:
        st.error("Error leyendo tabla QUITAR_HORAS_TRABAJADORES.")
        st.code(f"{e.response.status_code}\n{e.response.text}")
        return pd.DataFrame()

    rows = []
    for r in records:
        f = r.get("fields", {})
        rows.append(
            {
                "record_id": r["id"],
                "Fecha_Registro": f.get("Fecha_Registro", ""),
                "Trabajador_Nombre": f.get("Trabajador_Nombre", ""),
                "Trabajador_DNI": f.get("Trabajador_DNI", ""),
                "Horas_Quitadas": f.get("Horas_Quitadas", None),
                "Responsable": f.get("Responsable", ""),  # ✅ NUEVO
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df["Fecha_Registro_dt"] = pd.to_datetime(df["Fecha_Registro"], errors="coerce")
        df["Month_Key"] = df["Fecha_Registro_dt"].dt.strftime("%Y-%m")
    return df


def registrar_quita_horas(trabajador: Dict, horas: int, responsable: str):
    """Escribe en quitar_horas_trabajadores usando la API principal de APP2."""
    table = Table(HORAS_API_KEY, HORAS_BASE_ID, HORAS_QUITAR_HORAS_TABLE_NAME)
    fields = {
        "Trabajador_Nombre": trabajador["Nombre"],
        "Trabajador_DNI": trabajador["DNI"],
        "Horas_Quitadas": horas,
        "Responsable": (responsable or "").strip(),  # ✅ NUEVO
        "Fecha_Registro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    table.create(fields)


def _render_resumen_y_detalle(df_in: pd.DataFrame, titulo: str):
    if df_in.empty:
        st.info("No hay registros para los filtros seleccionados.")
        return

    df_grouped = (
        df_in.groupby(["Trabajador_DNI", "Trabajador_Nombre"], as_index=False)
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
        # Mostrar columnas ordenadas, incluyendo Responsable
        cols = ["Fecha_Registro_dt", "Trabajador_Nombre", "Trabajador_DNI", "Horas_Quitadas", "Responsable"]
        cols = [c for c in cols if c in df_in.columns]
        df_det = df_in[cols].sort_values("Fecha_Registro_dt", ascending=False)
        st.dataframe(df_det, use_container_width=True, hide_index=True)


def app_quitar_horas():
    st.title("⏱️ Gestión de Horas Quitadas a Repartidores")
    st.markdown(
        """
### Funcionalidades:
1. Buscar trabajador por DNI  
2. Seleccionar cuántas horas quitar (1 a 9)  
3. Ver resumen del mes actual + histórico con selector de meses  
"""
    )

    if "trabajador_horas" not in st.session_state:
        st.session_state.trabajador_horas = None
    if "selection_locked_horas" not in st.session_state:
        st.session_state.selection_locked_horas = False

    # ✅ NUEVO: Responsable obligatorio
    st.subheader("👤 Responsable de la acción")
    responsable = st.text_input(
        "Nombre del responsable",
        placeholder="Ej: Juan José / RRHH / Supervisor",
        key="responsable_horas",
    ).strip()

    st.divider()

    st.subheader("1️⃣ Buscar trabajador")
    col1, col2 = st.columns([1.5, 2])

    with col1:
        dni_input = st.text_input("Escribe DNI", placeholder="Ej: 54398765A", key="dni_horas")

    with col2:
        resultados = buscar_trabajadores_por_dni_horas(dni_input) if dni_input else []

        if resultados:
            opciones = [f"{t['Nombre']} — {t['DNI']}" for t in resultados]

            default_index = 0
            if st.session_state.trabajador_horas:
                for i, t in enumerate(resultados):
                    if (
                        t["Nombre"] == st.session_state.trabajador_horas["Nombre"]
                        and t["DNI"] == st.session_state.trabajador_horas["DNI"]
                    ):
                        default_index = i
                        break

            idx = st.radio(
                "Coincidencias encontradas",
                options=range(len(opciones)),
                format_func=lambda i: opciones[i],
                index=default_index,
                disabled=st.session_state.selection_locked_horas,
                key="radio_trabajador_horas",
            )

            col_btn1, col_btn2 = st.columns([1, 1])
            with col_btn1:
                if st.button("✅ Seleccionar trabajador", disabled=st.session_state.selection_locked_horas, key="btn_sel_trab_horas"):
                    st.session_state.trabajador_horas = resultados[idx]
                    st.session_state.selection_locked_horas = True
                    st.success(f"Seleccionado: **{resultados[idx]['Nombre']} — {resultados[idx]['DNI']}**")
            with col_btn2:
                if st.session_state.selection_locked_horas:
                    if st.button("🔄 Cambiar trabajador", key="btn_cambiar_trab_horas"):
                        st.session_state.trabajador_horas = None
                        st.session_state.selection_locked_horas = False
                        st.rerun()
        else:
            if dni_input:
                st.info("No hay coincidencias.")

    if st.session_state.trabajador_horas:
        t = st.session_state.trabajador_horas
        st.markdown(f"### Trabajador seleccionado:\n**Nombre:** {t['Nombre']}  \n**DNI:** {t['DNI']}")

    st.divider()

    st.subheader("2️⃣ Registrar horas quitadas")

    if not st.session_state.trabajador_horas:
        st.info("Selecciona un trabajador primero.")
    else:
        with st.form("form_horas_quitar"):
            colh1, colh2 = st.columns([1, 1])
            with colh1:
                horas = st.selectbox("Horas a quitar", list(range(1, 10)))
            with colh2:
                guardar = st.form_submit_button("💾 Guardar")

            if guardar:
                if not responsable:
                    st.warning("Debes indicar el responsable antes de guardar.")
                else:
                    registrar_quita_horas(st.session_state.trabajador_horas, horas, responsable)
                    st.success(f"Se registraron **{horas} horas** quitadas por **{responsable}**.")
                    get_quitas_de_horas.clear()

    st.markdown("### 🔄 Corrección de horas (devolver)")

    if not st.session_state.trabajador_horas:
        st.info("Selecciona un trabajador primero para poder corregir horas.")
    else:
        with st.form("form_devolver_horas"):
            colc1, colc2 = st.columns([1, 1])
            with colc1:
                horas_devolver = st.selectbox("Horas a devolver", list(range(1, 10)), key="select_horas_devolver")
            with colc2:
                corregir = st.form_submit_button("↩️ Devolver horas")

            if corregir:
                if not responsable:
                    st.warning("Debes indicar el responsable antes de corregir.")
                else:
                    registrar_quita_horas(st.session_state.trabajador_horas, -horas_devolver, responsable)
                    st.success(f"Se han DEVUELTO **{horas_devolver} horas** por **{responsable}** (guardado como *-{horas_devolver}*).")
                    get_quitas_de_horas.clear()
                    st.rerun()

    st.divider()

    # =========================
    # 3) MES ACTUAL (EN CURSO)
    # =========================
    st.subheader("3️⃣ Horas en curso (mes actual)")

    df = get_quitas_de_horas()
    if df.empty:
        st.info("No hay registros aún.")
        return

    now_local = datetime.now(tz.tzlocal())
    mes_actual = now_local.strftime("%Y-%m")

    c1, c2 = st.columns([2, 2])
    with c1:
        f_dni = st.text_input("Filtrar por DNI", key="filtro_dni_horas_mes_actual")
    with c2:
        f_nombre = st.text_input("Filtrar por nombre", key="filtro_nombre_horas_mes_actual")

    df_mes = df[df["Month_Key"] == mes_actual].copy()
    if f_dni:
        df_mes = df_mes[df_mes["Trabajador_DNI"].str.contains(f_dni, case=False, na=False)]
    if f_nombre:
        df_mes = df_mes[df_mes["Trabajador_Nombre"].str.contains(f_nombre, case=False, na=False)]

    if df_mes.empty:
        st.info(f"No hay registros para el mes actual ({mes_actual}).")
    else:
        _render_resumen_y_detalle(df_mes, f"#### 📊 Resumen mes actual ({mes_actual})")

    st.divider()

    # =========================
    # 4) HISTÓRICO (SELECTOR MES)
    # =========================
    st.subheader("📚 Histórico de horas (meses anteriores)")
    st.caption("🔒 Solo lectura: el histórico no se puede modificar desde el dashboard.")

    meses_disponibles = sorted(
        [m for m in df["Month_Key"].dropna().unique().tolist() if isinstance(m, str)],
        reverse=True
    )
    meses_disponibles = [m for m in meses_disponibles if m != mes_actual]

    if not meses_disponibles:
        st.info("Todavía no hay meses anteriores con registros.")
        return

    mes_sel = st.selectbox(
        "Selecciona mes (YYYY-MM)",
        options=meses_disponibles,
        index=0,
        key="hist_mes_selector_unico"
    )
    st.caption(f"Mes seleccionado: **{mes_sel}**")

    c3, c4 = st.columns([2, 2])
    with c3:
        f_dni_h = st.text_input("Filtrar por DNI (histórico)", key="filtro_dni_horas_hist")
    with c4:
        f_nombre_h = st.text_input("Filtrar por nombre (histórico)", key="filtro_nombre_horas_hist")

    df_hist = df[df["Month_Key"] == mes_sel].copy()
    if f_dni_h:
        df_hist = df_hist[df_hist["Trabajador_DNI"].str.contains(f_dni_h, case=False, na=False)]
    if f_nombre_h:
        df_hist = df_hist[df_hist["Trabajador_Nombre"].str.contains(f_nombre_h, case=False, na=False)]

    if df_hist.empty:
        st.info("No hay registros para ese mes / filtros.")
    else:
        _render_resumen_y_detalle(df_hist, f"#### 📊 Resumen histórico ({mes_sel})")


# =========================
# MENÚ SUPERIOR (TABS)
# =========================

tab1, tab2 = st.tabs(["📣 Llamados de atención", "⏱️ Quitar horas"])

with tab1:
    app_llamados_atencion()

with tab2:
    app_quitar_horas()