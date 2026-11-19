"""Capa de acceso a datos unificada.

Backend por defecto: Supabase (PostgREST sobre HTTPS -> sin DLLs nativas,
ideal para PyInstaller en Windows). Si no hay credenciales, usa SQLite local.

Variables de entorno (.env):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=eyJ...            # service_role recomendada (o anon con RLS)
    DB_BACKEND=auto                 # auto | supabase | sqlite
    DATABASE_PATH=data/universidad_horarios.db
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# En PyInstaller, los recursos (templates, data, assets) se extraen a _MEIPASS.
# La BD escribible se coloca junto al ejecutable (o en el directorio del usuario).
EMPACETADO = getattr(sys, "frozen", False)
RECURSOS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
ROOT = RECURSOS if EMPACETADO else Path(__file__).resolve().parent.parent

if EMPACETADO:
    base_escritura = Path(sys.executable).resolve().parent
    if os.name == "nt":
        base_escritura = Path(os.environ.get("APPDATA", base_escritura)) / "UNESR_Horarios"
    else:
        base_escritura = Path(os.path.expanduser("~/.local/share/UNESR_Horarios"))
    base_escritura.mkdir(parents=True, exist_ok=True)
    DATA_DIR = base_escritura / "data"
else:
    DATA_DIR = ROOT / "data"

DB_PATH = Path(os.environ.get("DATABASE_PATH", DATA_DIR / "universidad_horarios.db"))
if not DB_PATH.is_absolute():
    DB_PATH = (base_escritura if EMPACETADO else ROOT) / DB_PATH

DIAS_ORDEN = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
FRANJAS = [
    ("07:40", "09:20"), ("09:20", "10:50"), ("10:50", "12:10"),
    ("12:30", "13:50"), ("14:10", "15:30"), ("15:40", "17:00"), ("17:10", "18:30"),
]
CAPACIDAD_DEFECTO = 45
# Inventario oficial: numeración global consecutiva por piso (37 salones) más
# 2 satelitales y 2 virtuales. Solo estos 41 ambientes se muestran y se pueden
# asignar; cualquier otro código que aparezca en una importación se ignora.
AMBIENTES_OFICIALES: Tuple[str, ...] = tuple(
    [f"P1-{n:02d}" for n in range(1, 9)]
    + [f"P2-{n:02d}" for n in range(9, 23)]
    + [f"P3-{n:02d}" for n in range(23, 28)]
    + [f"P4-{n:02d}" for n in range(28, 38)]
    + ["SATELITAL-01", "SATELITAL-02", "VIRTUAL-01", "VIRTUAL-02"]
)
AMBIENTES_ESPECIALES = ["SATELITAL-01", "SATELITAL-02", "VIRTUAL-01", "VIRTUAL-02"]

_local = threading.local()


def _env() -> Dict[str, str]:
    """Carga .env simple sin dependencias externas.

    En el ejecutable se busca también junto al .exe y en la carpeta de datos
    del usuario (APPDATA en Windows), para no tener que incrustar credenciales
    dentro del bundle. Las variables de entorno reales tienen prioridad.
    """
    rutas = [ROOT / ".env"]
    if EMPACETADO:
        rutas += [Path(sys.executable).resolve().parent / ".env",
                  base_escritura / ".env"]
    result: Dict[str, str] = {}
    for path in rutas:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def configuracion() -> Dict[str, str]:
    cfg = _env()
    for key in ("SUPABASE_URL", "SUPABASE_KEY", "DB_BACKEND", "DATABASE_PATH", "FLASK_SECRET_KEY"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def backend_activo() -> str:
    cfg = configuracion()
    mode = cfg.get("DB_BACKEND", "auto").lower()
    if mode == "sqlite":
        return "sqlite"
    if cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_KEY"):
        return "supabase" if mode in ("auto", "supabase") else "sqlite"
    return "sqlite"


class DatabaseError(RuntimeError):
    pass


# ─────────────────────────── SQLite ───────────────────────────

def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _sqlite_query(sql: str, params: Sequence = ()) -> List[Dict]:
    conn = _sqlite_conn()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def _sqlite_execute(sql: str, params: Sequence = ()) -> int:
    conn = _sqlite_conn()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid if cur.lastrowid is not None else cur.rowcount
    finally:
        conn.close()


def _sqlite_executescript(sql: str) -> None:
    conn = _sqlite_conn()
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────── Supabase / PostgREST ───────────────────────────

def _rest(sql_or_table: str, method: str = "GET", params: Optional[Dict] = None,
          body: Optional[Any] = None, extra_headers: Optional[Dict] = None,
          return_headers: bool = False) -> Any:
    import httpx

    cfg = configuracion()
    url = cfg.get("SUPABASE_URL", "").rstrip("/")
    key = cfg.get("SUPABASE_KEY", "")
    if not url or not key:
        raise DatabaseError("Faltan SUPABASE_URL/SUPABASE_KEY en .env")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if extra_headers:
        headers.update(extra_headers)
    endpoint = f"{url}/rest/v1/{sql_or_table}"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(method, endpoint, headers=headers, params=params, json=body)
            if resp.status_code >= 400:
                raise DatabaseError(f"Supabase {resp.status_code}: {resp.text[:400]}")
            datos = resp.json() if resp.content else None
            return (datos, dict(resp.headers)) if return_headers else datos
    except DatabaseError:
        raise
    except Exception as exc:  # pragma: no cover - red
        raise DatabaseError(f"No se pudo conectar con Supabase: {exc}") from exc


def _parse_where(sql: str) -> Tuple[str, List[Tuple[str, str, Any]]]:
    """Traduce un WHERE simple de SQLite a filtros PostgREST.

    Soporta: col = ?, col != ?, col IN (?,?), AND, ORDER BY y LIMIT.
    """
    text = " ".join(sql.split())
    where: List[Tuple[str, str, Any]] = []
    if " WHERE " in text:
        text, rest = text.split(" WHERE ", 1)
        rest = rest.split(" ORDER BY ")[0].split(" LIMIT ")[0]
        for part in rest.split(" AND "):
            part = part.strip()
            if " != ?" in part:
                col = part.split("!=")[0].strip()
                where.append((col, "neq", None))
            elif " IS NOT NULL" in part.upper():
                where.append((part.upper().replace(" IS NOT NULL", ""), "not.is", None))
            elif " LIKE ?" in part.upper():
                where.append((part.upper().replace(" LIKE ?", ""), "like", None))
            elif " = ?" in part:
                where.append((part.split("=")[0].strip(), "eq", None))
    return text, where


def _table_from_select(sql: str) -> str:
    text = " ".join(sql.split())
    after = text.split(" FROM ", 1)[1] if " FROM " in text else text.split("FROM", 1)[-1]
    return after.strip().split()[0].strip('"')


# ─────────────────────────── API pública ───────────────────────────

# Caché de Supabase -> SQLite en memoria. Las consultas con JOIN no se pueden
# traducir a PostgREST; en vez de caer a la BD local (datos desactualizados),
# replicamos las tablas remotas a una BD efímera y ejecutamos el SQL original.
_mem_cache: Dict[str, Any] = {"at": 0.0, "conn": None}
TABLAS_REMOTAS = ["facultades", "carreras", "salones", "profesores",
                  "disponibilidades", "planes_estudio", "materias",
                  "carga_academica", "asignaciones_horario", "configuracion"]


def _supabase_mem_conn() -> sqlite3.Connection:
    import re
    import time

    ahora = time.monotonic()
    if _mem_cache["conn"] is not None and ahora - _mem_cache["at"] < 5.0:
        return _mem_cache["conn"]

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for tabla in TABLAS_REMOTAS:
        try:
            filas = _rest(tabla, "GET", {"select": "*"}) or []
        except DatabaseError:
            continue
        if not filas:
            continue
        cols = list(filas[0].keys())
        tipos = {}
        for col in cols:
            for f in filas:
                v = f.get(col)
                if v is None:
                    continue
                tipos[col] = "INTEGER" if isinstance(v, (int, bool)) else "REAL" if isinstance(v, float) else "TEXT"
                break
            else:
                tipos[col] = "TEXT"
        ddl = ", ".join(f'"{c}" {tipos[c]}' for c in cols)
        conn.execute(f'CREATE TABLE "{tabla}" ({ddl})')
        marcadores = ",".join("?" for _ in cols)
        conn.executemany(
            f'INSERT INTO "{tabla}" ({",".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({marcadores})',
            [tuple(f.get(c) for c in cols) for f in filas],
        )
    conn.commit()
    _mem_cache.update(at=ahora, conn=conn)
    return conn


def query(sql: str, params: Sequence = ()) -> List[Dict[str, Any]]:
    """SELECT. Consultas simples -> PostgREST; JOINs -> caché SQLite de Supabase."""
    if backend_activo() == "sqlite":
        return _sqlite_query(sql, params)
    try:
        return _rest_query(sql, params)
    except DatabaseError:
        conn = _supabase_mem_conn()
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def _rest_query(sql: str, params: Sequence) -> List[Dict]:
    """Traduce SELECT simples a PostgREST.

    PostgREST no interpreta SQL: `select` solo admite nombres de columna. Por eso
    `AS` y `DISTINCT` se normalizan aquí (y se reaplican en Python), mientras que
    JOINs, subconsultas, funciones y WHERE no traducibles lanzan
    ``DatabaseError`` para que ``query()`` los ejecute en la caché SQLite remota.
    Nunca se envía un fragmento de SQL tal cual: eso producía los 42703.
    """
    text = " ".join(sql.split())
    if not re.match(r"^SELECT\s", text, re.I) or " FROM " not in text:
        raise DatabaseError(f"SELECT no traducible a PostgREST: {sql[:120]}")

    selects = text.split(" FROM ", 1)[0][6:].strip()
    table = _table_from_select(text)

    # COUNT(*) se resuelve con Prefer: count=exact (Content-Range), no como columna.
    mcount = re.fullmatch(r"COUNT\s*\(\s*\*\s*\)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
                          selects, re.I)
    if mcount:
        if " JOIN " in text or " WHERE " in text:
            # De lo contrario se contaría la tabla completa, ignorando filtros.
            raise DatabaseError("COUNT(*) con JOIN/WHERE -> caché SQLite")
        alias = mcount.group(1) or "n"
        _, hdrs = _rest(table, "GET", {"select": "id", "limit": 1},
                        extra_headers={"Prefer": "count=exact"}, return_headers=True)
        total = 0
        rango = hdrs.get("content-range", "")
        if "/" in rango:
            try:
                total = int(rango.rsplit("/", 1)[1])
            except ValueError:
                pass
        return [{alias: total}]

    # --- SELECT: solo columnas, con DISTINCT y AS reaplicados en Python ---
    cols: List[str] = []
    alias_map: Dict[str, str] = {}
    distinct = False
    items = ["*"] if selects == "*" else [c.strip() for c in selects.split(",")]
    for item in items:
        m = re.fullmatch(r"DISTINCT\s+([A-Za-z_]\w*)", item, re.I)
        if m:
            distinct = True
            cols.append(m.group(1))
            continue
        m = re.fullmatch(r"([A-Za-z_]\w*)\s+AS\s+([A-Za-z_]\w*)", item, re.I)
        if m:
            cols.append(m.group(1))
            alias_map[m.group(2)] = m.group(1)
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", item):
            cols.append(item)
            continue
        raise DatabaseError(f"SELECT no traducible a PostgREST: {item}")

    # --- WHERE: traducir TODAS las condiciones; nunca descartar una ---
    params_list = list(params)
    filters: List[str] = []
    cond = ""
    if " WHERE " in text:
        cond = text.split(" WHERE ", 1)[1].split(" ORDER BY ")[0].split(" LIMIT ")[0]
    op_map = {"=": "eq", "!=": "neq", "<>": "neq", ">": "gt", "<": "lt", ">=": "gte", "<=": "lte"}
    for raw in ([c.strip() for c in cond.split(" AND ")] if cond.strip() else []):
        if not raw:
            continue
        m = re.fullmatch(r"(\w+)\s*(>=|<=|!=|<>|=|>|<)\s*\?", raw)
        if m and params_list:
            filters.append(f"{m.group(1)}={op_map[m.group(2)]}.{_rest_val(params_list.pop(0))}")
            continue
        m = re.fullmatch(r"(\w+)\s+IN\s*\(\s*(\?(?:\s*,\s*\?)*)\s*\)", raw, re.I)
        if m and params_list and raw.count("?") <= len(params_list):
            n = raw.count("?")
            vals = params_list[:n]
            del params_list[:n]
            filters.append(f"{m.group(1)}=in.({','.join(_rest_val(v) for v in vals)})")
            continue
        m = re.fullmatch(r"(\w+)\s+IS\s+NOT\s+NULL", raw, re.I)
        if m:
            filters.append(f"{m.group(1)}=not.is.null")
            continue
        m = re.fullmatch(r"(\w+)\s*(!=|<>|=)\s*'([^']*)'", raw)
        if m:
            filters.append(f"{m.group(1)}={op_map[m.group(2)]}.{m.group(3)}")
            continue
        m = re.fullmatch(r"(\w+)\s+LIKE\s+'([^']*)'", raw, re.I)
        if m:
            filters.append(f"{m.group(1)}=like.{m.group(2)}")
            continue
        raise DatabaseError(f"WHERE no traducible a PostgREST: {raw}")

    if params_list:
        raise DatabaseError("Parámetros WHERE sin consumir -> caché SQLite")

    q: Dict[str, Any] = {"select": ",".join(cols)}
    for f in filters:
        key, val = f.split("=", 1)
        if key in q:
            q[key] = q[key] + "," + val
        else:
            q[key] = val

    if " ORDER BY " in text:
        orden: List[str] = []
        for parte in text.split(" ORDER BY ", 1)[1].split(" LIMIT ")[0].split(","):
            m = re.fullmatch(r"\s*([A-Za-z_]\w*)(?:\s+(ASC|DESC))?\s*", parte, re.I)
            if not m:
                raise DatabaseError(f"ORDER BY no traducible a PostgREST: {parte}")
            orden.append(f"{m.group(1)}.{(m.group(2) or 'ASC').lower()}")
        q["order"] = ",".join(orden)
    if " LIMIT " in text:
        lim = text.split(" LIMIT ", 1)[1].split()[0]
        if not lim.isdigit():
            raise DatabaseError(f"LIMIT no traducible a PostgREST: {lim}")
        q["limit"] = int(lim)

    filas = _rest(table, "GET", q) or []
    if distinct and cols != ["*"]:
        vistos: set = set()
        unicas: List[Dict] = []
        for fila in filas:
            clave = tuple(str(fila.get(c)) for c in cols)
            if clave not in vistos:
                vistos.add(clave)
                unicas.append(fila)
        filas = unicas
    for alias, original in alias_map.items():
        for fila in filas:
            if alias not in fila and original in fila:
                fila[alias] = fila[original] if original in cols else fila.pop(original)
    return filas


def _rest_val(v: Any) -> str:
    """Valor literal para filtros PostgREST.

    NO se envuelve entre comillas dobles: este PostgREST interpreta las comas
    como parte del valor y las comillas Like literalmente (un nombre como
    'Ética, Valores y Autobiografía' deja de encontrarse). httpx ya codifica
    los parámetros en la URL.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _invalidar_cache() -> None:
    _mem_cache.update(at=0.0, conn=None)


