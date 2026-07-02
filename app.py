import os
import sys
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from traceback import format_exc
from functools import wraps

import cloudinary
import cloudinary.uploader
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}

DB_PATH = Path("data") / "notas.db"
TEXTS_DIR = Path("data") / "textos"
TEXTS_DIR.mkdir(parents=True, exist_ok=True)

# --- auth ---
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logado"):
            if request.is_json:
                return jsonify({"error": "Não autorizado"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logado"):
            if request.is_json:
                return jsonify({"error": "Não autorizado"}), 401
            return redirect(url_for("login"))
        if not session.get("admin"):
            return jsonify({"error": "Apenas admin"}), 403
        return f(*args, **kwargs)
    return wrapper

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
                canhoto_url TEXT,
                status TEXT DEFAULT 'pendente',
                raw_response TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                admin INTEGER DEFAULT 0
            )
        """)
        # cria admin padrao se nao existir
        cur = conn.execute("SELECT id FROM usuarios WHERE username = 'admin'")
        if not cur.fetchone():
            conn.execute("INSERT INTO usuarios (username, password, admin) VALUES (?, ?, 1)",
                         ("admin", "admin123"))
        # atualiza senha do admin via env var se definida
        env_pass = os.getenv("ADMIN_PASS")
        if env_pass:
            conn.execute("UPDATE usuarios SET password = ? WHERE username = 'admin'", (env_pass,))
init_db()

def limpar_antigas():
    limite = datetime.now() - timedelta(days=365)
    removidas = 0
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, canhoto_url FROM notas WHERE created_at < ?", (limite.strftime("%Y-%m-%d %H:%M:%S"),)
        ).fetchall()
        for row in rows:
            nota_id, url = row
            if url:
                try:
                    public_id = url.split("/")[-1].rsplit(".", 1)[0]
                    cloudinary.uploader.destroy(public_id)
                except:
                    pass
            conn.execute("DELETE FROM notas WHERE id = ?", (nota_id,))
            removidas += 1
    return removidas

limpar_antigas()

def nf_existe(numero):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id FROM notas WHERE numero_nota = ?", (numero,)).fetchone()
        return row is not None

def allowed_file(name, exts=ALLOWED_EXTENSIONS):
    return "." in name and name.rsplit(".", 1)[1].lower() in exts

def upload_to_cloudinary(filepath, folder="notas"):
    r = cloudinary.uploader.upload(str(filepath), folder=folder, resource_type="auto")
    return r["secure_url"]

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

# --- auth routes ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("user", "")
        passw = request.form.get("pass", "")
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, username, password, admin FROM usuarios WHERE username = ?", (user,)
            ).fetchone()
        if row and row[2] == passw:
            session["logado"] = True
            session["admin"] = bool(row[3])
            session["username"] = row[1]
            return redirect(url_for("index"))
        return render_template("login.html", erro="Usuário ou senha inválidos")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("logado", None)
    return redirect(url_for("login"))

# --- protected routes ---
@app.route("/")
@login_required
def index():
    return render_template("index.html", admin=session.get("admin", False), username=session.get("username", ""))

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "python": sys.version})

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if "files" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado"}), 400

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    todas = []
    erros = []
    ignoradas = 0

    for file in files:
        if not allowed_file(file.filename):
            erros.append({"file": file.filename, "error": "Formato inválido"})
            continue
        filename = secure_filename(file.filename)
        unique_name = f"{batch_id}_{filename}"
        save_path = Path(f"/tmp/{unique_name}")
        file.save(save_path)
        try:
            raw_text = extract_text(save_path)
        except Exception as e:
            erros.append({"file": filename, "error": f"Erro OCR: {str(e)}"})
            save_path.unlink(missing_ok=True)
            continue
        text_path = TEXTS_DIR / f"{unique_name}.txt"
        text_path.write_text(raw_text, encoding="utf-8")
        try:
            notas = extract_with_ai(raw_text)
        except Exception as e:
            erros.append({"file": filename, "error": f"Erro IA: {str(e)}"})
            save_path.unlink(missing_ok=True)
            continue
        save_path.unlink(missing_ok=True)
        for nota in notas:
            nf_num = nota.get("numero_nota", "").strip()
            if nf_num and nf_existe(nf_num):
                ignoradas += 1
                erros.append({"file": filename, "error": f"NF {nf_num} já existe no banco"})
                continue
            dados_json = json.dumps(nota, ensure_ascii=False)
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute(
                    """INSERT INTO notas
                       (batch_id, filename, extracted_text, nome_cliente, endereco, numero_nota, raw_response)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, unique_name, raw_text,
                     nota.get("nome_cliente", ""), nota.get("endereco", ""),
                     nf_num, dados_json),
                )
                nota_id = cur.lastrowid
            todas.append({**nota, "id": nota_id})
    msg = f"{len(todas)} nota(s) processada(s)"
    if ignoradas:
        msg += f", {ignoradas} ignorada(s) por duplicidade"
    return jsonify({"message": msg, "notas": todas, "erros": erros, "batch_id": batch_id})

