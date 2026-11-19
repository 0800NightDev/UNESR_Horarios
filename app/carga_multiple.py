"""Carga académica múxima manual con validación de conflictos.

El formulario permite registrar varias materias para uno o varios docentes en
una sola operación. Todas las filas se validan ANTES de escribir en la BD:
si dos filas pierdan el mismo día/hora para el mismo docente (o el mismo
ambiente preferido), no se guarda nada y se devuelven los errores.

Después de guardar se regenera la asignación automática; el generador ya
garantiza que ningún docente ni ambiente quede con dos clases simultáneas.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import db, scheduler

DIAS_FORM = ["Lunes", "Martes", "Miércoles", "Jueves", "Sábado"]


def _a_minutos(hhmm: str) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _solapan(a: Tuple[str, str], b: Tuple[str, str]) -> bool:
    return max(_a_minutos(a[0]), _a_minutos(b[0])) < min(_a_minutos(a[1]), _a_minutos(b[1]))


def _hora_valida(valor: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(valor or "")))


def _entero(valor, default: int = 0) -> int:
    try:
        return db.normalizar_entero(valor, default=default)
    except ValueError:
        return int(default)


def parsear_filas(form) -> List[Dict]:
    """Lee los campos indexados ``nombre_0``, ``dia_0``, ... del formulario."""
    total = min(max(_entero(form.get("total_filas"), 0), 0), 100)
    filas: List[Dict] = []
    for i in range(total):
        pref = f"_{i}"
        fila = {
            "indice": i + 1,
            "profesor": str(form.get("profesor" + pref, "")).strip(),
            "cedula": str(form.get("cedula" + pref, "")).strip(),
            "email": str(form.get("email" + pref, "")).strip(),
            "condicion": str(form.get("condicion" + pref, "")).strip(),
            "carrera": str(form.get("carrera" + pref, "")).strip(),
            "materia": str(form.get("materia" + pref, "")).strip(),
            "codigo": str(form.get("codigo" + pref, "")).strip(),
            "seccion": str(form.get("seccion" + pref, "")).strip() or "Sección A",
            "semestre": str(form.get("semestre" + pref, "")).strip() or "1er Semestre",
            # Se guardan como texto para que validar_filas() pueda rechazar
            # decimales, exponentes o caracteres antes de escribir en la BD.
            "creditos": form.get("creditos" + pref, ""),
            "bloques": form.get("bloques" + pref, ""),
            "ambiente": str(form.get("ambiente" + pref, "")).strip().upper(),
            "compartidos": bool(form.get("compartidos" + pref)),
            "dias": list(form.getlist("dia" + pref)),
            "hora_inicio": str(form.get("hora_inicio" + pref, "")).strip(),
            "hora_fin": str(form.get("hora_fin" + pref, "")).strip(),
        }
        if any(fila[k] for k in ("profesor", "carrera", "materia", "codigo", "seccion")):
            filas.append(fila)
    return filas


def validar_filas(filas: List[Dict]) -> List[str]:
    """Devuelve mensajes de error; si la lista está vacía se puede guardar."""
    errores: List[str] = []
    if not filas:
        return ["Agregue al menos una materia a la carga."]

    for f in filas:
        n = f["indice"]
        # Normalización estricta de campos numéricos y cédula (nunca se
        # escriben valores inválidos; se reportan por fila).
        try:
            f["cedula"] = db.normalizar_cedula(f.get("cedula", ""))
        except ValueError as exc:
            errores.append(f"Fila {n}: {exc}")
        for campo, opciones in (
            ("creditos", {"minimo": 0, "maximo": 30, "default": 0}),
            ("bloques", {"minimo": 1, "maximo": 7, "default": 1}),
        ):
            try:
                f[campo] = db.normalizar_entero(f.get(campo), **opciones)
            except ValueError as exc:
                errores.append(f"Fila {n}: {campo} — {exc}")
        if not f["profesor"]:
            errores.append(f"Fila {n}: el docente es obligatorio.")
        if not f["carrera"]:
            errores.append(f"Fila {n}: la carrera es obligatoria.")
        if not f["materia"]:
            errores.append(f"Fila {n}: la materia es obligatoria.")
        if not f["dias"]:
            errores.append(f"Fila {n}: seleccione al menos un día.")
        if not _hora_valida(f["hora_inicio"]) or not _hora_valida(f["hora_fin"]):
            errores.append(f"Fila {n}: el rango horario no es válido.")
        elif _a_minutos(f["hora_inicio"]) >= _a_minutos(f["hora_fin"]):
            errores.append(f"Fila {n}: la hora de inicio debe ser menor a la de fin.")

    # Duplicados dentro del propio formulario.
    vistos: Dict[Tuple[str, str, str], int] = {}
    for f in filas:
        clave = (db.normalizar(f["profesor"]), db.normalizar(f["materia"]), db.normalizar(f["seccion"]))
        if clave in vistos:
            errores.append(f"Fila {f['indice']}: la misma materia ya está en la fila {vistos[clave]}.")
        else:
            vistos[clave] = f["indice"]

    # Conflictos entre las filas nuevas.
    for i, a in enumerate(filas):
        for b in filas[i + 1:]:
            if not any(db.normalizar(d) == db.normalizar(e) for d in a["dias"] for e in b["dias"]):
                continue
            if not _solapan((a["hora_inicio"], a["hora_fin"]), (b["hora_inicio"], b["hora_fin"])):
                continue
            if (db.normalizar(a["profesor"]) == db.normalizar(b["profesor"])
                    and not (a["compartidos"] and b["compartidos"])):
                errores.append(
                    f"Conflicto: {a['profesor']} tiene dos materias el mismo día y hora "
                    f"(filas {a['indice']} y {b['indice']}).")
            if (a["ambiente"] and a["ambiente"] == b["ambiente"]
                    and not scheduler.es_compartible(a["ambiente"])):
                errores.append(
                    f"Conflicto: el ambiente {a['ambiente']} se repite en el mismo horario "
                    f"(filas {a['indice']} y {b['indice']}).")

    # Conflictos con reasignaciones MANUALES existentes (son intocables).
    manuales = db.query("SELECT * FROM asignaciones_horario WHERE estado='Manual'")
    profesores = {db.normalizar(p["nombre"]): int(p["id"]) for p in db.query("SELECT id,nombre FROM profesores")}
    salones = {str(s["codigo"]).upper(): int(s["id"]) for s in db.query("SELECT id,codigo FROM salones")}
    for f in filas:
        rango = (f["hora_inicio"], f["hora_fin"])
        pid = profesores.get(db.normalizar(f["profesor"]))
        sid = salones.get(f["ambiente"])
        for a in manuales:
            if not any(db.normalizar(d) == db.normalizar(a["dia"]) for d in f["dias"]):
                continue
            if not _solapan(rango, (a["hora_inicio"], a["hora_fin"])):
                continue
            if pid is not None and int(a["profesor_id"]) == pid:
                errores.append(
                    f"Conflicto: {f['profesor']} ya tiene una clase manual en ese horario "
                    f"(fila {f['indice']}).")
            if sid is not None and int(a["salon_id"]) == sid and not scheduler.es_compartible(f["ambiente"]):
                errores.append(
                    f"Conflicto: el ambiente {f['ambiente']} ya está ocupado manualmente "
                    f"(fila {f['indice']}).")
    return errores


def guardar_filas(filas: List[Dict]) -> Dict:
    """Guarda todas las filas y regenera horarios. Llamar solo tras validar."""
    guardados = {"profesores": 0, "materias": 0, "disponibilidades": 0, "carga": 0}
    for f in filas:
        nombre = f["profesor"].upper()
        prof_id = db.get_or_create("profesores", "nombre", nombre)
        guardados["profesores"] += 1
        cedula = db.normalizar_cedula(f.get("cedula", ""))
        email, condicion = f.get("email", ""), f.get("condicion", "")
        bloques = db.normalizar_entero(f.get("bloques"), minimo=1, maximo=7, default=1)
        if cedula or email or condicion:
            actual = db.query("SELECT cedula,email,condicion FROM profesores WHERE id=?",
                              (prof_id,))
            actual = actual[0] if actual else {}
            db.execute("UPDATE profesores SET cedula=?,email=?,condicion=? WHERE id=?",
                       (cedula or actual.get("cedula", ""),
                        email or actual.get("email", ""),
                        condicion or actual.get("condicion", ""), prof_id))

        filas_car = db.query("SELECT id FROM carreras WHERE nombre=? LIMIT 1", (f["carrera"],))
        if filas_car:
            car_id = int(filas_car[0]["id"])
        else:
            fac_id = db.get_or_create("facultades", "nombre", "Facultad de Ciencias Económicas y Sociales")
            car_id = db.get_or_create("carreras", "nombre", f["carrera"], {"facultad_id": fac_id})

        mat_id = db.get_or_create_materia(f["materia"], car_id, f["codigo"],
                                          f["semestre"], f["creditos"])
        guardados["materias"] += 1

        for dia in f["dias"]:
            existe = db.query("""SELECT id FROM disponibilidades WHERE profesor_id=? AND dia=?
                AND hora_inicio=? AND hora_fin=? LIMIT 1""",
                (prof_id, dia, f["hora_inicio"], f["hora_fin"]))
            if not existe:
                db.execute("""INSERT INTO disponibilidades (profesor_id,dia,hora_inicio,hora_fin)
                              VALUES (?,?,?,?)""", (prof_id, dia, f["hora_inicio"], f["hora_fin"]))
                guardados["disponibilidades"] += 1

        existe_carga = db.query("""SELECT id FROM carga_academica WHERE profesor_id=? AND
            materia_id=? AND seccion=? LIMIT 1""", (prof_id, mat_id, f["seccion"]))
        compartidos = "COMPARTIDOS" if f["compartidos"] else ""
        if existe_carga:
            db.execute("""UPDATE carga_academica SET bloques_semanales=?, salon_preferido=?, compartidos=?
                          WHERE id=?""",
                       (bloques, f["ambiente"], compartidos, existe_carga[0]["id"]))
        else:
            db.execute("""INSERT INTO carga_academica
                (profesor_id,materia_id,seccion,bloques_semanales,salon_preferido,compartidos)
                VALUES (?,?,?,?,?,?)""",
                (prof_id, mat_id, f["seccion"], bloques, f["ambiente"], compartidos))
        guardados["carga"] += 1

    resultado = scheduler.generar_horarios_automaticos()
    return {"guardados": guardados, "asignacion": resultado}