def execute(sql: str, params: Sequence = ()) -> int:
    """INSERT/UPDATE/DELETE. Devuelve lastrowid (SQLite) o 1."""
    if backend_activo() == "sqlite":
        return _sqlite_execute(sql, params)
    resultado = _rest_write(sql, params)
    _invalidar_cache()
    if isinstance(resultado, list) and resultado and isinstance(resultado[0], dict):
        if "id" in resultado[0]:
            return int(resultado[0]["id"])
    return 1


def _rest_write(sql: str, params: Sequence) -> Any:
    import re

    text = " ".join(sql.split())
    vals = list(params)
    m = re.match(r"INSERT INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)", text, re.I)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        row = dict(zip(cols, vals))
        return _rest(table, "POST", body=row)
    m = re.match(r"UPDATE (\w+) SET (.+?) WHERE (\w+)\s*=\s*\?", text, re.I)
    if m:
        table, sets, key = m.group(1), m.group(2), m.group(3)
        assignments = {}
        for item in sets.split(","):
            c, _, v = item.partition("=")
            assignments[c.strip()] = vals.pop(0) if vals else None
        pk = vals.pop(0) if vals else None
        # Quitar el id del cuerpo para no intentar cambiarlo.
        assignments.pop("id", None)
        return _rest(table, "PATCH", {"id": f"eq.{_rest_val(pk)}"}, assignments)
    m = re.match(r"DELETE FROM (\w+) WHERE (\w+)\s*=\s*\?", text, re.I)
    if m:
        return _rest(m.group(1), "DELETE", {m.group(2): f"eq.{_rest_val(vals[0])}"})
    raise DatabaseError(f"SQL no soportado por el adaptador Supabase: {sql[:120]}")


