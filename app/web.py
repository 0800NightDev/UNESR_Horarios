"""Aplicación Flask: API + vistas web (misma interfaz del sistema original)."""
from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

from . import (carga_multiple, constantes, db, importers, pdf_importers,
               queries, reports, scheduler)

ROOT = Path(__file__).resolve().parent.parent
# Carpeta escribible: en modo normal es <proyecto>/uploads; dentro del .exe
# es la carpeta de datos del usuario (no _MEIPASS, que es solo lectura).
UPLOADS = db.DATA_DIR.parent / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder=None)
app.secret_key = db.configuracion().get("FLASK_SECRET_KEY", "unesr_horarios_clave_local")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


@app.before_request
def _init():
    db.init_db()


@app.context_processor
def _globales():
    return {
        "profesores_global": queries.profesores(),
        "carreras_global": [{"id": c["id"], "nombre": c["nombre"]} for c in queries.carreras_con_pensum()],
        "backend": db.backend_activo(),
        "dias_form": carga_multiple.DIAS_FORM,
        "condiciones_docente": constantes.CONDICIONES_DOCENTE,
    }


@app.route("/logo.png")
def logo():
    return send_file(db.RECURSOS / "assets" / "UNESR.png", mimetype="image/png")


@app.route("/")
def index():
    stats = db.estadisticas()
    conflictos = scheduler.conflictos()
    stats["conflictos"] = len(conflictos)
    return render_template("index.html", stats=stats, conf=None,
                           conflictos=conflictos)


# ── Dashboard ──────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    return redirect(url_for("index"))


# ── Nueva carga ────────────────────────────────────────────────────────
@app.route("/nueva-carga", methods=["GET", "POST"])
def nueva_carga():
    opciones = queries.opciones_carga()
    if request.method == "POST":
        datos = request.form
        errores = []
        nombre = (datos.get("profesor") or "").strip()
        materia = (datos.get("materia") or "").strip()
        carrera = (datos.get("carrera") or "").strip()
        if not nombre:
            errores.append("El nombre del docente es obligatorio.")
        if not materia:
            errores.append("La materia es obligatoria.")
        if not carrera:
            errores.append("La carrera es obligatoria.")
        dias = request.form.getlist("dia")
        inicio = (datos.get("hora_inicio") or "").strip()
        fin = (datos.get("hora_fin") or "").strip()
        if not dias:
            errores.append("Seleccione al menos un día de disponibilidad.")
        if not _hora_valida(inicio) or not _hora_valida(fin) or inicio >= fin:
            errores.append("El rango horario no es válido.")
        if errores:
            for e in errores:
                flash(e, "danger")
            return render_template("nueva_carga.html", opciones=opciones, form=datos,
                                   filas_multi=session.get("filas_multi", []))
        try:
            cedula = db.normalizar_cedula(datos.get("cedula"))
            creditos = db.normalizar_entero(datos.get("creditos"), minimo=0, maximo=30, default=0)
            bloques = db.normalizar_entero(datos.get("bloques"), minimo=1, maximo=7, default=1)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("nueva_carga.html", opciones=opciones, form=datos,
                                   filas_multi=session.get("filas_multi", []))

        try:
            prof_id = db.get_or_create("profesores", "nombre", nombre.upper())
            condicion = (datos.get("condicion") or "").strip()
            if cedula or condicion:
                actual = db.query("SELECT cedula,condicion FROM profesores WHERE id=?",
                                  (prof_id,))
                actual = actual[0] if actual else {}
                db.execute("UPDATE profesores SET cedula=?, condicion=? WHERE id=?",
                           (cedula or actual.get("cedula", ""),
                            condicion or actual.get("condicion", ""), prof_id))
            filas_car = db.query("SELECT id FROM carreras WHERE nombre=? LIMIT 1", (carrera,))
            if not filas_car:
                fac_id = db.get_or_create("facultades", "nombre", "Facultad de Ciencias Económicas y Sociales")
                car_id = db.get_or_create("carreras", "nombre", carrera, {"facultad_id": fac_id})
            else:
                car_id = filas_car[0]["id"]
            mat_id = db.get_or_create_materia(materia, car_id, datos.get("codigo", ""),
                                              datos.get("semestre", "1er Semestre"), creditos)
            for dia in dias:
                existe = db.query("""SELECT id FROM disponibilidades WHERE profesor_id=? AND dia=?
                    AND hora_inicio=? AND hora_fin=? LIMIT 1""", (prof_id, dia, inicio, fin))
                if not existe:
                    db.execute("""INSERT INTO disponibilidades (profesor_id,dia,hora_inicio,hora_fin)
                                  VALUES (?,?,?,?)""", (prof_id, dia, inicio, fin))
            seccion = (datos.get("seccion") or "").strip() or "Sección A"
            existe_carga = db.query("""SELECT id FROM carga_academica WHERE profesor_id=? AND
                materia_id=? AND seccion=? LIMIT 1""", (prof_id, mat_id, seccion))
            if existe_carga:
                cid = existe_carga[0]["id"]
                db.execute("UPDATE carga_academica SET bloques_semanales=?, salon_preferido=? WHERE id=?",
                           (bloques, datos.get("ambiente", ""), cid))
            else:
                db.execute("""INSERT INTO carga_academica
                    (profesor_id,materia_id,seccion,bloques_semanales,salon_preferido)
                    VALUES (?,?,?,?,?)""",
                    (prof_id, mat_id, seccion, bloques, datos.get("ambiente", "")))
            resultado = scheduler.generar_horarios_automaticos()
            flash(f"Ficha guardada. {resultado['asignados']} clases asignadas automáticamente.", "success")
            for m in resultado["mensajes"][:5]:
                if not m.startswith("OK"):
                    flash(m, "warning")
            return redirect(url_for("horarios", carrera_id=car_id))
        except Exception as exc:
            flash(f"Error al guardar: {exc}", "danger")
    return render_template("nueva_carga.html", opciones=opciones, form={},
                           filas_multi=session.pop("filas_multi", []))


