"""Importadores de datos: plan de estudio (pensum completo) y oferta académica."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from . import db

ROOT = Path(__file__).resolve().parent.parent

# Mapa de hoja del plan de estudio -> nombre canónico de carrera (BD principal)
MAPA_PLAN_CARRERA = {
    "ARRHH_2024": "Administración - Administración de Recursos Humanos",
    "ARMF 2024": "Administración - Recursos Materiales y Financieros",
    "AINF_2024": "Administración - Informática",
    "AMER_2024": "Administración - Mercadeo",
    "AOYS_2024": "Administración - Organización y Sistemas",
    "EDMT_2024": "Educación - Docencia en Matemáticas",
    "EDII_2024": "Educación - Docencia en Idioma Inglés",
    "EINI_2024": "Educación Inicial - Sin Mención",
    "EINT_2024": "Educación Integral - Sin Mención",
}

FACULTAD_POR_CARRERA = {
    "Educación": "Facultad de Ciencias de la Educación",
    "Educación Inicial": "Facultad de Ciencias de la Educación",
    "Educación Integral": "Facultad de Ciencias de la Educación",
}


def _facultad(carrera: str) -> str:
    for prefijo, fac in FACULTAD_POR_CARRERA.items():
        if carrera.startswith(prefijo):
            return fac
    return "Facultad de Ciencias Económicas y Sociales"


def importar_plan_estudio(ruta: Optional[Path] = None) -> Dict[str, int]:
    """Carga el pensum completo desde 'PLAN DE ESTUDIO 2024(1).xlsx'.

    Inserta en planes_estudio y completa el catálogo materias (sin borrar las
    que ya existen por la oferta). Devuelve conteos por hoja.
    """
    import openpyxl

    ruta = ruta or ROOT / "data" / "PLAN DE ESTUDIO 2024(1).xlsx"
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el plan de estudio: {ruta}")

    wb = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    conteos: Dict[str, int] = {}
    total = 0
    for ws in wb.worksheets:
        nombre_carrera = MAPA_PLAN_CARRERA.get(ws.title.strip(), ws.title.strip())
        fac_id = db.get_or_create("facultades", "nombre", _facultad(nombre_carrera))
        car_id = db.get_or_create("carreras", "nombre", nombre_carrera,
                                  {"facultad_id": fac_id})
        filas = list(ws.iter_rows(min_row=1, max_col=8, values_only=True))
        cabecera = next((i for i, r in enumerate(filas)
                         if str(r[0] or "").strip().lower() in ("código", "codigo")), None)
        n = 0
        if cabecera is not None:
            ciclo_actual = "INTRODUCTORIO"
            for fila in filas[cabecera + 1:]:
                codigo = str(fila[0] or "").strip()
                nombre = str(fila[1] or "").strip()
                if not codigo or not nombre:
                    continue
                if codigo.upper().startswith("CICLO"):
                    ciclo_actual = nombre.upper() if nombre else codigo.upper()
                    continue
                # Descarta totales / filas de texto repetido
                if len(codigo) > 25 or len(nombre) > 120:
                    continue
                try:
                    uc = db.normalizar_entero(fila[2], minimo=0, maximo=30, default=0)
                    ht = db.normalizar_entero(fila[3], minimo=0, default=0)
                    hp = db.normalizar_entero(fila[4], minimo=0, default=0)
                except ValueError:
                    uc = ht = hp = 0
                opc = str(fila[5] or "").strip()
                tecnica = str(fila[6] or "").strip()
                prel = str(fila[7] or "").strip() if fila[7] else ""

                existe = db.query(
                    "SELECT id FROM planes_estudio WHERE carrera_id=? AND codigo=? AND nombre=? LIMIT 1",
                    (car_id, codigo, nombre))
                datos_plan = (car_id, codigo, nombre, uc, ht, hp, ciclo_actual, opc, tecnica, prel)
                if existe:
                    db.execute("""UPDATE planes_estudio SET unidad_credito=?, horas_teoria=?,
                                  horas_practica=?, ciclo=?, opcionalidad=?, tecnica=?, prelacion=?
                                  WHERE id=?""",
                               (uc, ht, hp, ciclo_actual, opc, tecnica, prel, existe[0]["id"]))
                else:
                    db.execute("""INSERT INTO planes_estudio
                        (carrera_id,codigo,nombre,unidad_credito,horas_teoria,horas_practica,ciclo,opcionalidad,tecnica,prelacion)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", datos_plan)
                # El pensum también alimenta el catálogo de materias de la UI.
                db.get_or_create_materia(nombre, car_id, codigo, ciclo_actual, uc)
                n += 1
        conteos[ws.title] = n
        total += n
    wb.close()
    return {"total": total, **conteos}