def borrar_donde(tabla: str, campo: str, valor: Any) -> None:
    """DELETE genérico que funciona en ambos backends."""
    if backend_activo() == "sqlite":
        execute(f"DELETE FROM {tabla} WHERE {campo} = ?", (valor,))
    else:
        _rest(tabla, "DELETE", {campo: f"eq.{_rest_val(valor)}"})
        _invalidar_cache()


def borrar_donde_neq(tabla: str, campo: str, valor: Any) -> None:
    """DELETE de todas las filas cuyo campo DIFIERE de valor (ambos backends)."""
    if backend_activo() == "sqlite":
        execute(f"DELETE FROM {tabla} WHERE {campo} != ?", (valor,))
    else:
        _rest(tabla, "DELETE", {campo: f"neq.{_rest_val(valor)}"})
        _invalidar_cache()


def exec_many(sql: str, seq: Iterable[Sequence]) -> None:
    if backend_activo() == "sqlite":
        conn = _sqlite_conn()
        try:
            conn.executemany(sql, [tuple(x) for x in seq])
            conn.commit()
        finally:
            conn.close()
        return
    for params in seq:
        execute(sql, params)


def init_db() -> None:
    """Crea el esquema local. En Supabase se ejecuta sql/schema.sql una vez."""
    if backend_activo() != "sqlite":
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Primera ejecución del .exe: copiar la BD semilla incluida en el paquete.
    if not DB_PATH.exists():
        semilla = RECURSOS / "data" / DB_PATH.name
        if semilla.exists():
            shutil.copy2(semilla, DB_PATH)
    _sqlite_executescript("""
    -- Migración proveedora de columnas nuevas sobre BDs históricas.
    CREATE TABLE IF NOT EXISTS facultades (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS carreras (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL,
        facultad_id INTEGER NOT NULL, codigo TEXT DEFAULT '', UNIQUE(nombre, facultad_id),
        FOREIGN KEY(facultad_id) REFERENCES facultades(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS salones (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE,
        capacidad INTEGER DEFAULT 40, edificio TEXT DEFAULT 'Edificio Principal', es_camillas INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS profesores (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE,
        cedula TEXT DEFAULT '', email TEXT DEFAULT '', condicion TEXT DEFAULT 'Contratado/a', mencion TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS disponibilidades (id INTEGER PRIMARY KEY AUTOINCREMENT,
        profesor_id INTEGER NOT NULL, dia TEXT NOT NULL, hora_inicio TEXT NOT NULL, hora_fin TEXT NOT NULL,
        UNIQUE(profesor_id,dia,hora_inicio,hora_fin), FOREIGN KEY(profesor_id) REFERENCES profesores(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS planes_estudio (id INTEGER PRIMARY KEY AUTOINCREMENT, carrera_id INTEGER NOT NULL,
        codigo TEXT DEFAULT '', nombre TEXT NOT NULL, unidad_credito INTEGER DEFAULT 0,
        horas_teoria INTEGER DEFAULT 0, horas_practica INTEGER DEFAULT 0, ciclo TEXT DEFAULT '',
        opcionalidad TEXT DEFAULT '', tecnica TEXT DEFAULT '', prelacion TEXT DEFAULT '',
        UNIQUE(carrera_id,codigo,nombre), FOREIGN KEY(carrera_id) REFERENCES carreras(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS materias (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL,
        codigo TEXT DEFAULT '', carrera_id INTEGER NOT NULL, semestre TEXT DEFAULT '1er Semestre',
        unidad_credito INTEGER DEFAULT 0, mencion TEXT DEFAULT '', UNIQUE(nombre,carrera_id),
        FOREIGN KEY(carrera_id) REFERENCES carreras(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS carga_academica (id INTEGER PRIMARY KEY AUTOINCREMENT,
        profesor_id INTEGER NOT NULL, materia_id INTEGER NOT NULL, seccion TEXT DEFAULT 'Sección A',
        bloques_semanales INTEGER DEFAULT 1, salon_preferido TEXT DEFAULT '', dia_fijo TEXT DEFAULT '',
        hora_inicio_fija TEXT DEFAULT '', hora_fin_fija TEXT DEFAULT '', compartidos TEXT DEFAULT '',
        UNIQUE(profesor_id,materia_id,seccion), FOREIGN KEY(profesor_id) REFERENCES profesores(id) ON DELETE CASCADE,
        FOREIGN KEY(materia_id) REFERENCES materias(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS asignaciones_horario (id INTEGER PRIMARY KEY AUTOINCREMENT,
        carga_id INTEGER NOT NULL, profesor_id INTEGER NOT NULL, materia_id INTEGER NOT NULL,
        salon_id INTEGER NOT NULL, dia TEXT NOT NULL, hora_inicio TEXT NOT NULL, hora_fin TEXT NOT NULL,
        estado TEXT DEFAULT 'Confirmado', FOREIGN KEY(carga_id) REFERENCES carga_academica(id) ON DELETE CASCADE,
        FOREIGN KEY(profesor_id) REFERENCES profesores(id) ON DELETE CASCADE,
        FOREIGN KEY(materia_id) REFERENCES materias(id) ON DELETE CASCADE,
        FOREIGN KEY(salon_id) REFERENCES salones(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS configuracion (clave TEXT PRIMARY KEY, valor TEXT);
    CREATE INDEX IF NOT EXISTS idx_asignaciones_dia ON asignaciones_horario(dia);
    CREATE INDEX IF NOT EXISTS idx_asignaciones_profesor ON asignaciones_horario(profesor_id);
    CREATE INDEX IF NOT EXISTS idx_asignaciones_salon ON asignaciones_horario(salon_id);
    CREATE INDEX IF NOT EXISTS idx_asignaciones_materia ON asignaciones_horario(materia_id);
    CREATE INDEX IF NOT EXISTS idx_carga_profesor ON carga_academica(profesor_id);
    CREATE INDEX IF NOT EXISTS idx_materias_carrera ON materias(carrera_id);
    CREATE INDEX IF NOT EXISTS idx_plan_carrera ON planes_estudio(carrera_id);
    """)
    # Columnas añadidas en versiones nuevas (SQLite no tiene ADD COLUMN IF NOT EXISTS).
    for tabla, columna, ddl in [
        ("carreras", "codigo", "TEXT DEFAULT ''"),
        ("planes_estudio", "ciclo", "TEXT DEFAULT ''"),
    ]:
        cols = {r["name"] for r in _sqlite_query(f"PRAGMA table_info({tabla})")}
        if columna not in cols:
            _sqlite_execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {ddl}")