@app.route("/carga-multiple", methods=["POST"])
def carga_multiple_route():
    """Registra varias materias en una sola carga, sin conflictos."""
    filas = carga_multiple.parsear_filas(request.form)
    errores = carga_multiple.validar_filas(filas)
    if errores:
        for e in errores:
            flash(e, "danger")
        session["filas_multi"] = filas
        return redirect(url_for("nueva_carga", _anchor="multiple"))

    try:
        res = carga_multiple.guardar_filas(filas)
        g = res["guardados"]
        asig = res["asignacion"]
        flash(f"Carga múltiple guardada: {g['carga']} ficha(s), "
              f"{g['disponibilidades']} disponibilidad(es). "
              f"{asig['asignados']} clases asignadas, {asig['pendientes']} pendientes.",
              "success" if not asig["pendientes"] else "warning")
        for m in asig["mensajes"][:8]:
            if not m.startswith("OK"):
                flash(m, "info")
        session.pop("filas_multi", None)
        carrera = filas[0]["carrera"]
        fila_car = db.query("SELECT id FROM carreras WHERE nombre=? LIMIT 1", (carrera,))
        if fila_car:
            return redirect(url_for("horarios", carrera_id=fila_car[0]["id"]))
    except Exception as exc:
        flash(f"Error al guardar la carga múltiple: {exc}", "danger")
        session["filas_multi"] = filas
    return redirect(url_for("nueva_carga", _anchor="multiple"))


@app.route("/importar", methods=["POST"])
def importar():
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Seleccione un archivo.", "danger")
        return redirect(url_for("nueva_carga"))
    ext = Path(secure_filename(archivo.filename)).suffix.lower()
    if ext not in (".xlsx", ".txt", ".csv"):
        flash("Formato no soportado. Use .xlsx, .txt o .csv.", "danger")
        return redirect(url_for("nueva_carga"))
    destino = UPLOADS / secure_filename(archivo.filename)
    archivo.save(destino)
    try:
        resultado = importers.importar_oferta_excel(destino)
        scheduler.generar_horarios_automaticos()
        flash(f"Importadas {resultado['insertados']} filas de oferta académica.", "success")
    except Exception as exc:
        flash(f"Error al importar: {exc}", "danger")
    return redirect(url_for("horarios"))


@app.route("/cargar-plan-estudio", methods=["POST"])
def cargar_plan():
    try:
        r = importers.importar_plan_estudio()
        flash(f"Plan de estudio cargado: {r['total']} materias en {len(r)-1} carreras.", "success")
    except Exception as exc:
        flash(f"Error al cargar el plan de estudio: {exc}", "danger")
    return redirect(url_for("carreras"))


@app.route("/cargar-pnfs", methods=["POST"])
def cargar_pnfs():
    """Carga el pensum de los 3 PNF desde los PDFs oficiales."""
    try:
        r = pdf_importers.importar_pnfs(ruta_uploads=UPLOADS)
        total = sum(r.values())
        flash(f"PNF cargados: {total} materias "
              f"(Fisioterapia {r.get('fisioterapia', 0)}, "
              f"Veterinaria {r.get('veterinaria', 0)}, "
              f"Agroecología {r.get('agroecologia', 0)}).", "success")
    except Exception as exc:
        flash(f"Error al cargar los PNF: {exc}", "danger")
    return redirect(url_for("carreras"))


