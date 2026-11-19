"""Generación automática de horarios y asignación de ambientes.

Reglas corregidas ( frente al scheduler original ):
  1. Toda clase se asigna DENTRO de una franja de disponibilidad del docente.
  2. Un docente no puede tener dos clases simultáneas (salvo cursos compartidos
     explícitamente marcados como 'COMPARTIDOS').
  3. Un ambiente no puede tener dos clases simultáneas, salvo ambientes
     compartibles (VIRTUAL, COORDINACIÓN, AUDITORIO, CANCHA...).
  4. Si la carga tiene día/hora fijos (Excel), se respetan; si no hay hueco,
     se reporta el motivo en lugar de asignar igual.
  5. Se prioriza el ambiente preferido y luego el de menor uso, para repartir.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import db

DIAS = db.DIAS_ORDEN


def es_compartible(codigo: str) -> bool:
    c = db.normalizar(codigo)
    return any(k in c for k in ("VIRTUAL", "COORDINAC", "ONLINE", "AUDITORIO", "CANCHA", "SATELITAL"))


def _a_minutos(hhmm: str) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _franjas_solapadas(a: Tuple[str, str], b: Tuple[str, str]) -> bool:
    return max(_a_minutos(a[0]), _a_minutos(b[0])) < min(_a_minutos(a[1]), _a_minutos(b[1]))


def _hora_mayor(a: str, b: str) -> str:
    return a if _a_minutos(a) >= _a_minutos(b) else b


def _hora_menor(a: str, b: str) -> str:
    return a if _a_minutos(a) <= _a_minutos(b) else b


def _dias_ordenados(dia: str) -> int:
    d = db.normalizar(dia)
    for i, nombre in enumerate(DIAS):
        if db.normalizar(nombre) == d:
            return i
    return len(DIAS)


def generar_horarios_automaticos() -> Dict:
    """Regenera todas las asignaciones desde la carga académica y disponibilidad."""
    salones = db.query("SELECT id,codigo,capacidad FROM salones ORDER BY id")
    if not salones:
        return {"asignados": 0, "pendientes": 0, "mensajes": ["No hay ambientes registrados."]}

    # Disponibilidad por profesor
    disponibilidad: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for d in db.query("SELECT profesor_id,dia,hora_inicio,hora_fin FROM disponibilidades"):
        disponibilidad[int(d["profesor_id"])].append((d["dia"], d["hora_inicio"], d["hora_fin"]))

    carga = db.query("""
        SELECT ca.id, ca.profesor_id, ca.materia_id, ca.seccion, ca.bloques_semanales,
               ca.salon_preferido, ca.dia_fijo, ca.hora_inicio_fija, ca.hora_fin_fija,
               ca.compartidos, p.nombre AS profesor, m.nombre AS materia
        FROM carga_academica ca
        JOIN profesores p ON p.id=ca.profesor_id
        JOIN materias m ON m.id=ca.materia_id
        ORDER BY p.nombre, m.nombre, ca.seccion
    """)

    # Solo las reasignaciones MANUALES son intocables. Las asignaciones
    # 'Confirmado' que vienen de la BD histórica y las 'Auto' se regeneran;
    # tratarlas como bloqueos era el bug que dejaba 0 clases asignadas.
    prof_ocupado: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    salon_ocupado: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for a in db.query("SELECT * FROM asignaciones_horario WHERE estado = 'Manual'"):
        prof_ocupado[int(a["profesor_id"])].append((a["dia"], a["hora_inicio"], a["hora_fin"]))
        salon_ocupado[int(a["salon_id"])].append((a["dia"], a["hora_inicio"], a["hora_fin"]))

    # Regenera todo excepto las reasignaciones manuales.
    db.borrar_donde_neq("asignaciones_horario", "estado", "Manual")

    asignados = 0
    pendientes = 0
    mensajes: List[str] = []
    detalles: List[Dict] = []
    uso_salon: Dict[int, int] = defaultdict(int)
    codigo_id = {str(s["codigo"]).strip().upper(): int(s["id"]) for s in salones}

    for c in carga:
        cid, pid, mid = int(c["id"]), int(c["profesor_id"]), int(c["materia_id"])
        compartido = bool(str(c["compartidos"] or "").strip())
        bloques = max(1, int(c["bloques_semanales"] or 1))
        pref = str(c["salon_preferido"] or "").strip().upper()
        pref_id = codigo_id.get(pref)

        # Candidatos: franjas de disponibilidad del profesor (o fijas del Excel).
        if c["dia_fijo"] and c["hora_inicio_fija"] and c["hora_fin_fija"]:
            candidatos = [(c["dia_fijo"], c["hora_inicio_fija"], c["hora_fin_fija"])]
        else:
            candidatos = sorted(disponibilidad.get(pid, []), key=lambda x: (_dias_ordenados(x[0]), _a_minutos(x[1])))
        if not candidatos:
            pendientes += bloques
            mensaje = f"Sin disponibilidad: {c['profesor']} — {c['materia']}."
            mensajes.append(mensaje)
            detalles.append({"motivo": "Sin disponibilidad", "detalle": mensaje,
                             "profesor": c["profesor"], "materia": c["materia"]})
            continue

        puestos = 0
        for dia, ini, fin in candidatos:
            if puestos >= bloques:
                break
            franja = (ini, fin)
            # Conflicto de docente
            choque_prof = next((
                (d, o_ini, o_fin) for d, o_ini, o_fin in prof_ocupado[pid]
                if db.normalizar(d) == db.normalizar(dia)
                and _franjas_solapadas(franja, (o_ini, o_fin))
            ), None) if not compartido else None
            if choque_prof:
                detalles.append({
                    "motivo": "Docente ocupado",
                    "detalle": (f"{c['profesor']} ya imparte {c['materia']} el {choque_prof[0]} "
                                f"{choque_prof[1]}–{choque_prof[2]}; el bloque {dia} {ini}–{fin} "
                                f"se solapa y no se reasignó automáticamente."),
                    "profesor": c["profesor"], "materia": c["materia"],
                    "dia": dia, "desde": ini, "hasta": fin,
                })
                continue

            # Elegir ambiente: preferido -> libre -> compartible; repartir por uso.
            orden = sorted(salones, key=lambda s: (
                0 if int(s["id"]) == pref_id else 1,
                0 if es_compartible(s["codigo"]) else 1 if compartido else 0,
                uso_salon[int(s["id"])],
                int(s["id"]),
            ))
            elegido = None
            for s in orden:
                sid = int(s["id"])
                libre = not any(
                    db.normalizar(d) == db.normalizar(dia) and _franjas_solapadas(franja, (o_ini, o_fin))
                    for d, o_ini, o_fin in salon_ocupado[sid]
                )
                if libre or (compartido and es_compartible(s["codigo"])):
                    elegido = s
                    break
            if elegido is None:
                detalles.append({
                    "motivo": "Sin ambiente libre",
                    "detalle": (f"No hay ambiente disponible para {c['materia']} "
                                f"({c['profesor']}) el {dia} {ini}–{fin}: los {len(salones)} "
                                f"ambientes están ocupados o no son compartibles."),
                    "profesor": c["profesor"], "materia": c["materia"],
                    "dia": dia, "desde": ini, "hasta": fin,
                })
                continue

            sid = int(elegido["id"])
            db.execute("""INSERT INTO asignaciones_horario
                (carga_id,profesor_id,materia_id,salon_id,dia,hora_inicio,hora_fin,estado)
                VALUES (?,?,?,?,?,?,?,'Auto')""",
                (cid, pid, mid, sid, dia, ini, fin))
            prof_ocupado[pid].append((dia, ini, fin))
            salon_ocupado[sid].append((dia, ini, fin))
            uso_salon[sid] += 1
            asignados += 1
            puestos += 1

        if puestos < bloques:
            pendientes += bloques - puestos
            mensaje = f"Faltaron {bloques - puestos} bloque(s) para {c['materia']} ({c['profesor']})."
            mensajes.append(mensaje)
            detalles.append({"motivo": "Bloques pendientes", "detalle": mensaje,
                             "profesor": c["profesor"], "materia": c["materia"]})

    if not mensajes:
        mensajes.append(f"OK: {asignados} clases asignadas sin conflictos.")
    return {"asignados": asignados, "pendientes": pendientes,
            "mensajes": mensajes, "detalles": detalles}


def validar_cambio_manual(asignacion_id: int, salon_id: int, dia: str,
                          ini: str, fin: str) -> Tuple[bool, str]:
    """Valida la reasignación manual de ambiente/horario."""
    actual = db.query("SELECT * FROM asignaciones_horario WHERE id=?", (asignacion_id,))
    if not actual:
        return False, "La asignación no existe."
    salon = db.query("SELECT codigo FROM salones WHERE id=?", (salon_id,))
    if not salon:
        return False, "El ambiente no existe."
    if not es_compartible(salon[0]["codigo"]):
        choque = db.query("""SELECT id FROM asignaciones_horario
            WHERE salon_id=? AND dia=? AND id!=? AND hora_inicio<? AND hora_fin>? LIMIT 1""",
            (salon_id, dia, asignacion_id, fin, ini))
        if choque:
            return False, "El ambiente ya está ocupado en ese horario."
    prof = int(actual[0]["profesor_id"])
    choque_prof = db.query("""SELECT id FROM asignaciones_horario
        WHERE profesor_id=? AND dia=? AND id!=? AND hora_inicio<? AND hora_fin>? LIMIT 1""",
        (prof, dia, asignacion_id, fin, ini))
    if choque_prof:
        return False, "El docente ya tiene clase en ese horario."
    return True, ""


def conflictos() -> List[Dict]:
    """Detecta colisiones actuales y explica por qué ocurren.

    Conserva las claves históricas ``a``, ``b`` y ``motivo``; añade datos para
    que el dashboard y el cuadro de horarios puedan mostrar el motivo legible,
    la carrera afectada y si conviene reasignar automáticamente o a mano.
    """
    filas = db.query("""SELECT a.*, p.nombre AS profesor, s.codigo AS ambiente, m.nombre AS materia,
        m.carrera_id AS carrera_id, c.nombre AS carrera, ca.seccion AS seccion,
        ca.dia_fijo AS dia_fijo
        FROM asignaciones_horario a
        JOIN profesores p ON p.id=a.profesor_id
        JOIN salones s ON s.id=a.salon_id
        JOIN materias m ON m.id=a.materia_id
        LEFT JOIN carreras c ON c.id=m.carrera_id
        LEFT JOIN carga_academica ca ON ca.id=a.carga_id""")
    resultado = []
    for i, a in enumerate(filas):
        for b in filas[i + 1:]:
            if db.normalizar(a["dia"]) != db.normalizar(b["dia"]):
                continue
            if not _franjas_solapadas((a["hora_inicio"], a["hora_fin"]), (b["hora_inicio"], b["hora_fin"])):
                continue
            motivos = []
            if int(a["profesor_id"]) == int(b["profesor_id"]):
                motivos.append("docente")
            if int(a["salon_id"]) == int(b["salon_id"]) and not es_compartible(a["ambiente"]):
                motivos.append("ambiente")
            if not motivos:
                continue
            desde = _hora_mayor(a["hora_inicio"], b["hora_inicio"])
            hasta = _hora_menor(a["hora_fin"], b["hora_fin"])

            def _quien(f: Dict) -> str:
                carrera = f.get("carrera") or "carrera sin asignar"
                seccion = f.get("seccion") or "—"
                return (f"{f['materia']} ({f['profesor']}, {f['ambiente']}, "
                        f"{carrera}, secc. {seccion})")

            manual = ("Manual" in (str(a.get("estado") or ""), str(b.get("estado") or ""))
                      or bool(a.get("dia_fijo")) or bool(b.get("dia_fijo")))
            resultado.append({
                "a": a, "b": b,
                "motivo": " y ".join(motivos),
                "motivos": motivos,
                "dia": a["dia"],
                "desde": desde,
                "hasta": hasta,
                "ids": [int(a["id"]), int(b["id"])],
                "carrera_id": int(a["carrera_id"]) if a.get("carrera_id") else None,
                "b_carrera_id": int(b["carrera_id"]) if b.get("carrera_id") else None,
                "detalle": (f"{'/'.join(motivos).capitalize()} el {a['dia']} {desde}–{hasta}: "
                            f"{_quien(a)} vs {_quien(b)}."),
                "sugerencia": "manual" if manual else "auto",
            })
    return resultado