# ─────────────────────────── helpers de dominio ───────────────────────────

def normalizar(texto: Any) -> str:
    if not texto:
        return ""
    t = "".join(c for c in unicodedata.normalize("NFD", str(texto))
                if unicodedata.category(c) != "Mn")
    return " ".join(t.upper().replace("À", "A").split())


def normalizar_entero(valor: Any, *, minimo: int = 0, maximo: Optional[int] = None,
                      default: Optional[int] = 0) -> int:
    """Convierte a ``int`` solo valores enteros reales.

    Rechaza decimales, notación exponencial y texto (``3.5``, ``1e3``,
    ``"3 créditos"``). Vacío/``None`` devuelve ``default`` para no romper
    formularios antiguos. Devuelve siempre ``int``, nunca ``str``.
    """
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if default is None:
            raise ValueError("El valor numérico es obligatorio.")
        return int(default)
    if isinstance(valor, bool):
        raise ValueError("El valor debe ser un número entero.")
    if isinstance(valor, int):
        numero = valor
    elif isinstance(valor, float):
        if not valor.is_integer():
            raise ValueError(f"'{valor}' no es un número entero.")
        numero = int(valor)
    else:
        texto = str(valor).strip()
        if not re.fullmatch(r"[+-]?\d+", texto):
            raise ValueError(f"'{texto}' no es un número entero válido.")
        numero = int(texto)
    if numero < minimo or (maximo is not None and numero > maximo):
        rango = f" entre {minimo} y {maximo}" if maximo is not None else f" mayor o igual a {minimo}"
        raise ValueError(f"El valor debe ser{rango}.")
    return numero