# ── Horarios (por carrera y día; SIN oferta general) ────────────────────
@app.route("/horarios")
def horarios():
    carrera_id = request.args.get("carrera_id", type=int)
    dia = request.args.get("dia") or None
    profesor_id = request.args.get("profesor_id", type=int)
    carreras = queries.carreras_con_pensum()
    if carrera_id and not any(c["id"] == carrera_id for c in carreras):
        flash("La carrera seleccionada no existe.", "warning")
        carrera_id = None
    filas = queries.oferta(carrera_id=carrera_id, dia=dia, profesor_id=profesor_id)
    por_dia = None
    cuadro = None
    estado = None
    carrera = None
    conflictos_por_id: Dict[int, str] = {}
    conflictos_carrera: List[Dict] = []
    for conflicto in scheduler.conflictos():
        for fila in (conflicto["a"], conflicto["b"]):
            cid = int(fila["id"])
            motivos = conflictos_por_id.setdefault(cid, set())
            motivos.add(conflicto["motivo"])
        if carrera_id and carrera_id in (conflicto.get("carrera_id"),
                                         conflicto.get("b_carrera_id")):
            conflictos_carrera.append(conflicto)
    conflictos_por_id = {cid: " y ".join(sorted(v))
                         for cid, v in conflictos_por_id.items()}
    if carrera_id:
        carrera = next(c for c in carreras if c["id"] == carrera_id)
        por_dia = queries.horario_por_carrera(carrera_id)
        cuadro = queries.cuadro_horario(carrera_id, dia=dia, profesor_id=profesor_id)
        estado = queries.estado_carga_carrera(carrera_id)
    return render_template("horarios.html", carreras=carreras, carrera_id=carrera_id,
                           carrera=carrera, dia=dia, profesor_id=profesor_id,
                           filas=filas, por_dia=por_dia, cuadro=cuadro, estado=estado,
                           profesores=queries.profesores(),
                           ambientes=db.ordenar_ambientes(
                               db.query("SELECT id,codigo FROM salones")),
                           conflictos_por_id=conflictos_por_id,
                           conflictos_carrera=conflictos_carrera,
                           dias=db.DIAS_ORDEN)


@app.route("/oferta-general")
def oferta_general():
    return redirect(url_for("horarios"))


@app.route("/asignar-auto", methods=["POST"])
def asignar_auto():
    resultado = scheduler.generar_horarios_automaticos()
    flash(f"Asignación automática: {resultado['asignados']} clases; "
          f"{resultado['pendientes']} pendientes.", "success" if not resultado["pendientes"] else "warning")
    for m in resultado["mensajes"][:8]:
        if not m.startswith("OK"):
            flash(m, "info")
    for d in resultado.get("detalles", [])[:5]:
        flash(d["detalle"], "info")
    return redirect(url_for("horarios"))


@app.route("/reasignar/<int:aid>", methods=["POST"])
def reasignar(aid: int):
    try:
        salon_id = int(request.form["salon_id"])
        dia = request.form["dia"]
        ini, fin = request.form["hora_inicio"], request.form["hora_fin"]
        ok, msg = scheduler.validar_cambio_manual(aid, salon_id, dia, ini, fin)
        if not ok:
            flash(msg, "danger")
        else:
            db.execute("""UPDATE asignaciones_horario SET salon_id=?,dia=?,hora_inicio=?,hora_fin=?,estado='Manual'
                          WHERE id=?""", (salon_id, dia, ini, fin, aid))
            flash("Ambiente reasignado correctamente.", "success")
    except Exception as exc:
        flash(f"Datos inválidos: {exc}", "danger")
    return redirect(request.referrer or url_for("horarios"))


# ── Ambientes ──────────────────────────────────────────────────────────
@app.route("/ambientes", methods=["GET", "POST"])
def ambientes():
    if request.method == "POST":
        try:
            capacidad = db.normalizar_entero(
                request.form.get("capacidad"), minimo=0, maximo=1000, default=45)
        except ValueError as exc:
            flash(str(exc), "danger")
            capacidad = None
        accion = request.form.get("accion")
        if accion == "crear" and capacidad is not None:
            codigo = (request.form.get("codigo") or "").strip().upper()
            if not codigo:
                flash("El código del ambiente es obligatorio.", "danger")
            else:
                existe = db.query("SELECT id FROM salones WHERE codigo=?", (codigo,))
                if existe:
                    flash("Ese ambiente ya existe.", "warning")
                else:
                    db.execute("INSERT INTO salones (codigo,capacidad,edificio,es_camillas) VALUES (?,?,?,?)",
                               (codigo, capacidad,
                                request.form.get("edificio", "Núcleo Palo Verde"),
                                1 if request.form.get("es_camillas") else 0))
                    flash("Ambiente creado.", "success")
        elif accion == "eliminar":
            try:
                db.execute("DELETE FROM salones WHERE id=?", (int(request.form["id"]),))
                flash("Ambiente eliminado.", "success")
            except Exception:
                flash("No se puede eliminar: tiene clases asignadas.", "danger")
        elif accion == "editar" and capacidad is not None:
            try:
                db.execute("UPDATE salones SET capacidad=?, edificio=?, es_camillas=? WHERE id=?",
                           (capacidad, request.form.get("edificio", ""),
                            1 if request.form.get("es_camillas") else 0, int(request.form["id"])))
                flash("Ambiente actualizado.", "success")
            except Exception as exc:
                flash(f"Error: {exc}", "danger")
    filas = db.query("""SELECT s.*, (SELECT COUNT(*) FROM asignaciones_horario a WHERE a.salon_id=s.id) AS usos
                         FROM salones s""")
    # Piso 1 → Piso 4 → Satelitales → Planta Baja → Virtuales → Otros,
    # con numeración natural (P1-02 antes que P1-10) dentro de cada piso.
    grupos = db.agrupar_ambientes(filas)
    return render_template("ambientes.html", salones=filas, grupos=grupos,
                           sectores=db.SECTORES_AMBIENTE)