@app.route("/notas")
@login_required
def list_notas():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, filename, nome_cliente, endereco, numero_nota, status, batch_id, canhoto_url, created_at FROM notas ORDER BY id DESC"
        ).fetchall()
    return jsonify([
        {"id": r[0], "filename": r[1], "nome_cliente": r[2] or "",
         "endereco": r[3] or "", "numero_nota": r[4] or "",
         "status": r[5] or "pendente", "batch_id": r[6],
         "canhoto_url": r[7] or "", "created_at": r[8]}
        for r in rows
    ])

@app.route("/notas/<int:nota_id>")
@login_required
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
@login_required
def get_batch(batch_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, nome_cliente, endereco, numero_nota, status, canhoto_url FROM notas WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
    return jsonify([
        {"id": r[0], "nome_cliente": r[1], "endereco": r[2], "numero_nota": r[3],
         "status": r[4], "canhoto_url": r[5] or ""}
        for r in rows
    ])

@app.route("/upload-canhoto/<int:nota_id>", methods=["POST"])
@login_required
def upload_canhoto(nota_id):
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo"}), 400
    file = request.files["file"]
    if not allowed_file(file.filename, IMAGE_EXTENSIONS):
        return jsonify({"error": "Apenas PNG/JPG"}), 400
    ext = Path(file.filename).suffix.lower()
    filename = f"canhoto_{nota_id}{ext}"
    save_path = Path(f"/tmp/{filename}")
    file.save(save_path)
    try:
        url = upload_to_cloudinary(save_path, "canhotos")
    except Exception as e:
        save_path.unlink(missing_ok=True)
        return jsonify({"error": f"Erro ao salvar no Cloudinary: {str(e)}"}), 500
    save_path.unlink(missing_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE notas SET canhoto_url = ?, status = 'entregue' WHERE id = ?",
                     (url, nota_id))
    return jsonify({"message": "Canhoto salvo na nuvem!", "canhoto_url": url})

@app.route("/admin/limpar", methods=["POST"])
@login_required
def admin_limpar():
    qtd = limpar_antigas()
    return jsonify({"message": f"{qtd} nota(s) antiga(s) removida(s)"})

@app.route("/admin/usuarios", methods=["GET"])
@admin_required
def listar_usuarios():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, username, admin FROM usuarios").fetchall()
    return jsonify([{"id": r[0], "username": r[1], "admin": bool(r[2])} for r in rows])

@app.route("/admin/usuarios", methods=["POST"])
@admin_required
def criar_usuario():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    is_admin = 1 if data.get("admin") else 0
    if not username or not password:
        return jsonify({"error": "Usuário e senha obrigatórios"}), 400
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO usuarios (username, password, admin) VALUES (?, ?, ?)",
                         (username, password, is_admin))
        return jsonify({"message": "Usuário criado!"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Usuário já existe"}), 400

@app.route("/admin/usuarios/<int:uid>", methods=["DELETE"])
@admin_required
def deletar_usuario(uid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM usuarios WHERE id = ? AND admin = 0", (uid,))
    return jsonify({"message": "Usuário removido"})

@app.route("/notas/busca")
@login_required
def busca_notas():
    q = request.args.get("q", "").strip()
    with sqlite3.connect(DB_PATH) as conn:
        if q:
            rows = conn.execute(
                "SELECT id, filename, nome_cliente, endereco, numero_nota, status, canhoto_url, created_at FROM notas WHERE numero_nota LIKE ? ORDER BY id DESC",
                (f"%{q}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, filename, nome_cliente, endereco, numero_nota, status, canhoto_url, created_at FROM notas ORDER BY id DESC"
            ).fetchall()
    return jsonify([
        {"id": r[0], "filename": r[1], "nome_cliente": r[2] or "",
         "endereco": r[3] or "", "numero_nota": r[4] or "",
         "status": r[5] or "pendente", "canhoto_url": r[6] or "", "created_at": r[7]}
        for r in rows
    ])

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