def normalizar_cedula(valor: Any) -> str:
    """Canonicaliza cédulas venezolanas a ``V-1234567`` / ``E-12345678``.

    Acepta prefijo V/E, con o sin guion, y puntos de miles (``V-1.234.567``).
    Vacío se conserva como vacío (los datos actuales no tienen cédula cargada).
    """
    if valor is None:
        return ""
    texto = " ".join(str(valor).strip().upper().split())
    if not texto:
        return ""
    if not (re.fullmatch(r"[VE][ -]?\d{7,8}", texto)
            or re.fullmatch(r"[VE][ -]?\d{1,3}(?:\.\d{3}){1,2}", texto)):
        raise ValueError("Cédula inválida. Ejemplos: V-1234567 o V-1.234.567.")
    digitos = re.sub(r"\D", "", texto[1:])
    if not 7 <= len(digitos) <= 8:
        raise ValueError("La cédula debe tener 7 u 8 dígitos.")
    return f"{texto[0]}-{digitos}"


# Orden oficial de ambientes, tomado del inventario del sistema original
# (database.py / SGHU_Universidad.py de servicio-comunitario-horarios).
SECTORES_AMBIENTE: Tuple[Tuple[str, int], ...] = (
    ("Piso 1", 1),
    ("Piso 2", 2),
    ("Piso 3", 3),
    ("Piso 4", 4),
    ("Satelitales", 5),
    ("Planta Baja (PB)", 6),
    ("Virtuales", 7),
    ("Laboratorios y Otros", 8),
)