# ── Docentes ───────────────────────────────────────────────────────────
@app.route("/docentes", methods=["GET", "POST"])
def docentes():
    if request.method == "POST":
        accion = request.form.get("accion")
        try:
            pid = int(request.form["id"])
            if accion == "editar":
                cedula = db.normalizar_cedula(request.form.get("cedula", ""))
                db.execute("UPDATE profesores SET nombre=?, cedula=?, email=?, condicion=?, mencion=? WHERE id=?",
                           ((request.form.get("nombre") or "").strip().upper(),
                            cedula, request.form.get("email", ""),
                            request.form.get("condicion", "Contratado/a"),
                            request.form.get("mencion", ""), pid))
                flash("Docente actualizado.", "success")
            elif accion == "eliminar":
                db.execute("DELETE FROM profesores WHERE id=?", (pid,))
                flash("Docente eliminado.", "success")
        except Exception as exc:
            flash(f"Error: {exc}", "danger")
    profesores = queries.profesores()
    condiciones = sorted(
        set(constantes.CONDICIONES_DOCENTE)
        | {(p["condicion"] or "").strip() for p in profesores if (p["condicion"] or "").strip()},
        key=lambda s: (s.casefold(), s))
    return render_template("docentes.html", profesores=profesores, condiciones=condiciones)


@app.route("/docente/<int:pid>")
def docente_detalle(pid: int):
    prof = db.query("SELECT * FROM profesores WHERE id=?", (pid,))
    if not prof:
        flash("Docente no encontrado.", "warning")
        return redirect(url_for("docentes"))
    return render_template("calendario_profesores.html", profesor=prof[0],
                           asignaciones=queries.asignaciones_profesor(pid),
                           dias=db.DIAS_ORDEN)


# ── Carreras (pensum completo, sin recortes) ────────────────────────────
@app.route("/carreras")
def carreras():
    return render_template("carreras.html", carreras=queries.carreras_con_pensum())


@app.route("/api/carreras/<int:carrera_id>/horario")
def api_horario_carrera(carrera_id: int):
    if not db.query("SELECT id FROM carreras WHERE id=?", (carrera_id,)):
        return jsonify({"error": "Carrera no encontrada"}), 404
    return jsonify({dia: filas for dia, filas in queries.horario_por_carrera(carrera_id).items()})


# ── Reportes ───────────────────────────────────────────────────────────
@app.route("/reporte")
def reporte():
    carrera_id = request.args.get("carrera_id", type=int)
    if carrera_id:
        if not db.query("SELECT id FROM carreras WHERE id=?", (carrera_id,)):
            flash("Carrera inválida; se generó el reporte completo.", "warning")
            carrera_id = None
    html = reports.reporte_por_carrera(carrera_id, incluir_profesores=request.args.get("profesores") == "1")
    if request.args.get("descargar") == "1":
        return Response(html, mimetype="text/html", headers={
            "Content-Disposition": "attachment; filename=Horario_Por_Carrera.html"})
    return html


@app.route("/reporte-imprimible")
def reporte_imprimible():
    return redirect(url_for("reporte", **request.args.to_dict()))


# ── Utilidades del sistema ─────────────────────────────────────────────
@app.route("/vaciar", methods=["POST"])
def vaciar():
    db.limpiar_carga_academica()
    flash("Clases, carga académica y disponibilidades eliminadas. Se conservaron "
          "carreras, docentes, materias, pensum y ambientes.", "success")
    return redirect(url_for("index"))


@app.route("/api/estadisticas")
def api_stats():
    return jsonify(db.estadisticas())


def _hora_valida(valor: str) -> bool:
    try:
        h, m = valor.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        return False


def _entero(valor, default=0) -> int:
    """Compatibilidad: numéricos siempre enteros reales, nunca texto suelto."""
    try:
        return db.normalizar_entero(valor, default=default)
    except ValueError:
        return int(default)
