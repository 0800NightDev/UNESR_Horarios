"""Importadores del pensum de los 3 PNF a partir de sus PDFs oficiales.

Fuentes (en ``uploads/``):
  * ``DOCUMENTO_RECTOR_FISIOTERAPIA_(1).pdf``  -> PNF Fisioterapia
  * ``Malla_Curricular_-_PNF_en_Medicina_Veterinaria.pdf`` -> PNF Veterinaria
  * ``Récord_Académico_-_Mercedes_Mireya_Rodríguez_Muñoz.pdf`` -> PNF Agroecología

El texto se extrae con ``pdftotext -layout`` (poppler). Si no está instalado,
se usa una heurística sobre el nombre del archivo para no romper el arranque.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import db

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "uploads"

FACULTAD_PNF = "Programas Nacionales de Formación (PNF)"

# ( patrón de nombre de archivo, nombre canónico de la carrera )
PNFS = (
    ("DOCUMENTO_RECTOR_FISIOTERAPIA", "PNF Fisioterapia", "fisioterapia"),
    ("Malla_Curricular_-_PNF_en_Medicina_Veterinaria", "PNF Veterinaria", "veterinaria"),
    ("Récord_Académico", "PNF Agroecología", "agroecologia"),
)

Unidad = Tuple[str, int, str, int, int]  # codigo, uc, ciclo, horas_teoria, horas_practica


def _limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


def _sin_acentos(texto: str) -> str:
    """Comparación tolerante: los PDFs tienen acentos en NFC y en NFD."""
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "")
                   if not unicodedata.combining(c))


def _binario_pdftotext() -> Optional[str]:
    """Usa el pdftotext incluido en el bundle, o el del PATH del sistema."""
    incluido = db.RECURSOS / "bin" / ("pdftotext.exe" if sys.platform == "win32" else "pdftotext")
    if incluido.exists():
        return str(incluido)
    return shutil.which("pdftotext")


def _extraer_texto(pdf: Path) -> str:
    """Extrae el texto con pdftotext -layout (conserva el diseño de tablas)."""
    binario = _binario_pdftotext()
    if not binario:
        raise RuntimeError(
            "Se necesita poppler-utils (pdftotext) para importar los PNF. "
            "Instala con: sudo apt-get install poppler-utils  (Linux) o "
            "poppler-for-windows y agrega bin/ al PATH  (Windows)."
        )
    with tempfile.TemporaryDirectory(prefix="unesr_pdf_") as tmp:
        salida = Path(tmp) / "plan.txt"
        subprocess.run([binario, "-layout", str(pdf), str(salida)],
                       check=True, capture_output=True)
        return salida.read_text(encoding="utf-8", errors="replace")


# ─────────────────────────── Parseo específico por PNF ───────────────────────────

def _parsear_fisioterapia(texto: str) -> Dict[str, Unidad]:
    """Documento rector: 'Unidad Curricular' + fila HTE/HTA/HTI/UC/COD."""
    lineas = texto.splitlines()
    encontradas: Dict[str, Unidad] = {}

    # Trayecto inicial: 'UNIDAD DE EXPLORACIÓN: X' + 'Trayecto/UC/Código:'
    for i, linea in enumerate(lineas):
        m_nombre = re.search(r"UNIDAD\s+DE\s+EXPLORACI[OÓ]N\s*:\s*(.+)", linea, re.I)
        if not m_nombre:
            continue
        bloque = " ".join(lineas[i:i + 5])
        tray = re.search(r"Trayecto\s*:\s*(\w+)", bloque, re.I)
        uc = re.search(r"UC\s*:\s*(\d+)", bloque, re.I)
        cod = re.search(r"(?:C[OÓ]DIGO|COD)\s*:\s*([A-Z0-9]{4,})", bloque, re.I)
        if not (uc and cod):
            continue
        hte = re.search(r"HTE\s*:\s*(\d+)", bloque, re.I)
        hta = re.search(r"HTA\s*:\s*(\d+)", bloque, re.I)
        encontradas[cod.group(1)] = (
            _limpiar(m_nombre.group(1)), int(uc.group(1)),
            f"Trayecto {tray.group(1) if tray else 'Inicial'}",
            int(hte.group(1)) if hte else 0, int(hta.group(1)) if hta else 0,
        )

    # Trayectos I-IV: 'Unidad Curricular: X' + 4 números + código (COD:)
    patron_valores = re.compile(
        r"\b(\d{1,4})\s+(\d{1,4})\s+(\d{1,4})\s+(\d{1,2})\s+([A-Z]{2,8}\d{2,6})\b")
    for i, linea in enumerate(lineas):
        m_nombre = re.search(r"Unidad\s+Curricular\s*:\s*(.+)", linea, re.I)
        if not m_nombre:
            continue
        bloque = " ".join(lineas[i:i + 5])
        tray = re.search(r"Trayecto\s*:\s*([IVX]+|Inicial)", bloque, re.I)
        valores = patron_valores.search(bloque)
        if not valores:
            continue
        hte, hta, _hti, uc, cod = valores.groups()
        nombre = _limpiar(m_nombre.group(1))
        # El área/tramo distingue unidades con el mismo nombre genérico.
        m_area = re.search(r"[ÁA]rea:\s*(.+?)(?=\s+H(?:TE|TA|TI):|\s+UC:|$)", bloque, re.I)
        if m_area:
            area = _limpiar(re.sub(r"\b(Tramo:?\s*[IVX0-9]+|Área:)\b", "", m_area.group(1), flags=re.I))
            area = re.sub(r"^\s*(y|de|del|la|el)\s+", "", area, flags=re.I)
            if 3 < len(area) < 70:
                nombre = f"{nombre} ({area})"
        encontradas[cod] = (nombre, int(uc),
                            f"Trayecto {tray.group(1)}" if tray else "",
                            int(hte), int(hta))
    return encontradas


# El layout de la malla mezcla columnas en algunas filas; estos nombres se
# toman literalmente del PDF y corrigen el texto resultante del parseo.
CORRECCIONES_VETERINARIA = {
    "MVCBT001": ("Ciencias Básicas y Tecnología: Matemática, Química, Biología y TIC", 0),
    "MVMV410": ("Microbiología Veterinaria", 3),
    "MVARC125": ("Actividad Cultural Recreativa y Deportiva", 1),
    "MVRAI250": ("Reproducción Animal I", 2),
    "MVENI350": ("Enfermedades Infecciosas", 2),
    "MVPRP370": ("Prevención y Riesgo en la Profesión", 2),
    "MVGA140": ("Gestión Agropecuaria", 2),
    "MVIPL450": ("Industria de Productos Lácteos", 3),
    "MVELIIIFS": ("Electiva III: Fauna Silvestre", 1),
    "MVPEP415": ("Parasitología y Enfermedades Parasitarias", 3),
}


def _parsear_veterinaria(texto: str) -> Dict[str, Unidad]:
    """Malla tabular: códigos MV..., créditos y horas por tramo/trayecto."""
    lineas = texto.splitlines()
    trayecto = "Inicial"
    encontradas: Dict[str, Unidad] = {}

    cod_en_linea = re.compile(r"\bMV[A-Z0-9]{3,10}\b")

    def es_fin(j: int) -> bool:
        s = lineas[j]
        return (bool(cod_en_linea.search(s))
                or "TOTAL" in s.upper()
                or "TRAYECTO" in s.upper()
                or "Malla Curricular" in s)

    i = 0
    while i < len(lineas):
        linea = lineas[i]
        m_tray = re.search(r"TRAYECTO\s+([IVX]+|INICIAL)", linea, re.I)
        if m_tray:
            trayecto = m_tray.group(1).upper() if len(m_tray.group(1)) > 1 else m_tray.group(1)
            i += 1
            continue
        m_cod = re.match(r"^\s*(MV[A-Z0-9]{3,10})\s+(.*)$", linea)
        if not m_cod:
            i += 1
            continue

        codigo, resto = m_cod.group(1), m_cod.group(2)
        # El nombre a veces empieza en la línea anterior (layout de dos columnas).
        # Solo se acepta si tiene una palabra larga y no es encabezado de tabla.
        if i > 0:
            anterior = lineas[i - 1].strip()
            encabezado = any(k in anterior.upper() for k in (
                "CÓDIGO", "CODIGO", "PERIOD", "CRÉDITO", "CREDITO", "TOTAL",
                "TRAYECTO", "PÁGINA", "PAGE", "SEMANALES", "ACADÉMIC", "Malla"))
            palabra_larga = any(len(p) >= 6 for p in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]+", anterior))
            if (palabra_larga and not encabezado and not cod_en_linea.search(anterior)
                    and not re.search(r"\d", anterior)):
                resto = anterior + " " + resto
        j = i + 1
        while j < len(lineas) and not es_fin(j):
            sig = lineas[j].strip()
            # Código partido por el layout (p. ej. MVANAM70 + 5, MVMCA + 390).
            if re.match(r"^\d{1,3}$", sig):
                termina_en_digitos = bool(re.search(r"\d$", codigo))
                un_digito = len(sig) == 1
                sin_digitos = not re.search(r"\d", codigo)
                if sin_digitos or (termina_en_digitos and un_digito):
                    codigo += sig
                    j += 1
                    continue
            # Una línea sin números seguida de código MV es la continuación del
            # NOMBRE de la fila siguiente, no de esta.
            siguiente_es_codigo = (j + 1 < len(lineas)
                                   and cod_en_linea.search(lineas[j + 1]))
            if not siguiente_es_codigo or re.search(r"\d", sig):
                resto += " " + sig
            j += 1

        # Los tokens numéricos son créditos/horas; el resto forma el nombre.
        nums = re.findall(r"\b\d+\b", resto)
        nombre = _limpiar(" ".join(p for p in resto.split() if not p.isdigit()))
        uc = int(nums[0]) if nums else 0
        # Las horasDoc = columna HAS/HAP si están presentes.
        hte = int(nums[2]) if len(nums) > 2 and int(nums[2]) < 500 else 0
        hta = int(nums[3]) if len(nums) > 3 and int(nums[3]) < 500 else 0
        if nombre and len(nombre) > 3 and codigo not in encontradas:
            encontradas[codigo] = (nombre, uc, f"Trayecto {trayecto}", hte, hta)
        i = j

    # Renombra el código partido 'MVMCA' + '390' y aplica correcciones.
    if "MVMCA" in encontradas:
        nombre, uc, ciclo, hte, hta = encontradas.pop("MVMCA")
        encontradas["MVMCA390"] = ("Medicina de Cerdos y Aves", uc, ciclo, hte, hta)
    for codigo, (nombre, uc) in CORRECCIONES_VETERINARIA.items():
        ciclo = encontradas.get(codigo, (None, None, "Trayecto II"))[2]
        encontradas[codigo] = (nombre, uc, ciclo, 0, 0)
    return encontradas


def _parsear_agroecologia(texto: str) -> Dict[str, Unidad]:
    """Récord académico: PNFAGT..., nombre y U.C. (deduplicaSecundaria)."""
    encontradas: Dict[str, Unidad] = {}
    lineas = texto.splitlines()
    for idx, linea in enumerate(lineas):
        m_cod = re.search(r"\b(PNFAG[A-Z0-9-]+)\s+(.+)$", linea)
        if not m_cod:
            continue
        codigo = m_cod.group(1)
        if codigo.endswith("-") or re.search(r"-\d+$", codigo):
            continue
        resto = m_cod.group(2)
        if "secundaria" in resto.lower():
            continue
        m_tray = re.match(r"^\s*(\d)", linea)
        tray = m_tray.group(1) if m_tray else "0"
        nums = re.findall(r"\b(\d+)\b", resto)
        if not nums:
            continue
        uc = int(nums[0])  # columna U.C. (los siguientes son calificación y puntos)
        nombre = re.split(r"\s+\d+\s", resto + " ")[0]
        nombre = _limpiar(nombre)
        # El sufijo romano del proyecto a veces queda en la línea siguiente.
        if "PROYECTO SOCIOPRODUCTIVO INTEGRADOR" in nombre.upper() and " I" not in nombre.upper():
            sig = _limpiar(lineas[idx + 1]) if idx + 1 < len(lineas) else ""
            if re.fullmatch(r"[IVX]{1,3}", sig):
                nombre = f"{nombre} {sig}"
        if nombre and len(nombre) > 4 and codigo not in encontradas:
            encontradas[codigo] = (nombre, uc, f"Trayecto {tray}", 0, 0)
    return encontradas


# ─────────────────────────── Carga en base de datos ───────────────────────────

def _encontrar_pdf(patron: str) -> Optional[Path]:
    buscado = _sin_acentos(patron).lower()
    # Se busca en el proyecto/bundle y en la carpeta escribible del ejecutable.
    carpetas = (UPLOADS, db.DATA_DIR.parent / "uploads")
    for carpeta in carpetas:
        if not carpeta.exists():
            continue
        for p in sorted(carpeta.glob("*.pdf")):
            if buscado in _sin_acentos(p.name).lower():
                return p
    return None


def importar_pnfs(ruta_uploads: Optional[Path] = None) -> Dict[str, int]:
    """Importa los 3 pensum PNF y devuelve conteos por carrera.

    Idempotente: actualiza las filas existentes por (carrera, código, nombre).
    """
    global UPLOADS
    if ruta_uploads is not None:
        UPLOADS = Path(ruta_uploads)

    facultad_id = db.get_or_create("facultades", "nombre", FACULTAD_PNF)
    conteos: Dict[str, int] = {}

    for patron, nombre_carrera, clave in PNFS:
        pdf = _encontrar_pdf(patron)
        if not pdf:
            conteos[clave] = 0
            continue
        texto = _extraer_texto(pdf)
        parser = {"fisioterapia": _parsear_fisioterapia,
                  "veterinaria": _parsear_veterinaria,
                  "agroecologia": _parsear_agroecologia}[clave]
        unidades = parser(texto)
        car_id = db.get_or_create("carreras", "nombre", nombre_carrera,
                                  {"facultad_id": facultad_id})
        n = 0
        for codigo, (nombre, uc, ciclo, hte, hta) in unidades.items():
            existe = db.query(
                "SELECT id FROM planes_estudio WHERE carrera_id=? AND codigo=? AND nombre=? LIMIT 1",
                (car_id, codigo, nombre))
            if existe:
                db.execute("""UPDATE planes_estudio SET unidad_credito=?, horas_teoria=?,
                              horas_practica=?, ciclo=? WHERE id=?""",
                           (uc, hte, hta, ciclo, existe[0]["id"]))
            else:
                db.execute("""INSERT INTO planes_estudio
                    (carrera_id,codigo,nombre,unidad_credito,horas_teoria,horas_practica,ciclo)
                    VALUES (?,?,?,?,?,?,?)""", (car_id, codigo, nombre, uc, hte, hta, ciclo))
            db.get_or_create_materia(nombre, car_id, codigo, ciclo, uc)
            n += 1
        conteos[clave] = n
    return conteos
