#!/bin/bash
echo "--- INICIANDO ---"
echo "PORT=$PORT"
echo "Python: $(python --version)"
echo "Tentando importar app..."
python -c "from app import app; print('OK: app importado')" 2>&1
echo "Iniciando gunicorn..."
gunicorn app:app --bind 0.0.0.0:${PORT:-5000}
