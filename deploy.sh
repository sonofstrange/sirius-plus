#!/bin/bash
set -e

echo "=== Установка Пирожкового Диспетчера ==="

# 1. Install Python & system deps
echo "[1/5] Установка системных зависимостей..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv curl

# 2. Create virtualenv
echo "[2/5] Создание виртуального окружения..."
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python packages
echo "[3/5] Установка Python-зависимостей..."
pip install -r requirements.txt

# 4. Install Playwright browser
echo "[4/5] Установка Chromium для Playwright..."
playwright install --with-deps chromium

# 5. Generate encryption key
echo "[5/5] Генерация ключа шифрования..."
if [ ! -f encryption_key.txt ]; then
    python3 -c "import os,base64; open('encryption_key.txt','w').write(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    echo "Ключ создан: encryption_key.txt"
fi

echo ""
echo "=== Готово! ==="
echo ""
echo "Запуск вручную:"
echo "  source .venv/bin/activate"
echo "  SIRIUS_HOST=0.0.0.0 python main.py"
echo ""
echo "Или установи как systemd-сервис:"
echo "  sudo cp sirius-plus.service /etc/systemd/system/"
echo "  sudo systemctl enable --now sirius-plus"
