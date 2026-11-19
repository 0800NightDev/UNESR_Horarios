"""Consultas de dominio reutilizables por la web y los reportes."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import db

SQL_OFERTAS = """
SELECT a.id, a.dia, a.hora_inicio, a.hora_fin, a.estado,
       c.id AS carrera_id, c.nombre AS carrera,
       f.nombre AS facultad,
       p.id AS profesor_id, p.nombre AS profesor,
       m.id AS materia_id, m.nombre AS materia, m.codigo AS codigo,
       s.id AS salon_id, s.codigo AS ambiente,
       ca.seccion
FROM asignaciones_horario a
JOIN carga_academica ca ON ca.id=a.carga_id
JOIN profesores p ON p.id=a.profesor_id
JOIN materias m ON m.id=a.materia_id
JOIN carreras c ON c.id=m.carrera_id
JOIN facultades f ON f.id=c.facultad_id
JOIN salones s ON s.id=a.salon_id
"""


def oferta(facultad_id: Optional[int] = None, carrera_id: Optional[int] = None,
           profesor_id: Optional[int] = None, dia: Optional[str] = None) -> List[Dict]:
    conditions, params = [], []
    if facultad_id:
        conditions.append("f.id=?"); params.append(facultad_id)
    if carrera_id:
        conditions.append("c.id=?"); params.append(carrera_id)
    if profesor_id:
        conditions.append("p.id=?"); params.append(profesor_id)
    if dia:
        conditions.append("a.dia=?"); params.append(dia)
    sql = SQL_OFERTAS
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY c.nombre, a.dia, a.hora_inicio, p.nombre, m.nombre"
    return db.query(sql, params)


def carreras_con_pensum() -> List[Dict]:
    """Todas las carreras con TODAS las materias de su pensum (sin cortes)."""
    carreras = db.query("""
        SELECT c.id, c.nombre, c.codigo, f.nombre AS facultad
        FROM carreras c JOIN facultades f ON f.id=c.facultad_id
        ORDER BY f.nombre, c.nombre
    """)
    planes = db.query("""
        SELECT carrera_id, codigo, nombre, unidad_credito, horas_teoria,
               horas_practica, ciclo, opcionalidad, tecnica, prelacion
        FROM planes_estudio ORDER BY ciclo, codigo, nombre
    """)
    # Si aún no se importó el plan, mostrar las materias del catálogo.
    materias = db.query("""
        SELECT carrera_id, codigo, nombre, semestre AS ciclo, unidad_credito
        FROM materias ORDER BY semestre, nombre
    """)
    por_carrera: Dict[int, List[Dict]] = {}
    usan_plan = bool(planes)
    for p in (planes if usan_plan else materias):
        por_carrera.setdefault(int(p["carrera_id"]), []).append(dict(p))
    resultado = []
    for c in carreras:
        resultado.append({
            "id": int(c["id"]),
            "nombre": c["nombre"],
            "facultad": c["facultad"],
            "codigo": c.get("codigo") or "",
            "materias": por_carrera.get(int(c["id"]), []),
            "fuente": "Plan de estudio 2024" if usan_plan else "Catálogo de oferta",
        })
    return resultado


def estado_carga_carrera(carrera_id: int) -> Dict:
    """Diferencia 'pensum cargado' de 'sin docentes asignados'.

    Los PDF de los PNF traen la malla curricular, no fichas docentes: existen
    unidades curriculares pero no hay ``carga_academica`` ni asignaciones. La
    vista de horarios debe decir eso, no sugerir que no se importó el pensum.
    """
    carrera_id = int(carrera_id)
    pensum = next((c["materias"] for c in carreras_con_pensum()
                   if c["id"] == carrera_id), [])

    materias = db.query("SELECT id, nombre FROM materias WHERE carrera_id = ?",
                        (carrera_id,))
    mat_ids = [int(m["id"]) for m in materias]
    if mat_ids:
        marcadores = ",".join("?" for _ in mat_ids)
        carga = db.query(
            f"SELECT id, materia_id FROM carga_academica WHERE materia_id IN ({marcadores})",
            mat_ids)
    else:
        carga = []
    carga_ids = [int(c["id"]) for c in carga]
    if carga_ids:
        marcadores = ",".join("?" for _ in carga_ids)
        asignaciones = db.query(
            f"SELECT id FROM asignaciones_horario WHERE carga_id IN ({marcadores})",
            carga_ids)
    else:
        asignaciones = []

    con_docente = {db.normalizar(m["nombre"]) for m in materias
                   if int(m["id"]) in {int(c["materia_id"]) for c in carga}}
    pendientes = [m for m in pensum
                  if db.normalizar(m.get("nombre")) not in con_docente]
    return {
        "pensum": pensum,
        "total_uc": sum(int(m.get("unidad_credito") or 0) for m in pensum),
        "materias_pensum": len(pensum),
        "materias_con_docente": len(pensum) - len(pendientes),
        "pendientes": pendientes,
        "carga": len(carga),
        "asignaciones": len(asignaciones),
        "sin_clases": not asignaciones,
    }


def horario_por_carrera(carrera_id: int) -> "OrderedDict[str, List[Dict]]":
    """Estructura {Lunes: [...], Martes: [...]} con todas las clases de la carrera.

    Es el reemplazo del 'muestreo general de todas las carreras'.
    """
    filas = oferta(carrera_id=carrera_id)
    por_dia: OrderedDict[str, List[Dict]] = OrderedDict()
    for dia in db.DIAS_ORDEN:
        por_dia[dia] = []
    for fila in filas:
        dia = next((d for d in db.DIAS_ORDEN if db.normalizar(d) == db.normalizar(fila["dia"])), fila["dia"])
        por_dia.setdefault(dia, []).append(fila)
    for dia in por_dia:
        por_dia[dia].sort(key=lambda r: (_hora(r["hora_inicio"]), r["profesor"], r["materia"]))
    return por_dia


def cuadro_horario(carrera_id: int, dia: Optional[str] = None,
                   profesor_id: Optional[int] = None) -> Dict:
    """Matriz día × franja horaria para UNA carrera (nunca 'todas').

    Devuelve ``dias``, ``franjas`` [(inicio, fin), ...] y
    ``celdas[dia][inicio] = [clases]`` para renderizar una cuadrícula real
    en lugar de una lista plana.
    """
    filas = oferta(carrera_id=carrera_id, dia=dia, profesor_id=profesor_id)
    dias = list(db.DIAS_ORDEN[:6])  # Lunes a Sábado
    for fila in filas:
        if fila["dia"] not in dias:
            dias.append(fila["dia"])
    franjas = sorted({(f["hora_inicio"], f["hora_fin"]) for f in filas},
                     key=lambda fr: (_hora(fr[0]), _hora(fr[1])))
    celdas: Dict[str, Dict[str, List[Dict]]] = {d: {} for d in dias}
    for fila in filas:
        dia_fila = next(
            (d for d in dias if db.normalizar(d) == db.normalizar(fila["dia"])),
            fila["dia"],
        )
        celdas.setdefault(dia_fila, {}).setdefault(fila["hora_inicio"], []).append(fila)
    for dia_key in celdas:
        for franja in celdas[dia_key]:
            celdas[dia_key][franja].sort(key=lambda r: (r["profesor"], r["materia"], r["ambiente"]))
    return {"dias": dias, "franjas": franjas, "celdas": celdas,
            "total": len(filas)}


def _hora(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 9999


def profesores() -> List[Dict]:
    return db.query("SELECT id,nombre,cedula,email,condicion,mencion FROM profesores ORDER BY nombre")


def salones() -> List[Dict]:
    """Ambientes en el orden oficial: pisos, satelitales, PB, virtuales y otros."""
    return db.ambientes_oficiales(
        db.query("SELECT id,codigo,capacidad,edificio,es_camillas FROM salones"))


def facultades() -> List[Dict]:
    return db.query("SELECT id,nombre FROM facultades ORDER BY nombre")


def asignaciones_profesor(profesor_id: int) -> List[Dict]:
    return oferta(profesor_id=profesor_id)


def opciones_carga() -> Dict[str, Any]:
    """Opciones para los formularios de carga, con materias por carrera.

    ``materias_por_carrera`` se usa en el cliente para que el ``datalist`` de
    materias muestre solo las unidades curriculares de la carrera elegida.
    """
    carreras = db.query("SELECT id, nombre FROM carreras ORDER BY nombre")
    por_id = {int(c["id"]): c["nombre"] for c in carreras}
    materias_por_carrera: Dict[str, List[str]] = {nombre: [] for nombre in por_id.values()}
    for m in db.query("SELECT carrera_id, nombre FROM materias ORDER BY nombre"):
        nombre_carrera = por_id.get(int(m["carrera_id"]))
        nombre = m["nombre"]
        if nombre_carrera and nombre and nombre not in materias_por_carrera[nombre_carrera]:
            materias_por_carrera[nombre_carrera].append(nombre)
    # El pensum puede tener unidades que aún no están en el catálogo de materias.
    for c in carreras_con_pensum():
        destino = materias_por_carrera.setdefault(c["nombre"], [])
        for m in c["materias"]:
            if m["nombre"] and m["nombre"] not in destino:
                destino.append(m["nombre"])
    for nombre in materias_por_carrera:
        materias_por_carrera[nombre].sort()

    return {
        "profesores": [p["nombre"] for p in profesores()],
        "carreras": list(por_id.values()),
        "materias": [m["nombre"] for m in db.query("SELECT nombre FROM materias ORDER BY nombre")],
        "materias_por_carrera": materias_por_carrera,
        "codigos": [m["codigo"] for m in db.query("SELECT DISTINCT codigo FROM materias WHERE codigo!='' ORDER BY codigo")],
        "ambientes": [s["codigo"] for s in salones()],
    }
