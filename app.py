import os
import sys
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from traceback import format_exc

from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = Path("uploads")
app.config["CANHOTO_FOLDER"] = Path("uploads") / "canhotos"
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}

DB_PATH = Path("data") / "notas.db"
TEXTS_DIR = Path("data") / "textos"
TEXTS_DIR.mkdir(parents=True, exist_ok=True)
app.config["CANHOTO_FOLDER"].mkdir(parents=True, exist_ok=True)

# --- DB ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                extracted_text TEXT,
                nome_cliente TEXT,
                endereco TEXT,
                numero_nota TEXT,
                canhoto_path TEXT,
                status TEXT DEFAULT 'pendente',
                raw_response TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
init_db()

def allowed_file(name, exts=ALLOWED_EXTENSIONS):
    return "." in name and name.rsplit(".", 1)[1].lower() in exts

# --- OCR ---
def extract_text_pdf(path):
    import pdfplumber
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)

def extract_text_image(path):
    try:
        from google.cloud import vision
    except ImportError:
        raise Exception("OCR para fotos nao disponivel. Instale google-cloud-vision ou use PDF.")
    client = vision.ImageAnnotatorClient()
    with open(path, "rb") as f:
        content = f.read()
    image = vision.Image(content=content)
    response = client.text_detection(image=image)
    if response.error.message:
        raise Exception(f"Vision API error: {response.error.message}")
    return response.full_text_annotation.text

def extract_text(filepath):
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        return extract_text_pdf(filepath)
    return extract_text_image(filepath)

# --- AI extraction ---
def extract_with_ai(raw_text):
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
    client = OpenAI(api_key=api_key, base_url=base_url)
    prompt = f"""O texto abaixo pode conter UMA OU MAIS notas fiscais.
Extraia TODAS as notas fiscais encontradas. Para cada uma, extraia:
- nome_cliente
- endereco (completo, incluindo rua, numero, bairro, cidade, estado, cep)
- numero_nota

Responda EXCLUSIVAMENTE com um array JSON válido, sem markdown, no formato:
[
  {{ "nome_cliente": "", "endereco": "", "numero_nota": "" }}
]

Se encontrar apenas uma nota, retorne um array com 1 elemento.
Se encontrar múltiplas, retorne todas.
Se não encontrar nenhuma, retorne array vazio [].

Texto:
{raw_text}"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    content = resp.choices[0].message.content.strip()
    for prefix in ("```json", "```"):
        if content.startswith(prefix):
            content = content[len(prefix):]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    parsed = json.loads(content)
    if isinstance(parsed, dict):
        return [parsed]
    return parsed

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "python": sys.version})

@app.route("/upload", methods=["POST"])
def upload():
    if "files" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado"}), 400

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    todas = []
    erros = []

    for file in files:
        if not allowed_file(file.filename):
            erros.append({"file": file.filename, "error": "Formato inválido"})
            continue
        filename = secure_filename(file.filename)
        unique_name = f"{batch_id}_{filename}"
        save_path = app.config["UPLOAD_FOLDER"] / unique_name
        file.save(save_path)
        try:
            raw_text = extract_text(save_path)
        except Exception as e:
            erros.append({"file": filename, "error": f"Erro OCR: {str(e)}"})
            continue
        text_path = TEXTS_DIR / f"{unique_name}.txt"
        text_path.write_text(raw_text, encoding="utf-8")
        try:
            notas = extract_with_ai(raw_text)
        except Exception as e:
            erros.append({"file": filename, "error": f"Erro IA: {str(e)}"})
            continue
        for nota in notas:
            dados_json = json.dumps(nota, ensure_ascii=False)
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute(
                    """INSERT INTO notas
                       (batch_id, filename, extracted_text, nome_cliente, endereco, numero_nota, raw_response)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, unique_name, raw_text,
                     nota.get("nome_cliente", ""), nota.get("endereco", ""),
                     nota.get("numero_nota", ""), dados_json),
                )
                nota_id = cur.lastrowid
            todas.append({**nota, "id": nota_id})
    return jsonify({
        "message": f"{len(todas)} nota(s) processada(s)",
        "notas": todas,
        "erros": erros,
        "batch_id": batch_id,
    })

@app.route("/notas")
def list_notas():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, filename, nome_cliente, endereco, numero_nota, status, batch_id, created_at FROM notas ORDER BY id DESC"
        ).fetchall()
    return jsonify([
        {"id": r[0], "filename": r[1], "nome_cliente": r[2] or "",
         "endereco": r[3] or "", "numero_nota": r[4] or "",
         "status": r[5] or "pendente", "batch_id": r[6], "created_at": r[7]}
        for r in rows
    ])

@app.route("/notas/<int:nota_id>")
def get_nota(nota_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM notas WHERE id = ?", (nota_id,)).fetchone()
    if not row:
        return jsonify({"error": "Nota não encontrada"}), 404
    cols = [d[0] for d in conn.execute("PRAGMA table_info(notas)").fetchall()]
    d = dict(zip(cols, row))
    if d.get("raw_response"):
        d["raw_response"] = json.loads(d["raw_response"])
    return jsonify(d)

@app.route("/batch/<batch_id>")
def get_batch(batch_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, nome_cliente, endereco, numero_nota, status FROM notas WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
    return jsonify([
        {"id": r[0], "nome_cliente": r[1], "endereco": r[2], "numero_nota": r[3], "status": r[4]}
        for r in rows
    ])

@app.route("/upload-canhoto/<int:nota_id>", methods=["POST"])
def upload_canhoto(nota_id):
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo"}), 400
    file = request.files["file"]
    if not allowed_file(file.filename, IMAGE_EXTENSIONS):
        return jsonify({"error": "Apenas PNG/JPG"}), 400
    ext = Path(file.filename).suffix.lower()
    filename = f"canhoto_{nota_id}{ext}"
    save_path = app.config["CANHOTO_FOLDER"] / filename
    file.save(save_path)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE notas SET canhoto_path = ?, status = 'entregue' WHERE id = ?",
                     (str(save_path), nota_id))
    return jsonify({"message": "Canhoto salvo!", "path": str(save_path)})

@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({"error": str(e), "trace": format_exc() if app.debug else ""}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Rota não encontrada"}), 404

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"Servidor iniciando na porta {port}")
    app.run(debug=False, host="0.0.0.0", port=port)