def categoria_ambiente(codigo: Any) -> Tuple[str, int]:
    """Devuelve ``(sector, prioridad)`` para un código de ambiente.

    Reproduce ``categorizar_piso_ambiente()`` del sistema original: P1, P2, P3,
    P4, SATELITAL, PB, VIRTUAL y, por último, laboratorios/dependencias varias.
    """
    c = str(codigo or "").strip().strip("´'`\"").upper()
    for prefijo in ("P1.", "P2.", "P3.", "P4."):
        if c.startswith(prefijo):
            c = prefijo[:2] + "-" + c[3:]
    if (c.startswith("P1-") or c.startswith("P1") or "PISO 1" in c
            or c.startswith("AULA 1")):
        return "Piso 1", 1
    if (c.startswith("P2-") or c.startswith("P2") or "PISO 2" in c
            or c.startswith("AULA 2")):
        return "Piso 2", 2
    if (c.startswith("P3-") or c.startswith("P3") or "PISO 3" in c
            or c.startswith("AULA 3") or "MAGNA" in c):
        return "Piso 3", 3
    if (c.startswith("P4-") or c.startswith("P4") or "PISO 4" in c
            or c.startswith("AULA 4")):
        return "Piso 4", 4
    if "SATELITAL" in c:
        return "Satelitales", 5
    if c.startswith("PB") or "PLANTA BAJA" in c:
        return "Planta Baja (PB)", 6
    if "VIRTUAL" in c or "ONLINE" in c:
        return "Virtuales", 7
    return "Laboratorios y Otros", 8


