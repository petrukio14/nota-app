#!/bin/bash
set -e

echo "============================================"
echo "  Nota-App - Setup VPS Oracle (Ubuntu)"
echo "============================================"
echo ""

# Verifica se esta rodando como ubuntu
if [ "$(whoami)" != "ubuntu" ]; then
  echo "ERRO: Execute como usuario ubuntu"
  exit 1
fi

# 1. System packages
echo "[1/8] Instalando pacotes do sistema..."
sudo apt update -qq
sudo apt install -y -qq python3 python3-pip python3-venv nginx git curl

# 2. Create app directory
echo "[2/8] Criando diretorio do app..."
mkdir -p /home/ubuntu/nota-app
cd /home/ubuntu/nota-app

# 3. Clone repositorio
echo "[3/8] Clonando repositorio..."
if [ -d ".git" ]; then
  echo "  Repositorio ja existe, atualizando..."
  git pull
else
  git clone https://github.com/petrukio14/nota-app.git .
fi

# 4. Create .env
echo "[4/8] Configurando .env..."
if [ ! -f .env ]; then
  cat > .env << 'ENVEOF'
OPENROUTER_API_KEY=coloque-sua-chave-aqui
OPENAI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-4o-mini
CLOUDINARY_CLOUD_NAME=coloque-seu-cloud-name
CLOUDINARY_API_KEY=coloque-sua-api-key
CLOUDINARY_API_SECRET=coloque-seu-secret
SECRET_KEY=coloque-uma-chave-segura-aqui
ADMIN_PASS=87416180
ENVEOF
  echo "  .env criado COM PLACEHOLDERS. Edite agora: nano /home/ubuntu/nota-app/.env"
else
  echo "  .env ja existe, mantendo..."
fi

# 5. Python venv + dependencias
echo "[5/8] Instalando dependencias Python..."
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

# 6. Data directory
echo "[6/8] Criando diretorio de dados..."
mkdir -p data
echo "  Se tiver um backup do notas.db, coloque em: /home/ubuntu/nota-app/data/"

# 7. Configure nginx
echo "[7/8] Configurando nginx..."
sudo tee /etc/nginx/sites-available/nota-app > /dev/null << 'NGINXEOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
NGINXEOF
sudo ln -sf /etc/nginx/sites-available/nota-app /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# 8. Configure systemd service
echo "[8/8] Configurando servico systemd..."
sudo tee /etc/systemd/system/nota-app.service > /dev/null << 'SERVICEEOF'
[Unit]
Description=Nota App Flask
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/nota-app
EnvironmentFile=/home/ubuntu/nota-app/.env
ExecStart=/home/ubuntu/nota-app/venv/bin/gunicorn app:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable nota-app
sudo systemctl restart nota-app

# Final
VPS_IP=$(curl -s ifconfig.me 2>/dev/null || echo "VERIFIQUE SEU IP")
echo ""
echo "============================================"
echo "  Setup concluido!"
echo "============================================"
echo ""
echo "  Acesse: http://$VPS_IP"
echo "  Login:  admin / 87416180"
echo ""
echo "  Comandos uteis:"
echo "    sudo systemctl status nota-app   # Status do servico"
echo "    sudo journalctl -u nota-app -f   # Logs ao vivo"
echo ""
echo "  Se tiver backup do notas.db, copie agora:"
echo "    scp notas.db ubuntu@$VPS_IP:/home/ubuntu/nota-app/data/"
echo ""