def importar_oferta_excel(ruta: Path) -> Dict[str, int]:
    """Importa la oferta 2026-2 (9 hojas) a carga_academica.

    Reutiliza la lógica del parser original pero escribe en la capa unificada.
    """
    import openpyxl

    if not ruta.exists():
        raise FileNotFoundError(ruta)
    wb = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    insertados = 0
    errores: List[str] = []
    for ws in wb.worksheets:
        filas = list(ws.iter_rows(values_only=True))
        cab = next((i for i, r in enumerate(filas)
                    if any("NOMBRES Y APELLIDOS" in str(c or "").upper() for c in r)), None)
        if cab is None:
            continue
        headers = [str(c or "").strip().upper() for c in filas[cab]]

        def col(*claves: str) -> Optional[int]:
            for i, h in enumerate(headers):
                if all(k in h for k in claves):
                    return i
            return None

        i_prof = col("NOMBRES", "APELLIDOS")
        i_cod = col("CÓDIGO") or col("CODIGO")
        i_mat = col("NOMBRE", "CURSO")
        i_carr = col("CARRERA")
        i_menc = col("MENCIÓN") or col("MENCION")
        i_sec = col("SECCIÓN") or col("SECCION")
        i_dia = col("DÍA") or col("DIA")
        i_hora = col("HORARIO")
        i_amb = col("AMBIENTE") or col("AULA")
        i_uc = col("UNIDAD", "CRÉDITO") or col("UNIDAD", "CREDITO")
        i_comp = col("COMPARTID")

        for fila in filas[cab + 1:]:
            def val(i: Optional[int]) -> str:
                return str(fila[i] or "").strip() if i is not None and i < len(fila) else ""

            profesor = val(i_prof)
            materia = val(i_mat)
            if not profesor or not materia:
                continue
            carrera = val(i_carr)
            mencion = val(i_menc)
            if carrera and mencion and db.normalizar(mencion) not in (db.normalizar(carrera), "SIN MENCIÓN"):
                nombre_carrera = f"{carrera} - {mencion}"
            else:
                nombre_carrera = carrera or mencion or ws.title
            # Evita el bug histórico de carreras concatenadas con '/'
            if "/" in nombre_carrera:
                nombre_carrera = nombre_carrera.split("/")[0].strip()

            try:
                fac_id = db.get_or_create("facultades", "nombre", _facultad(nombre_carrera))
                car_id = db.get_or_create("carreras", "nombre", nombre_carrera, {"facultad_id": fac_id})
                prof_id = db.get_or_create("profesores", "nombre", profesor.upper())
                try:
                    creditos = db.normalizar_entero(val(i_uc), minimo=0, maximo=30, default=0)
                except ValueError:
                    creditos = 0
                mat_id = db.get_or_create_materia(materia, car_id, val(i_cod), "OFERTA 2026-2", creditos)
                seccion = val(i_sec) or "Sección A"
                dia = val(i_dia)
                hora = val(i_hora)
                inicio, fin = _parsear_hora(hora)
                compartidos = val(i_comp)
                exists = db.query(
                    "SELECT id FROM carga_academica WHERE profesor_id=? AND materia_id=? AND seccion=? LIMIT 1",
                    (prof_id, mat_id, seccion))
                if exists:
                    cid = exists[0]["id"]
                    db.execute("""UPDATE carga_academica SET dia_fijo=?, hora_inicio_fija=?,
                                  hora_fin_fija=?, salon_preferido=?, compartidos=? WHERE id=?""",
                               (dia, inicio, fin, val(i_amb), compartidos, cid))
                else:
                    db.execute("""INSERT INTO carga_academica
                        (profesor_id,materia_id,seccion,bloques_semanales,salon_preferido,
                         dia_fijo,hora_inicio_fija,hora_fin_fija,compartidos)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (prof_id, mat_id, seccion, 1, val(i_amb), dia, inicio, fin, compartidos))
                # Disponibilidad derivada de la hora fija declarada.
                if dia and inicio and fin:
                    dia_norm = next((d for d in db.DIAS_ORDEN if db.normalizar(d) == db.normalizar(dia)), dia)
                    disp = db.query("""SELECT id FROM disponibilidades WHERE profesor_id=? AND dia=?
                                       AND hora_inicio=? AND hora_fin=? LIMIT 1""",
                                    (prof_id, dia_norm, inicio, fin))
                    if not disp:
                        db.execute("""INSERT INTO disponibilidades (profesor_id,dia,hora_inicio,hora_fin)
                                      VALUES (?,?,?,?)""", (prof_id, dia_norm, inicio, fin))
                insertados += 1
            except Exception as exc:  # pragma: no cover
                errores.append(f"{ws.title}: {exc}")
    wb.close()
    return {"insertados": insertados, "errores": errores[:20]}


def _parsear_hora(texto: str):
    """'10:50 a 12:10' -> ('10:50','12:10')."""
    m = re.search(r"(\d{1,2}:\d{2})\s*(?:a|-|a las)\s*(\d{1,2}:\d{2})", str(texto or ""), re.I)
    if not m:
        return "", ""
    def norm(t: str) -> str:
        h, mi = t.split(":")
        return f"{int(h):02d}:{mi}"
    return norm(m.group(1)), norm(m.group(2))