def clave_orden_ambiente(codigo: Any) -> List[Tuple[int, Any]]:
    """Orden natural: ``P1-2`` antes que ``P1-10`` (no orden alfabético)."""
    c = str(codigo or "").strip().strip("´'`\"").upper()
    return [(0, int(t)) if t.isdigit() else (1, normalizar(t))
            for t in re.split(r"(\d+)", c) if t != ""]


def ordenar_ambientes(filas: Iterable[Dict], campo: str = "codigo") -> List[Dict]:
    """Ordena ambientes por sector y, dentro de cada sector, por número."""
    return sorted(
        filas,
        key=lambda f: (categoria_ambiente(f.get(campo))[1],
                       clave_orden_ambiente(f.get(campo)),
                       str(f.get("id") or "")),
    )


def ambientes_oficiales(filas: Iterable[Dict], campo: str = "codigo") -> List[Dict]:
    """Filtra a los 41 salones oficiales y los deja en el orden correcto."""
    permitidos = set(AMBIENTES_OFICIALES)
    return ordenar_ambientes(
        [f for f in filas if str(f.get(campo) or "").strip().upper() in permitidos],
        campo,
    )


def agrupar_ambientes(filas: Iterable[Dict], campo: str = "codigo") -> List[Dict]:
    """Agrupa los ambientes ya ordenados para mostrar secciones seguidas por piso."""
    por_sector: Dict[str, List[Dict]] = {}
    for fila in ordenar_ambientes(filas, campo):
        sector, _ = categoria_ambiente(fila.get(campo))
        por_sector.setdefault(sector, []).append(fila)
    return [
        {"sector": sector, "total": len(por_sector[sector]), "filas": por_sector[sector]}
        for sector, _prioridad in SECTORES_AMBIENTE
        if sector in por_sector
    ]


def get_or_create(table: str, field: str, value: str, extra: Optional[Dict] = None) -> int:
    extra = extra or {}
    rows = query(f"SELECT id FROM {table} WHERE {field} = ? LIMIT 1", (value,))
    if rows:
        return int(rows[0]["id"])
    cols = [field] + list(extra)
    vals = [value] + list(extra.values())
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    rid = execute(sql, vals)
    if not rid:  # Supabase: consultar el id recién creado
        rows = query(f"SELECT id FROM {table} WHERE {field} = ? LIMIT 1", (value,))
        rid = rows[0]["id"] if rows else 0
    return int(rid)


def get_or_create_materia(nombre: str, carrera_id: int, codigo: str = "",
                          semestre: str = "1er Semestre", creditos: int = 0) -> int:
    # Punto central de escritura de unidad_credito: todas las importaciones
    # y cargas pasan por aquí, así que ningún texto llega a una columna INTEGER.
    creditos = normalizar_entero(creditos, minimo=0, maximo=30, default=0)
    rows = query("SELECT id FROM materias WHERE nombre = ? AND carrera_id = ? LIMIT 1",
                 (nombre, carrera_id))
    if rows:
        mid = int(rows[0]["id"])
        if codigo or creditos:
            execute("UPDATE materias SET codigo=?, unidad_credito=? WHERE id=?",
                    (codigo, creditos, mid))
        return mid
    return int(execute(
        "INSERT INTO materias (nombre,codigo,carrera_id,semestre,unidad_credito) VALUES (?,?,?,?,?)",
        (nombre, codigo, carrera_id, semestre, creditos)))


def _borrar_tablas(tablas: List[str]) -> None:
    if backend_activo() == "sqlite":
        conn = _sqlite_conn()
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            for tabla in tablas:
                conn.execute(f"DELETE FROM {tabla}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" %
                         ",".join("?" for _ in tablas), tuple(tablas))
            conn.commit()
        finally:
            conn.close()
    else:
        for tabla in tablas:
            _rest(tabla, "DELETE", {"id": "gte.1"})


def limpiar_carga_academica() -> None:
    """Borra solo clases, fichas de carga y disponibilidades.

    Conserva carreras, docentes, materias, pensum, facultades y ambientes:
    es la acción del botón «Vaciar datos», que permite regenerar el horario
    sin perder los catálogos ya importados.
    """
    _borrar_tablas(["asignaciones_horario", "carga_academica", "disponibilidades"])


def clear_all_data(keep_salones: bool = True) -> None:
    """Borrado total de catálogos y carga (no lo usa la interfaz)."""
    tablas = ["asignaciones_horario", "carga_academica", "disponibilidades",
              "planes_estudio", "materias", "profesores", "carreras", "facultades"]
    if not keep_salones:
        tablas.insert(0, "salones")
    _borrar_tablas(tablas)


def estadisticas() -> Dict[str, int]:
    stats: Dict[str, int] = {}
    for key, sql in {
        "carreras": "SELECT COUNT(*) n FROM carreras",
        "profesores": "SELECT COUNT(*) n FROM profesores",
        "materias": "SELECT COUNT(*) n FROM materias",
        "planes_estudio": "SELECT COUNT(*) n FROM planes_estudio",
        "salones": "SELECT COUNT(*) n FROM salones",
        "clases_programadas": "SELECT COUNT(*) n FROM asignaciones_horario",
        "ambientes_en_uso": "SELECT COUNT(DISTINCT salon_id) n FROM asignaciones_horario",
        "profesores_activos": "SELECT COUNT(DISTINCT profesor_id) n FROM asignaciones_horario",
    }.items():
        try:
            stats[key] = int(query(sql)[0]["n"])
        except Exception:
            stats[key] = 0
    return stats
